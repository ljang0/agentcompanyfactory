"""The bulk layer: records built by code from generator specs the model writes.

Most of a real company's volume is not hand-written: automated notifications, recurring
invoices and statements, calendar recurrences, routine tickets, log-like updates. That bulk is
repetitive in life, so building it by program is correct, and it is coherent by construction:
every record derives from the same entities, dates fall in stated windows, numbers come from
stated ranges, ids never collide. The model writes a spec (a template record with placeholders,
a window, a cadence, lookup tables drawn from the world); this module expands it.

Placeholders, inside string values of the template:
  {{seq}} {{seq:4}}          running number, optionally zero-padded
  {{date}} {{datetime}}       the record's instant (per cadence, inside the window)
  {{date+3}} {{date-14}}      that instant shifted by days
  {{pick:table}}              one value from spec.tables[table]
  {{pick:table.field}}        one field of a row from a table of objects
  {{int:10-500}} {{amount:120-4800}}   an integer, or a 2-decimal amount
  {{choice:a|b|c}}            one of the listed values
  {{person}} {{person.email}} a user of this app (from reference_people)
  {{id:prefix}}               prefix plus the running number
  {{id:prefix:0042}}          prefix plus the running number counted from 42, padded to 4 digits
A value that is exactly "{{int:...}}" or "{{amount:...}}" becomes a number, not a string.

The id prefixes a spec declares are rewritten before expansion into the scheme the seeded state
already uses for that collection (``native_ids``), so a filler record's id does not announce
itself.
"""

import calendar
import copy
import json
import random
import re
from collections import Counter
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from .world_check import record_identifiers

PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][\w.+-]*)(?::([^}]*))?\s*\}\}")
ID_PLACEHOLDER = re.compile(r"\{\{\s*id:([^}]*?)\s*\}\}")
CADENCES = ("random", "daily", "weekdays", "weekly", "monthly")
STEP_DAYS = {"daily": 1, "weekdays": 1, "weekly": 7, "monthly": 30.4}


class BulkSpec(BaseModel):
    app_id: str = Field(min_length=1)
    collection: str = Field(min_length=1)
    what: str = Field(min_length=1, description="What these records are, in one sentence")
    count: int = Field(ge=1, le=20000)
    start: str = Field(min_length=10, description="ISO date")
    end: str = Field(min_length=10, description="ISO date")
    cadence: str = Field(default="random", pattern="^(random|daily|weekdays|weekly|monthly)$")
    weekdays_only: bool = Field(
        default=False,
        description="Move recurrence anchors to the next weekday inside the window; use for ordinary office work.",
    )
    id_prefix: str = Field(min_length=1)
    # Strict structured output forbids free-form objects, so the template record and the
    # lookup tables travel as JSON text and are parsed here.
    template_json: str = Field(min_length=2, description="The template record as a JSON object string")
    tables_json: str = Field(default="{}", description="JSON object: table name -> list of values or objects")
    keyed_by: str | None = Field(
        default=None, description="For collections that are maps: which template field is the key"
    )

    @property
    def template(self):
        value = json.loads(self.template_json)
        if not isinstance(value, dict):
            raise ValueError("template_json must be a JSON object")  # noqa: TRY004 -- callers catch ValueError
        return value

    @property
    def tables(self):
        value = json.loads(self.tables_json or "{}")
        if not isinstance(value, dict):
            raise ValueError("tables_json must be a JSON object")  # noqa: TRY004 -- callers catch ValueError
        return {k: (v if isinstance(v, list) else [v]) for k, v in value.items()}


class BulkSpecs(BaseModel):
    rationale: str = Field(min_length=1)
    specs: list[BulkSpec]


def _window(spec):
    """The spec's window as two datetimes, never empty."""
    start = datetime.fromisoformat(spec.start[:10])
    end = datetime.fromisoformat(spec.end[:10])
    return start, end if end > start else start + timedelta(days=1)


def periods(spec):
    """The days the cadence allows inside the window: every day, weekday, week or month of it."""
    if spec.cadence == "random":
        return []
    start, end = _window(spec)
    step = timedelta(days=STEP_DAYS[spec.cadence])
    out, cursor, period_index = [], start, 0
    while cursor < end:
        if spec.cadence != "weekdays" or cursor.weekday() < 5:
            # Anchor business-hour offsets at midnight, including monthly periods.
            candidate = cursor.replace(hour=0, minute=0, second=0, microsecond=0)
            while spec.weekdays_only and candidate.weekday() > 4:
                candidate += timedelta(days=1)
            if candidate < end:
                out.append(candidate)
        period_index += 1
        if spec.cadence == "monthly":
            # Keep the original day, including Jan 31 -> Feb 28 -> Mar 31.
            year, month = divmod(start.year * 12 + start.month + period_index - 1, 12)
            month += 1
            cursor = start.replace(
                year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1])
            )
        else:
            cursor += step
    return out


def varies_per_record(template):
    """Whether the template draws a different entity per record or repeats one thing.

    A statement per account and a notice per person are many records in the same period, and the
    period is still honest; a reminder that names nothing is one event, and a second copy of it in
    the same period is an event that did not happen.
    """
    return bool(re.search(r"\{\{\s*(pick|person|choice)\b", json.dumps(template, ensure_ascii=False)))


def _instants(spec, rng, template):
    """One instant per record, on a day the cadence allows; fewer than asked when it cannot hold them.

    A window decides how many records a cadence can carry and this ignored it: when a spec asked
    for more than its window held, the surplus landed uniformly at random across the span. 1,578
    of the 1,842 specs on disk ask for more, 195,700 records' worth, so "weekdays" reminders wrote
    3,247 weekend records and a "monthly" check landed 58 times in one month and 6 times in one
    day. The cadence was a label on the spec and nothing else.

    The surplus now cycles over the cadence's own days, so every record falls on one of them, and
    a template that repeats a single thing is capped at one record per period. The caller compares
    what came back with ``spec.count`` and records the difference.
    """
    start, end = _window(spec)
    if spec.cadence == "random":
        span = (end - start).total_seconds()
        instants = [start + timedelta(seconds=rng.random() * span) for _ in range(spec.count)]
        if spec.weekdays_only:
            days = [
                start + timedelta(days=i)
                for i in range((end - start).days)
                if (start + timedelta(days=i)).weekday() < 5
            ]
            if not days:
                return []
            instants = [rng.choice(days) + timedelta(hours=8 + rng.random() * 9) for _ in range(spec.count)]
        return sorted(instants)
    allowed = periods(spec)
    if not allowed:
        return []
    per_period = spec.count if varies_per_record(template) else 1
    count = min(spec.count, len(allowed) * per_period)
    # Stratify over the whole window. Taking the first N weekdays left most
    # recent CRM history empty whenever N was smaller than the window.
    out = [
        allowed[min(len(allowed) - 1, (2 * index + 1) * len(allowed) // (2 * count))]
        + timedelta(hours=8 + rng.random() * 9)
        for index in range(count)
    ]
    out.sort()
    return out


def _fill(value, ctx, rng):
    if isinstance(value, dict):
        return {k: _fill(v, ctx, rng) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, ctx, rng) for v in value]
    if not isinstance(value, str):
        return value
    whole = PLACEHOLDER.fullmatch(value.strip())
    if whole and whole.group(1) in ("int", "amount"):
        return _resolve(whole.group(1), whole.group(2), ctx, rng)
    return PLACEHOLDER.sub(lambda m: str(_resolve(m.group(1), m.group(2), ctx, rng)), value)


def _resolve(name, arg, ctx, rng):
    when = ctx["when"]
    if name == "seq":
        return str(ctx["seq"]).zfill(int(arg)) if arg else str(ctx["seq"])
    if name == "id":
        # "prefix:0042": the running number counted from 42 and padded to the width written, so a
        # collection whose seeded ids are a number series can be continued instead of restarted.
        prefix, sep, first = (arg or "").rpartition(":")
        if sep and first.isdigit():
            return f"{prefix}{str(ctx['seq'] + int(first) - 1).zfill(len(first))}"
        return f"{arg or ''}{ctx['seq']}"
    if name == "date":
        return when.date().isoformat()
    if name == "datetime":
        return when.replace(microsecond=0).isoformat()
    if name.startswith(("date+", "date-")):
        days = int(name[4:])
        return (when + timedelta(days=days)).date().isoformat()
    if name in ("int", "amount"):
        # The same placeholder twice in one record is the same number: a statement's body
        # quotes the amount its amount field carries.
        memo = ctx["picks"].setdefault("__numbers", {})
        key = f"{name}:{arg}"
        if key not in memo:
            lo, hi = _range(arg)
            memo[key] = rng.randint(int(lo), int(hi)) if name == "int" else round(rng.uniform(lo, hi), 2)
        return memo[key]
    if name == "choice":
        options = [o.strip() for o in (arg or "").split("|") if o.strip()]
        return rng.choice(options) if options else ""
    if name == "pick":
        table, _, field = (arg or "").partition(".")
        rows = ctx["tables"].get(table) or []
        if not rows:
            return ""
        row = ctx["picks"].setdefault(table, rng.choice(rows))
        return row.get(field, "") if field and isinstance(row, dict) else row
    if name == "person" or name.startswith("person."):
        people = ctx["people"]
        if not people:
            return ""
        person = ctx["picks"].setdefault("__person", rng.choice(people))
        field = name.partition(".")[2]
        return person.get(field, "") if field else person.get("name") or person.get("id") or ""
    return f"{{{{{name}}}}}"


def _range(arg):
    lo, hi = (arg or "0-0").split("-", 1) if arg and "-" in arg.lstrip("-") else (arg or "0", arg or "0")
    return float(lo), float(hi)


def expand(spec, people=(), seed=0):
    """Records for one spec: deterministic for the same spec, people and seed.

    Fewer than ``spec.count`` when the cadence cannot hold that many; the caller says so.
    """
    rng = random.Random(f"{seed}:{spec.app_id}:{spec.collection}:{spec.id_prefix}")
    template, tables, people = spec.template, spec.tables, list(people)
    records = []
    for index, when in enumerate(_instants(spec, rng, template), 1):
        ctx = {"seq": index, "when": when, "tables": tables, "people": people, "picks": {}}
        records.append(_fill(copy.deepcopy(template), ctx, rng))
    return records


def people_of(state):
    """User-like records of an app state, for {{person}}."""
    for key in ("users", "members", "people", "accounts"):
        value = state.get(key)
        rows = list(value.values()) if isinstance(value, dict) else value if isinstance(value, list) else []
        rows = [r for r in rows if isinstance(r, dict)]
        if rows:
            return [
                {
                    "id": r.get("id") or r.get("userId"),
                    "name": r.get("name") or r.get("fullName") or r.get("displayName") or r.get("username"),
                    "email": r.get("email"),
                }
                for r in rows
            ]
    # Mail and calendar have no plural directory. Ignoring their current-user
    # object made {{person.email}} expand to an empty string.
    for key in ("user", "currentUser"):
        person = state.get(key)
        if isinstance(person, dict) and person.get("email"):
            return [
                {
                    "id": person.get("id") or person.get("userId"),
                    "name": person.get("name")
                    or person.get("fullName")
                    or person.get("displayName")
                    or person.get("username"),
                    "email": person["email"],
                }
            ]
    return []


def rows_of(value):
    """The records of a state collection, in any of the three shapes a state stores them in."""
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)]
    if isinstance(value, dict):
        if value and all(isinstance(v, list) for v in value.values()):
            return [r for rows in value.values() for r in rows if isinstance(r, dict)]
        return [r for r in value.values() if isinstance(r, dict)]
    return []


def singular_forms(name):
    """Every singular a collection name could be, so an irregular plural still finds its own key.

    ``"activities".removesuffix("s")`` is ``"activitie"``, so the universe looked for
    ``activitieId`` and Salesforce's 183 activities -- which key on ``activityId`` and resolve
    perfectly -- entered it not once. A field whose values all resolve then reads as a field that
    never resolves, which is the one state in which every guard downstream abstains.
    """
    name = str(name).lower()
    out = {name}
    if name.endswith("ies") and len(name) > 4:
        out.add(f"{name[:-3]}y")
    if name.endswith("es") and len(name) > 3:
        out.add(name[:-2])
    if name.endswith("s"):
        out.add(name[:-1])
    return out


def self_keys(collection):
    """The field names a record of ``collection`` may use for itself."""
    keys = {"id", "sys_id", "uuid", "_id"}
    for form in singular_forms(collection):
        keys |= {f"{form}id", f"{form}_id", f"{form}_key"}
    return keys  # compared case-insensitively: activityId, activity_id, ACTIVITYID are one key


def collection_identifiers(name, value):
    """The ids the records of one collection define for themselves."""
    out, rows = set(), rows_of(value)
    wanted = self_keys(name)
    for record in rows:
        for key, entry in record.items():
            if key.lower() in wanted and isinstance(entry, (str, int)) and not isinstance(entry, bool):
                out.add(str(entry))
    if isinstance(value, dict) and rows and not all(isinstance(v, list) for v in value.values()):
        out |= {str(k) for k in value}
    return out


def state_identifiers(state):
    """Everything a record in this state can legitimately be pointed at by.

    Primary keys whatever they are called, the keys of a map-shaped collection, and the people:
    a record names a person by id, by address or by name depending on the app.
    """
    out = set()
    for name, value in (state or {}).items():
        out |= collection_identifiers(name, value)
    for person in people_of(state):
        out |= {str(v) for v in person.values() if isinstance(v, (str, int)) and str(v)}
    return out


def id_fields(node, path=""):
    """Every id-shaped field under ``node``, including the ones nested in lists and objects."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key != "id" and key.endswith(("Id", "_id", "ID")) and isinstance(value, (str, int)):
                yield f"{path}/{key}", key, str(value)
            yield from id_fields(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from id_fields(value, f"{path}/{index}")


def reference_fields(state):
    """The id-shaped record fields of this state that actually name a record in it.

    A field whose values never resolve is how the product works, not a fault: Gmail groups by a
    ``threadId`` and has no threads collection, exactly as the real API does, and the reference
    check learned that at the cost of 170,408 warnings. So a template's reference is only judged
    on fields the seeded state resolves at least once. Nested ones count: a drive item shares
    itself through ``sharedWith[].userId``, which is as much a reference as its ``ownerId``.
    """
    universe, fields = state_identifiers(state), set()
    for value in (state or {}).values():
        for record in rows_of(value):
            for _where, key, entry in id_fields(record):
                if entry in universe:
                    fields.add(key)
    return fields


CONTAINER_PREFIX = re.compile(r"(?i)^(?:parent|root)")
REFERENCE_SUFFIX = re.compile(r"(?:_id|Id|ID)$")


def record_collections_with_ids(state):
    """Top-level collections that hold records and give those records ids.

    A lone object is not a collection: amazon's ``user`` is one signed-in buyer with an address
    and a payment method nested inside it, and reading those as rows makes ``userId`` look like a
    field naming a directory this app has. It names Amazon's reviewers, who are nobody here.
    """
    out = {}
    for name, value in (state or {}).items():
        if isinstance(value, dict) and not all(isinstance(v, (dict, list)) for v in value.values()):
            continue
        if not isinstance(value, (dict, list)) or not rows_of(value):
            continue
        ids = collection_identifiers(name, value)
        if ids:
            out[name] = ids
    return out


def container_fields(state):
    """Id fields whose *name* names a collection of records this app holds.

    ``reference_fields`` judges a template's reference only on fields the seeded state resolves at
    least once, because a field that never resolves is usually how the product works -- Gmail
    groups by ``threadId`` and has no threads collection. The cost of that rule is that it switches
    the guard off exactly when the human layer is already broken: one drive holds four items whose
    ``parentId`` all dangle and no folder at all, so ``parentId`` is not judged, and 3,000 bulk
    items are laid under folders nobody wrote. Every one of them is paid for and none can be
    opened -- served, that world draws "This folder is empty".

    So a never-resolving field is judged as well when its own name names a record collection this
    app holds: ``parentId`` on a collection means its own kind, ``columnId`` means ``columns``.
    Measured over the 60 worlds, that is the whole of the difference and none of the exemption:
    gmail's ``threadId`` (58 worlds), slack's ``callId``, clio's ``ledesClientId``, salesforce's
    ``postId``, airtable's ``fld_*_id`` and shopify's ``variantId`` all name no collection here and
    stay exempt; google_calendar's ``userId`` names ``user``, one signed-in person rather than a
    directory, and stays exempt with it.
    """
    holders, out = record_collections_with_ids(state), set()
    for name, value in (state or {}).items():
        for record in rows_of(value):
            for _where, key, _entry in id_fields(record):
                base = CONTAINER_PREFIX.sub("", REFERENCE_SUFFIX.sub("", key)).strip("_")
                if not base:
                    if name in holders:
                        out.add(key)
                    continue
                forms = singular_forms(base)
                if any(forms & singular_forms(holder) for holder in holders):
                    out.add(key)
    return out


def judged_fields(state):
    """Every id field a template's reference is checked on."""
    return reference_fields(state) | container_fields(state)


def _clean_prefix(prefix):
    """A spec's id prefix with the machine word taken out of it, wherever it sits.

    The word arrives glued as often as separated -- "bulk-gcal-intake-", "wmbulk-desk-handover-",
    "bulk92617cal-dover-desk-" -- so every occurrence goes. An id is opaque, so losing the middle
    of a word that happens to contain "bulk" costs nothing next to a corpus where one substring
    tells a reader which records were not written by hand.
    """
    out = re.sub(r"(?i)bulk", "", prefix)
    trimmed = re.sub(r"[-_.]{2,}", "-", out).strip("-_. ")
    return trimmed + ("-" if trimmed and prefix[-1] in "-_." else "")


MACHINE_STAMPED = re.compile(r"(?i)^.*bulk.*?(\d+)$")


def machine_stamped(identifier):
    """Whether an id was stamped by a bulk layer written before ``_clean_prefix``: the word and a number.

    A layer that no marker lists is never stripped, and it poisons everything after it: 33,880
    records in 10 of the 42 worlds on disk belong to one. Fairfax's gmail holds 2,808 of them under
    a marker that lists 60 ids, they ate the byte budget so the new layer got 60 of 12,895, they
    are not excluded from the texture check so 3,115 machine-stamped emails are judged as human
    prose -- both of that world's blocking errors -- and no repair can rewrite them.

    Nothing else identifies them. The recorded specs' ``id_prefix`` catches 49 of the 33,880,
    because the residue came from a generation whose specs BULK.json no longer holds: fairfax's
    residue is ``crpa-bulk-mail-*`` against recorded prefixes ``crpa-bulk-d7c4-*``, and its drive
    residue belongs to an app the marker has no entry for at all. What every one of them does carry
    is the tell ``_clean_prefix`` now removes, followed by the expander's running number. The
    number matters: "bulk" is also an English word, and ``prod-qa-pushin-bulkhead``,
    ``lr-history-251124-holiday-bulk-access`` and two more like them are records a person wrote.
    """
    return bool(MACHINE_STAMPED.fullmatch(str(identifier)))


def primary_identifiers(state):
    """What each record in this state calls itself, never what it points at.

    ``state_identifiers`` adds every foreign key and the people because reference checking needs
    them. Deciding which records belong to a previous bulk layer needs the opposite: a human record
    that merely names a generated one must not be taken for one. Recording the wide set as "the
    bulk layer's ids" is how 4,732 of the fleet's 81,886 human records came to be deleted by the
    strip that precedes a re-lay -- nordstrom's drive went from 2,608 items to 1, because its bulk
    items name the human folders in ``parentId`` and the folders were stripped as bulk.
    """
    out = set()
    for name, value in (state or {}).items():
        singular = name.removesuffix("s")
        rows = rows_of(value)
        for record in rows:
            for key in ("id", "sys_id", f"{singular}Id", f"{singular}_id", f"{singular}_key"):
                if isinstance(record.get(key), (str, int)):
                    out.add(str(record[key]))
                    break
        if isinstance(value, dict) and rows and not all(isinstance(v, list) for v in value.values()):
            out |= {str(k) for k in value}
    return out


def id_scheme(state, collection):
    """The id shape the seed already used for a collection: (leading token, number width, next).

    A filler record's id should read like the records around it, and 349,135 ids on disk carry the
    word "bulk": one substring separates every generated record from every authored one, so a task
    that says "the newest invoice" is answerable with grep and a reviewer reading a sample can see
    which records were not written by hand. The scheme is the leading token most of the
    collection's ids share ("file-", "u-", "MSG_") and, when they end in numbers, the width of
    those numbers and the first one still free.
    """
    value = (state or {}).get(collection)
    ids = [str(r["id"]) for r in rows_of(value) if isinstance(r.get("id"), (str, int))]
    if isinstance(value, dict) and value and not all(isinstance(v, list) for v in value.values()):
        ids += [str(k) for k in value]
    if not ids:
        return "", 0, 1
    numbers = [i for i in ids if i.isdigit()]
    if len(numbers) * 2 >= len(ids):  # a number series: continue it rather than start another
        return "", min(len(i) for i in numbers), max(int(i) for i in numbers) + 1
    tokens = Counter(m.group(0) for i in ids if (m := re.match(r"[A-Za-z]+[-_.]", i)))
    token, seen = tokens.most_common(1)[0] if tokens else ("", 0)
    if _clean_prefix(token) != token:
        # A collection an earlier layer already polluted must not teach its own tell: one world's
        # emails are 1,365 records of a layer BULK.json no longer names, so the commonest leading
        # token there is "bulk-" and adopting it would reproduce exactly what this removes.
        return "", 0, 1
    return (token if seen * 2 >= len(ids) else ""), 0, 1


def native_ids(state, specs):
    """A replacement for every id prefix these specs stamp, in the collection's own id scheme.

    One mapping for the whole call, because specs reference each other's prefixes: a Zendesk
    comments spec sets ``ticket_id`` to ``{{id:88101}}``, the prefix the tickets spec stamps, and
    rewriting the two independently would break every comment's reference to its ticket.
    """
    mapping, owners, taken, free = {}, {}, set(), {}
    for spec in specs:
        owners.setdefault(spec.id_prefix, spec)
        for arg in ID_PLACEHOLDER.findall(spec.template_json + spec.tables_json):
            owners.setdefault(arg, spec)
    for prefix, spec in sorted(owners.items(), key=lambda kv: kv[0]):
        token, width, first = id_scheme(state, spec.collection)
        if width:
            # The specs of one collection take disjoint stretches of the series, so two of them
            # cannot stamp the same number and lose one record to the other's duplicate.
            start = free.get(spec.collection, first)
            free[spec.collection] = start + spec.count
            mapping[prefix] = f":{str(start).zfill(width)}"
            continue
        body = _clean_prefix(prefix) or spec.collection
        candidate = body if token and body.startswith(token) else f"{token}{body}"
        existing = {str(r.get("id")) for r in rows_of((state or {}).get(spec.collection))}
        suffix = 1
        while candidate in taken or existing & {f"{candidate}{n}" for n in range(1, spec.count + 1)}:
            suffix += 1
            candidate = f"{candidate.rstrip('-_.')}-{suffix}-"
        taken.add(candidate)
        mapping[prefix] = candidate
    return {old: new for old, new in mapping.items() if old != new}


def with_native_ids(spec, mapping):
    """``spec`` with every id prefix replaced, in the placeholders and in any literal that quotes one."""
    if not mapping:
        return spec
    keys = sorted(mapping, key=len, reverse=True)
    pattern = re.compile("|".join([ID_PLACEHOLDER.pattern, *(re.escape(k) for k in keys)]))

    def swap(match):
        if match.group(1) is not None:
            return "{{id:" + mapping.get(match.group(1), match.group(1)) + "}}"
        return mapping[match.group(0)]

    return spec.model_copy(
        update={
            "id_prefix": mapping.get(spec.id_prefix, spec.id_prefix),
            "template_json": pattern.sub(swap, spec.template_json),
            "tables_json": pattern.sub(swap, spec.tables_json),
        }
    )


def _count_base(key):
    """The thing a count field counts: comment_count -> comment, reviewsCount -> review."""
    match = re.fullmatch(r"(.+?)s?[_]?(?:count|Count|COUNT)", key)
    return match.group(1) if match and match.group(1) not in ("", "_") else None


def _children(record, base, state, parent):
    """How many children a record really has, or None when nothing here can say."""
    for name in (base, f"{base}s", f"{base}es"):
        for key in (name, name[:1].lower() + name[1:]):
            if isinstance(record.get(key), list):
                return len(record[key])
    identifier = str(record.get("id"))
    for name in (f"{base}s", base, f"{base}es"):
        rows = rows_of((state or {}).get(name)) or rows_of((state or {}).get(name[:1].lower() + name[1:]))
        if not rows:
            continue
        for link in (f"{parent}Id", f"{parent}_id", f"{parent}_Id"):
            if any(link in row for row in rows):
                return sum(1 for row in rows if str(row.get(link)) == identifier)
    return None


def recompute_derived(state, owned):
    """Correct what the records ``owned`` names say about their own children and their own size.

    A count is a fact about other records, and the expander writes whatever the template typed:
    one number on every record it stamps. So ``comment_count`` was 1 on every generated ticket
    whatever its comments, ``ordersCount`` was wrong on 19 of 300 customers, and a file's ``size``
    stayed the template's constant while its ``content`` varied by hundreds of bytes. Nothing
    downstream recomputes them, so the app shows a number that contradicts the records beside it.

    Only the generated records are touched: a number the app author wrote is the author's to
    answer for. Returns the number of fields corrected.
    """
    fixed = 0
    for name, value in (state or {}).items():
        parent = name.removesuffix("s")
        for record in rows_of(value):
            if not own_identifiers(record) & owned:
                continue
            content = record.get("content")
            if isinstance(record.get("size"), int) and isinstance(content, str) and content:
                size = len(content.encode("utf-8"))
                fixed += record["size"] != size
                record["size"] = size
            for key, entry in list(record.items()):
                base = _count_base(key) if isinstance(entry, int) and not isinstance(entry, bool) else None
                actual = _children(record, base, state, parent) if base else None
                if actual is not None and actual != entry:
                    record[key], fixed = actual, fixed + 1
    return fixed


def own_identifiers(record, spec=None):
    """The identifiers a record owns, never the ones it merely points at.

    ``record_identifiers`` collects every id-shaped field because reference checking needs the
    foreign keys too. Using it to detect duplicates made every generated record look like one it
    already had: a Slack message carries channelId, senderId and threadId, all of which exist on
    records already in the state, so the first insertion matched and every record after it was
    skipped. Slack kept 3 of 273,453 declared records fleet-wide and the calendar 3 of 124,265,
    while the plain-list apps, whose records carry no such keys, kept everything.

    A generated record's own id is the one the spec stamped with its prefix; failing that, the
    plain ``id``. The bucket field a keyed collection groups by is never an identity. With no spec,
    the answer is the plain ``id``: what a record already on disk calls itself.
    """
    prefix = getattr(spec, "id_prefix", None)
    bucket = getattr(spec, "keyed_by", None)
    owned = set()
    for key, value in record.items():
        if key == bucket or not isinstance(value, (str, int)):
            continue
        text = str(value)
        if prefix and text.startswith(prefix) or not prefix and key == "id":
            owned.add(text)
    if owned:
        return owned
    plain = record.get("id")
    return {str(plain)} if plain is not None else set()


ID_LIST_SUFFIX = re.compile(r"(?:Ids|IDs|_ids)$")


def container_lists(state, collection):
    """``{container record id: the list to append a new child's id to}`` for one collection.

    A container's id list is what the app draws its children from -- trello renders a list from
    ``list.cardIds`` (``List.jsx:59``) and monday a group from ``group.itemIds`` -- so a record
    appended to ``state["cards"]`` and to nothing else is a record on no screen. The expander wrote
    the child and left the other side of the link alone: 62 generated trello cards name a list that
    exists and appear in no ``cardIds``, and served, that board draws 23 of 3,288 cards.

    Only a list field the container already has is offered. Inventing one would put records
    somewhere the app does not read, which is the same defect facing the other way.
    """
    out = {}
    for name, value in (state or {}).items():
        if name == collection:
            continue
        for parent in rows_of(value):
            fields = [
                entry
                for key, entry in parent.items()
                if isinstance(entry, list)
                and singular_forms(ID_LIST_SUFFIX.sub("", key)) & singular_forms(collection)
            ]
            if not fields:
                continue
            for key in self_keys(name):
                for pkey, pvalue in parent.items():
                    if pkey.lower() == key and isinstance(pvalue, (str, int)):
                        out.setdefault(str(pvalue), (fields[0], parent))
    return out


def register_in_container(containers, spec, record):
    """Put a new record's id in the container list its own fields say it belongs to.

    Only where the record names that container itself: this completes a link the record declares,
    it never invents one. Returns the container's own identifiers, so a count it keeps of its
    children can be recomputed -- a list that gains 3,123 cards and keeps saying it holds 13 is
    the defect ``check_counts`` reports, and it would be this function that made it.
    """
    if not containers:
        return set()
    ids = own_identifiers(record, spec)
    if not ids:
        return set()
    for key, value in record.items():
        # An id-shaped field, not any value that happens to equal a container's id: Zendesk keys
        # on small integers, where a `priority` of 3 would file the record under ticket 3.
        if not REFERENCE_SUFFIX.search(key) or not isinstance(value, (str, int)):
            continue
        if isinstance(value, bool) or str(value) in ids:
            continue
        entry = containers.get(str(value))
        if entry is None:
            continue
        held, parent = entry
        if not ids & {str(v) for v in held}:
            held.append(min(ids))
            return own_identifiers(parent)
    return set()


def add_to_state(state, spec, records, max_bytes, report=None):
    """Append records to the collection in the app's shape; stop at the byte budget.

    Returns the number actually inserted: records whose identifier already exists are skipped in
    every shape, so a rerun adds nothing twice.

    ``report``, when given, is filled with how many records the budget had room for and how many
    bytes were left. The caller needs it to say *which* limit refused a record: "byte budget or
    duplicate ids" was one string for two unrelated causes, one of which is fixed by a number in
    config.toml and the other by a different spec.
    """
    value = state.get(spec.collection)
    budget = max_bytes - len(json.dumps(state))
    if report is not None:
        report.update(budget_left=budget, room=0, asked_of_budget=len(records))
    if budget <= 0:
        return 0
    per_record = max(1, len(json.dumps(records[0])) if records else 1)
    room = max(0, int(budget / per_record))
    if report is not None:
        report.update(room=room, per_record=per_record)
    records = records[:room]
    # Built once for the whole spec: a child looks its container up by id, so a collection of
    # thousands does not rescan every parent per record.
    containers = container_lists(state, spec.collection)
    inserted, touched = 0, set()
    if isinstance(value, list):
        seen = set().union(*(record_identifiers(r) for r in value if isinstance(r, dict)))
        for r in records:
            rid = own_identifiers(r, spec)
            if rid & seen:
                continue
            seen |= rid
            value.append(r)
            touched |= register_in_container(containers, spec, r)
            inserted += 1
    elif isinstance(value, dict) and (
        (value and all(isinstance(v, list) for v in value.values()))
        or (not value and spec.keyed_by and spec.keyed_by not in ("id",))
    ):
        # A map of lists keyed by channel, thread or day: the template's keyed_by field names
        # the bucket; without one, everything goes into the first existing bucket.
        key = spec.keyed_by or next(iter(value), None)
        if key is None:
            return 0
        seen = set().union(
            *(record_identifiers(r) for rows in value.values() for r in rows if isinstance(r, dict))
        )
        for r in records:
            rid = own_identifiers(r, spec)
            if rid & seen:
                continue
            seen |= rid
            bucket = r.get(spec.keyed_by) if spec.keyed_by else key
            value.setdefault(bucket, []).append(r)
            touched |= register_in_container(containers, spec, r)
            inserted += 1
    elif isinstance(value, dict):
        for r in records:
            rid = str(r.get(spec.keyed_by or "id") or "")
            if rid and rid not in value:
                value[rid] = r
                touched |= register_in_container(containers, spec, r)
                inserted += 1
    if report is not None:
        report["touched_containers"] = touched
    return inserted
