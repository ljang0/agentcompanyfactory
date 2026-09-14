"""Add the bulk layer to a seeded world: the model writes generator specs, code builds records.

One call per app, after the human layer is seeded and reviewed. The model sees the app's state
schema, what the human layer already holds (counts and samples per collection, the people),
the canonical world, and the placeholder vocabulary, and returns specs for the repetitive
traffic that unit really has. Records are expanded deterministically, appended under the byte
budget per app in config.toml, recorded in world/BULK.json, and excluded from the texture gate
because they are meant to repeat. The budget was set against the browser storage quota, which the
storage shim has made a recorded no-op; what it costs now is measured in the marker's ``volume``.

The folder is then checked the way the check stage checks it. An error the human layer shows
on its own goes back to the app author through seeding's mechanical repair (``folder_author``,
``repair_mechanical``), the bulk is laid again over the repaired state, and the rounds are
recorded in BULK.json as ``mechanical_repair_rounds``.

BULK.json records ``bulk_version``, the expansion semantics that wrote the layer. A world laid by
older code is stale, the driver runs the step again, and the previous layer is stripped from the
states on disk before the new one goes on: a layer left under a second one reads as the human
layer from then on, which is how one world's gmail came to hold 1,365 records of a layer BULK.json
no longer names.
"""

import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from difflib import SequenceMatcher
from pathlib import Path

from company_envs.config import load_config
from company_envs.models import ModelOutputInvalid, Models
from company_envs.receipt import FAULTED, REFUSED, Receipt, outcome_of, rollup
from company_envs.storage import digest, now, read, write

from .bulk import (
    PLACEHOLDER,
    BulkSpec,
    BulkSpecs,
    add_to_state,
    expand,
    id_fields,
    judged_fields,
    machine_stamped,
    native_ids,
    own_identifiers,
    people_of,
    primary_identifiers,
    recompute_derived,
    rows_of,
    state_identifiers,
    with_native_ids,
)
from .bulk import __doc__ as PLACEHOLDERS
from .hub_app import record_collections, validate_state
from .state_seed import (
    MECHANICAL_REPAIR_ROUNDS,
    folder_author,
    load_skill,
    mechanical_feedback,
    project_world,
    repair_mechanical,
    seed_schema,
    skill_for_call,
)
from .world_check import check_folder, reachability, template_tells

SKILL = "company-world-states"
DEFAULT_MAX_BYTES = 4_000_000

# Two different ceilings limit how much one app may hold, and only one of them is storage.
#
# * The browser's. ``localStorage`` takes 5 MiB of ``JSON.stringify`` characters per origin, and
#   the ``hub_app`` shim makes an over-quota write a no-op, so past it the cache is *absent*. For
#   25 of the 26 apps measured that is harmless -- they gate their first fetch on the state key
#   itself, find it missing and re-read the server -- and they render, write and read back
#   unchanged at 12 MB.
# * The clock. Most apps virtualise and cost nothing extra to 12 MB, but ``gmail_mock`` renders the
#   whole mailbox: 578,099 characters of visible text at 12 MB, about **+3 s of page load per MB**.
#   A 25-minute per-worker episode makes that the binding limit long before storage is.
#
# Measured 2026-09-10 over 136 probes at 2-12 MB; ``experiments/VOLUME-CEILINGS.md`` has the ladder
# per app and the screenshots. An app that is not in the map takes the default, so an app nobody
# has measured behaves exactly as it did before the map existed.
#
# ``google_docs_mock`` is not in the map, and cannot be put in it. It gates its server fetch on a
# separate ``_initial_`` key that its own demo data writes at module load, before the seeded state
# arrives, so that 5 KB write always fits: past the quota ``isRefresh`` is true while the state
# cache is absent, the fetch is skipped, and DocsContext posts the demo state back with
# ``action: 'set'``. At 5.03 MB it passes every check; at 5.36 MB the first reload replaced a
# company's 1,947 documents with the five the clone ships ("Project Proposal", "Meeting Notes -
# Feb 14") and took a typed write with them. Every other entry here truncates a world when it is
# wrong; this one corrupts it, so it is held in code where a config edit cannot raise it.
HARD_MAX_BYTES = {"google_docs_mock": 4_500_000}

# Why each measured ceiling is where it is. The two constraints have different remedies and a
# reader who mistakes one for the other raises the wrong number: more volume bought against the
# clock is paid for out of the episode, not out of storage. Written into the marker beside the
# budget, so "gmail was 400 emails short" arrives with the reason it was capped there.
CEILING_REASONS = {
    "gmail_mock": (
        "clock, not quota: storage-clean to 12.07 MB, but it renders the whole mailbox at about "
        "+3 s of page load per MB, and a worker gets 25 minutes"
    ),
    "google_docs_mock": (
        "quota: past 5.03 MB it skips the server fetch, serves the five documents the clone ships "
        "and posts them back over the world; held in HARD_MAX_BYTES, not raisable from config"
    ),
    "slack_mock": "measured clean to 12.28 MB, flat render cost; a 40-person quarter would be 15.3 MB",
    "google_drive_mock": "measured clean to 12.11 MB, flat render cost; a department drive is 9-90 MB",
    "shopify_admin_mock": "measured clean to 11.99 MB, flat render cost; a merchant year is ~51 MB",
    "google_calendar_mock": "realistic volume, not a ceiling: 40 people for half a year is 2.2 MB",
    "trello_mock": "realistic volume, not a ceiling: a real board holds 200-2,000 cards, 0.24-2.4 MB",
    "robinhood_mock": "realistic volume, not a ceiling: an account's alerts are hundreds, under 0.5 MB",
}


def max_bytes_for(config, app_id):
    """The byte budget for one app: its own entry, else the default, never above a hard ceiling.

    A missing ``bulk_max_bytes_by_app`` entry means the default and nothing else. The clamp is one
    way only -- it can lower a configured budget, never raise one -- so an app with a hard ceiling
    is safe whatever the map or the default says.
    """
    design = (config or {}).get("design") or {}
    default = int(design.get("bulk_max_bytes_per_app", DEFAULT_MAX_BYTES))
    budget = int((design.get("bulk_max_bytes_by_app") or {}).get(app_id, default))
    return min(budget, HARD_MAX_BYTES.get(app_id, budget))


# The expansion semantics this module lays records with. Bump it whenever a change would make an
# existing layer come out different -- what lands, where, what its ids look like -- and every world
# laid by the older code becomes stale: ``current`` says so, the driver re-runs the step instead of
# seeing a marker and moving on, and ``add_bulk`` strips the old layer before laying the new one.
#
# That path did not exist, and it is how a fix reaches nobody. The driver gates this step on
# world/BULK.json being present and newer than the app assignment, so the 42 worlds on disk -- laid
# by code that kept 3 of 273,453 declared Slack messages and 3 of 124,265 calendar events, and
# stamped the word "bulk" on 349,135 ids -- would have carried that layer to delivery however often
# the expander was fixed. A marker with no ``bulk_version`` at all is one of those.
#
# 2: the marker lists the ids the records own instead of every id-shaped field they carry, a
# previous layer is stripped by what it left rather than by what the marker listed, an invented
# reference is anchored instead of refused, and a shortfall names its cause.
# 3: a generated record is filed in the container list the app draws it from, and a reference is
# judged on a field that names one of this app's collections even when the human layer resolves
# none of it. Both change where records land: blastx's trello board draws 23 of 3,288 cards under
# 2 and 3,146 of 3,229 under 3, so a layer laid by the older code is a different world.
# 4: include single-user mail/calendar identities in placeholder expansion.
# 5: spread sparse streams across their history window; use calendar months without drift.
BULK_VERSION = 5

# Tries at a spec set before the app keeps its human layer and the marker says why. Each attempt is
# one model call and sees the previous attempt's refusals. Three was the number when the only gate
# was the schema; the gate now refuses template tells, unaddressable records and invented
# references, and it refuses 140 of the 199 spec sets on disk even after their references are
# anchored -- 84 for records that reach no worker, 104 for an em dash in a title, 57 for prose that
# restates a field. Three tries against a gate with that prior loses a whole app's layer too often.
SPEC_ATTEMPTS = 4


def laid_by_this_code(marker):
    """Whether a BULK.json's layer was expanded by this module's semantics."""
    return isinstance(marker, dict) and marker.get("bulk_version") == BULK_VERSION


def current(marker):
    """Whether a BULK.json is a finished layer this module need not lay again.

    A marker naming a fault is not one. An app whose specs were *refused* got a verdict and keeps
    its human layer; an app whose run hit a provider outage got no verdict at all and still holds
    the layer it had. Reading the second as done is how a stage with no failure receipt becomes a
    world nobody looks at again -- so the marker records the fault and reads as stale, and the
    driver runs the step again instead of seeing a marker and moving on.

    The question is asked of the marker's ``outcome`` now, through the shared reader, which also
    answers it for the markers already on disk: ``outcome_of`` reads the ``faults`` key this module
    used to be the only one to understand. The staleness rule is unchanged -- only ``faulted`` is
    stale -- because a refusal is a verdict and re-running it reaches the same answer.
    """
    return laid_by_this_code(marker) and outcome_of(marker) != FAULTED


def lay_bulk(state, specs, seed, max_bytes, *, people=None):
    """Expand ``specs`` over the people of ``state`` and append the records in place.

    Deterministic for the same specs, people and seed, so the layer can be laid again over a
    repaired human layer. Returns (records added per collection, identifiers, shortfalls).

    A spec asks for a number and the cadence and the byte budget decide how many of them fit, so
    what a world ships is routinely less than what it declared: fleet-wide 197,452 of 653,424
    declared records reached disk, and two companies declared thousands and wrote none at all. That
    was invisible because only the inserted count was ever recorded. The third return value is what
    each spec asked for against what it got, so the caller can say so instead of guessing.

    ``state`` must be a human layer with no bulk on it: the ids come from the collection's own
    scheme, so laying over a state that already holds a layer continues that layer's numbering
    instead of repeating it. ``add_bulk`` and ``world_repair`` both strip before they lay.

    The identifiers returned are the ones the records own, never the ones they point at. Returning
    every id-shaped field instead put 27,883 foreign keys in the fleet's markers as "bulk ids", and
    a foreign key there is a human record: the strip that precedes a re-lay deleted 4,732 human
    records across the fleet -- every folder of nordstrom's drive, 2,608 items down to 1 -- and the
    texture gate excused every human record a generated one happened to name.
    """
    people = people_of(state) if people is None else people
    # One id mapping for the whole layer, computed from the state the records are going into, so
    # the filler reads like the records already there instead of announcing itself.
    mapping = native_ids(state, specs)
    added, owned, short, containers = {}, set(), [], set()
    for spec in (with_native_ids(s, mapping) for s in specs):
        records = expand(spec, people, seed=seed)
        fit = {}
        n = add_to_state(state, spec, records, max_bytes, report=fit)
        added[spec.collection] = added.get(spec.collection, 0) + n
        if n < spec.count:
            # Measured against what the spec asked for, not against what expansion produced: a
            # cadence that cannot hold the count is as much missing volume as a full disk, and
            # comparing with len(records) hid it entirely.
            #
            # Each cause is named separately because each has a different lever: the window and the
            # cadence are the spec's, the byte budget is config.toml's, and a duplicate id is this
            # expansion colliding with the records already there.
            lost = {
                "cadence": max(0, spec.count - len(records)),
                "byte budget": max(0, len(records) - fit.get("room", len(records))),
            }
            lost["duplicate ids"] = max(0, min(len(records), fit.get("room", len(records))) - n)
            reasons = [
                f"cadence {spec.cadence} holds {len(records)}" if lost["cadence"] else "",
                f"byte budget holds {fit.get('room', 0)} of {len(records)}" if lost["byte budget"] else "",
                f"{lost['duplicate ids']} ids already in the state" if lost["duplicate ids"] else "",
            ]
            short.append(
                {
                    "collection": spec.collection,
                    "asked": spec.count,
                    "written": n,
                    "lost_to": {k: v for k, v in lost.items() if v},
                    "reason": "; ".join(r for r in reasons if r),
                }
            )
        for r in records[:n]:
            if isinstance(r, dict):
                owned |= own_identifiers(r, spec)
        # A container this layer filed records into keeps a count of them in some apps. It is the
        # author's record and stays out of ``owned`` -- putting a human id in the marker is how a
        # strip deleted 4,732 human records -- but its count is now this layer's doing, so it is
        # recomputed with the generated ones.
        containers |= fit.get("touched_containers") or set()
    recompute_derived(state, owned | containers)  # after every spec: a ticket's comments may be a later one's
    return added, owned, short


# What to do about each cause of a shortfall, named in the receipt so the reader does not have to
# know. A note nobody can act on is the same as no note: the fleet shipped 197,452 of 653,424
# declared records with the difference recorded only as a per-spec count.
SHORTFALL_LEVERS = {
    "byte budget": "raise this app's design.bulk_max_bytes_by_app entry in config.toml (or the "
    "design.bulk_max_bytes_per_app default) and run add-bulk --again; an app in "
    "bulk_layer.HARD_MAX_BYTES cannot be raised at all",
    "cadence": "the spec's window cannot hold its count: re-spec with a wider window or a denser cadence",
    "duplicate ids": "the spec's ids collide with records already in the state: re-spec the id_prefix",
}


def _volume(results):
    """Declared against written across every app, what refused the difference, and the lever for it.

    A shortfall has to be actionable to be worth writing down. ``lost_to`` totals the causes over
    the whole world and ``levers`` names the one change that fixes each, so a driver or a reader can
    tell a world that is 2% short on a cadence it chose from one that is 40% short because a number
    in config.toml is too small.
    """
    asked = written = 0
    short, lost = {}, {}
    for app_id, row in (results or {}).items():
        if not isinstance(row, dict):
            continue
        asked += sum(int(s.get("count") or 0) for s in row.get("specs") or [])
        written += sum(int(n) for n in (row.get("added") or {}).values())
        if row.get("short"):
            short[app_id] = row["short"]
            for entry in row["short"]:
                for cause, n in (entry.get("lost_to") or {}).items():
                    lost[cause] = lost.get(cause, 0) + int(n)
    return {
        "asked": asked,
        "written": written,
        "share": round(written / asked, 4) if asked else 1.0,
        "complete": not short,
        **({"lost_to": lost} if lost else {}),
        **({"levers": {c: SHORTFALL_LEVERS[c] for c in lost if c in SHORTFALL_LEVERS}} if lost else {}),
        **({"short_by_app": short} if short else {}),
    }


def _summary(state, keys):
    out = {}
    for key in keys:
        value = state.get(key)
        rows = (
            list(value.values())
            if isinstance(value, dict) and not all(isinstance(v, list) for v in value.values())
            else (
                [r for v in value.values() for r in v]
                if isinstance(value, dict)
                else value
                if isinstance(value, list)
                else []
            )
        )
        rows = [r for r in rows if isinstance(r, dict)]
        out[key] = {
            "count": len(rows),
            "samples": rows[:2],
            "shape": "map"
            if isinstance(value, dict) and not all(isinstance(v, list) for v in (value or {}).values())
            else "keyed-lists"
            if isinstance(value, dict)
            else "list",
        }
    return out


def check_addressing_template(spec, template, state, *, people=None):
    """A generated message must reach a mailbox: some party field has to name a person here.

    Every worker sees the shared store filtered to the records that name them, so a template
    whose ``to`` is one fixed outside address expands into records no worker can open. 19,140 of
    the fleet's 27,042 records that reach nobody came out of templates like that. ``{{person}}``
    and ``{{person.email}}`` draw from this app's own users, which is what the placeholder is
    for; a literal address counts when it belongs to somebody in the app.
    """
    from .hub_identity import ACCOUNT_SCOPES

    fields = ACCOUNT_SCOPES.get(spec.collection)
    if not fields or spec.collection == "threads":
        return []
    present = {f: template[f] for f in fields if template.get(f) not in (None, "", [], {})}
    if not present:
        return []  # no party field at all: the record stays visible to everyone
    here = {
        str(value).strip().casefold()
        for person in (people_of(state) if people is None else people)
        for value in person.values()
        if isinstance(value, str) and value.strip()
    }
    text = json.dumps(present, ensure_ascii=False)
    if "{{person" in text or any(name and name in text.casefold() for name in here):
        return []
    return [
        (
            f"{spec.collection}: {'/'.join(present)} names nobody who works here, so not one of "
            f"the {spec.count} records is visible to any worker. Address them with {{{{person}}}} "
            "and {{person.email}}, or to a person this app already has"
        )
    ]


def _candidates(value, spec, prefixes):
    """The values a template's reference field can take: (the set, whether the set is knowable).

    "literal" is every value it will ever hold. "generated" is an id this expansion invents, and
    the set is the prefixes it is built from. "free" is a person draw or a placeholder whose
    target cannot be decided here, and nothing is judged.
    """
    text = value.strip()
    if "{{" not in text:
        return {text}, "literal"
    if "{{person" in text:
        return set(), "free"
    whole = PLACEHOLDER.fullmatch(text)
    if not whole:
        return set(), "free"
    name, arg = whole.group(1), whole.group(2) or ""
    if name == "pick":
        table, _, field = arg.partition(".")
        rows = spec.tables.get(table) or []
        return {str(r.get(field, "")) if field and isinstance(r, dict) else str(r) for r in rows}, "literal"
    if name == "choice":
        return {o.strip() for o in arg.split("|") if o.strip()}, "literal"
    if name == "id":
        prefix, sep, first = arg.rpartition(":")
        return {prefix if sep and first.isdigit() else arg}, "generated"
    return set(), "free"


def check_reference_template(spec, template, state, prefixes=()):
    """Every foreign key a template emits must name a record the app already has.

    A template is expanded ``count`` times, so one invented parent is ``count`` unreachable
    records. One company's drive holds 2,573 items of which 2,529 are parented to folders that
    were never created and 2,517 owned by users the app has never heard of; another holds 3,004
    items and no folder at all. Fleet-wide that is 20,155 items with a dangling parent and 10,580
    with an owner nobody can be. The records exist, are paid for, and cannot be opened.

    Nothing downstream catches it. The check stage's reference check deliberately collapses a field
    that never resolves into a single warning, because a grouping key with no collection behind it
    is how Gmail works; a template that invents every one of its parents looks exactly like that.
    So the spec is refused here, before it expands, while the model can still be asked for a
    better one -- on the fields ``judged_fields`` names, which are the ones the seeded state
    resolves plus the ones whose own name names a collection this app holds. That second half is
    what sees the worst case: where the human layer resolves a field nowhere, judging only what
    resolves abstains, and abstaining is how 3,000 items came to sit under folders nobody wrote.
    """
    universe, judged, problems = state_identifiers(state), judged_fields(state), []
    for where, key, value in id_fields(template):
        if key not in judged:
            continue  # a grouping or external key: this app holds no records it could name
        values, kind = _candidates(value, spec, prefixes)
        if kind == "free":
            continue
        if kind == "generated":
            if values - set(prefixes):
                problems.append(
                    f"{where} builds an id from {min(values)!r}, which no spec in this call "
                    f"stamps, so every one of the {spec.count} records points at a record nothing "
                    f"creates; reference an id the app already has"
                )
            continue
        missing = sorted(v for v in values if v and v not in universe)
        if missing:
            # The ids the human layer already puts in this field, so the revision has somewhere to
            # point instead of guessing: each refusal costs a model call.
            here = sorted({v for _p, k, v in id_fields(state) if k == key and v in universe})[:5]
            problems.append(
                f"{where} is {missing[0]!r}, which is not a record of this app, so every one of the "
                f"{spec.count} records points at something that does not exist; "
                + (
                    f"this app has {here} for {key}, so use one of those or drop the field"
                    if here
                    else f"and this app holds no record any {key} names, so there is nothing to "
                    f"file these under -- write the containers first, or drop the field"
                )
            )
    return problems


def anchor_references(spec, state):
    """File a template's invented parent where the app already files things, instead of refusing it.

    ``check_reference_template`` is the right place to catch an invented reference, but a refusal
    costs one of the few attempts the call has, and the answer is mechanical: this collection
    already puts real values in this field. 23,367 records' worth of templates name a parent their
    collection does not have -- 20,559 drive items under folders nobody created, 2,808 outlook
    messages in a folder that does not exist -- and every one of them is unopenable volume.

    Only a field the seeded state resolves at least once is touched, and only a literal: a
    placeholder's target is decided per record, and a field that never resolves (Gmail's
    ``threadId``) is not a reference at all. The value chosen is the one the human layer's own
    records most look like for that field, so a file lands in the folder its siblings are in.
    Returns (the spec, one note per field rewritten).
    """
    judged, universe, notes = judged_fields(state), state_identifiers(state), []
    used = {}
    for value in (state or {}).values():
        for record in rows_of(value):
            for _where, key, entry in id_fields(record):
                if key in judged and entry in universe:
                    used.setdefault(key, Counter())[entry] += 1

    # An id that exists is not the same as a place records can be seen from. One drive's 183 bulk
    # items were filed under a folder that exists and is itself an orphan, so they moved from
    # "names a container that does not exist" to "under a container that is itself unreachable" and
    # not one of them reached a screen. Where the human layer offers a reachable target, it wins.
    unreachable = {
        rid for _name, (missing, _total, _mech, _why) in reachability(state).items() for rid in missing
    }

    def pick(key, value):
        options = used.get(key) or Counter()
        if key not in judged or value in universe or not options:
            return None
        return max(
            options,
            key=lambda v: (v not in unreachable, SequenceMatcher(None, value, v).ratio(), options[v], v),
        )

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if key != "id" and key.endswith(("Id", "_id", "ID")) and isinstance(value, (str, int)):
                    if "{{" not in str(value) and (chosen := pick(key, str(value))) is not None:
                        node[key] = chosen
                        notes.append(
                            f"{path}/{key}: {value!r} is no record of this app, filed under {chosen!r}"
                        )
                else:
                    walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}/{index}")

    template = spec.template  # a fresh parse every access: this is the spec's own copy
    walk(template)
    if not notes:
        return spec, []
    return spec.model_copy(update={"template_json": json.dumps(template, ensure_ascii=False)}), notes


def check_specs(specs, app, state, keys, max_bytes, *, people=None):
    # Specs of one call reference each other's ids -- a comments spec sets ticket_id to the prefix
    # the tickets spec stamps -- so a generated reference is judged against all of them.
    declared = {s.id_prefix for s in specs}
    problems = []
    for spec in specs:
        if spec.app_id != app["app_id"]:
            problems.append(f"spec for {spec.app_id} in a call for {app['app_id']}")
        if spec.collection not in keys:
            problems.append(f"{spec.collection!r} is not a record collection of {app['app_id']}: {keys}")
            continue
        try:
            template, _ = spec.template, spec.tables
        except ValueError as exc:
            problems.append(f"{spec.collection}: {exc}")
            continue
        sample = _summary(state, [spec.collection])[spec.collection]["samples"]
        if sample and not set(template) & set(sample[0]):
            problems.append(
                f"{spec.collection} template shares no field with existing records; shape it like {sorted(sample[0])[:8]}"
            )
        if spec.keyed_by and spec.keyed_by not in template:
            problems.append(f"{spec.collection}: keyed_by {spec.keyed_by!r} is not a field of the template")
        if spec.start[:10] > spec.end[:10]:
            problems.append(f"{spec.collection}: start must not be after end")
        if len(spec.template_json) * spec.count > max_bytes * 2:
            problems.append(
                f"{spec.collection}: {spec.count} records of this size exceed the app's storage budget; lower the count"
            )
        # What the template stamps on all `count` records. The state check needs a repeat before
        # it calls a shape a stamp; here the repeat is the spec, and this is the last point at
        # which anything can be asked for a better one -- once the records are laid, the repair
        # path goes to the app author, who did not write them.
        problems += [f"{spec.collection}: {p}" for p in template_tells(template)]
        problems += check_addressing_template(spec, template, state, people=people)
        problems += [
            f"{spec.collection}: {p}" for p in check_reference_template(spec, template, state, declared)
        ]
    return problems


def numbered_families(ids):
    """The ids that belong to a run the expander stamped: a shared prefix and a running number.

    Part of reading an older marker's id list safely. Before ``bulk_version`` 2 the list held every
    id-shaped field of every generated record, so a parent or an owner the records point at is in it,
    and stripping by the whole list deletes the human record that is: 4,732 of the fleet's 81,886
    human records, nordstrom's drive from 2,608 items to 1. What a generated id always is, and a
    pointed-at human record usually is not, is one of ``count`` ids sharing a prefix and counting up;
    a folder called ``drive-policies`` is in no such run.
    """
    families = {}
    for identifier in ids:
        if match := re.fullmatch(r"(.*\D)(\d+)", str(identifier)):
            families.setdefault(match.group(1), set()).add(str(identifier))
    return {i for members in families.values() if len(members) > 1 for i in members}


def laid_by_older_marker(state, entry):
    """An older marker's id list, narrowed to the records that layer actually wrote.

    Two narrowings, because the list is every id-shaped field of every generated record. Only ids a
    record of an added-to collection calls itself by: a calendar's ``c1``/``c2`` is in the list
    because every generated event names it, and stripping by it took all 3,751 of one world's events.
    And only ids in a numbered run, which is what the expander stamps and a folder name is not.
    """
    recorded = set(entry.get("ids") or ())
    ids = set()
    for collection in entry.get("added") or state:
        ids |= numbered_families(recorded & primary_identifiers({collection: state.get(collection)}))
    return ids


def recorded_specs(entry):
    """The specs a marker entry holds, as specs; an entry that cannot be parsed holds none."""
    out = []
    for raw in (entry or {}).get("specs") or []:
        try:
            out.append(BulkSpec.model_validate(raw))
        except ValueError:
            return []
    return out


def previous_layer_ids(state, entry, seed=0, trusted=False):
    """Every record of a bulk layer already on ``state``, whatever marker does or does not name it.

    Three sources, because no one of them is complete:

    * the ids the marker lists, when the marker was written by this module (``trusted``). Older
      markers list every id-shaped field of every generated record, and a foreign key there names a
      human record, so an untrusted list goes through ``laid_by_older_marker`` first.
    * the machine-stamped ids the state itself carries. A layer nothing claims is stripped by
      nothing: 33,880 records in 10 of 42 worlds, and fairfax's drive residue sits under an app the
      marker has no entry for. This is the only source that finds those.
    * a recorded spec's ``id_prefix``, with the running number it stamps, for a layer laid before
      the prefixes were rewritten into the collection's own scheme.

    Measured over the 42 worlds on disk: stripping by the marker's list alone leaves 25,974 records
    of unclaimed layers behind and destroys 4,732 human records; this leaves 0 behind and destroys
    0. The re-expansion for a marker whose ``ids`` is null stays as it was.
    """
    ids, specs = set(), entry.get("specs") or []
    present = primary_identifiers(state)
    recorded = entry.get("ids")
    if recorded is None:
        # A marker that predates ``ids``: rebuild them from the specs it does record. Approximate,
        # because the prefixes were rewritten against the human layer that is no longer on disk.
        people = people_of(state)
        for raw in specs:
            spec = BulkSpec.model_validate(raw)
            for r in expand(spec, people, seed=seed):
                if isinstance(r, dict):
                    ids |= own_identifiers(r, spec)
    elif trusted:
        ids |= set(recorded)
    else:
        ids |= laid_by_older_marker(state, entry)
    ids |= {i for i in present if machine_stamped(i)}
    if not trusted:
        stamps = [re.compile(re.escape(p) + r"\d+") for p in {s.get("id_prefix") for s in specs} if p]
        ids |= {i for i in present if any(s.fullmatch(i) for s in stamps)}
    return ids


def bulk_ids_for(root, folder):
    """Identifiers of bulk-layer records per app, from BULK.json and from the states themselves.

    Every app is looked at, not only the ones the marker has an entry for: a layer the marker does
    not name is still a bulk layer, and the texture gate judging it as human prose is how fairfax
    came to fail on 3,115 machine-stamped emails that no repair path could have rewritten.
    """
    marker = Path(folder) / "world" / "BULK.json"
    if not marker.is_file():
        return {}
    config = load_config(root)
    seed = config.get("generation", {}).get("seed", 0)
    recorded = read(marker)
    trusted = current(recorded)
    entries = recorded.get("apps") or {}
    out = {}
    for state_path in sorted((Path(folder) / "world").glob("*.state.json")):
        app_id = state_path.name.removesuffix(".state.json")
        entry = entries.get(app_id)
        ids = previous_layer_ids(
            read(state_path), entry if isinstance(entry, dict) else {}, seed=seed, trusted=trusted
        )
        if ids or isinstance(entry, dict):
            out[app_id] = ids
    return out


def add_bulk(root, folder, models=None, again=False):
    from .world_repair import strip_bulk  # imported here: world_repair lays this module's layer

    root, folder = Path(root), Path(folder)
    marker = folder / "world" / "BULK.json"
    try:
        previous = read(marker) if marker.is_file() else {}
    except ValueError:
        previous = {}  # a half-written marker names no layer: lay one and write a marker that does
    if previous and not again and current(previous):
        # Nothing ran, and that is a pass: the layer this marker names is on disk and is the one
        # this code lays. The outcome says so in the shared word rather than leaving a reader to
        # decide what a bare ``skipped`` meant -- the ambiguity that made eight of these defects.
        return {**Receipt.passed(apps=0).body, "skipped": "bulk layer present"}
    if not (folder / "world" / "SEED.json").is_file():
        raise ValueError("seed the world first")
    entries = previous.get("apps") or {}
    config = load_config(root)
    default_max_bytes = int(config["design"].get("bulk_max_bytes_per_app", DEFAULT_MAX_BYTES))
    models = models if models is not None else Models(config, folder / "world" / "bulk")
    skill_text, skill = load_skill(root, SKILL)
    instructions = skill_for_call(skill_text, "bulk_specs")
    seed = read(folder / "world" / "SEED.json")
    company = read(folder / "company.json")
    world = read(folder / "world" / "world.json")
    apps = read(folder / "apps.json")["apps"]
    generation_seed = config.get("generation", {}).get("seed", 0)
    results, bulk_ids, human, faults = {}, {}, {}, {}
    # A layer laid before comes off before this one goes on. Leaving it would make the records on
    # it read as the human layer from here on: the app author would be shown generated records to
    # repair, the new layer would stack on the old one, and BULK.json would name half of what is
    # bulk. An app whose specs are refused keeps its human layer, which is what an author can
    # answer for.
    #
    # Every app is stripped, not only the ones the marker has an entry for, and by what a previous
    # layer really left rather than by the list the marker happens to hold: fairfax's drive residue,
    # 4,872 records, belongs to an app the marker has no entry for.
    #
    # The strip happens *in memory*. It used to be written to disk before any spec was authored, so
    # anything that stopped the laying destroyed a layer that was fine: a cache miss on one run left
    # blastx's trello at 106 cards, having served 3,288, with nothing recording why. A world is only
    # replaced once its replacement exists, so a provider outage now costs a retry instead of a
    # company's volume.
    bare, stripped = {}, {}
    for app in apps:
        path = folder / "world" / f"{app['app_id']}.state.json"
        if not path.is_file():
            continue
        entry = entries.get(app["app_id"])
        state = read(path)
        ids = previous_layer_ids(
            state,
            entry if isinstance(entry, dict) else {},
            seed=generation_seed,
            trusted=laid_by_this_code(previous),
        )
        bare[app["app_id"]] = strip_bulk(state, ids) if ids else state
        if ids:
            stripped[app["app_id"]] = len(ids)

    def one_app(app):
        state_path = folder / "world" / f"{app['app_id']}.state.json"
        if app["app_id"] not in bare:
            return app["app_id"], None, None
        state = deepcopy(bare[app["app_id"]])
        # Resolved per app, not once for the world: slack renders and writes cleanly at 12.28 MB
        # while google_docs cannot be trusted past 5.03 MB, and one number cannot serve both.
        max_bytes = max_bytes_for(config, app["app_id"])
        schema_text = (root / app["schema"]).read_text()
        declared = record_collections(schema_text)
        keys = [k for k in (declared or []) if k in state]
        if not keys:
            # An app with nothing to fill said so by being absent from BULK.json, which reads
            # exactly like an app that was never tried. 20 of the 94 schemas landed here because
            # their record table is written in TypeScript (``Issue[]``) and went unread; the one
            # that remains, canvas_mock, really holds a serialized canvas and no records.
            #
            # This is a verdict, so the previous layer comes off: the state written here is the
            # human layer, which is what an app with nothing to fill should hold.
            write(state_path, state)
            return (
                app["app_id"],
                {
                    "skipped": "no record collections this layer can fill"
                    if declared
                    else "the schema document declares no record collections",
                    "record_collections": declared or [],
                    "added": {},
                    "specs": [],
                    "bytes": len(json.dumps(state)),
                    "receipt": None,
                    "ids": [],
                },
                state,
            )
        human_layer = deepcopy(state)  # kept so a repair rewrites the human layer, never the bulk
        people = people_of(state)
        payload = {
            "call": "bulk_specs",
            "app": {
                "app_id": app["app_id"],
                "schema_document": seed_schema(schema_text),
                "record_collections": keys,
            },
            "company_summary": {k: company.get(k) for k in ("id", "name", "sector", "operations")},
            "reference_date": seed.get("reference_date"),
            "human_layer": _summary(state, keys),
            "people": people[:40],
            "canonical_world": project_world(world, app["app_id"]),
            "budget": {"max_bytes_for_app": max_bytes, "bytes_used": len(json.dumps(state))},
            "placeholders": PLACEHOLDERS,
            "output_contract": "specs only for records that repeat by nature in this unit; every table value must come from the canonical world, the people, or the human layer; ids use id_prefix so they cannot collide.",
        }

        def judge(candidates):
            """Anchor what can be anchored, then the gate's verdict. (specs, notes, problems)."""
            repaired = [anchor_references(s, state) for s in candidates]
            specs = [s for s, _notes in repaired]
            notes = [f"{s.collection}{n}" for s, notes in repaired for n in notes]
            return specs, notes, check_specs(specs, app, state, keys, max_bytes)

        # The specs BULK.json already holds are re-laid when they still pass the gate against this
        # human layer, and only then: the expander changed, the specs did not. 59 of the 199 spec
        # sets on disk pass, and they are 312,979 records for no model call at all, against a fresh
        # call that the same gate refuses 140 times in 199.
        feedback, receipt = None, None
        specs, anchored, problems = judge(recorded_specs(entries.get(app["app_id"])))
        reused = bool(specs) and not problems
        if not reused:
            for _attempt in range(SPEC_ATTEMPTS):
                prompt = (
                    instructions
                    + "\n"
                    + json.dumps(
                        {**payload, **({"revision_feedback": feedback} if feedback else {})},
                        ensure_ascii=False,
                    )
                )
                result, receipt = models.call("world_states", prompt, BulkSpecs)
                # An invented reference is repaired here rather than sent back: the answer is in the
                # state, and an attempt spent on it is an attempt not spent on the prose.
                specs, anchored, problems = judge(result.specs)
                if not problems:
                    break
                feedback = problems
            else:
                raise ModelOutputInvalid("; ".join(problems[:6]), result.model_dump(), receipt)
        added, ids, short = lay_bulk(state, specs, generation_seed, max_bytes)
        validate_state(
            {"id": app["app_id"], "state_keys": app.get("top_level_keys") or []}, state, schema_text
        )
        write(state_path, state)
        return (
            app["app_id"],
            {
                **Receipt.passed().body,
                "specs": [s.model_dump() for s in specs],
                "added": added,
                **({"reused_specs": True} if reused else {}),
                # What each spec asked for against what the byte budget let through. Empty means
                # the app got everything it declared; anything here is volume the world does not
                # have and nobody would otherwise know was missing.
                "short": short,
                "bytes": len(json.dumps(state)),
                # The budget this app was laid under, which the world-level one no longer implies,
                # and why it is that number rather than the default.
                "max_bytes": max_bytes,
                **({"max_bytes_reason": reason} if (reason := CEILING_REASONS.get(app["app_id"])) else {}),
                "receipt": receipt,
                "ids": sorted(ids),
                **({"anchored": anchored} if anchored else {}),
                **({"stripped_ids": stripped[app["app_id"]]} if app["app_id"] in stripped else {}),
            },
            human_layer,
        )

    def guarded(app):
        """One app, and the difference between a verdict and a fault.

        A refusal is a verdict about this world: the specs never validated, the app keeps its human
        layer, and the marker says so. Anything else -- a provider outage, a dead socket, a bug --
        says nothing about the world, so the state on disk is left exactly as it was, layer and
        all, and the marker records a fault that makes it stale. Writing the human layer for both
        is how an operation meant to repair a world came to destroy the volume it already had.

        The split is not a judgement call: ``models.py`` already encodes it in the exception
        hierarchy. ``PromptTooLarge`` and ``ModelOutputInvalid`` are ``ValueError`` -- verdicts
        about the work -- while ``ModelUnavailable`` and ``CallBudgetExhausted`` are
        ``RuntimeError`` -- faults about the environment. So ``except ValueError`` is the verdict
        catch anywhere in this pipeline, and everything else, a plain bug included, is a fault
        because it says nothing about the world either.
        """
        app_id = app["app_id"]
        state_path = folder / "world" / f"{app_id}.state.json"
        try:
            return one_app(app)
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt | SystemExit):
                raise
            # One classifier for both branches, and it is the type hierarchy rather than this
            # function's judgement: ValueError is a verdict, everything else is a fault.
            answer = Receipt.from_exception(exc, f"bulk layer for {app_id}")
            print(f"warning: bulk layer {answer.outcome} for {app_id}: {answer.reason}", file=sys.stderr)
            if answer.outcome == REFUSED:
                if app_id in bare:
                    write(state_path, bare[app_id])  # the verdict: this app holds its human layer
                return (
                    app_id,
                    {
                        **answer.body,
                        # The old spelling, kept because the cohort report and the markers on disk
                        # read it: a refused app's entry said ``skipped: <reason>`` and nothing else.
                        "skipped": answer.reason,
                        "added": {},
                        "specs": [],
                        "bytes": None,
                        "receipt": None,
                        "ids": [],
                    },
                    bare.get(app_id),
                )
            faults[app_id] = f"{type(exc).__name__}: {str(exc)[:250]}"
            entry = entries.get(app_id)
            # Nothing was written, so the layer this app already had is still on disk and still
            # this marker's to name. Dropping its ids would leave those records reading as human.
            return (
                app_id,
                {
                    **(entry if isinstance(entry, dict) else {"added": {}, "specs": [], "ids": []}),
                    **answer.body,
                    "fault": faults[app_id],
                },
                None,
            )

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(apps)))) as pool:
        for app_id, outcome, human_layer in pool.map(guarded, apps):
            if outcome:
                results[app_id] = outcome  # ids stay on disk: later checks exclude them from texture
                bulk_ids[app_id] = set(outcome.get("ids") or [])
            if human_layer is not None:
                human[app_id] = human_layer
    # A faulted app was not written, so nothing came off it: saying otherwise would have the
    # marker report a strip that never happened.
    stripped = {app_id: n for app_id, n in stripped.items() if app_id not in faults}
    checks = check_folder(root, folder, bulk_ids=bulk_ids)

    def human_check():
        """The same check over the human layers alone: what the app author can answer for."""
        return check_folder(root, folder, states=human, bulk_ids={})

    def relay():
        """Write every human layer with its bulk laid again on top, then check the folder."""
        for app in apps:
            if app["app_id"] not in human:
                continue
            state = deepcopy(human[app["app_id"]])
            if app["app_id"] in results:
                specs = [BulkSpec.model_validate(s) for s in results[app["app_id"]]["specs"]]
                added, ids, short = lay_bulk(
                    state, specs, generation_seed, max_bytes_for(config, app["app_id"])
                )
                validate_state(
                    {"id": app["app_id"], "state_keys": app.get("top_level_keys") or []},
                    state,
                    (root / app["schema"]).read_text(),
                )
                # The repaired human layer is a different size, so what fits changes with it:
                # the shortfall is re-recorded rather than kept from the first laying.
                results[app["app_id"]].update(
                    {"added": added, "short": short, "bytes": len(json.dumps(state)), "ids": sorted(ids)}
                )
                bulk_ids[app["app_id"]] = ids
            write(folder / "world" / f"{app['app_id']}.state.json", state)
        return check_folder(root, folder, bulk_ids=bulk_ids)

    # An error the check reports against an app goes back to that app's author the way
    # seeding's mechanical repair does, if the human layer shows it on its own: the author
    # rewrites the human layer and the bulk is laid again over the repaired state. A fault
    # that appears only with the bulk records (a template's one-line bodies) is the spec's,
    # not the author's; no author round is spent on it and the check stage reports it.
    repair_rounds, repair_receipts, repair_error = 0, {}, None
    if not checks["ok"]:
        human_checks = human_check()
        if mechanical_feedback(human_checks, human):
            try:
                author, contract = folder_author(root, folder, models, skill_text, config, repair_receipts)
                _, repair_rounds = repair_mechanical(
                    contract, human, human_checks, human_check, author, MECHANICAL_REPAIR_ROUNDS
                )
                checks = relay()
            except Exception as exc:  # noqa: BLE001 -- the layer is laid; the repair is the option
                # Nothing was written: the layered states on disk are the ones ``checks`` judged.
                # The catch is deliberately everything. It was ModelOutputInvalid/ValueError/OSError,
                # so a provider fault here escaped after every state had been written and before the
                # marker was: one run left a re-laid drive on disk under a BULK.json that named the
                # layer before it. A world whose marker does not name its own layer is the thing
                # BULK_VERSION exists to prevent, and losing the marker is a worse outcome than
                # skipping an optional repair, so the type is recorded and the marker is written.
                print(
                    f"warning: mechanical repair after bulk failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                repair_error = f"{type(exc).__name__}: {str(exc)[:280]}"
    write(folder / "world" / "CHECKS.json", checks)
    # The world's outcome in the shared vocabulary, rolled up from the per-app entries and whatever
    # the optional mechanical repair did. ``faults`` below is what makes the marker stale and stays
    # where it is; this says the same thing in the one word every other stage's marker now uses, so
    # the driver does not need a bulk-specific reader to learn that a run measured nothing.
    #
    # The optional mechanical repair is deliberately *not* folded in. It failing is a fault, but the
    # layer it was offered for is laid and validated, and ``faulted`` here would make the marker
    # stale and buy the whole world's bulk again -- hours of model calls for a repair the stage is
    # allowed to skip. It is recorded as ``mechanical_repair_error`` below, where the check stage
    # reads it. "Never block on a defect the pipeline cannot repair" decides this, not the roll-up.
    whole = rollup(list(results.values()), "apps")
    write(
        marker,
        {
            "added_at": now(),
            "outcome": whole.outcome,
            **({"reason": whole.reason} if whole.reason else {}),
            "bulk_version": BULK_VERSION,
            "skill": skill,
            "apps": results,
            # One line for the whole world: what the specs asked for, what reached disk, and the
            # apps that fell short. Silent truncation is how two companies declared thousands of
            # records and shipped none of them without anything noticing.
            "volume": _volume(results),
            # The default this layer was laid under, so a reader -- or the driver's staleness
            # question -- can tell a world that declined volume from one that was refused it by a
            # number that has since changed. An app with its own entry in bulk_max_bytes_by_app was
            # laid under a different cap; each app entry above carries the one it actually got.
            "max_bytes": default_max_bytes,
            # How many identifiers of a previous layer came off each app before this one went on.
            # An app here whose entry the marker did not hold was carrying a layer nothing claimed.
            **({"stripped": stripped} if stripped else {}),
            # Apps whose run said nothing about the world. Their states were not touched and their
            # entries are the ones the previous marker held; the key is what makes this marker read
            # as stale, so the step runs again rather than a fault passing for a finished layer.
            **({"faults": faults} if faults else {}),
            "mechanical": {"errors": checks["errors"], "warnings": checks["warnings"]},
            "mechanical_repair_rounds": repair_rounds,
            **({"mechanical_repair_receipts": repair_receipts} if repair_receipts else {}),
            **({"mechanical_repair_error": repair_error} if repair_error else {}),
        },
    )
    manifest = read(folder / "MANIFEST.json")
    manifest["hashes"] = {
        str(p.relative_to(folder)): digest(p.read_bytes()) for p in sorted(folder.rglob("*")) if p.is_file()
    }
    write(folder / "MANIFEST.json", manifest)
    return {
        **whole.body,
        "apps": len(results),
        "added": {a: r.get("added") or {} for a, r in results.items()},
        "mechanical": checks["errors"],
        "mechanical_repair_rounds": repair_rounds,
        **({"faults": faults} if faults else {}),
    }
