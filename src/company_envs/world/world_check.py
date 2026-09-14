"""Mechanical coherence checks across a company's canonical world, app states, identities and materials.

These catch what a reader notices first: duplicated records, a worker whose name or role differs
between apps, a fact (price, status, date, name) that differs between the canonical world and an
app or between two apps, and timestamps after the reference date. They do not judge realism; the
fresh-session review does that. Findings are data for a bounded repair, not a verdict.
"""

import json
import re
from collections import Counter, defaultdict, deque
from datetime import UTC, date, datetime, time
from decimal import Decimal
from functools import cache
from pathlib import Path

TIMESTAMP_KEYS = re.compile(
    r"(?:^at$|^timestamp$|(?:created|updated|posted|sent|received|last_?login|completed|closed|opened|snapshot)(?:_?at|_?date|_?time)?)$",
    re.IGNORECASE,
)
FUTURE_OK_KEYS = re.compile(
    r"(due|expir|next_|deadline|end|until|valid|scheduled|start|renew)", re.IGNORECASE
)
ID_LIKE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
# Collections that are a people directory by name. "workers" is not one: the only app that has a
# ``workers`` collection is Cloudflare, whose Workers are scripts (morning-brew failed on it).
USER_COLLECTIONS = {"users", "members", "people", "staff", "employees", "agents"}
DIRECTORY_ALIASES = {"userdirectory", "teammembers", "userlist", "userprofiles", "directory"}


def normalize(value):
    if isinstance(value, str):
        text = value.strip().casefold()
        try:
            return float(text.replace("$", "").replace(",", ""))
        except ValueError:
            return re.sub(r"\s+", " ", text)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    return value


def records(node, path="", out=None):
    """Every dict carrying a literal ``id`` anywhere in a JSON tree, with its path.

    A *literal* ``id`` only: an app that names its key after the record (slack's ``messageId``,
    ``channelId``) has no record here at all. Callers that need a record's identifier whatever
    the app calls it want ``primary_ids``, and callers that need every id in a state -- the
    reference check's universe -- want ``state_ids``. Reading this function as "every record"
    is what blinded the reference check to the cohort's most-used app.
    """
    out = [] if out is None else out
    if isinstance(node, dict):
        if "id" in node and isinstance(node["id"], (str, int)):
            out.append((path, node))
        for key, value in node.items():
            records(value, f"{path}/{key}", out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            records(value, f"{path}/{index}", out)
    return out


def check_duplicates(label, tree):
    findings = []
    for path, node in walk_lists(tree):
        ids = Counter(str(item["id"]) for item in node if isinstance(item, dict) and "id" in item)
        for value, count in ids.items():
            if count > 1:
                findings.append(finding("error", label, path, f"id {value!r} appears {count} times"))
        for key in ("email", "subject", "number"):
            values = Counter(
                normalize(item[key])
                for item in node
                if isinstance(item, dict) and isinstance(item.get(key), str) and item[key].strip()
            )
            for value, count in values.items():
                if count > 1:
                    findings.append(
                        finding("warning", label, path, f"{key} {value!r} repeated {count} times")
                    )
    return findings


def walk_lists(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk_lists(value, f"{path}/{key}")
    elif isinstance(node, list):
        if any(isinstance(item, dict) for item in node):
            yield path, node
        for index, value in enumerate(node):
            yield from walk_lists(value, f"{path}/{index}")


def finding(severity, source, path, message, records=()):
    """One finding. ``records`` is the population it is about, not the sample it cites.

    A counted finding names three examples and a number -- "3,350 of 4,960 threads name a root
    message that does not exist" -- and the repair contract narrows to the records a finding
    cites, so the sample was the whole of what reached the author: one round put 9.3% of the
    condemned records in front of it and reported success on the rest. Declaring the population
    alongside the citations takes that to 65.0%, with 56 of 66 reachability populations covered
    whole. A checker that sets nothing here behaves exactly as it did.
    """
    out = {"severity": severity, "source": source, "path": path, "message": message}
    return {**out, "records": sorted(records)} if records else out


def check_identities(identities, company_workers):
    """The same worker must be the same person in every app and match the dossier."""
    findings = []
    titles = {w["id"]: w.get("title", "") for w in company_workers}
    for worker, apps in identities.items():
        names = {normalize(rec.get("name")) for rec in apps.values() if rec.get("name")}
        emails = {normalize(rec.get("email")) for rec in apps.values() if rec.get("email")}
        if len(names) > 1:
            findings.append(
                finding(
                    "error",
                    "identities",
                    f"/{worker}",
                    f"worker has different names across apps: {sorted(names)}",
                )
            )
        if len(emails) > 1:
            findings.append(
                finding(
                    "error",
                    "identities",
                    f"/{worker}",
                    f"worker has different emails across apps: {sorted(emails)}",
                )
            )
        title = normalize(titles.get(worker, ""))
        for app_id, rec in apps.items():
            blob = normalize(
                " ".join(
                    str(rec.get(k, "")) for k in ("title", "role", "signature", "department", "position")
                )
            )
            if title and isinstance(blob, str) and title not in blob and rec.get("title"):
                findings.append(
                    finding(
                        "warning",
                        "identities",
                        f"/{worker}/{app_id}",
                        f"title {rec.get('title')!r} differs from dossier {titles[worker]!r}",
                    )
                )
    return findings


def same_identity(record, candidate):
    """Recognize enriched/subset copies without mistaking a ticket ID for a user ID."""
    if not isinstance(candidate, dict):
        return False
    key = "id" if record.get("id") is not None else "email"
    return (
        record.get(key) is not None
        and str(record[key]) == str(candidate.get(key))
        and any(
            record.get(field) is not None and normalize(record[field]) == normalize(candidate.get(field))
            for field in ("name", "email", "username")
        )
    )


def identity_members(state, record):
    """Locate user collection members, excluding the singleton login view.

    Prefer named user collections; custom collection names require a recognizable
    person record, so an unrelated business record cannot satisfy membership by ID.
    """
    collections = {k: v for k, v in state.items() if k in USER_COLLECTIONS}
    marker = "id" if record.get("id") is not None else "email"
    hits = []
    for key, value in (collections or state).items():
        items = (
            value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else []
        )
        for index, item in items:
            if (
                isinstance(item, dict)
                and record.get(marker) is not None
                and str(item.get(marker)) == str(record[marker])
                and (collections or same_identity(record, item))
            ):
                hits.append((f"/{key}/{index}", item))
    return hits


def has_user_collection(state, identities=()):
    """Whether the app keeps a people directory at all (single-account apps do not).

    A directory is a collection named for people (``users``, ``members``, ``teamMembers``) or,
    under any other name, a collection that holds one of the workers' own login records. A
    table of customers, contacts or marketing profiles is people too, with ids and emails, but
    no worker is in it; it proves nothing about a directory (lkq-corporation's Klaviyo
    ``profiles`` were read as one, and every worker was then "missing" from it).
    """
    for key, value in state.items():
        if not isinstance(value, (list, dict)):
            continue
        if key in USER_COLLECTIONS or re.sub(r"[_\s-]", "", key).casefold() in DIRECTORY_ALIASES:
            return True
    return any(identity_members(state, record) for record in identities if isinstance(record, dict))


def people_directory(state):
    """The key of the app's people directory, or None when it keeps no directory."""
    for key, value in state.items():
        if not isinstance(value, (list, dict)):
            continue
        if key in USER_COLLECTIONS or re.sub(r"[_\s-]", "", key).casefold() in DIRECTORY_ALIASES:
            return key
    return None


def splice_identities(state, identities):
    """Put each worker's own login record into the app's directory where it is missing.

    A worker absent from the directory is not a defect an author has to rewrite: the record is known
    in full from identities.json, and the runtime proxy overwrites the signed-in identity anyway.
    Raising instead threw away every other app already authored in the same seed -- around ten
    companies are terminal on it with nothing on disk for thousands of model calls, and six seeded
    worlds cannot be served for the same reason. Returns the records added.

    Conservative by design: a directory keyed by something other than ``id`` is left alone, so the
    finding stands rather than being papered over with a key the app cannot read.
    """
    key = people_directory(state)
    if key is None:
        return 0
    added = 0
    for record in identities.values():
        if not isinstance(record, dict) or identity_members(state, record):
            continue
        # Only a record that names a person is a directory entry. An identity carrying nothing but
        # an id is a malformed identity from the core, and splicing it in would put a nameless user
        # in the app's directory -- that finding has to stand.
        if not any(record.get(field) for field in ("name", "email", "username", "fullName")):
            continue
        directory = state[key]
        if isinstance(directory, list):
            directory.append(dict(record))
            added += 1
        elif isinstance(directory, dict) and record.get("id") is not None:
            directory[str(record["id"])] = dict(record)
            added += 1
    return added


def check_state_identities(app_id, state, identities):
    """Check membership and agreement of person fields against the native users.

    Single-account apps (Gmail, Calendar, Drive mocks: one ``user`` object, no directory)
    cannot list every worker; there the proxy injects each worker's identity, so membership
    is not required and only field agreement with a singleton login view is checked.
    """
    findings = []
    single_account = not has_user_collection(state, identities.values())
    for worker, record in identities.items():
        hits = identity_members(state, record)
        if not hits and single_account:
            continue
        if not hits:
            findings.append(
                finding(
                    "error",
                    app_id,
                    f"/identities/{worker}",
                    f"{worker}'s identity user record is not in any state collection",
                )
            )
        for path, item in hits:
            for field in ("name", "email", "username"):
                # Compared only where both sides have the field. zoom_web's contacts carry no
                # username at all, so comparing against a missing one reported a mismatch no model
                # could ever satisfy, and retired the company after three identical deaths.
                if (
                    field in record
                    and field in item
                    and normalize(record[field]) != normalize(item.get(field))
                ):
                    findings.append(
                        finding(
                            "error",
                            app_id,
                            path,
                            f"{worker}'s identity {field} {record[field]!r} differs from user record {item.get(field)!r}",
                        )
                    )
    return findings


# Who a record says did the work. Exactly the fields the audit measured: every further field is
# a new class of false positive, and these six carry the finding the reviewers raise by hand.
ACTOR_FIELDS = ("reporterId", "assigneeId", "ownerId", "userId", "createdBy", "senderId")
# The signed-in singleton. The runtime proxy overwrites it per worker, so whoever it names in the
# seeded file is not an attribution.
LOGIN_VIEWS = frozenset({"currentUser", "current_user", "user", "account", "me", "profile", "viewer"})


def worker_keys(identities):
    """Per worker, every normalized name, email and handle any app knows them by."""
    keys = {}
    for worker, apps in (identities or {}).items():
        found = set()
        for record in (apps or {}).values():
            if not isinstance(record, dict):
                continue
            for field in ("email", "name", "fullName", "displayName", "username"):
                value = record.get(field)
                if isinstance(value, str) and value.strip():
                    found.add(normalize(value))
        keys[worker] = found
    return keys


def check_actor_logins(app_id, state, identities, worker_apps, severity="warning"):
    """A record may only name a worker who can open the app it lives in.

    ``worker_apps.json`` is the grant: the VM forwards only the apps it lists for that worker
    (``hub_vm._plan``), so a jira issue assigned to someone without jira is work its owner can
    never see, and a task keyed on it cannot be done. Nothing checked this, and it is the
    commonest blocking finding the model reviewers raise by hand -- each instance costing a repair
    round. Measured over the seeded worlds: 2,054 records across 18 company/app pairs, among them
    218 jira records at greenbrier-companies (including the task's own manager), 216 salesforce
    records at federal-home-loan-bank-des-moines and 153 jira records at seattle-police-department,
    several in the app their company's own task keys on.

    A worker's identity record exists in every app with an ``identity_key`` whether they hold it or
    not (``validate_core`` requires one), so presence in the directory proves nothing and is not
    judged: the *attribution* is. ``severity`` is error while authoring -- the seed prompt already
    tells the author ``workers_using_this_app``, and the repair is to attribute the record to a
    holder -- and a warning afterwards, because ``sync_worker_apps`` rewrites the grant from the
    task contract after a world is seeded, and 206 of 287 workers in the cohort already hold
    identities for apps the contract does not grant. Turning that into an error on a world whose
    repair budget is spent is a revision nobody can make.
    """
    if not worker_apps or not isinstance(state, dict):
        return []
    holders = {w for w, apps in worker_apps.items() if app_id in (apps or ())}
    outsiders = {w for w in worker_apps if w not in holders}
    if not outsiders:
        return []
    keys = worker_keys(identities)
    by_actor = {}
    for worker in outsiders:
        record = (identities.get(worker) or {}).get(app_id)
        if isinstance(record, dict):
            for value in primary_ids("users", record):
                by_actor[value] = worker
    directory = people_directory(state)
    holder_keys = set().union(*(keys.get(holder, set()) for holder in holders), set())
    for record in records_of(state.get(directory) or {}) if directory else []:
        found = {
            normalize(record[f])
            for f in ("email", "name", "fullName", "displayName", "username")
            if isinstance(record.get(f), str) and record[f].strip()
        }
        owner = next((w for w in sorted(outsiders) if found & keys.get(w, set())), None)
        # A directory entry matching a holder too is not evidence about anyone; leave it alone.
        if owner and not found & holder_keys:
            for value in primary_ids(directory, record):
                by_actor.setdefault(value, owner)
    if not by_actor:
        return []
    counts, examples = Counter(), {}

    def visit(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ACTOR_FIELDS and isinstance(value, (str, int)) and str(value) in by_actor:
                    worker = by_actor[str(value)]
                    counts[worker] += 1
                    examples.setdefault(worker, f"{path}/{key}")
                visit(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{path}/{index}")

    for key, value in state.items():
        # Not the login view, and not the directory: ``users[].userId`` is that person's own id,
        # and being listed among an app's people is not a claim that they work in it.
        if key not in LOGIN_VIEWS and key != directory:
            visit(value, f"/{key}")
    if not counts:
        return []
    named = ", ".join(f"{worker} ({counts[worker]})" for worker, _ in counts.most_common(MAX_EXAMPLES))
    return [
        finding(
            severity,
            app_id,
            examples[counts.most_common(1)[0][0]],
            f"{sum(counts.values())} records name {named} in {'/'.join(ACTOR_FIELDS)}, and "
            f"worker_apps.json does not give {'them' if len(counts) > 1 else 'that worker'} "
            f"{app_id}: they can never open it. Attribute the work to "
            + (f"one of {', '.join(sorted(holders))}" if holders else "a worker who holds this app"),
        )
    ]


def _record_kind(path, record):
    # Generic projection tables have no entity type of their own. Canonical
    # entities can supply a kind; otherwise retain legacy coded-ID matching.
    parts = path.split("/")[1:-1]
    while parts and parts[-1].isdecimal():
        parts.pop()
    collection = parts[-1] if parts else path.rsplit("/", 1)[-1]
    if collection in {"entities", "records", "rows"}:
        collection = record.get("kind", "")
    collection = re.sub(r"[_\s-]", "", str(collection)).casefold()
    if collection in {"currentuser", "user", "users", "people", "members"}:
        return "user"
    return collection.removesuffix("s")


def check_facts(world, states):
    """Same-kind records sharing an ID must agree on shared scalar attributes.

    Untyped generic projections retain coded-ID matching. Bare numeric IDs need
    collection/kind evidence; they are commonly local counters in unrelated tables.

    How a person is *presented* is the app's business, not a fact about the world, so
    ``PRESENTATION_FIELDS`` are skipped: the same name is a different thing in each app and
    the error was unsatisfiable either way. ``avatar`` was all 25 errors blocking
    sungrow-power-supply, and ``username`` the single error blocking
    schweitzer-engineering-laboratories, where github's ``hainsworth`` is a correct handle and
    the calendar's is a display name -- the repair "fixed" it into the opposite violation and
    aborted, spending that company's one repair round on a pair nothing could satisfy.
    """
    findings = []
    by_id = defaultdict(list)
    for path, rec in records(world, "world"):
        by_id[str(rec["id"])].append(("world", path, rec))
    for app_id, state in states.items():
        for path, rec in records(state, app_id):
            by_id[str(rec["id"])].append((app_id, path, rec))
    record_paths = {path for hits in by_id.values() for _src, path, _rec in hits}

    def parent_record(path):
        # The nearest enclosing record, if any: a child record's id is scoped to its parent
        # (space K1 in two different registers is two different spaces).
        parts = path.split("/")
        for cut in range(len(parts) - 1, 0, -1):
            prefix = "/".join(parts[:cut])
            if prefix in record_paths:
                return prefix
        return None

    for rid, hits in by_id.items():
        if len(hits) < 2:
            continue
        for i in range(len(hits)):
            for j in range(i + 1, len(hits)):
                (src_a, path_a, a), (src_b, path_b, b) = hits[i], hits[j]
                if src_a == src_b and src_a != "world":
                    continue
                parent_a, parent_b = parent_record(path_a), parent_record(path_b)
                if parent_a and parent_b and parent_a != parent_b:
                    continue
                kind_a, kind_b = _record_kind(path_a, a), _record_kind(path_b, b)
                if kind_a != kind_b and (kind_a and kind_b or rid.isdecimal()):
                    continue
                for key in a.keys() & b.keys():
                    va, vb = a[key], b[key]
                    if (
                        key == "id"
                        or isinstance(va, (dict, list))
                        or isinstance(vb, (dict, list))
                        or va is None
                        or vb is None
                        # Prose is written once per place; an app's copy of a document body or a
                        # note may be worded differently from the canonical world's. Facts are
                        # the scalars: amounts, dates, statuses, owners, names.
                        or key in PROSE_FIELDS
                        # How each app shows a person is per-app, not a fact about the world.
                        or key in PRESENTATION_FIELDS
                        or (isinstance(va, str) and isinstance(vb, str) and max(len(va), len(vb)) > 160)
                    ):
                        continue
                    comparable_a, comparable_b = normalize(va), normalize(vb)
                    if (
                        key == "status"
                        and kind_a == kind_b == "issue"
                        and {src_a, src_b} == {"github_mock", "jira_mock"}
                    ):
                        # GitHub tracks open/closed; Jira separates unfinished work
                        # into three stages. A completion disagreement still blocks.
                        jira_status = {
                            "to do": "open",
                            "in progress": "open",
                            "in review": "open",
                            "done": "closed",
                        }
                        if src_a == "jira_mock":
                            comparable_a = jira_status.get(comparable_a, comparable_a)
                        else:
                            comparable_b = jira_status.get(comparable_b, comparable_b)
                    elif _lifecycle_word(va) != _lifecycle_word(vb) and not (
                        _status_like(va) and _status_like(vb)
                    ):
                        # The same field name means different things: an app's account
                        # ``state`` (enabled/disabled) is not the customer's US state (NJ).
                        continue
                    if comparable_a != comparable_b:
                        findings.append(
                            finding(
                                "error",
                                f"{src_a}~{src_b}",
                                f"{path_a} vs {path_b}",
                                f"{rid}.{key}: {va!r} vs {vb!r}",
                            )
                        )
    return findings


def check_timestamps(label, tree, reference_date):
    """Compare ISO instants; date-only references include the entire UTC day.

    Naive timestamps use UTC. A reference containing a time is an exact cutoff.
    Non-ISO strings are outside this mechanical check.
    """
    findings = []
    ref = reference_date
    try:
        cutoff = datetime.combine(date.fromisoformat(ref), time.max, UTC)
    except ValueError:
        cutoff = datetime.fromisoformat(ref)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)

    def after(value):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed > cutoff

    def visit(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    isinstance(value, str)
                    and TIMESTAMP_KEYS.search(key)
                    and not FUTURE_OK_KEYS.search(key)
                    and after(value)
                ):
                    findings.append(
                        finding("error", label, f"{path}/{key}", f"{value} is after the reference date {ref}")
                    )
                visit(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{path}/{index}")

    visit(tree, "")
    return findings


PATTERN_MISSES = 5  # below this a field is not a convention, it is a handful of broken references
MAX_REPORTS = 6
MAX_EXAMPLES = 3  # a counted finding carries a few citations so a repair can be narrowed
PRIMARY_KEYS = ("id", "sys_id", "uuid", "_id")
# A field that says where a record hangs: without it the record is not reachable in the app at
# all, which is a different fault from a stale pointer between two visible records.
STRUCTURAL_KEY = re.compile(r"parent|root", re.IGNORECASE)
# Lists of bare ids that do not end in ``Ids``. Each is a membership list the app reads.
ID_LIST_FIELDS = frozenset({"members", "participants", "followers", "assignees", "watchers", "readBy"})
ID_LIST_KEY = re.compile(r"(?:Ids|IDs|_ids)$")


def primary_ids(collection, record):
    """The identifiers a record uses for *itself*, as strings.

    ``id`` and ``sys_id``, plus the key named after the record's own collection: slack keys
    messages on ``messageId`` and channels on ``channelId``, Salesforce on ``accountId`` and
    ``caseId``. Fields naming *other* records are deliberately left out -- putting a
    ``parentMessageId`` into the universe would make every reference resolve to itself.
    """
    singular = str(collection).removesuffix("s")
    out = set()
    for key in (*PRIMARY_KEYS, f"{singular}Id", f"{singular}_id", f"{singular}ID"):
        value = record.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            out.add(str(value))
    return out


def state_ids(state):
    """Every id a state defines: each record's own primary key, plus map-collection keys.

    The universe the reference check compares against. A collection shaped ``{id: record}`` or
    ``{groupId: [record...]}`` defines its keys as ids too -- slack's messages are stored by
    channel and Zendesk's comments by ticket -- and the records inside keep their own collection's
    name, not the key they sit under.
    """
    ids = set()

    def visit(node, collection):
        if isinstance(node, dict):
            values = list(node.values())
            if values and (
                all(isinstance(v, dict) for v in values) or all(isinstance(v, list) for v in values)
            ):
                ids.update(str(key) for key in node)
                for value in values:  # the map's keys are ids; its records belong to ``collection``
                    visit(value, collection)
                return
            ids.update(primary_ids(collection, node))
            for key, value in node.items():
                visit(value, key if isinstance(value, (list, dict)) else collection)
        elif isinstance(node, list):
            for value in node:
                visit(value, collection)

    # The state itself is never a map of records: its keys are collection names, not ids.
    for key, value in (state if isinstance(state, dict) else {}).items():
        visit(value, key)
    return ids


def check_references(label, state, *, severity="warning"):
    """``*_id`` values should point at a record that exists somewhere in the same state.

    The id universe was only records carrying a literal ``id``, and every other id-shaped field
    was compared against it. That made this check the largest output of the whole mechanical pass
    and almost none of it real: 170,408 of 185,716 warnings across 60 worlds, of which about 396
    were genuinely dangling. ``threadId`` alone accounted for 133,605, because Gmail groups by a
    thread key and has no threads collection, exactly as the real API does. Salesforce names its
    keys ``accountId`` and ``caseId``, so every record was reported as not resolving to itself.

    The noise had a real cost: the reviewer's packet is filled from these findings in list order,
    so in 18 worlds a genuine error was pushed out entirely, and 8 worlds showed their reviewer
    none of their cross-app fact conflicts.

    So the universe is ``state_ids``: each record's own primary key whatever it is called, and the
    keys of a map-shaped collection. A grouping key with no collection behind it is how the product
    works, not a fault, and stays one suppressed finding.

    Two things the old universe and the old walk could not see, each measured over the 60 seeded
    worlds:

    * slack keys messages on ``messageId``, so not one message entered the universe, every
      ``parentMessageId`` missed, and the suppression branch above collapsed the whole class into
      one benign warning. 3,350 of 4,960 threads name a root message that does not exist, hiding
      7,354 replies, and none of it reached a single CHECKS.json. In a live guest 61 of 85 threads
      render as a row with no sender and no body: ``ThreadPanel.jsx`` returns null with no parent.
    * a field holding a *list* of bare ids (``cardIds``, ``members``, ``followers``) was never
      resolved at all, which is the other half of how 20,155 of 78,462 unreachable drive items
      and 6,240 of 6,794 containerless trello cards stayed invisible.

    And the report is now a count per field, not six rows: at ``MAX_REPORTS`` rows a field that
    dangles twenty thousand times looked the same as one that dangles six, so the largest
    structural fault in the fleet could never weigh more than a typo.

    ``severity`` is the caller's: a structural miss (a child naming a parent that does not exist)
    is an error while the author is still in the repair loop and a warning once the world is on
    disk, because a rule applied retroactively to a spent repair budget is a world nobody can
    revise.
    """
    findings = []
    ids = state_ids(state)
    if not ids:
        return findings
    seen, unresolved = defaultdict(Counter), {}

    def note(key, value, where):
        if not (isinstance(value, int) and not isinstance(value, bool)) and not ID_LIKE.match(str(value)):
            return
        seen[key][str(value) in ids] += 1
        if str(value) not in ids:
            unresolved.setdefault(key, []).append((where, value))

    def visit(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, (str, int)) and re.search(r"(_id|Id)$", key) and key != "id":
                    note(key, value, f"{path}/{key}")
                elif isinstance(value, list) and (ID_LIST_KEY.search(key) or key in ID_LIST_FIELDS):
                    # A list of bare ids is a reference each: a thread's replies, a list's cards,
                    # a channel's members. Anything that is not id-shaped is left to ID_LIKE.
                    for index, item in enumerate(value):
                        if isinstance(item, (str, int)):
                            note(key, item, f"{path}/{key}/{index}")
                visit(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{path}/{index}")

    visit(state, "")
    for key, counts in seen.items():
        misses = counts[False]
        if not misses:
            continue
        rows = unresolved[key]
        structural = bool(STRUCTURAL_KEY.search(key))
        # A parent key names records in this app by definition, so "not one resolves" is the worst
        # case and never a convention: 3,010 drive items in four worlds hang under invented folders
        # with no correct parent anywhere to prove the field means what it says.
        if counts[True] or misses < PATTERN_MISSES or structural:
            # Some values resolve, or there are too few to call it a convention: each one is a
            # reference that should have landed somewhere. One finding carrying the count, the
            # number of distinct targets that do not exist, and citations a repair can be narrowed
            # to: 128 invented folder ids is a different brief from 20,155 separate mistakes.
            targets = {str(value) for _where, value in rows}
            examples = ", ".join(f"{value!r} at {where}" for where, value in rows[:MAX_EXAMPLES])
            findings.append(
                finding(
                    severity if structural else "warning",
                    label,
                    rows[0][0],
                    f"{misses} of {counts[True] + misses} {key} values name no record in this app "
                    f"({len(targets)} missing target{'s' if len(targets) != 1 else ''}, "
                    f"e.g. {examples})"
                    + (
                        "; a record whose parent does not exist is unreachable in the app -- write "
                        "the parent or point the child at one that exists"
                        if structural
                        else "; each one should land on a record"
                    ),
                    # The population is the ids that resolve to nothing, which is what a repair has
                    # to write or re-point; the paths that carry them are in the citations.
                    records={str(value) for _where, value in rows},
                )
            )
        else:
            # Not one value of this field resolves, across many records: it groups records or
            # names something outside this app, the way an email's threadId does. Gmail has no
            # threads collection and neither does the real API. One finding, not 133,605.
            where, value = rows[0]
            findings.append(
                finding(
                    "warning",
                    label,
                    where,
                    f"no {key} resolves to a record here ({misses} values, e.g. {value!r}); "
                    "a grouping or external key unless this app is meant to hold them",
                )
            )
    return findings


def indexed_collections(state):
    """Every top-level collection as ``name -> ([(ids, record)...], {id: record})``.

    Ids are whatever the app keys on (``primary_ids``) plus the key of a map-shaped collection.
    """
    out = {}
    for name, value in (state if isinstance(state, dict) else {}).items():
        rows = []
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, dict):
                    rows.append((primary_ids(name, item) | {str(key)}, item))
                elif isinstance(item, list):
                    rows += [(primary_ids(name, r), r) for r in item if isinstance(r, dict)]
        elif isinstance(value, list):
            rows = [(primary_ids(name, r), r) for r in value if isinstance(r, dict)]
        rows = [(ids, record) for ids, record in rows if ids]
        if rows:
            out[name] = (rows, {i: record for ids, record in rows for i in ids})
    return out


GENERIC_KEYS = frozenset({"id", "_id", "key", "number", "uuid", "sys_id"})


def check_containment(label, state, severity="warning"):
    """A record its container does not hold is a record the app never draws.

    Two one-sided links, both measured over the seeded worlds and both repairable by writing the
    other side:

    * the child names its parent and the parent's own id list omits it. Trello renders a board
      from ``list.cardIds`` (``List.jsx:59``), so 145 of 168 cards at blastx-consulting and 152 of
      218 at median-technologies name a list that does not name them back, and monday the same for
      282 of 362 items at eastman-chemical-company. Only a two-sided link is judged: a contact in
      no ``contactIds`` list carries no ``listId`` either and is not reported.
    * records of a collection written inline in another record instead of in the collection the app
      reads. Slack's thread replies are stored as objects inside ``threads[].replies`` while the
      app renders ``state.messages`` filtered by ``threadId`` (``ThreadPanel.jsx:26``): 9,963
      declared replies fleet-wide, 11 of which exist as messages. Judged only where the inline
      records carry an app-named key (``messageId``) that is some collection's own primary key, so
      an ordinary inline list -- a card's checklists, a ticket's comments -- is not touched.

    ``severity`` is the caller's, for the reason ``check_references`` gives.
    """
    findings = []
    collections = indexed_collections(state)
    primary = {}
    for name, (rows, _index) in collections.items():
        key = record_key([record for _ids, record in rows])
        # A notification's messageId is a reference, not its own primary key.
        singular = name.removesuffix("s")
        if key in (f"{singular}Id", f"{singular}_id", f"{singular}ID"):
            primary.setdefault(key, name)
    if label == "slack_mock" and "messages" in collections:
        messages = collections["messages"][1]
        hidden = [
            str(thread["parentMessageId"])
            for _ids, thread in collections.get("threads", ([], {}))[0]
            if messages.get(str(thread.get("parentMessageId")), {}).get("threadId")
        ]
        if hidden:
            findings.append(
                finding(
                    severity,
                    label,
                    "/messages",
                    f"{len(hidden)} thread parents have a threadId; Slack filters these out of "
                    "the channel and treats them as replies. Parent messages need threadId=null.",
                    records=hidden,
                )
            )
    for parent_name, (parent_rows, parents) in collections.items():
        singular = parent_name.removesuffix("s")
        back_keys = (f"{singular}Id", f"{singular}_id", f"{singular}ID")
        fields = sorted(
            {
                key
                for _ids, record in parent_rows
                for key, value in record.items()
                if isinstance(value, list) and ID_LIST_KEY.search(key)
            }
        )
        for field in fields:
            base = ID_LIST_KEY.sub("", field)
            child_name = next(
                (c for c in (f"{base}s", base, f"{base}es") if c in collections and c != parent_name), None
            )
            if child_name is None:
                continue
            held, missing = 0, []
            for ids, child in collections[child_name][0]:
                parent_id = next(
                    (str(child[k]) for k in back_keys if isinstance(child.get(k), (str, int))), None
                )
                if parent_id is None or parent_id not in parents:
                    continue  # a dangling parent reference is check_references' finding, not this
                held += 1
                if not ids & {str(v) for v in (parents[parent_id].get(field) or [])}:
                    missing.append(min(ids))
            if missing and held:
                findings.append(
                    finding(
                        severity,
                        label,
                        f"/{child_name}",
                        f"{len(missing)} of {held} {child_name} name a {singular} whose {field} does "
                        f"not name them back (e.g. {', '.join(missing[:MAX_EXAMPLES])}); the app draws "
                        f"the {singular} from {field}, so those records are on no screen",
                        records=missing,
                    )
                )
    for holder_name, (rows, _index) in collections.items():
        inline = defaultdict(list)
        for _ids, record in rows:
            for key, value in record.items():
                if not isinstance(value, list) or not value:
                    continue
                element_key = record_key(value)
                if element_key and element_key not in GENERIC_KEYS and element_key in primary:
                    inline[(key, element_key)] += [str(r[element_key]) for r in value]
        for (field, element_key), values in sorted(inline.items()):
            target = primary[element_key]
            if target == holder_name:
                continue
            index = collections[target][1]
            absent = [v for v in values if v not in index]
            if absent:
                findings.append(
                    finding(
                        severity,
                        label,
                        f"/{holder_name}",
                        f"{len(absent)} of {len(values)} records in {holder_name}.{field} are not in "
                        f"{target} (e.g. {', '.join(absent[:MAX_EXAMPLES])}); {element_key} is how the "
                        f"app keys {target} and that collection is what it renders, so write them there",
                        records=absent,
                    )
                )
    return findings


TOTAL_OF = {
    "storageused": "size",
    "usedstorage": "size",
    "storageusedbytes": "size",
    "usedbytes": "size",
    "totalsize": "size",
}
TOTAL_OVER = 10  # a quota may cover files this state does not hold; 2,166x is not the same drive


def check_totals(label, state, severity="warning"):
    """A stored total must be the sum of the records it totals.

    Measured on the 60 seeded drives: 29 declare a ``storageUsed`` *below* the sum of their own
    files' sizes, which cannot happen (one declares 0 against 3.0 MB of files), and 10 declare
    more than ten times it, up to 2,166x -- 184 MB of quota against 85 KB of files. A real quota
    can include what this state does not hold (another product's files, the trash), so only a
    total under the sum or more than ``TOTAL_OVER`` times it is reported, and the repair is
    arithmetic: set the total to the sum, or write the files the total claims.
    """
    findings = []
    if not isinstance(state, dict):
        return findings
    for key, value in sorted(state.items()):
        part = TOTAL_OF.get(_field_name(key))
        declared = _number(value) if part else None
        if declared is None:
            continue
        parts = [
            amount
            for _path, record in _walk_dicts(state)
            for amount in [_number(record.get(part))]
            if amount is not None
        ]
        derived = sum(parts, Decimal(0))
        if not parts or derived <= 0:
            continue
        if declared < derived or declared > TOTAL_OVER * derived:
            findings.append(
                finding(
                    severity,
                    label,
                    f"/{key}",
                    f"{key} is {declared:,.0f} but the {len(parts)} records that carry {part} total "
                    f"{derived:,.0f}; a stored total is the sum of its parts",
                )
            )
    return findings


# A record the app cannot walk to is not in the app, however well formed it is. The state itself
# says where its records hang -- a parent pointer, a container's id list, a map keyed by the
# container's id -- so the walk is derived from the data rather than from a list of apps. Measured
# over the 60 seeded worlds: 27,658 records of 123,384 in a collection the state gives a container
# convention cannot be reached, and three of them were served and looked at. blastx-consulting's
# trello holds 3,288 cards and renders 23; los-angeles-county's drive holds 3,004 items and renders
# an empty My Drive; 193 messages of one slack world sit under a channel key that names no channel
# and not one reaches the screen.
REF_KEY = re.compile(r"(?:_id|Id|ID)$")
CONTAINER_SHARE = 0.5  # a back-reference most records carry is the collection's convention
CONTAINER_EXCLUSIVE = 0.9  # a container holds each child once; a label belongs to every email
REACH_REASONS = (
    "naming a container that does not exist",
    "in no container",
    "under a container that is itself unreachable",
    "in a cycle of containers",
)


def _scalar_id(value):
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def _same_name(word, collection):
    """``cardIds`` names ``cards``, ``propertyId`` names ``properties``, ``rooms`` names ``rooms``.

    ``_singular`` only strips a trailing ``s``, which is enough when both sides are already the
    same form and silently wrong across an irregular plural. ``property`` against ``properties``
    is the back-reference that decides whether a container is a container, and without it 16
    miscounted booking properties were missed; a one-way stemmer instead turned ``expenses`` into
    ``expens`` and missed 158 Expensify reports. So both words offer every singular they could be
    and the answer is whether the two sets meet.
    """

    def forms(name):
        name = str(name).lower()
        out = {name}
        if name.endswith("ies") and len(name) > 4:
            out.add(f"{name[:-3]}y")
        if name.endswith("es") and len(name) > 3:
            out.add(name[:-2])
        if name.endswith("s"):
            out.add(name[:-1])
        return out

    return bool(word) and bool(forms(word) & forms(collection))


def _field_target(cols, index, collection, key):
    """Which collection a scalar field's values land in, ignoring the record's own keys."""
    hits = Counter()
    for _ids, record in cols[collection][0]:
        value = record.get(key)
        if not _scalar_id(value) or str(value) in primary_ids(collection, record):
            continue
        for name, ids in index.items():
            if str(value) in ids:
                hits[name] += 1
    return hits.most_common(1)[0][0] if hits else None


def _named_target(cols, collection, key):
    """Which collection a structural field names, from its name alone.

    ``parentId`` means the collection's own kind, ``parentMessageId`` means ``messages``. The
    fallback is what sees the worst case: a field where *nothing* resolves carries no evidence,
    and one drive holds 3,004 items whose seven parent folders were none of them written, so a
    data-only rule reads a wiped drive as a collection with no container convention at all.
    """
    base = re.sub(r"(?i)^(?:parent|root)", "", REF_KEY.sub("", key)).strip("_")
    if not base:
        return collection
    return next((name for name in cols if _same_name(base, name)), None)


def _back_reference(cols, index, child, parent):
    """The field by which a child record says which parent holds it, if most of them do.

    This is what separates a container from a reference. gmail's labels are named by every
    email's ``labels`` and carry no ``emailId``: the sidebar renders them, and without this test
    165 of 244 labels across 32 worlds are called unreachable, which is a non-defect.
    """
    rows = cols[child][0]
    fields = []
    for key in sorted({k for _ids, record in rows for k in record}):
        if key in PRIMARY_KEYS:
            continue
        present = [record for _ids, record in rows if _scalar_id(record.get(key))]
        if len(present) >= CONTAINER_SHARE * len(rows):
            fields.append((key, present))
    # The field *named* after the parent first, and only then one that merely resolves into it.
    # Alphabetical order alone hands back ``author_id`` for a Zendesk comment, which resolves into
    # tickets only because both collections key on small integers.
    named = [key for key, _present in fields if _same_name(REF_KEY.sub("", key), parent)]
    if named:
        return named[0]
    for key, present in fields:
        landed = sum(1 for record in present if str(record[key]) in index.get(parent, {}))
        if landed >= CONTAINER_SHARE * len(present):
            return key
    return None


def container_edges(cols, index):
    """``(child, kind, parent, field)`` for every containment the state itself establishes.

    ``self``  -- a field that says where the record hangs (drive's ``parentId``, a slack thread's
                 ``parentMessageId``). Left empty it is a root of the tree.
    ``list``  -- the parent collection holds the child's ids (``lists.cardIds``,
                 ``groups.itemIds``) and the child names the parent back.

    Both sides are required for a list, and the ids must barely repeat: a card sits in one list,
    while one label id sits in a thousand emails. A reference is not a container, and calling one
    a container is how a check invents a defect.
    """
    edges = []
    for name, (rows, _ids) in cols.items():
        keys = {k for _i, record in rows for k in record if REF_KEY.search(k) or k in ("parent", "root")}
        for key in sorted(keys):
            if key in PRIMARY_KEYS or not any(_scalar_id(r.get(key)) for _i, r in rows):
                continue
            structural = bool(STRUCTURAL_KEY.search(key))
            target = _field_target(cols, index, name, key)
            if target is None and structural:
                target = _named_target(cols, name, key)
            if target == name and (structural or _same_name(REF_KEY.sub("", key), name)):
                edges.append((name, "self", name, key))
            elif target and target != name and structural:
                edges.append((name, "self", target, key))
    for parent, (rows, _ids) in cols.items():
        for field in sorted({k for _i, r in rows for k, v in r.items() if isinstance(v, list)}):
            values = [str(v) for _i, r in rows for v in (r.get(field) or []) if _scalar_id(v)]
            if not values:
                continue
            hits = Counter()
            for value in values:
                for name, ids in index.items():
                    if value in ids:
                        hits[name] += 1
            if not hits:
                continue
            child = hits.most_common(1)[0][0]
            if child == parent and not ID_LIST_KEY.search(field):
                continue
            if not _same_name(ID_LIST_KEY.sub("", field), child) and not _same_name(field, child):
                continue
            if len(set(values)) < CONTAINER_EXCLUSIVE * len(values):
                continue
            if not _back_reference(cols, index, child, parent):
                continue
            edges.append((child, "list", parent, field))
    return edges


def map_key_containers(state, cols, index):
    """Collections stored as ``{container_id: [record...]}``: the key itself is the container.

    slack keys messages by channel and Zendesk comments by ticket, and ``state_ids`` puts those
    keys *into* the id universe, so a key that names no channel is the one dangling reference no
    check could ever report. One world holds 193 messages under ``CH-OPS`` against channels named
    ``CH-BOROUGH``, ``CH-PROGRAM`` and ``CH-REPORT``; none of the 193 reaches the screen.
    """
    out = {}
    for name, value in (state if isinstance(state, dict) else {}).items():
        if name not in cols or not isinstance(value, dict) or not value:
            continue
        if not all(isinstance(v, list) for v in value.values()):
            continue
        if not any(isinstance(r, dict) for v in value.values() for r in v):
            continue
        targets, resolved = set(), 0
        for key in value:
            here = {other for other, ids in index.items() if other != name and str(key) in ids}
            targets |= here
            resolved += bool(here)
        # Most of the keys must name a record, or the map groups records by something this state
        # does not hold -- gmail's threadId, and the same reasoning check_references gives. Every
        # map on disk resolves none of its keys or at least 83% of them, so the line is clear of
        # the data on both sides.
        if targets and resolved >= CONTAINER_SHARE * len(value):
            out[name] = (value, sorted(targets))
    return out


def reachability(state):
    """Per collection, ``(unreachable keys, records, mechanisms, reasons)``.

    A collection the state gives no container convention is a collection the app lists itself, and
    every record in it is reachable: saying otherwise would condemn every top-level collection in
    the fleet. Where there is a convention, a record is reachable if *any* route it has reaches a
    root, so a record with two containers is judged by the one that works.
    """
    cols = indexed_collections(state)
    if not cols:
        return {}
    index = {name: ids for name, (_rows, ids) in cols.items()}
    key_of = {}
    for name, (rows, _ids) in cols.items():
        for ids, _record in rows:
            for one in ids:
                key_of.setdefault((name, one), min(ids))
    routes, mechanisms, invented = defaultdict(list), defaultdict(list), set()
    for child, kind, parent, field in container_edges(cols, index):
        mechanisms[child].append((kind, parent, field))
        if kind == "self":
            for ids, record in cols[child][0]:
                value = record.get(field)
                if not _scalar_id(value) or str(value) in primary_ids(child, record):
                    routes[(child, min(ids))].append((parent, None, "root"))
                else:
                    routes[(child, min(ids))].append((parent, str(value), kind))
        else:
            held = {}
            for parent_ids, parent_record in cols[parent][0]:
                for value in parent_record.get(field) or []:
                    if _scalar_id(value):
                        held.setdefault(str(value), min(parent_ids))
            back = _back_reference(cols, index, child, parent)
            for ids, record in cols[child][0]:
                hit = next((held[one] for one in sorted(ids) if one in held), None)
                routes[(child, min(ids))].append((parent, hit, kind))
                # Not a route -- the app draws the children from the parent's list, so naming a
                # parent is not being drawn by one. It is the difference between two repair
                # briefs, though: 3,120 blastx cards name lists nobody wrote and 145 are missing
                # from a list that exists, and one of those is fixed by writing a board.
                value = record.get(back) if back else None
                if hit is None and _scalar_id(value) and (parent, str(value)) not in key_of:
                    invented.add((child, min(ids)))
    for name, (mapping, targets) in map_key_containers(state, cols, index).items():
        mechanisms[name].append(("key", "|".join(targets), ""))
        for group, records in mapping.items():
            for record in records:
                ids = primary_ids(name, record) if isinstance(record, dict) else set()
                if ids:
                    target = next((t for t in targets if str(group) in index[t]), targets[0])
                    routes[(name, min(ids))].append((target, str(group), "key"))

    # Reachability grows from the roots instead of walking up from each record: a depth-first
    # walk has to cut a path that returns to a record already on its stack, and caching that cut
    # as a verdict makes the answer depend on which record the loop happened to start from.
    reachable, children = set(), defaultdict(list)
    for node, here in routes.items():
        for parent, parent_id, kind in here:
            if kind == "root":
                reachable.add(node)
                continue
            if parent_id is None:
                continue
            above = key_of.get((parent, parent_id))
            if above is None:
                continue
            if parent in mechanisms:
                children[(parent, above)].append(node)
            else:  # the container's own collection is one the app lists itself
                reachable.add(node)
    queue = deque(reachable)
    while queue:
        for child in children.get(queue.popleft(), ()):
            if child not in reachable:
                reachable.add(child)
                queue.append(child)

    def cause(node):
        """Why a record is out of reach, which is what tells a repair what to write."""
        here = routes.get(node) or []
        named = [(parent, pid) for parent, pid, kind in here if kind != "root" and pid is not None]
        if not named:
            return REACH_REASONS[1]
        if not [pair for pair in named if pair in key_of]:
            return REACH_REASONS[0]
        seen, stack = set(), [node]
        while stack:  # a container chain that comes back here never reaches a root
            for parent, parent_id, kind in routes.get(stack.pop()) or []:
                above = key_of.get((parent, parent_id)) if kind != "root" and parent_id else None
                if above is None:
                    continue
                if (parent, above) == node:
                    return REACH_REASONS[3]
                if (parent, above) not in seen:
                    seen.add((parent, above))
                    stack.append((parent, above))
        return REACH_REASONS[2]

    out = {}
    for name, (rows, _ids) in cols.items():
        if name not in mechanisms:
            continue
        missing, why = [], Counter()
        for ids, _record in rows:
            node = (name, min(ids))
            if node in reachable:
                continue
            missing.append(min(ids))
            reason = cause(node)
            why[REACH_REASONS[0] if node in invented and reason == REACH_REASONS[1] else reason] += 1
        out[name] = (missing, len(rows), mechanisms[name], why)
    return out


def check_reachability(label, state, severity="warning"):
    """A record nothing links to is a record the app never draws, however well formed it is.

    The expensive way to fail: a collection full of correct records, a full seed spent writing
    them, and an app that renders empty. Every instance below was invisible to every check the
    pass had, because each check asked whether a *reference* resolved and none asked whether a
    *record* could be reached.

    Measured over the 60 worlds on disk, and three of them served and looked at:

    * trello: blastx-consulting holds 3,288 cards and renders **23**. ``List.jsx:59`` draws a
      list from ``list.cardIds``, so 3,120 cards naming lists that were never written and 145 in
      no ``cardIds`` are on no board; the navbar search skips them too
      (``if (!board || !list) continue``). 3,417 of 3,506 cards fleet-wide.
    * drive: 20,192 of 78,462 items cannot be reached from a root folder, 37 of them only
      transitively -- their parent exists and its parent does not, which no one-hop check can
      see. los-angeles-county declares 3,004 items under seven folders that were never written
      and serves an empty My Drive.
    * slack: 3,350 of 4,960 threads name a root message that does not exist, and 196 messages
      sit under a channel key that names no channel.
    * monday: 503 of 727 items are in no group's ``itemIds``.

    One finding per collection, carrying the count and the split by cause, because a repair brief
    for 3,120 invented list ids is not the brief for 145 missing memberships. ``severity`` is the
    caller's, for the reason ``check_references`` gives: 22,952 of the 27,658 are bulk records no
    author owns, so an error outside the authoring loop condemns a world nobody can revise, while
    inside it every one of 4,706 is a record its own author can still link.
    """
    findings = []
    for name, (missing, total, mechanisms, reasons) in sorted(reachability(state).items()):
        if not missing:
            continue
        how = ", ".join(
            f"{parent}.{field}" if field else f"a key naming {parent}" for _kind, parent, field in mechanisms
        )
        split = "; ".join(f"{count} {reason}" for reason, count in reasons.most_common())
        findings.append(
            finding(
                severity,
                label,
                f"/{name}",
                f"{len(missing)} of {total} {name} cannot be reached from anything the app draws "
                f"({split}; e.g. {', '.join(missing[:MAX_EXAMPLES])}); this app holds {name} "
                f"through {how}, so those records are on no screen",
                records=missing,
            )
        )
    return findings


COUNT_FIELD = re.compile(r"^(?:num|n)([A-Z]\w+)$")


def _declared_counts(cols, name):
    """``field -> (base, how to count it)`` for every asserted child count on a collection."""
    rows = cols[name][0]
    out = {}
    for key in sorted({k for _ids, record in rows for k in record}):
        match = COUNT_KEY.match(key) or COUNT_FIELD.match(key)
        if not match or not any(_numeric(r.get(key)) is not None for _ids, r in rows):
            continue
        base = match.group(1)
        inline = next(
            (
                field
                for _ids, record in rows
                for field in record
                if isinstance(record.get(field), list) and _same_name(field, base)
            ),
            None,
        )
        if inline:
            out[key] = (base, ("inline", inline))
            continue
        child = next((c for c in cols if _same_name(base, c) and c != name), None)
        if child:
            out[key] = (base, ("collection", child))
    return out


def check_counts(label, state, severity="warning"):
    """A stored child count must be the number of children the state holds.

    ``check_totals`` does this for a stored sum; a count is the same contract and was believed
    instead of derived. Measured over the worlds on disk, 685 records carry a count their own
    state contradicts: Zendesk tickets print "3 comments" over seven, 158 Expensify reports
    declare expenses and hold none, 191 weibo posts declare comments and hold none.

    Two directions, and only one of them is always a defect:

    * more children than the count says is impossible under any reading -- the records are there
      and the header under-counts them (159 records).
    * a count above the children is ordinary for a public total over a stored sample, which is
      why amazon's 60 products declaring 86 reviews over 4 are left alone. It is only reported
      when the record holds **none** and some record of the same collection counts its children
      exactly, which is the collection telling you the field means what it says (526 records).
    """
    findings = []
    cols = indexed_collections(state)
    index = {name: ids for name, (_rows, ids) in cols.items()}
    for name, (rows, _ids) in sorted(cols.items()):
        for key, (base, (kind, where)) in _declared_counts(cols, name).items():
            held = None
            if kind == "collection":
                back = _back_reference(cols, index, where, name)
                if not back or not _same_name(REF_KEY.sub("", back), name):
                    continue
                held = Counter()
                for _ids, child in cols[where][0]:
                    if _scalar_id(child.get(back)):
                        held[str(child[back])] += 1
            rows_counted = []
            for ids, record in rows:
                declared = _numeric(record.get(key))
                if declared is None:
                    continue
                actual = len(record.get(where) or []) if kind == "inline" else sum(held[i] for i in ids)
                rows_counted.append((min(ids), int(declared), actual))
            if not rows_counted:
                continue
            exact = [row for row in rows_counted if row[1] == row[2]]
            under = [row for row in rows_counted if row[2] > row[1]]
            empty = [row for row in rows_counted if row[1] > 0 and row[2] == 0]
            for rows_wrong, message in (
                (under, f"hold more {where} than their {key} says"),
                (empty if exact else [], f"declare {key} above zero and hold no {where}"),
            ):
                if not rows_wrong:
                    continue
                examples = ", ".join(
                    f"{rid}: {key} {declared}, {actual} {where}"
                    for rid, declared, actual in rows_wrong[:MAX_EXAMPLES]
                )
                findings.append(
                    finding(
                        severity,
                        label,
                        f"/{name}",
                        f"{len(rows_wrong)} of {len(rows_counted)} {name} {message} "
                        f"(e.g. {examples}); a count is the number of records it counts, and the "
                        f"{base.lower()} records are in {where}",
                        records=[rid for rid, _declared, _actual in rows_wrong],
                    )
                )
    return findings


def _walk_dicts(node, path=""):
    """Visit records even when they have no ID or live in a keyed collection."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk_dicts(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk_dicts(value, f"{path}/{index}")


def _field_name(key):
    return re.sub(r"[_ -]", "", key).casefold()


def _number(value):
    # Strings may hold rates or formatted money; their units are not certain.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = Decimal(str(value))
        if number.is_finite():
            return number
    return None


def _line_amount(line):
    if not isinstance(line, dict):
        return None
    fields = {_field_name(k): v for k, v in line.items()}
    amounts = [_number(fields[k]) for k in ("amount", "total") if k in fields]
    if amounts:
        return amounts[0] if None not in amounts and len(set(amounts)) == 1 else None
    price = _number(fields.get("price", fields.get("unitprice")))
    quantity = _number(fields.get("quantity"))
    if price is not None and quantity is not None:
        return price * quantity
    return None


def check_arithmetic(label, state):
    """Check clear totals and signed statement movements, within one cent.

    Incomplete lines, competing totals and unknown adjustments are left to the
    reviewer. A total may include the listed adjustments or already include them
    in its lines. Report at most eight bad records per collection.
    """
    findings = []
    counts = Counter()
    for path, record in _walk_dicts(state):
        collection = path.rsplit("/", 1)[0]
        if counts[collection] >= 8:
            continue
        fields = {_field_name(k): (k, v) for k, v in record.items()}
        lists = [
            (k, v)
            for name, (k, v) in fields.items()
            if name in {"items", "lines", "lineitems", "entries", "transactions", "charges"}
            and isinstance(v, list)
            and v
        ]
        if len(lists) != 1:
            continue
        list_key, lines = lists[0]
        amounts = [_line_amount(line) for line in lines]
        if None in amounts:
            continue
        # Unsigned debits and credits need account-specific sign rules.
        if _field_name(list_key) == "transactions" and any(
            _field_name(k) in {"type", "direction", "debit", "credit"} for line in lines for k in line
        ):
            continue
        currencies = {
            str(v) for row in [record, *lines] for k, v in row.items() if _field_name(k) == "currency"
        }
        if len(currencies) > 1:
            continue
        total = sum(amounts, Decimal(0))
        expected = [(total, f"{list_key} sum {total}")]
        if "openingbalance" in fields and "closingbalance" in fields:
            if _field_name(list_key) != "transactions":
                continue
            opening = _number(fields["openingbalance"][1])
            target_key, value = fields["closingbalance"]
            if opening is None:
                continue
            expected = [
                (opening + total, f"opening balance {opening} + transactions sum {total} = {opening + total}")
            ]
        else:
            targets = [
                pair
                for name, pair in fields.items()
                if name in {"total", "amount", "balance", "subtotal", "grandtotal", "totalamount"}
            ]
            if (
                len(targets) == 2
                and "subtotal" in fields
                and any(name in fields for name in ("total", "grandtotal", "totalamount"))
            ):
                subtotal = fields["subtotal"]
                amount = _number(subtotal[1])
                if amount is None:
                    continue
                # Check the subtotal first; one finding is enough to repair this record.
                targets = (
                    [subtotal]
                    if abs(amount - total) > Decimal("0.01")
                    else [pair for pair in targets if pair != subtotal]
                )
            if len(targets) != 1:
                continue
            target_key, value = targets[0]
            adjustments = [
                (name, k, _number(v))
                for name, (k, v) in fields.items()
                if name.startswith(("tax", "fee", "discount"))
            ]
            if any(
                amount is None
                or name not in {"tax", "taxamount", "fee", "fees", "feeamount", "discount", "discountamount"}
                for name, _, amount in adjustments
            ):
                continue
            if adjustments and _field_name(target_key) != "subtotal":
                adjusted = total
                detail = f"{list_key} sum {total}"
                for name, key, amount in adjustments:
                    delta = -abs(amount) if name.startswith("discount") else amount
                    adjusted += delta
                    detail += f" {'-' if delta < 0 else '+'} {key} {abs(delta)}"
                expected.append((adjusted, f"{detail} = {adjusted}"))
        actual = _number(value)
        if actual is None or any(abs(actual - amount) <= Decimal("0.01") for amount, _ in expected):
            continue
        detail = " or ".join(description for _, description in expected)
        findings.append(
            finding(
                "error",
                label,
                path,
                f"record {record.get('id', path)!r}: {target_key} {actual} differs from {detail}; correct the total or its parts",
            )
        )
        counts[collection] += 1
    return findings


MONTH_DATE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+"
    r"(\d{1,2})(?!\d)(?:,?\s+(\d{4})(?!\d))?\b",
    re.IGNORECASE,
)
MONTH_NUMBERS = {
    name: i
    for i, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
REPLY_PREFIX = re.compile(r"^\s*(?:(?:re|fwd):\s*)+", re.IGNORECASE)


def _record_time(record, *, reply=False):
    times = []
    for key, value in record.items():
        name = _field_name(key)
        if name not in {
            "timestamp",
            "updatedat",
            "modifiedat",
            "createdat",
            "sentat",
            "date",
        } or not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            continue
        parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        times.append((key, value, parsed))
    if reply:
        # Later edits to a parent do not move the time at which it was sent.
        for names in ({"timestamp", "sentat"}, {"createdat"}, {"date"}):
            sent = [entry for entry in times if _field_name(entry[0]) in names]
            if sent:
                times = sent
                break
    return max(times, key=lambda entry: entry[2]) if times else None


def _prose_dates(text, year):
    text = re.sub(r"<[^>]*>", " ", text)
    iso_dates = re.findall(r"(?<![\w-])\d{4}-\d{2}-\d{2}(?![\d-])", text)
    months = list(MONTH_DATE.finditer(text))
    years = {int(value) for value in re.findall(r"\b(?:19|20)\d{2}\b", text)}
    # A year in the prose takes precedence over the record's year. Multiple
    # years leave abbreviated dates ambiguous, so only explicit dates survive.
    context_year = next(iter(years)) if len(years) == 1 else year if not years else None
    for value in iso_dates:
        try:
            yield date.fromisoformat(value)
        except ValueError:
            pass
    for match in months:
        use_year = int(match[3]) if match[3] else context_year
        if use_year is not None:
            try:
                yield date(use_year, MONTH_NUMBERS[match[1][:3].lower()], int(match[2]))
            except ValueError:
                pass


def check_chronology(label, state, reference_date):
    """Check prose dates and replies against their own records, not the cutoff.

    The reference cutoff belongs to check_timestamps. A bare month and day use
    the prose's single explicit year, or the record's year. Subject matching
    needs one original in the same collection; shared thread IDs are not parents.
    """
    findings = []
    rows = list(_walk_dicts(state))
    by_id = defaultdict(list)
    subjects = defaultdict(list)
    for path, record in rows:
        if isinstance(record.get("id"), (str, int)):
            by_id[str(record["id"])].append((path, record))
        subject = record.get("subject")
        if isinstance(subject, str) and not REPLY_PREFIX.match(subject):
            subjects[(path.rsplit("/", 1)[0], subject.strip().casefold())].append((path, record))
    for path, record in rows:
        latest = _record_time(record)
        if latest:
            for key in ("body", "content", "description", "notes"):
                if not isinstance(record.get(key), str):
                    continue
                later = sorted(
                    {day for day in _prose_dates(record[key], latest[2].year) if day > latest[2].date()}
                )
                if later:
                    # A plan or a deadline legitimately names a later date; only the reviewer can
                    # tell that from a document describing what has not happened yet. A lead, not
                    # a gate: calibration on five worlds showed plans and deadlines dominate.
                    findings.append(
                        finding(
                            "warning",
                            label,
                            f"{path}/{key}",
                            f"record {record.get('id', path)!r}: {key} cites {later[0]} after {latest[0]} {latest[1]}; a plan may, a report of events may not",
                        )
                    )
                    break
        sent = _record_time(record, reply=True)
        if not sent:
            continue
        parents = []
        links = [
            v
            for k, v in record.items()
            if _field_name(k) in {"inreplyto", "parentid", "threadid"} and isinstance(v, (str, int))
        ]
        for link in links:
            hits = [hit for hit in by_id[str(link)] if hit[0] != path]
            if len(hits) == 1:
                parents.extend(hits)
        subject = record.get("subject")
        explicit_parent = any(
            _field_name(k) in {"inreplyto", "parentid"} and v is not None for k, v in record.items()
        )
        if not parents and not explicit_parent and isinstance(subject, str) and REPLY_PREFIX.match(subject):
            original = REPLY_PREFIX.sub("", subject).strip().casefold()
            hits = subjects[(path.rsplit("/", 1)[0], original)]
            thread = record.get("threadId", record.get("thread_id"))
            if thread is not None:
                hits = [hit for hit in hits if hit[1].get("threadId", hit[1].get("thread_id")) == thread]
            if original and len(hits) == 1:
                parents = hits
        # Conflicting links do not establish which message this one answers.
        if len({p for p, _ in parents}) != 1:
            continue
        parent_path, parent = parents[0]
        parent_time = _record_time(parent, reply=True)
        if not parent_time:
            continue
        sent_end = sent[2]
        if len(sent[1]) == 10:
            sent_end = datetime.combine(sent_end.date(), time.max, sent_end.tzinfo)
        if sent_end < parent_time[2]:
            findings.append(
                finding(
                    "error",
                    label,
                    path,
                    f"reply {record.get('id', path)!r} at {sent[1]} precedes {parent.get('id', parent_path)!r} at {parent_time[1]}; correct the reply date or parent link",
                )
            )
    return findings


CODE_TOKEN = re.compile(r"\b(?:[A-Z]{2,}[A-Z0-9-]*\d[A-Z0-9-]*|[A-Z]{3,})\b")


def proper_name_tokens(*trees):
    """Explicit name fields exempt capitalized proper names, never digit codes."""
    names = set()

    def visit(node):
        if isinstance(node, dict):
            if isinstance(node.get("name"), str):
                names.update(re.findall(r"(?<![\w-])[A-Z]{2,}(?![\w-])", node["name"]))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    for tree in trees:
        visit(tree)
    return names


def check_plain_language(label, texts, max_codes_per_100_words=6.0, min_words=40, proper_names=()):
    """Prose written for people should not read like a system log.

    ``texts`` maps a path to a prose string (message bodies, notes, briefs). A high density of
    reference codes and all-caps tokens in prose is reported as a warning for the reviewer; ids in
    their own record fields are not prose and are not checked here.
    """
    findings = []
    for path, text in texts.items():
        words = len(text.split())
        if words < min_words:
            continue
        codes = [token for token in CODE_TOKEN.findall(text) if token not in proper_names]
        density = 100.0 * len(codes) / words
        if density > max_codes_per_100_words:
            sample = sorted(set(codes))[:8]
            findings.append(
                finding(
                    "warning",
                    label,
                    path,
                    f"reads like a system log: {density:.1f} codes per 100 words (e.g. {sample})",
                )
            )
    return findings


def prose_texts(states, materials=None):
    """Collect human-facing prose: message-like fields in app records plus material contents."""
    texts = {}
    prose_keys = {"body", "text", "message", "description", "content", "comment", "note", "notes", "snippet"}
    for app_id, state in states.items():
        for path, rec in records(state, app_id):
            for key in prose_keys & rec.keys():
                value = rec[key]
                if isinstance(value, str) and len(value.split()) >= 8:
                    texts.setdefault(f"{app_id}:{key}", []).append(value)
    joined = {k: " ".join(v) for k, v in texts.items()}
    for path, content in (materials or {}).items():
        # Reference codes belong in exports and worksheets. Their raw cells
        # are not prose; counting them as jargon only creates repair noise.
        if Path(path).suffix.lower() not in {".csv", ".tsv", ".xlsx", ".xls", ".ods"}:
            joined[f"materials:{path}"] = content
    return joined


GENERATOR_RULE = re.compile(
    r"\b[a-z]\s*=\s*\d+\s*\.\.\s*\d+\b"  # i=1..600
    r"|\b(?:pad|floor|mod)\(\s*\(?\s*[ijkvn]\b"  # pad(i,4), floor((i-1)/20), mod(v-1,20); not =FLOOR(B2/7,1)
    r"|\+\s*space\s*\+"  # firstNames[...] + space + surnames[...]
    r"|\bexpand (?:them|these|this|the (?:ranges|tables|rules)) literally\b",
    re.IGNORECASE,
)


def check_literal_records(label, tree, max_reports=8):
    """Seeded data must be literal records. Nothing downstream expands ranges or formulas.

    Under volume pressure a model may write compact generator tables ("i=1..600",
    "pad(i,4)", "firstNames[floor((i-1)/20)+1]") instead of rows. The hydrator posts the JSON
    verbatim, so such a world has no people, no records and dangling references everywhere.
    Reported as errors so the seed stops and the call is redone with this feedback.
    """
    findings = []

    def visit(node, path):
        if len(findings) >= max_reports:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                visit(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{path}/{index}")
        elif isinstance(node, str) and not node.lstrip().startswith("=") and GENERATOR_RULE.search(node):
            findings.append(
                finding(
                    "error",
                    label,
                    path,
                    f"generator rule instead of literal data: {node[:90]!r}; write every record out in full",
                )
            )

    visit(tree, "")
    return findings


MESSAGE_COLLECTIONS = {"emails", "messages", "mail", "inbox", "sent", "threads", "conversations", "tickets"}


def check_communications(label, state, min_median_body=80):
    """Messages people exchange have a sender, a recipient, a subject and a body with substance.

    One collection at a time: a majority of emails without a recipient or subject, or a median
    body under ``min_median_body`` characters, means the collection was written as one-line
    stubs and is reported as an error for that collection.
    """
    findings = []
    for name, value in state.items() if isinstance(state, dict) else []:
        if name not in MESSAGE_COLLECTIONS:
            continue
        rows = (
            value
            if isinstance(value, list)
            else [r for v in value.values() for r in (v if isinstance(v, list) else [v])]
            if isinstance(value, dict)
            else []
        )
        rows = [r for r in rows if isinstance(r, dict)]
        if len(rows) < 5:
            continue
        email_like = [r for r in rows if "subject" in r and ("from" in r or "sender" in r)]
        if len(email_like) >= len(rows) / 2:  # mail, not tickets or chat
            missing = {
                field: sum(1 for r in rows if not r.get(field) and not r.get(alt))
                for field, alt in (("to", "recipients"), ("subject", "title"))
            }
            parties = [
                party
                for r in rows
                for party in [
                    r.get("from"),
                    *(r.get("to") if isinstance(r.get("to"), list) else [r.get("to")]),
                ]
                if isinstance(party, dict)
            ]
            # Some apps address people by id and name only (a clinical in-basket); the rule
            # applies where the app models addresses at all.
            blank = sum(1 for party in parties if "email" in party and not (party.get("email") or "").strip())
            if any("email" in party for party in parties) and blank > len(rows) * 0.1:
                findings.append(
                    finding(
                        "error",
                        label,
                        f"/{name}",
                        f"{blank} sender or recipient entries have no email address; every party to a message has one",
                    )
                )
            for field, count in missing.items():
                if count > len(rows) * 0.1:
                    findings.append(
                        finding(
                            "error",
                            label,
                            f"/{name}",
                            f"{count} of {len(rows)} records have no {field}; every message names who it went to and what it is about",
                        )
                    )
        bodies = sorted(len(r["body"]) for r in rows if isinstance(r.get("body"), str))
        if len(bodies) >= 5 and bodies[len(bodies) // 2] < min_median_body:
            findings.append(
                finding(
                    "error",
                    label,
                    f"/{name}",
                    f"median body is {bodies[len(bodies) // 2]} characters across {len(bodies)} records; these are one-line stubs, not the messages people write",
                )
            )
    return findings


WORD = re.compile(r"[a-z']+")
NOISE_TOKENS = {"http", "https", "href", "www", "com", "ai", "p", "br", "div", "span", "a", "itemid", "xlang"}
PROSE_FIELDS = {"body", "content", "text", "description", "summary", "notes", "note", "message", "comment"}
# Fields whose meaning belongs to the app that renders them, not to the person they describe.
# A handle, an avatar, a short name and a working time zone are how one product shows someone;
# two apps disagreeing about them is not a fact conflict, and no rewrite can make a GitHub login
# and a calendar display name the same string without breaking one of the two.
PRESENTATION_FIELDS = frozenset(
    {
        "username",
        "avatar",
        "avatarColor",
        "initials",
        "displayName",
        "timeZone",
        "color",
        "textColor",
    }
)
SIGN_OFF = re.compile(
    r"\n\s*(?:best|regards|kind regards|thanks|thank you|cheers|sincerely|--|this (?:e-?mail|message) (?:is|may))",
    re.IGNORECASE,
)
TITLE_FIELDS = {"title", "subject", "name"}

LIFECYCLE_WORDS = {
    "enabled",
    "disabled",
    "active",
    "inactive",
    "archived",
    "pending",
    "open",
    "closed",
    "draft",
    "published",
    "paid",
    "unpaid",
    "approved",
    "rejected",
    "cancelled",
    "canceled",
    "completed",
    "in_progress",
    "new",
}


def _lifecycle_word(value):
    """Whether a scalar is an app lifecycle status rather than a fact about the world."""
    return isinstance(value, str) and value.strip().lower().replace(" ", "_") in LIFECYCLE_WORDS


def _status_like(value):
    """A short lowercase word such as ``active`` or ``ended``: comparable with a lifecycle status."""
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z_ -]{0,24}", value.strip()) is not None


RECORD_KEYS = ("id", "sys_id", "uuid", "_id", "key", "number", "messageId")


def record_key(records):
    """The field that identifies every record in a list (ServiceNow uses sys_id), or None."""
    if not isinstance(records, list) or not records or not all(isinstance(r, dict) for r in records):
        return None
    for key in RECORD_KEYS:
        if all(type(r.get(key)) in (str, int) for r in records):
            return key
    return None


def record_identifiers(record):
    """Every identifier a record carries, as strings: id, messageId, sys_id, *Id."""
    out = set()
    for key, value in record.items():
        if isinstance(value, (str, int)) and (key == "id" or key.endswith(("Id", "_id", "ID"))):
            out.add(str(value))
    return out


SENDER_FIELDS = ("from", "sender", "senderId", "author", "authorId", "userId", "user", "owner", "createdBy")


def _sender(record):
    for key in SENDER_FIELDS:
        value = record.get(key)
        if isinstance(value, dict):
            value = value.get("email") or value.get("id") or value.get("name")
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    return ""


def _signature_like(senders, occurrences, total_rows):
    """A phrase mostly written by one known sender, in a collection with several senders."""
    known = {k: v for k, v in senders.items() if k}
    if not known:
        return False
    top = max(known.values())
    return top >= 0.8 * occurrences and len(known) < total_rows / 4


def check_texture(label, state, max_share=0.15, min_records=20, exclude_ids=frozenset()):
    """Real data is written by many people about many things; templated fill is not.

    Every collection with at least ``min_records`` records is checked: an opening that more
    than ``max_share`` of its prose fields share, or a four-word phrase that recurs in more
    than ``max_share`` of the records (``2 * max_share`` outside message collections, where
    recurring titles such as a weekly meeting are ordinary), is an error naming the phrase.
    The repair pass hands it back to the author, who rewrites the collection instead of the
    reviewer having to read all of it.
    """
    findings = []

    def collections(node, path=""):
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if (
                isinstance(value, list)
                and len(value) >= min_records
                and all(isinstance(r, dict) for r in value[:5])
            ):
                yield f"{path}/{key}", [r for r in value if isinstance(r, dict)]
            elif isinstance(value, dict):
                flat = [
                    r
                    for v in value.values()
                    for r in (v if isinstance(v, list) else [v])
                    if isinstance(r, dict)
                ]
                keyed_lists = sum(isinstance(v, list) for v in value.values())
                if keyed_lists and keyed_lists >= len(value) / 2 and len(flat) >= min_records:
                    yield f"{path}/{key}", flat
                else:
                    yield from collections(value, f"{path}/{key}")

    for path, rows in collections(state):
        name = path.rsplit("/", 1)[-1]
        share = max_share if name in MESSAGE_COLLECTIONS else 2 * max_share
        if exclude_ids:  # the bulk layer repeats by design and is not judged for texture
            rows = [r for r in rows if not (record_identifiers(r) & exclude_ids)]
            if len(rows) < min_records:
                continue
        prose = [
            re.sub(r"<[^>]+>", " ", " ".join(str(r[k]) for k in PROSE_FIELDS if isinstance(r.get(k), str)))
            for r in rows
        ]
        prose = [t for t in prose if len(WORD.findall(t)) >= 4]
        if len(prose) >= min_records:
            openings = Counter(" ".join(WORD.findall(t.lower())[:3]) for t in prose)
            phrase, count = openings.most_common(1)[0]
            if count > share * len(prose):
                findings.append(
                    finding(
                        "error",
                        label,
                        path,
                        f"{count} of {len(prose)} records open with {phrase!r}; templated text, rewrite so each record is its own",
                    )
                )
        # Recurring appointments legitimately keep their title. Still check
        # their descriptions, dates, participants and references normally.
        title_fields = set() if label == "google_calendar_mock" and name == "events" else TITLE_FIELDS
        titled = [
            re.sub(
                r"<[^>]+>",
                " ",
                " ".join(str(r[k]) for k in PROSE_FIELDS | title_fields if isinstance(r.get(k), str)),
            )
            for r in rows
        ]
        grams = Counter()
        senders_by_gram = {}
        for r, t in zip(rows, titled, strict=False):
            body = SIGN_OFF.split(t, maxsplit=1)[0]  # a signature or disclaimer repeats by nature
            words = [w for w in WORD.findall(body.lower()) if w not in NOISE_TOKENS]
            row_grams = {" ".join(words[i : i + 4]) for i in range(len(words) - 3)}
            grams.update(row_grams)
            sender = _sender(r)
            for g in row_grams:
                senders_by_gram.setdefault(g, Counter())[sender] += 1
        # A phrase one person repeats in most of their own messages is a signature line, not
        # templated fill; only a phrase spread across senders counts.
        candidates = [
            (g, n) for g, n in grams.most_common(8) if not _signature_like(senders_by_gram[g], n, len(rows))
        ]
        if candidates:
            phrase, count = candidates[0]
            if count > share * len(rows):
                findings.append(
                    finding(
                        "error",
                        label,
                        path,
                        f"the phrase {phrase!r} recurs in {count} of {len(rows)} records; people do not repeat themselves like that",
                    )
                )
    return findings


LEAK_PATTERNS = (
    (
        "error",
        re.compile(r"xlang\.ai|cua-gym|cuagym|picsum\.photos", re.IGNORECASE),
        "benchmark platform reference",
    ),
    (
        "error",
        re.compile(
            r"@(?:[a-z0-9-]+\.)?(?:example|company|acme)\.(?:com|org|net)\b|https?://(?:www\.)?(?:example|company)\.(?:com|org)",
            re.IGNORECASE,
        ),
        "placeholder domain",
    ),
    (
        "error",
        re.compile(
            r"\b(?:seeded|seed data|placeholder|mock|grader|rubric|workflow_id|for (?:llm |rl )?evaluation)\b"
            r"|\b(?:newly authored context|new context\s*:)"
            r"|\bsynthetic (?:company|compan(?:y|ies)|data|dataset|world|scenario|polic(?:y|ies)|rules?|records?|example|environment|internal)\b"
            r"|(?-i:(?:^|[.!?:]\s+|<p>|<li>|\n)Synthetic\b(?! (?:leather|fibers?|fibres?|fabrics?|turf|rubber|materials?|blends?|opioids?|drugs?|cannabinoids?|oils?|lubricants?|uppers?|fill|fur|hair|diamonds?|textiles?)))",
            re.IGNORECASE,
        ),
        "pipeline vocabulary in company text",
    ),
    (
        # The harness itself, described to the person sitting in front of it. 44 desktop files
        # across 15 companies defined computer-use automation, virtual machines and bash, printed
        # the worker's internal id, or documented the apps' HTTP state route outright -- one of
        # them spelled out GET /state?sid= and which key each app returns. A company does not
        # explain its own instrumentation, and this is how agents learned to read the applications
        # over HTTP rather than open them.
        "error",
        re.compile(
            r"\bcomputer[- ]use automation\b|\(CUA\)|\bCUA[- ]Gym\b"
            r"|\bvirtual machine \(VM\)|\byour (?:own )?(?:personal )?VM\b|\bpersonal virtual machine\b"
            r"|\bGET /state\b|\bstored_state\b|/post\?sid=|\?sid=&lt;|\bset_current\b"
            r"|\bworker[ _]id\s*[:=]|\bruntime launcher\b|\breference clock\b|\bevidence cutoff\b",
            re.IGNORECASE,
        ),
        "harness vocabulary in company text",
    ),
    (
        "warning",
        re.compile(r"\b(?:John Smith|Sarah Johnson|Jane Doe|John Doe)\b"),
        "sample person from the schema example",
    ),
)


def _business_usage(text, hit):
    """Ambiguous business words need nearby authoring context, not an industry whitelist."""
    word = hit.group(0).lower()
    if word in {"placeholder", "for evaluation"}:
        # Bound to this sentence: benign usage elsewhere cannot excuse a leak.
        before = re.split(r"[.!?\n]", text[: hit.start()])[-1]
        after = re.split(r"[.!?\n]", text[hit.end() :])[0]
        context = (before[-100:] + " " + after[:100]).lower()
        return not re.search(
            r"\b(?:data|dataset|records?|tasks?|agents?|benchmark|training|llm|rl|harness|"
            r"generated|generation|seed|seeded|synthetic|scenarios?)\b",
            context,
        )
    return False


def check_leaks(label, tree, max_reports=6):
    """Nothing in a company's data knows it is a benchmark.

    Platform hosts, placeholder domains, the schema examples' sample people and the pipeline's
    own vocabulary (synthetic, seeded, mock, grader) come from the prompt, not the company.
    Prose and title fields are checked for vocabulary; every string is checked for hosts and
    domains. Errors feed the repair pass.
    """
    findings = []
    counts = {}

    def visit(node, path, key=None):
        if isinstance(node, dict):
            for k, v in node.items():
                visit(v, f"{path}/{k}", k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                visit(v, f"{path}/{i}", key)
        elif isinstance(node, str):
            for severity, pattern, what in LEAK_PATTERNS:
                if what.startswith("pipeline") and key not in PROSE_FIELDS | TITLE_FIELDS:
                    continue
                hit = next(
                    (
                        m
                        for m in pattern.finditer(node)
                        if not (what.startswith("pipeline") and _business_usage(node, m))
                    ),
                    None,
                )
                if hit:
                    counts[what] = counts.get(what, 0) + 1
                    if counts[what] <= max_reports:
                        findings.append(
                            finding(
                                severity,
                                label,
                                path,
                                f"{what}: {hit.group(0)!r}; the company has its own names, domains and words",
                            )
                        )

    visit(tree, "")
    for what, n in counts.items():
        if n > max_reports:
            findings.append(
                finding(
                    "error" if not what.startswith("sample") else "warning",
                    label,
                    "",
                    f"{what}: {n} occurrences in total",
                )
            )
    return findings


# --- Machine tells: shapes a generator leaves in text a person is supposed to have written ----
#
# Measured over the 60 seeded companies on disk. 77.9% of Gmail subjects and 52.5% of Drive
# filenames carry an em dash, nearly always the stamp "<Noun> reminder - 2026-05-14"; 45,478
# records (60,025 by the cohort audit's looser reading) repeat a sentence saying what the record
# does *not* mean ("This reminder carries no new case assignments"); 85.7% of calendar
# descriptions end "Organizer: <name>", 75% of them with an address after it, which the app
# already shows. None of it contradicts anything, so no other check sees it, and nearly all of
# it comes from the bulk layer, where one template stamps its skeleton on every record it
# expands: 371 of 400 Gmail specs carried an em dash and 283 of 1,842 specs a disclaimer.
#
# Every rule below needs a repeat or a share, never one sentence. Legal, clinical and editorial
# prose genuinely negates -- "The notice does not authorize burning garden waste", "A
# water-resistance statement does not mean the product stays effective indefinitely" -- and
# people do write an em dash in a subject line: across the fleet 313 self-negating sentences
# occur exactly once, and one calendar holds 44% hand-written em-dash titles, all different.
# What nobody does is write the same one twenty times. The thresholds sit in the gap the fleet
# leaves between the two: the largest hand-written em-dash repeat is 11 and the smallest stamp
# 50; 48 collections carry one self-negation apiece and the next value on disk is 12; quoted
# "From:/Subject:" headers reach 2.5% of a collection while the organizer stamps reach 33%.

EM_DASH = "—"
TELL_MIN_RECORDS = 20  # a shape is a habit only over a collection worth scrolling
STAMPED_TITLE_REPEATS = 20
DISCLAIMER_REPEATS = 3
LABEL_SHARE = 0.15
UNADDRESSED_SHARE = 0.15

SENTENCE = re.compile(r"[^.!?]+[.!?]?")
SELF_NEGATION = re.compile(
    # The record talking about itself, from the first word of the sentence, to deny what it
    # means. "A customer should explain whether the item ... does not work as expected" is a
    # person writing about the world and never matches; "This reminder does not change the case
    # record" is a template hedging about itself and always does.
    r"(?:this|that|these|those|the|a|an)\s+(?:[\w-]+\s+){0,3}"
    r"(?:reminders?|notifications?|notices?|messages?|e-?mails?|alerts?|digests?|bulletins?|"
    r"announcements?|exports?|statements?|receipts?|confirmations?|summar(?:y|ies)|entr(?:y|ies)|"
    r"logs?|files?|documents?|reports?|records?|tickets?|items?|updates?|snapshots?|refreshe?s?|"
    r"scans?)\b"
    r"[^.!?]{0,110}?\b(?:"
    r"(?:does|do|is|are|was|were|will|would|can|shall|should)\s+not\s+(?:\w+\s+){0,2}?"
    r"(?:mean|indicate|imply|constitute|confirm|establish|authori[sz]e|replace|supersede|"
    r"represent|create|change|amend|alter|report|record|register|guarantee|prove|reflect|"
    r"assign|contain|include|supply|extend|modify|affect|grant|approve|entitle|obligate|"
    r"commit|waive|determine)"
    r"|(?:carr(?:ies|y)|contains?|holds?|includes?)\s+no\b"
    r"|is\s+not\s+(?:an?\s+)?(?:evidence|proof|confirmation|approval|authori[sz]ation|"
    r"permission|substitute|replacement|guarantee|commitment|instruction|decision|record)\b"
    r")\b",
    re.IGNORECASE,
)
# What an app renders from structured data whatever its schema calls it. A label matching one of
# the collection's own field names counts too, so "Status:" in a ticket's own notes is caught
# without listing every schema's vocabulary here.
RENDERED_LABELS = frozenset(
    {
        "organizer",
        "organiser",
        "host",
        "attendees",
        "guests",
        "participants",
        "invitees",
        "location",
        "start",
        "end",
        "starttime",
        "endtime",
        "due",
        "duedate",
        "deadline",
        "assignee",
        "owner",
        "status",
        "priority",
        "calendar",
        "from",
        "to",
        "cc",
        "bcc",
        "subject",
        "sender",
        "recipient",
        "recipients",
        "filename",
        "folder",
        "created",
        "modified",
        "lastmodified",
        "duration",
        "meetinglink",
        "attachment",
        "attachments",
        "label",
        "labels",
    }
)
FIELD_LABEL = re.compile(
    r"(?:^|[.!?;)\]]\s+|\n\s*|\|\s*|<br\s*/?>\s*)([A-Za-z][A-Za-z ._/-]{1,22}):[ \t]*(?=\S)"
)


def _label_key(text):
    return re.sub(r"[\s._/-]", "", text).casefold()


def _plain(text):
    return re.sub(r"<[^>]+>", " ", text)


def _shape(text):
    """A title with its numbers masked: two records share a shape when a template stamped them."""
    return re.sub(r"\d+", "#", " ".join(text.split())).casefold()


def self_negations(text):
    """Digit-masked sentences in which a record denies what it means.

    Text after a sign-off is dropped the way ``check_texture`` drops it: a footer disclaimer
    under "Regards," repeats by nature and is the one place this sentence belongs.
    """
    for raw in SENTENCE.findall(_plain(SIGN_OFF.split(text, maxsplit=1)[0])):
        sentence = raw.strip()
        if SELF_NEGATION.match(sentence):
            yield _shape(sentence)


def restated_labels(record, own_fields=()):
    """Labels of structured fields written back into this record's own prose."""
    found = set()
    for key, value in record.items():
        if key not in PROSE_FIELDS or not isinstance(value, str):
            continue
        for match in FIELD_LABEL.finditer(_plain(value)):
            name = _label_key(match.group(1))
            if name in RENDERED_LABELS or name in own_fields:
                found.add(name)
    return found


def check_machine_tells(label, state, severity="warning", max_reports=MAX_REPORTS):
    """Repeated shapes that betray records as generated rather than written.

    Three rules, each over one collection of at least ``TELL_MIN_RECORDS`` records: a title
    shape carrying an em dash stamped on ``STAMPED_TITLE_REPEATS`` records or more; a sentence
    denying what the record means repeated across ``DISCLAIMER_REPEATS`` records; and a
    structured field restated in the prose of more than ``LABEL_SHARE`` of them.

    ``severity`` is the caller's judgement, not this rule's. A world being authored has an
    author to send the collection back to, so it is an error there; a bulk template already
    expanded on disk has none -- ``world_repair`` leaves "errors that appear only with the bulk
    records" unrepaired -- so it is a warning for the reviewer once it is laid.
    """
    findings, seen = [], Counter()

    def report(rule, path, message):
        seen[rule] += 1
        if seen[rule] <= max_reports:
            findings.append(finding(severity, label, path, message))

    for path, rows in record_collections(state, keyed=True):
        if len(rows) < TELL_MIN_RECORDS:
            continue
        stamps, denials, restated = Counter(), Counter(), 0
        own_fields = {_label_key(k) for r in rows[:200] for k in r}
        for record in rows:
            # Automation repeats its subject by design. Only exempt this title
            # heuristic when the sender or service record explicitly says so;
            # other prose, joins and source-event checks still apply.
            sender = record.get("from", {})
            sender_name = sender.get("name", "") if isinstance(sender, dict) else ""
            automation = bool(
                re.search(r"\b(?:notifications|automated|no.reply)\b", sender_name, re.IGNORECASE)
                or (
                    record.get("source") == "web"
                    and re.match(r"Automated customer self-service request\b", record.get("description", ""))
                )
            )
            for key in TITLE_FIELDS:
                value = record.get(key)
                if not automation and isinstance(value, str) and EM_DASH in value:
                    stamps[_shape(value)] += 1
            denials.update(
                {
                    sentence
                    for key, value in record.items()
                    if key in PROSE_FIELDS and isinstance(value, str)
                    for sentence in self_negations(value)
                }
            )
            # Drive content is the document itself. An invoice's "Size: L"
            # is a garment size, not a restatement of the file's byte size.
            # Its description remains subject to the metadata repetition rule.
            prose_record = (
                {k: v for k, v in record.items() if k != "content"}
                if label == "google_drive_mock"
                else record
            )
            restated += bool(restated_labels(prose_record, own_fields))
        if stamps and stamps.most_common(1)[0][1] >= STAMPED_TITLE_REPEATS:
            shape, count = stamps.most_common(1)[0]
            report(
                "stamped title",
                path,
                f"{count} records are titled {shape!r}; an em dash with a date or a serial after "
                "it is a stamp, not a subject line or a filename a person typed",
            )
        if denials and denials.most_common(1)[0][1] >= DISCLAIMER_REPEATS:
            shape, count = denials.most_common(1)[0]
            report(
                "self-negating disclaimer",
                path,
                f"{count} records repeat {shape!r}; a record says what it is, never what it is "
                "not, and no two people would word the denial identically",
            )
        if restated > LABEL_SHARE * len(rows):
            report(
                "restated field",
                path,
                f"{restated} of {len(rows)} records restate in prose a field the app already "
                'shows ("Organizer: ...", "Location: ..."); the text says what happened, the '
                "fields say who and when",
            )
    for rule, count in seen.items():
        if count > max_reports:
            findings.append(finding(severity, label, "", f"{rule}: {count} collections in total"))
    return findings


TEMPLATE_TITLE_FIELDS = TITLE_FIELDS | {"filename", "fileName", "displayName", "path", "headline"}


def _template_strings(node, key=None):
    if isinstance(node, dict):
        for name, value in node.items():
            yield from _template_strings(value, name)
    elif isinstance(node, list):
        for value in node:
            yield from _template_strings(value, key)
    elif isinstance(node, str) and key is not None:
        yield key, node


def template_tells(template):
    """The machine tells a bulk generator template stamps on every record it expands.

    ``check_machine_tells`` will not call a shape a stamp until it repeats, because one em dash
    or one denial is how people write. A template *is* that repeat by construction: one em dash
    in its subject is ``count`` of them on disk, and 371 of 400 Gmail specs carried one. So here
    a single occurrence is the finding, and it is caught before the records exist, where the
    spec call can still be asked again for a better one. Returns sentences for that call.
    """
    problems = []
    for key, value in _template_strings(template if isinstance(template, dict) else {}):
        if key in TEMPLATE_TITLE_FIELDS and EM_DASH in value:
            problems.append(
                f"{key} {value!r} joins its parts with an em dash; every record this spec writes "
                "gets the same punctuation. Name the record the way the person filing it would, "
                "and put the date in the record's own date field"
            )
        for sentence in self_negations(value):
            problems.append(
                f"{key} says {sentence!r}; that sentence lands on every record this spec writes. "
                "A record states what it is; it never denies what it is not"
            )
        if key in PROSE_FIELDS:
            restated = restated_labels({key: value})
            if restated:
                problems.append(
                    f"{key} restates {', '.join(sorted(restated))} in its prose; the app renders "
                    "those fields already. Say what happened and leave the structured fields to "
                    "the record"
                )
    return problems


def _names_account(value, keys):
    """Whether a party field (a string, a person object, or a list of either) names an account."""
    if isinstance(value, str):
        return value.strip().casefold() in keys
    if isinstance(value, dict):
        return any(_names_account(value.get(k), keys) for k in ("email", "id", "userId", "name"))
    if isinstance(value, list):
        return any(_names_account(v, keys) for v in value)
    return False


def check_addressing(label, state, identities=(), severity="warning"):
    """Mail, events and direct messages name someone who works here, or no one ever sees them.

    Every worker opens the same shared store filtered to the records that name them
    (``hub_identity.visible_to``, whose ``ACCOUNT_SCOPES`` this reads), so a message whose
    from/to/cc/bcc names no account of the app sits in no mailbox at all: bytes in browser
    storage no worker can open. 27,042 records across the fleet reach nobody that way -- 16.5%
    of everything carrying a party field, 19,140 of them expanded from bulk templates whose
    ``to`` was one fixed outside address. A record with no party field at all (an office-closed
    day, a system notice) stays visible to everyone and is not judged.
    """
    from .hub_identity import ACCOUNT_SCOPES, account_keys  # the runtime's own scope map

    if not isinstance(state, dict):
        return []
    keys = set()
    for record in identities:
        if isinstance(record, dict):
            keys |= account_keys(record)
    for key in ("user", "currentUser", "current_user", "account", "me", "profile", "viewer"):
        if isinstance(state.get(key), dict):
            keys |= account_keys(state[key])
    if not keys:  # no logged-in account and no identities: nothing to be a party to
        return []
    findings = []
    for name, fields in ACCOUNT_SCOPES.items():
        if name == "threads":  # a container; its visibility follows the messages inside it
            continue
        value = state.get(name)
        rows = [r for r in value if isinstance(r, dict)] if isinstance(value, list) else []
        addressed = [r for r in rows if any(r.get(f) not in (None, "", [], {}) for f in fields)]
        if len(addressed) < TELL_MIN_RECORDS:
            continue
        unseen = sum(
            not any(_names_account(r.get(f), keys) for f in fields if r.get(f) not in (None, "", [], {}))
            for r in addressed
        )
        if unseen > UNADDRESSED_SHARE * len(addressed):
            findings.append(
                finding(
                    severity,
                    label,
                    f"/{name}",
                    f"{unseen} of {len(addressed)} records name nobody who works here in "
                    f"{'/'.join(fields)}; each one is invisible to every worker, so put the "
                    "person whose inbox, calendar or conversation it is on the record",
                )
            )
    return findings


DOCUMENT_COLLECTIONS = {
    "documents",
    "docs",
    "pages",
    "articles",
    "kbArticles",
    "wikiPages",
}  # notes are short by nature


def check_documents(label, state, min_chars=300, max_thin_share=0.3):
    """A document is its text. Titles with empty bodies are placeholders, not documents.

    For every document-like collection with at least five records that carry a content or
    body field, more than ``max_thin_share`` of them under ``min_chars`` characters is an error.
    """
    findings = []
    for name, value in state.items() if isinstance(state, dict) else []:
        if name not in DOCUMENT_COLLECTIONS:
            continue
        rows = [r for r in records_of(value) if any(k in r for k in ("content", "body", "text"))]
        if len(rows) < 5:
            continue
        thin = sum(
            1
            for r in rows
            if len(re.sub(r"<[^>]+>", " ", str(r.get("content") or r.get("body") or r.get("text") or "")))
            < min_chars
        )
        if thin > max_thin_share * len(rows):
            findings.append(
                finding(
                    "error",
                    label,
                    f"/{name}",
                    f"{thin} of {len(rows)} documents have fewer than {min_chars} characters of content; a document is its text, write it out",
                )
            )
    return findings


def person_name(record):
    """A person's full name however the app spells it: one field, or first and last parts."""
    for key in ("fullName", "name", "displayName", "full_name", "display_name"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for first, last in (
        ("first_name", "last_name"),
        ("firstName", "lastName"),
        ("givenName", "familyName"),
        ("given_name", "family_name"),
    ):
        if record.get(first) or record.get(last):
            return f"{record.get(first) or ''} {record.get(last) or ''}".strip()
    for key in ("user_name", "username", "login"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def check_directory(label, state, max_reports=6):
    """Every person in an app's directory has a full name and an email address.

    First-name-only entries with blank emails ("Damon", "Q. Park") were the reviewer's most
    frequent finding; they make identities drift across apps and leave documents with owners
    nobody can write to. An error per collection with counts, so the repair pass fixes the
    directory in one go.
    """
    findings = []
    for name in USER_COLLECTIONS & set(state if isinstance(state, dict) else ()):
        rows = records_of(state[name])
        if not rows:
            continue
        has_emails = any("email" in r for r in rows)
        thin = []
        for r in rows:
            full = person_name(r)
            email = r.get("email") or ""
            if len(full.split()) < 2 or (has_emails and "@" not in str(email)):
                thin.append(str(r.get("id") or r.get("userId") or r.get("sys_id") or full))
        if thin:
            findings.append(
                finding(
                    "error",
                    label,
                    f"/{name}",
                    f"{len(thin)} of {len(rows)} directory entries lack a full name or an email ({', '.join(thin[:max_reports])}); every person has both",
                )
            )
    return findings


# --- Uniformity: bulk records copied from one template -------------------------------------
#
# Authoring-time rules only (``check_world(..., authoring=True)``): the add-bulk loop copies a
# record and changes ids and dates, so a collection ends up with one text skeleton, an amount
# column with two values, or the same 24-month series under several members. A world that is
# already accepted on disk is never re-judged by these; graders keep ``authoring_problems``
# apart from calibration for the same reason.

UNIFORM_SHARE = 0.30  # more than this share of a collection copied from templates is a defect
UNIFORM_MIN_RECORDS = 20  # skeletons are judged on collections at least this big
UNIFORM_MIN_ROWS = 50  # numeric columns are judged over at least this many rows
UNIFORM_MIN_DISTINCT = 5  # an amount column with fewer distinct values than this is fill
SERIES_PERIODS = 12  # a numeric series this long is a time series
AMOUNT_KEY = re.compile(
    r"amount|charge|cost|price|balance|fee|revenue|principal|recover|adjust|allowance|paid|payment|"
    r"receipt|spend|budget|conversion|click|impression|usd|credit|debit|discount|salary|wage|rent|"
    r"deposit|refund|total|sum|value|sales|income|expense|premium|outstanding|cash|invoice",
    re.IGNORECASE,
)
NOT_AMOUNT_KEY = re.compile(
    r"hours|minutes|count|days|_?id$|rate|percent|probab|score|rating|priority|level|version|index|"
    r"qty|quantity|number|zoom|width|height",
    re.IGNORECASE,
)
CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")
NUMBER_TEXT = re.compile(r"^-?\$?\d[\d,]*(?:\.\d+)?%?$")
COUNT_KEY = re.compile(r"^(\w+?)_?[cC]ount$")
CONVERSATION_COLLECTIONS = {"conversations", "threads", "chats", "dms"}


def _amount_like(name):
    return bool(AMOUNT_KEY.search(str(name))) and not NOT_AMOUNT_KEY.search(str(name))


def _numeric(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and NUMBER_TEXT.match(value.strip()):
        try:
            return float(value.strip().replace("$", "").replace(",", "").rstrip("%"))
        except ValueError:
            return None
    return None


def _series(value):
    """A numeric series of at least SERIES_PERIODS values, from a list or a comma-separated cell."""
    parts = value if isinstance(value, list) else value.split(",") if isinstance(value, str) else []
    if len(parts) < SERIES_PERIODS:
        return None
    numbers = [_numeric(p.strip() if isinstance(p, str) else p) for p in parts]
    return tuple(numbers) if all(n is not None for n in numbers) else None


def _skeleton(text):
    """Prose with its variable parts removed: numbers, dates and ids become '#', capitalised
    words (names, places, products) become '@'. Two records built from one template share it."""
    words = re.findall(r"[A-Za-z0-9'#$%./:-]+", re.sub(r"<[^>]+>", " ", text))
    out = []
    for word in words:
        if re.search(r"\d", word):
            out.append("#")
        elif word[:1].isupper():
            out.append("@")
        else:
            out.append(word.strip(".:,/").lower())
    return " ".join(w for w in out if w)


def _singular(name):
    return str(name).removesuffix("s").lower()


def record_collections(node, path="", keyed=False):
    """(path, records) for every collection of dict records in a state: lists of dicts, dicts
    of lists (Zendesk comments by ticket) and, with ``keyed``, dicts of records by id."""
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if isinstance(value, list) and value and all(isinstance(r, dict) for r in value[:5]):
            yield f"{path}/{key}", [r for r in value if isinstance(r, dict)]
        elif isinstance(value, dict) and value:
            lists = [v for v in value.values() if isinstance(v, list)]
            flat = [r for v in lists for r in v if isinstance(r, dict)]
            if lists and len(lists) >= len(value) / 2 and flat:
                yield f"{path}/{key}", flat
            elif keyed and sum(isinstance(v, dict) and "id" in v for v in value.values()) >= len(value) / 2:
                yield f"{path}/{key}", [v for v in value.values() if isinstance(v, dict)]
            else:
                yield from record_collections(value, f"{path}/{key}", keyed)


def sheet_tables(state):
    """(sheet path, header row, columns) for every spreadsheet sheet stored as a cell map.

    A column whose cells pack a CSV row (the ledger export in one cell) is split into its
    fields, named from the header cell when it splits the same way."""
    for path, sheet in records(state):
        data = sheet.get("data")
        if not isinstance(data, dict) or not any(CELL_REF.match(str(k)) for k in list(data)[:5]):
            continue
        rows = defaultdict(dict)
        for ref, cell in data.items():
            match = CELL_REF.match(str(ref))
            if match:
                rows[int(match.group(2))][match.group(1)] = (
                    cell.get("value") if isinstance(cell, dict) else cell
                )
        if len(rows) < 2:
            continue
        first = min(rows)
        header = rows[first]
        columns = {}
        for col in sorted({c for r in rows.values() for c in r}):
            cells = [(i, rows[i][col]) for i in sorted(rows) if i != first and col in rows[i]]
            texts = [(i, v) for i, v in cells if isinstance(v, str) and v.strip()]
            widths = Counter(v.count(",") for _, v in texts)
            width, n = widths.most_common(1)[0] if widths else (0, 0)
            if width >= 3 and n >= UNIFORM_MIN_ROWS:
                names = str(header.get(col, "")).split(",")
                for j in range(width + 1):
                    name = names[j].strip() if len(names) == width + 1 else f"{col}[{j}]"
                    columns[f"{col}[{name}]"] = [
                        (i, v.split(",")[j].strip()) for i, v in texts if v.count(",") == width
                    ]
            else:
                columns[f"{col}[{header.get(col, '')}]"] = cells
        raw = [(f"{col}{i}", rows[i][col]) for i in sorted(rows) if i != first for col in sorted(rows[i])]
        yield f"{path}", header, columns, raw


def _low_cardinality(name, values):
    """(distinct count, examples) when an amount column has too few values over enough rows."""
    if not _amount_like(name) or len(values) < UNIFORM_MIN_ROWS:
        return None
    numbers = [_numeric(v) for _, v in values]
    if any(n is None for n in numbers):
        return None
    distinct = set(numbers)
    if len(distinct) >= UNIFORM_MIN_DISTINCT or distinct <= {0.0, 1.0}:
        return None
    return len(distinct), sorted(distinct)


def check_uniformity(label, state, exclude_ids=frozenset(), share=UNIFORM_SHARE):
    """Bulk records must be produced from per-record varied parameters, not copied.

    Errors, naming the collection and example records: prose skeletons shared by more than
    ``share`` of a collection; an amount column with fewer than five distinct values over fifty
    rows (record fields and sheet columns, CSV-packed cells included); a time series of twelve
    or more periods that is identical in two records, or several constant series in one sheet;
    conversations that have no messages, and count fields that disagree with the children."""
    findings = []

    def rid(record):
        return str(record.get("id", record.get("sys_id", "?")))

    def report(path, message):
        findings.append(finding("error", label, path, message))

    def vary(what):
        return f"; {what}: vary the bulk parameters per record (amounts from a range, jittered dates, its own wording) instead of copying one template"

    collections = list(record_collections(state, keyed=True))
    if exclude_ids:  # the bulk layer repeats by design and is judged by its own specs
        collections = [
            (p, [r for r in rows if not (record_identifiers(r) & exclude_ids)]) for p, rows in collections
        ]
    for path, rows in collections:
        n = len(rows)
        # 1. Text skeletons.
        skeletons = []
        for r in rows:
            skeleton = _skeleton(
                " ".join(str(r[k]) for k in sorted(PROSE_FIELDS) if isinstance(r.get(k), str))
            )
            if len([w for w in skeleton.split() if w not in ("#", "@")]) >= 4:
                skeletons.append((skeleton, rid(r)))
        if len(skeletons) >= UNIFORM_MIN_RECORDS:
            counts = Counter(s for s, _ in skeletons)
            templated = sum(c for c in counts.values() if c >= 2)
            if templated > share * len(skeletons):
                top, top_n = counts.most_common(1)[0]
                examples = [i for s, i in skeletons if s == top][:4]
                report(
                    path,
                    f"{templated} of {len(skeletons)} records are copies of a few templates; {top_n} share "
                    f"the skeleton {top[:90]!r} (e.g. {', '.join(examples)})"
                    + vary("rewrite the collection"),
                )
        # 2. Amount fields with a handful of values.
        if n >= UNIFORM_MIN_ROWS:
            for key in sorted({k for r in rows for k in r}):
                values = [(rid(r), r[key]) for r in rows if r.get(key) is not None]
                low = _low_cardinality(key, values)
                if low:
                    report(
                        path,
                        f"{key} takes only {low[0]} distinct values {low[1]} over {len(values)} records "
                        f"(e.g. {', '.join(i for i, _ in values[:4])})"
                        + vary("draw each record's amounts from a realistic range"),
                    )
        # 3. Time series repeated across records or constant.
        _series_findings(rows, path, report, vary, rid)
    # Sheets: columns and cell series.
    for path, _header, columns, raw in sheet_tables(state):
        constant = []
        for name, values in columns.items():
            low = _low_cardinality(name, values)
            if low:
                report(
                    path,
                    f"column {name} takes only {low[0]} distinct values {low[1]} over {len(values)} rows "
                    f"(rows {values[0][0]}-{values[-1][0]})" + vary("give each row its own amounts"),
                )
            numbers = [_numeric(v) for _, v in values if v not in (None, "")]
            if (
                len(numbers) >= SERIES_PERIODS
                and all(x is not None for x in numbers)
                and len(set(numbers)) == 1
                and numbers[0] not in (0.0, 1.0)
            ):
                constant.append((name, len(numbers), numbers[0]))
        if len(constant) >= 2:
            report(
                path,
                "constant series: "
                + "; ".join(f"column {c} repeats {v:g} over {n} periods" for c, n, v in constant)
                + vary("let each period move"),
            )
        _series_findings([{"id": ref, "value": v} for ref, v in raw], path, report, vary, rid)
    # 4. Parents that claim children they do not have.
    for path, rows in collections:
        _children_findings(path, rows, collections, report, share)
    return findings


def _series_findings(rows, path, report, vary, rid):
    seen = defaultdict(list)
    for r in rows:
        for key, value in r.items():
            series = _series(value)
            if series:
                seen[series].append(f"{rid(r)}.{key}" if key != "value" else rid(r))
    repeated = [(refs, s) for s, refs in seen.items() if len(refs) >= 2]
    if repeated:
        refs, s = max(repeated, key=lambda item: len(item[0]))
        report(
            path,
            f"the same {len(s)}-period series {', '.join(f'{x:g}' for x in s[:6])}... appears in {len(refs)} places "
            f"({', '.join(refs[:4])}); {len(repeated)} series are shared like this"
            + vary("give each record its own history"),
        )
    constant = [refs[0] for s, refs in seen.items() if len(set(s)) == 1 and s[0] not in (0.0, 1.0)]
    if len(constant) >= 2:
        report(
            path,
            f"{len(constant)} series repeat one value across {SERIES_PERIODS}+ periods (e.g. {', '.join(constant[:4])})"
            + vary("let each period move"),
        )


def _children_findings(path, parents, collections, report, share):
    name = path.rsplit("/", 1)[-1]
    ids = {str(p.get("id")) for p in parents if p.get("id") is not None}
    if len(ids) < UNIFORM_MIN_RECORDS:
        return
    singular = _singular(name)
    ref_keys = (f"{singular}Id", f"{singular}_id", f"{singular}ID")
    for child_path, children in collections:
        if child_path == path:
            continue
        child_name = child_path.rsplit("/", 1)[-1]
        counts = Counter(str(c[k]) for c in children for k in ref_keys if c.get(k) is not None)
        if not counts.keys() & ids:
            continue  # no child of this kind points at these parents; the join is not real
        if name in CONVERSATION_COLLECTIONS and child_name in MESSAGE_COLLECTIONS:
            empty = sorted(i for i in ids if counts[i] == 0)
            if len(empty) > share * len(ids):
                report(
                    path,
                    f"{len(empty)} of {len(ids)} {name} have no {child_name} (e.g. {', '.join(empty[:4])}); "
                    f"write the exchange behind every {singular} or drop the shell",
                )
        for key in sorted({k for p in parents for k in p if COUNT_KEY.match(k)}):
            if _singular(COUNT_KEY.match(key).group(1)) != _singular(child_name):
                continue
            claimed = [
                (str(p["id"]), _numeric(p[key]))
                for p in parents
                if p.get("id") is not None and _numeric(p.get(key)) is not None
            ]
            # Parents with children or a positive count are the evidence; a marketplace total
            # above the stored sample is ordinary, children above the count are copies added
            # without the count being touched.
            claimed = [(i, v) for i, v in claimed if v > 0 or counts[i] > 0]
            wrong = [(i, int(v), counts[i]) for i, v in claimed if counts[i] > v]
            if claimed and len(wrong) > (share * 2 / 3) * len(claimed):
                report(
                    path,
                    f"{len(wrong)} of {len(claimed)} {name} hold more {child_name} than their {key} says "
                    f"(e.g. {'; '.join(f'{i}: {key} {v}, {child_name} {c}' for i, v, c in wrong[:3])}); "
                    "remove the duplicated exchanges or make the count match the records",
                )


# A collection's records carry the field names the app reads, or the app renders nothing there.
# validate_state checks top-level keys only, so a seed can rename every field inside a collection
# and still pass: microsoft_teams was seeded `id`/`name` where the app reads `userId`/`displayName`,
# mailchimp's `user` arrived as a four-element array against `state.user.firstName[0]`, linear's
# workflowStates carried `type` where the app reads `category`, and salesforce's Chatter posts
# carried `createdAt` where the app reads `createdDate` and stamped every post at page-load time.
# The app's own records are the authority, captured by scripts/app_schema_drift.py.
MIN_RECORDS_FOR_FIELDS = 3
FIELD_REPORTS = 4
RECORD_FIELDS_PATH = Path(__file__).parent.parent.parent.parent / "catalogs/app_record_fields.json"


# What decides a mechanical verdict: this checker and the field contract it reads. The same two
# files are scripts/batch_companies.py's CHECK_CODE, and they have to stay the same two: the digest
# recorded in a CHECKS.json is compared against this tuple, so a file that decides the verdict and is
# missing here is a fix that reaches no world on disk -- four changes to this module on 2026-09-09
# re-dated none of the 60 CHECKS.json, which is the defect this records against.
DECIDES_THIS = (Path(__file__), RECORD_FIELDS_PATH)


def current(marker):
    """Whether a CHECKS.json was decided by the checker and the field contract as they stand now.

    The driver's accept slot for this marker, bulk_layer.current's contract: the module that writes a
    verdict is the only thing that knows which files decide it, so it answers and the driver only
    asks. A marker with no ``decided_by`` at all is stale -- every one of the 60 on disk on
    2026-09-10, which is what the migration costs and why it was done during the freeze.
    """
    from company_envs.storage import decided_by

    return isinstance(marker, dict) and marker.get("decided_by") == decided_by(DECIDES_THIS)


def app_record_fields(path=None):
    """Per app, the field names its own records carry, by collection; empty when unrecorded."""
    path = Path(path) if path else RECORD_FIELDS_PATH
    if not path.is_file():
        return {}
    return json.loads(path.read_text()).get("apps", {})


SCHEMA_DIR = Path(__file__).parent.parent.parent.parent / "catalogs/app_schemas"
DERIVED_WORDS = re.compile(
    r"auto-derived|derived from|if omitted|optional|computed|defaults? to", re.IGNORECASE
)


@cache
def schema_field_notes(app_id):
    """Fields the pinned schema names, and those it says the app fills in when they are omitted.

    A field the document tells the author to write is not invented, and a field the document calls
    derived is not missing. Without this the check reported the seed for obeying its own
    instructions: google_calendar's isDefault, recurring, reminders, meetLink and status are all
    documented, and gmail's snippet is documented as auto-derived from the body, yet both were
    reported against every world that had them right.
    """
    path = SCHEMA_DIR / f"{app_id}.md"
    if not path.is_file():
        return frozenset(), frozenset()
    named, derived = set(), set()
    for line in path.read_text(errors="replace").splitlines():
        fields = set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", line))
        named |= fields
        if DERIVED_WORDS.search(line):
            derived |= fields
    return frozenset(named), frozenset(derived)


def check_record_fields(label, state, shapes, severity="warning"):
    """Seeded records should carry the fields the app's own records carry.

    A field the app never has is one the app cannot read. A field the app always has and the seed
    never does is a blank column, a zero, or an undefined the page prints. Only collections the
    app itself held records for are judged: with nothing to compare against there is no contract.
    """
    findings = []
    for key, shape in sorted((shapes or {}).items()):
        expected = set((shape or {}).get("record_fields") or [])
        if not expected or key not in state:
            continue
        records = [r for r in records_of(state[key]) if isinstance(r, dict)]
        if len(records) < MIN_RECORDS_FOR_FIELDS:
            continue
        seen = {field for record in records for field in record}
        # A field most records carry, not one a single record happens to have.
        common = {f for f in seen if sum(f in r for r in records) * 2 > len(records)}
        named, derived = schema_field_notes(label)
        # A field the pinned schema told the author to write is a schema-versus-app mismatch, which
        # app_schema_drift reports; it is not the seed inventing something.
        invented = sorted(common - expected - named)
        missing = [f for f in expected if f not in derived and not any(f in r for r in records)]
        # Two different defects wore one sentence. ``expected`` is captured from each app's own demo
        # records, so it holds fields no schema document mentions -- and the author is asked for the
        # fields the schema documents. A field the schema names and the seed skipped is the seed's;
        # a field only the app's records have is the schema's, and no author could have known to
        # write it. Measured over the 406 seeded states on disk: 139 absent-field findings, 90 of
        # them naming a field no schema document mentions. google_calendar's ``textColor`` alone is
        # 56 of the 60 worlds -- the pinned Calendar object documents id, name, color, visible,
        # userId and isDefault, never textColor, and a calendar served without it does not get it
        # back from the app either. Both are still reported, because meta_ads is the case that says
        # why: its schema names none of ``callToAction`` or ``mediaItems``, and its app reads both,
        # so silencing the undocumented half would drop a real defect. They are told apart instead.
        absent = sorted(f for f in missing if not named or f in named)
        undocumented = sorted(f for f in missing if named and f not in named)
        if invented:
            findings.append(
                finding(
                    severity,
                    label,
                    key,
                    f"{len(records)} records carry {invented[:FIELD_REPORTS]}, which the app's own "
                    f"records do not have, so the app reads nothing there",
                )
            )
        if absent:
            findings.append(
                finding(
                    severity,
                    label,
                    key,
                    f"no record carries {absent[:FIELD_REPORTS]}, which every record in the app's "
                    f"own state has",
                )
            )
        if undocumented:
            findings.append(
                finding(
                    severity,
                    label,
                    key,
                    f"no record carries {undocumented[:FIELD_REPORTS]}, which every record in the "
                    f"app's own state has and the pinned schema document does not name: a "
                    f"schema-versus-app gap the seed was never asked to fill",
                )
            )
    return findings


def records_of(value):
    """The records of a collection: a list, a map keyed by id, or a map of lists.

    The map-of-lists shape was returning ``[]``, so every check reading a collection through here
    skipped the biggest collection in the fleet: slack's ``messages`` are stored by channel
    (``{channelId: [message...]}``) and so are Zendesk's comments by ticket -- 37,711 records
    across the seeded worlds, the largest collection in every world that has one.
    """
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)]
    if isinstance(value, dict):
        return [r for v in value.values() for r in (v if isinstance(v, list) else [v]) if isinstance(r, dict)]
    return []


def check_world(
    world,
    states,
    identities,
    company_workers,
    reference_date,
    materials=None,
    bulk_ids=None,
    *,
    worker_apps=None,
    authoring=False,
):
    """Run every check; errors block, warnings are handed to the reviewer.

    ``authoring`` adds the uniformity rules, which judge a world as it is being written and
    never re-judge one already accepted on disk, and raises the machine-tell, addressing,
    structural, reachability, count and attribution rules from warning to error: while the app
    author is still in the repair loop those collections can be rewritten, and once the bulk layer
    is laid over them no repair path reaches the template that stamped them. The same switch is
    what keeps a rule that lands after a world was seeded from condemning it to a revision nobody
    can make: 19,768 of the 20,155 unreachable drive items are bulk records, which no author owns,
    while all 4,706 records the reachability rule condemns in the human layers and all 685 records
    whose declared child count the state contradicts are the author's own.

    ``worker_apps`` is the worker -> app grant (``world/worker_apps.json``); without it the
    attribution check has no contract to read and does not run."""
    findings = []
    record_fields = app_record_fields()
    findings += check_duplicates("world", world)
    findings += check_literal_records("world", world)
    findings += check_leaks("world", world)
    findings += check_arithmetic("world", world)
    findings += check_chronology("world", world, reference_date)
    tell_severity = "error" if authoring else "warning"
    for app_id, state in states.items():
        findings += check_duplicates(app_id, state)
        findings += check_literal_records(app_id, state)
        findings += check_communications(app_id, state)
        findings += check_machine_tells(app_id, state, severity=tell_severity)
        findings += check_addressing(
            app_id,
            state,
            [apps[app_id] for apps in identities.values() if app_id in apps],
            severity=tell_severity,
        )
        findings += check_texture(app_id, state, exclude_ids=frozenset((bulk_ids or {}).get(app_id, ())))
        if authoring:
            findings += check_uniformity(
                app_id, state, exclude_ids=frozenset((bulk_ids or {}).get(app_id, ()))
            )
        findings += check_leaks(app_id, state)
        findings += check_documents(app_id, state)
        findings += check_directory(app_id, state)
        findings += check_timestamps(app_id, state, reference_date)
        findings += check_arithmetic(app_id, state)
        findings += check_chronology(app_id, state, reference_date)
        findings += check_references(app_id, state, severity=tell_severity)
        findings += check_containment(app_id, state, severity=tell_severity)
        findings += check_totals(app_id, state, severity=tell_severity)
        findings += check_reachability(app_id, state, severity=tell_severity)
        findings += check_counts(app_id, state, severity=tell_severity)
        findings += check_actor_logins(app_id, state, identities, worker_apps, severity=tell_severity)
        findings += check_record_fields(
            app_id, state, {key: {"record_fields": f} for key, f in (record_fields.get(app_id) or {}).items()}
        )
        # Only the workers who hold a login here. The identity book is deliberately complete -- it is
        # what lets a mined task widen a grant without re-seeding -- but asking the author to put a
        # non-holder into the app's own directory contradicts the access rule the same prompt states,
        # and cost one repair round per world doing it. 99 of the 149 surplus identities are
        # physically in a directory across 31 worlds.
        findings += check_state_identities(
            app_id,
            state,
            {
                worker: apps[app_id]
                for worker, apps in identities.items()
                if app_id in apps and (not worker_apps or app_id in (worker_apps.get(worker) or ()))
            },
        )
    # The rule exists for the documents a worker reads, and never ran over them: 19 companies' desktop
    # files carry harness vocabulary and every one passes the gate today. An error only while the
    # author is still in the loop, because the check stage's repair does not rewrite materials --
    # only the review stage's does -- so an error here would strand those 19 with no route.
    for path, text in (materials or {}).items():
        for leak in check_leaks(f"materials:{path}", text):
            findings.append({**leak, "severity": "error" if authoring else "warning"})
    findings += check_timestamps("world", world, reference_date)
    findings += check_identities(identities, company_workers)
    findings += check_facts(world, states)
    findings += check_plain_language(
        "prose",
        prose_texts(states, materials),
        proper_names=proper_name_tokens(world, states, company_workers),
    )
    errors = [f for f in findings if f["severity"] == "error"]
    return {
        "ok": not errors,
        "errors": len(errors),
        "warnings": len(findings) - len(errors),
        "findings": findings,
    }


def check_folder(
    root,
    folder,
    states=None,
    bulk_ids=None,
    *,
    world=None,
    identities=None,
    materials=None,
    reference_date=None,
    worker_apps=None,
    authoring=False,
):
    """Check a company folder the way every stage must: all app states named in apps.json
    (a missing state file is an error, never a skipped app), identities, materials, and the
    bulk layer excluded from texture.

    The keyword overrides stand in for files a stage has not written yet (seeding checks the
    world it is about to write) or serves from memory (the controller's ``states``). The app
    list, the company workers and the bulk exclusion always come from the folder, so a world
    that passes here at seed time passes the same call at the check stage.

    ``worker_apps`` is read from ``world/worker_apps.json``, and before that file exists --
    seeding writes it only after this check runs -- from the task contract the seed builds it
    from, so the attribution check is live in the one round where an author can still act on it.
    Without either there is no grant to read and the check stays silent rather than guessing."""
    from pathlib import Path

    from company_envs.storage import decided_by, read

    from .bulk_layer import bulk_ids_for

    root, folder = Path(root), Path(folder)
    apps = read(folder / "apps.json")
    if states is None:
        states = {}
        for app in apps["apps"]:
            name = app.get("state_file") or f"world/{app['app_id']}.state.json"
            path = folder / name
            if not path.is_file():
                raise FileNotFoundError(f"{app['app_id']}: missing state file {name}")
            states[app["app_id"]] = read(path)
    if identities is None:
        identities_path = folder / apps.get("identities_file", "world/identities.json")
        identities = read(identities_path) if identities_path.is_file() else {}
    if reference_date is None:
        seed_path = folder / "world" / "SEED.json"
        seed = read(seed_path) if seed_path.is_file() else {}
        core_path = folder / "world" / "CORE.json"
        if not seed and core_path.is_file():
            seed = read(core_path).get("core_metadata", {})
        reference_date = seed.get("reference_date", "2100-01-01")
    if worker_apps is None:
        worker_apps_path = folder / "world" / "worker_apps.json"
        if worker_apps_path.is_file():
            worker_apps = read(worker_apps_path)
        else:
            from .state_seed import STANDARD_APPS, contract_worker_apps

            contract = contract_worker_apps(folder)
            here = {app["app_id"] for app in apps["apps"]}
            worker_apps = (
                {w: sorted((set(named) | STANDARD_APPS) & here) for w, named in contract.items()}
                if contract
                else {}
            )
    if materials is None:
        materials_dir = folder / "world" / "materials"
        materials = (
            {
                str(p.relative_to(materials_dir)): p.read_text(errors="replace")
                for p in materials_dir.rglob("*")
                if p.is_file()
            }
            if materials_dir.is_dir()
            else {}
        )
        amendment_path = folder / "world/population/AMENDMENTS.json"
        if amendment_path.exists():
            from .population_amendments import corrected_materials
            from .seed_calls import WorkerMaterial

            entries = [
                WorkerMaterial(worker_id=k.split("/", 1)[0], path=k.split("/", 1)[1], content=v)
                for k, v in materials.items()
            ]
            materials = {f"{m.worker_id}/{m.path}": m.content for m in corrected_materials(folder, entries)}
    if world is None:
        world = read(folder / "world" / "world.json")
        extension_path = folder / "world/population/EXTENSION.json"
        if extension_path.is_file():
            from .population import additive_world

            world = additive_world(world, read(extension_path))
        from .population_amendments import corrected_world

        world = corrected_world(folder, world)
    if bulk_ids is None:
        bulk_ids = bulk_ids_for(root, folder)
        # The staged runner has per-app provenance, not the legacy BULK marker.
        if not (folder / "world/BULK.json").exists():
            for app_id in states:
                entry = folder / "world/population" / f"{app_id}.json"
                if entry.is_file():
                    bulk_ids[app_id] = set(read(entry).get("bulk_ids", []))
    result = check_world(
        world,
        states,
        identities,
        read(folder / "company.json").get("workers", []),
        reference_date,
        materials=materials,
        bulk_ids=bulk_ids,
        worker_apps=worker_apps,
        authoring=authoring,
    )
    result["apps_checked"] = sorted(states)
    result["decided_by"] = decided_by(DECIDES_THIS)
    return result


def check_cross_company_identities(companies_dir):
    """A person (name or email) must belong to exactly one company in the corpus."""
    from pathlib import Path

    from company_envs.storage import read

    owners = {}
    findings = []
    for path in sorted(Path(companies_dir).glob("*/world/identities.json")):
        company = path.parents[1].name
        try:
            identities = read(path)
        except (OSError, ValueError):
            continue
        seen_here = set()
        for worker, apps in identities.items():
            for rec in apps.values():
                keys = [rec.get(k) for k in ("name", "fullName", "email") if isinstance(rec.get(k), str)]
                for key in keys:
                    norm = normalize(key)
                    if norm in seen_here:
                        continue
                    seen_here.add(norm)
                    if norm in owners and owners[norm] != company:
                        findings.append(
                            finding(
                                "error",
                                "corpus",
                                f"/{company}/{worker}",
                                f"{key!r} also belongs to {owners[norm]}",
                            )
                        )
                    owners.setdefault(norm, company)
    return {
        "ok": not findings,
        "companies": len({p.parents[1].name for p in Path(companies_dir).glob("*/world/identities.json")}),
        "findings": findings,
    }
