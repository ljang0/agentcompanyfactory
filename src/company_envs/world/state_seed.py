"""Stage 2 for hub companies: the company's world as validated app state files.

Call 1 (``world_core``) produces the canonical world, worker identities, worker-app access and
desktop materials from the dossier, optional tasks and the app contract. ``app_state`` calls
populate bounded collection groups, with apps run concurrently. Each merged state is validated
against its schema table before anything is written to ``world/``. Nothing here grades a task.

The call schemas and skill loader live in ``seed_calls``; the review loop in ``world_review``.
Both are re-exported here so existing imports keep working.
"""

import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from functools import cache
from pathlib import Path, PurePosixPath

from company_envs.config import load_config
from company_envs.models import PROMPT_CEILING, Models, PromptTooLarge
from company_envs.receipt import Receipt
from company_envs.schemas import STANDARD_APPS
from company_envs.storage import digest, now, read, write

from .hub_app import UI_STATE_KEYS, validate_state
from .json_syntax import escape_csv_quotes
from .seed_calls import (
    AppStateResult,
    IdentitiesOnly,
    Identity,
    JsonRepair,
    WorkerMaterial,
    WorldCore,
    load_skill,
    skill_for_call,
)
from .world_check import (
    USER_COLLECTIONS,
    app_record_fields,
    check_folder,
    check_identities,
    check_literal_records,
    check_state_identities,
    finding,
    identity_members,
    person_name,
    splice_identities,
)
from .world_review import (
    MaterialsOnly,
    WorldReview,
    app_mentions,
    bound_states,
    cited_ids,
    repair_materials,
    review_world,
)

# Compatibility surface: names that moved to seed_calls / world_review but are imported from here.
__all__ = [
    "STANDARD_APPS",
    "AppStateResult",
    "IdentitiesOnly",
    "Identity",
    "JsonRepair",
    "MaterialsOnly",
    "WorkerMaterial",
    "WorldCore",
    "WorldReview",
    "app_mentions",
    "bound_states",
    "load_skill",
    "repair_materials",
    "seed_world",
    "skill_for_call",
]


REPAIR_INSTRUCTIONS = (
    "The text below was meant to be one JSON object but does not parse. Return it as valid JSON in "
    "fixed_json, preserving every record and value; fix only syntax (quotes, commas, brackets, "
    "escapes, duplicated keys). Do not summarize, drop or invent records. The bracketed line below "
    "names the text; it is not part of the object. Return the object itself, with its own top-level "
    "keys, never wrapped in a key of your own.\n\n"
)


def repair_json(text, what, models, expected=None):
    """One bounded model call to fix syntax in model-written JSON, then parse strictly.

    ``expected`` names the top-level keys the object must have. Without them the repair model
    had nothing to check its answer against and wrapped the payload in the label it was given.
    """
    if len(text) > 250_000:
        raise ValueError(f"{what}: JSON repair input exceeds the 250000-character limit")
    wanted = f"The object's top-level keys are exactly {sorted(expected)}.\n\n" if expected else ""
    fixed, _ = models.call("world_states", REPAIR_INSTRUCTIONS + wanted + f"[{what}]\n" + text, JsonRepair)
    return fixed.fixed_json


def unwrap_label(value, what, expected=None):
    """The payload itself when the repair model wrapped it in the label the prompt gave it.

    ``repair_json`` heads the text with ``[<what>]``; the repair model answered
    ``{"google_drive_mock collection group": {...}}`` -- the label as the only top-level key --
    and ``parse_json`` accepts any object, so the envelope reached the group check as a collection
    named after the prompt ("expected ['items'], got ['google_drive_mock collection group']") and
    cresa died at three strikes on it. One known label wrapping one object is an envelope, not a
    collection: unwrap it. A single-key object whose key is a real collection is left alone.
    """
    if not isinstance(value, dict) or len(value) != 1:
        return value
    ((key, inner),) = value.items()
    if not isinstance(inner, dict) or not isinstance(key, str):
        return value
    if expected is not None and key not in expected and set(inner) == set(expected):
        return inner
    labels = {what.strip().casefold(), f"[{what}]".casefold()}
    return inner if key.strip().casefold() in labels else value


def distinct_identity_errors(identities, apps):
    """Every worker must be a different person in every app: distinct ids and emails."""
    errors = []
    for app_id in apps:
        seen = {}
        for worker, recs in identities.items():
            rec = recs.get(app_id)
            if not rec:
                continue
            for field in ("id", "userId", "email", "name", "displayName"):
                value = rec.get(field)
                if value is None:
                    continue
                normalized = " ".join(str(value).casefold().split())
                if field in ("name", "displayName"):
                    # A shared desk is still one identity when the author adds
                    # a worker id or a numeric suffix to its display name.
                    suffixes = "|".join(re.escape(w.casefold()) for w in identities)
                    normalized = re.sub(rf"[\s_#-]+(?:{suffixes}|\d+)$", "", normalized)
                if not normalized:
                    continue
                key = (field, normalized)
                if key in seen and seen[key] != worker:
                    errors.append(f"{app_id}: {worker} and {seen[key]} share {field}={value!r}")
                seen.setdefault(key, worker)
    return errors


def repair_identities(core, apps, workers, company, feedback, models, instructions, people=None):
    """One small call that rewrites only the identities; the rest of the core is kept.

    ``people`` is the canonical world's own record of each worker (``canonical_people``). Without
    it the call invented six new employees for jabil and the Pennsylvania DGS while the world's
    staff, documents and messages kept the original six; every app then carried two casts.
    """
    payload = {
        "call": "identities_repair",
        "feedback": feedback,
        "workers": [w for w in company.get("workers", []) if w.get("id") in workers],
        "apps": [
            {"app_id": a, "identity_key": apps[a].get("identity_key")}
            for a in apps
            if apps[a].get("identity_key")
        ],
        "previous_identities": [i.model_dump() for i in core.identities],
        **({"canonical_people": people} if people else {}),
        "rule": (
            "Return identities only: one distinct person per worker per app, matching the worker's "
            "name and title, unique ids and emails within each app. Do not return shared desk accounts."
            + (
                " Each worker is the person canonical_people names for them: use exactly that name and "
                "email in every app; only the ids and app-specific fields are yours to write."
                if people
                else ""
            )
        ),
    }
    fixed, _ = models.call(
        "world_states", instructions + "\n" + json.dumps(payload, ensure_ascii=False), IdentitiesOnly
    )
    return core.model_copy(update={"identities": fixed.identities})


NAME_FIELDS = ("name", "fullName", "displayName", "full_name", "display_name", "realName", "real_name")
NAME_PARTS = (("firstName", "lastName"), ("first_name", "last_name"), ("givenName", "familyName"))


def canonical_people(world, workers):
    """The canonical world's own record of each worker: ``{worker_id: {"name", "email"}}``.

    ``world_core`` writes the staff into ``entities_json`` under the worker ids (or with a
    ``worker_id``), and every app author copies those records into directories, documents and
    messages. That makes them the one source an identity can be derived from; an identity
    naming anyone else splits the worker into two people. A person two workers both point at
    is ambiguous and left out, so the distinctness gate keeps its say.
    """
    wanted = set(workers)
    people = {}

    def visit(node):
        if isinstance(node, dict):
            for key in ("worker_id", "workerId", "id"):
                wid = node.get(key)
                if isinstance(wid, str) and wid in wanted and wid not in people:
                    name, email = person_name(node), node.get("email")
                    email = email.strip() if isinstance(email, str) and "@" in email else None
                    if name or email:
                        people[wid] = {"name": name or None, "email": email}
                    break
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(world)
    claims = Counter(p[k].casefold() for p in people.values() for k in ("name", "email") if p[k])
    return {w: p for w, p in people.items() if all(not p[k] or claims[p[k].casefold()] == 1 for k in p)}


def reconcile_identities(identities, people):
    """Make each worker's identity records that worker's canonical person, in place.

    Name and email fields the record already carries (whole names, first/last parts, and the
    same fields one level down as in Klaviyo's ``account.user``) take the canonical values; ids
    and app-specific fields are kept. Returns the ids of the workers whose records changed.
    """
    changed = set()

    def apply(record, person):
        touched = False
        name, email = person.get("name"), person.get("email")
        for key in NAME_FIELDS:
            if name and isinstance(record.get(key), str) and record[key] != name:
                record[key] = name
                touched = True
        for first, last in NAME_PARTS:
            if name and (first in record or last in record):
                parts = name.split()
                head, tail = parts[0], " ".join(parts[1:])
                if record.get(first) != head or record.get(last) != tail:
                    record[first], record[last] = head, tail
                    touched = True
        if email and isinstance(record.get("email"), str) and record["email"] != email:
            record["email"] = email
            touched = True
        for value in record.values():
            if isinstance(value, dict) and (any(k in value for k in NAME_FIELDS) or "email" in value):
                touched = apply(value, person) or touched
        return touched

    for worker, apps in identities.items():
        person = people.get(worker)
        if not person:
            continue
        for record in apps.values():
            if isinstance(record, dict) and apply(record, person):
                changed.add(worker)
    return changed


SEED_SECTIONS = ("State Schema", "Default IDs", "Default Data Summary", "State Normalization")


PLATFORM_URL = re.compile(r"https?://[^\s)\]\"'<>]*(?:xlang\.ai|cua-gym|picsum\.photos)[^\s)\]\"'<>]*")


def seed_schema(document):
    """The part of an app's SCHEMA.md the seeding model needs: the state tables, nothing else.

    A hub schema also documents the state API (POST /post, GET /state), routes, reducer actions,
    a "Minimal Inject Example" full of placeholder people, and "Observable State Changes (for LLM
    evaluation)". Shown that, the model wrote platform links, example.com addresses and the
    sample users into company data, and wrote for the evaluator. Only the sections that describe
    the data shape are kept; platform URLs in them are blanked.
    """
    parts = re.split(r"(?m)^(?=## (?!#))", document)
    kept = [part for part in parts if any(part.startswith(f"## {name}") for name in SEED_SECTIONS)]
    if not kept:  # a schema without the standard headings: drop only the sections known to mislead
        kept = [
            part
            for part in parts
            if not re.match(r"## (Minimal Inject Example|Observable State Changes|Routes|API Endpoint)", part)
        ]
    text = "".join(kept)
    # "### Default user IDs: user_1 (John Smith) ..." are the demo's sample values, not shape.
    text = re.sub(r"(?ms)^### Default [^\n]*\n.*?(?=^###? |\Z)", "", text)
    return PLATFORM_URL.sub("<link>", text).strip() + "\n"


def app_contract(root, folder):
    apps = read(folder / "apps.json")
    contract = []
    for app in apps["apps"]:
        if len(app["top_level_keys"]) != len(set(app["top_level_keys"])):
            raise ValueError(f"{app['app_id']} has duplicate collection names")
        if not app.get("hub_seedable"):
            raise ValueError(f"{app['app_id']} is not seedable through the hub contract")
        contract.append(
            {
                "app_id": app["app_id"],
                "role": app.get("role", "task"),
                "top_level_keys": app["top_level_keys"],
                "identity_key": app.get("identity_key"),
                "schema_document": seed_schema((root / app["schema"]).read_text()),
            }
        )
    return apps, contract


NAME_REGISTRY = "person_names.json"


def registry_path(companies_dir):
    return Path(companies_dir).parent / "data" / NAME_REGISTRY


def read_registry(companies_dir):
    """Names and emails claimed so far, each with the company that claimed it first."""
    path = registry_path(companies_dir)
    try:
        return read(path)
    except (OSError, ValueError):
        return {"names": {}, "emails": {}}


def register_people(companies_dir, company_id, names, emails=()):
    """Claim names for a company the moment they exist, so concurrent seeds avoid them.

    Finished worlds are read too, but sixty seeds started together see none of each other
    that way; the registry is the shared memory. First claim wins; later ones are ignored.
    """
    path = registry_path(companies_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    with open(lock, "w") as handle:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX)
        registry = read_registry(companies_dir)
        counts = registry.setdefault("counts", {})
        for name in names:
            if looks_like_person(name):
                name = name.strip()
                if registry["names"].setdefault(name, company_id) != company_id:
                    counts[name] = counts.get(name, 1) + 1  # reused by another company
        for email in emails:
            if isinstance(email, str) and "@" in email:
                registry["emails"].setdefault(email.strip().casefold(), company_id)
        write(path, registry)
        fcntl.flock(handle, fcntl.LOCK_UN)
    return registry


PARTICLES = {"de", "van", "von", "da", "di", "la", "le", "del", "der", "bin", "al"}
# Words that make a capitalised phrase the name of a thing, not of a person. "Balance Sheet",
# "Supplier ID", "US Operations", "Amazon Web Services", "Juniper Court" and "Dispatch Desk" all
# pass every other test here: two or three capitalised words and no digits.
NOT_A_NAME = {
    "balance",
    "bills",
    "building",
    "cleaning",
    "compensation",
    "court",
    "desk",
    "detail",
    "details",
    "directory",
    "exchange",
    "form",
    "forms",
    "id",
    "ids",
    "index",
    "inbox",
    "invoice",
    "invoices",
    "ledger",
    "list",
    "lists",
    "log",
    "logs",
    "operations",
    "payments",
    "payroll",
    "queue",
    "register",
    "report",
    "reports",
    "service",
    "services",
    "sheet",
    "sheets",
    "source",
    "sources",
    "staff",
    "summary",
    "supplier",
    "suppliers",
    "support",
    "team",
    "teams",
    "template",
    "trial",
    "unpaid",
    "vendor",
    "vendors",
    "web",
}
# Keys whose value is a person or a list of people, whatever that person's record looks like.
PERSON_CONTAINERS = {
    "users",
    "members",
    "people",
    "accounts",
    "employees",
    "workers",
    "staff",
    "contacts",
    "agents",
    "requesters",
    "assignees",
    "attendees",
    "guests",
    "recipients",
    "participants",
    "collaborators",
    "followers",
    "user",
    "person",
    "profile",
    "currentUser",
    "from",
    "to",
    "cc",
    "bcc",
    "sender",
    "author",
    "owner",
    "organizer",
    "requester",
    "assignee",
    "submitter",
    "createdBy",
    "updatedBy",
    "reporter",
    "manager",
    "customer",
    "client",
    "contact",
}
# Words that make a collection a collection of people whatever its records look like. The canonical
# world names its own containers, so the set is open: "panel", "active_patients", "owners" and an
# Airtable "fld_a_owner" hold people with nothing but an id and a name, while "companies",
# "properties", "vendors", "customers", "matters", "teams", "hotels" and "rooms" hold things whose
# names read exactly like people -- "Cairn Passage Investments", "Merriwell Terrace".
PERSON_CONTAINER_WORDS = (
    "patient",
    "owner",
    "panel",
    "member",
    "person",
    "people",
    "resident",
    "tenant",
    "employee",
    "worker",
    "contact",
    "guest",
    "attendee",
    "applicant",
    "candidate",
    "participant",
    "student",
    "staff",
    "caregiver",
    "nurse",
    "physician",
    "volunteer",
    "subscriber",
    "roster",
)
# Fields only a person has. A report has an id and a name; it has no mailbox and no job.
PERSON_ATTRIBUTES = (
    "email",
    "emailAddress",
    "mail",
    "jobTitle",
    "job_title",
    "initials",
    "avatar",
    "handle",
    "username",
    "employeeId",
    "employee_id",
    "reportsTo",
    "mobile",
)


def looks_like_person(value):
    """'Marisol Vega' yes; 'Waiting for reply', 'Office administration', 'Balance Sheet' no."""
    if not isinstance(value, str):
        return False
    words = value.strip().split()
    if not 1 < len(words) <= 3 or any(ch.isdigit() for ch in value) or any(ch in value for ch in ":—/()[]@#"):
        return False
    if any(w.strip(".,").lower() in NOT_A_NAME for w in words):
        return False
    return (
        all(w[:1].isupper() or w.lower() in PARTICLES for w in words)
        and words[0][:1].isupper()
        and words[-1][:1].isupper()
    )


def is_person_record(record, container=""):
    """Whether a record with a person-shaped name is a person, or a thing that reads like one.

    Every name here is claimed in the cross-company registry and renamed when another company
    claimed it first, so a thing caught by this becomes a person: 38 of the 202 renames on disk
    gave a human name to a QuickBooks report ("Balance Sheet", "General Ledger", "Unpaid Bills"),
    an Airtable field ("Supplier ID", "Observation ID"), a team ("People Operations"), a building
    ("Juniper Court"), a vendor ("Amazon Web Services") and a desk ("Dispatch Desk") -- in both
    delivered worlds among them. A person is a record that sits where people sit or carries what
    only a person carries.
    """
    if container in PERSON_CONTAINERS or any(w in container.casefold() for w in PERSON_CONTAINER_WORDS):
        return True
    return any(record.get(key) not in (None, "", [], {}) for key in PERSON_ATTRIBUTES)


def people_in(node):
    """Every person-like name and email in a tree, harvested from the records that are people."""
    names, emails = set(), set()

    def visit(n, container=""):
        if isinstance(n, dict):
            if is_person_record(n, container):
                for key in ("name", "fullName", "displayName"):
                    v = n.get(key)
                    if looks_like_person(v):
                        names.add(v.strip())
            if isinstance(n.get("email"), str) and "@" in n["email"]:
                emails.add(n["email"].strip().casefold())
            for key, v in n.items():
                visit(v, key)
        elif isinstance(n, list):
            for v in n:
                visit(v, container)

    visit(node)
    return names, emails


def used_person_names(companies_dir, exclude=None):
    """People already named anywhere in other seeded companies of this corpus.

    Workers, staff, customers and counterparties alike: a corpus where "Priya Shah" works at
    nine companies reads as one author, not fifty companies. Names come from worker identities
    and from every ``name``/``fullName`` field in the other companies' worlds and app states.
    """
    names, emails = set(), set()

    def visit(node, container=""):
        if isinstance(node, dict):
            if is_person_record(node, container):
                for key in ("name", "fullName", "displayName", "username"):
                    value = node.get(key)
                    if looks_like_person(value):
                        names.add(value.strip())
            if isinstance(node.get("email"), str) and "@" in node["email"]:
                emails.add(node["email"].strip().casefold())
            for key, value in node.items():
                visit(value, key)
        elif isinstance(node, list):
            for value in node:
                visit(value, container)

    for folder in sorted(Path(companies_dir).glob("*/world")):
        if exclude and folder.parent.name == exclude:
            continue
        for path in [folder / "identities.json", folder / "world.json", *sorted(folder.glob("*.state.json"))]:
            try:
                visit(read(path))
            except (OSError, ValueError, AttributeError):
                continue
    registry = read_registry(companies_dir)
    names.update(n for n, owner in registry["names"].items() if owner != exclude)
    emails.update(e for e, owner in registry["emails"].items() if owner != exclude)
    # The payload carries a capped list: names the model has reused before come first, then a
    # deterministic spread of the rest, so the cap never ends at "Blair Watanabe".
    counts = registry.get("counts", {})
    ordered = sorted(names, key=lambda n: (-counts.get(n, 0), digest(n)))
    return ordered, sorted(emails)


PROVENANCE_KEYS = {
    "evidence",
    "outlines",
    "evidence_ids",
    "rationale",
    "basis",
    "kind",
    "source_ids",
    "sources",
}


def company_view(company):
    """The dossier as the company would describe itself: facts, people, operations.

    Research provenance (evidence, rationales, "synthetic"/"inferred" labels) explains how the
    dossier was built. Shown to the seeding model it leaked into document titles such as
    "Synthetic foster-care payment rates"; the company does not know those words.
    """

    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k not in PROVENANCE_KEYS}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    return strip(company)


def scope_collections(task_dir):
    """Collections a task's grader reads or its golden trajectory writes, when those files exist:
    the review's blocking scope beyond feature_cell.collections. Names only, never criteria."""
    found = set()
    grader_path, golden_path = Path(task_dir) / "grader.json", Path(task_dir) / "golden.json"
    if grader_path.is_file():
        for check in (read(grader_path).get("grader") or {}).get("checks") or []:
            for path in [
                check.get("predicate"),
                *(check.get("guards") or []),
                *(check.get("app_paths") or []),
            ]:
                if isinstance(path, dict) and path.get("app_id") and isinstance(path.get("selector"), str):
                    match = re.match(r"\$?\.?([A-Za-z_]\w*)", path["selector"])
                    if match:
                        found.add(f"{path['app_id']}.{match.group(1)}")
    if golden_path.is_file():
        steps = read(golden_path)
        for step in steps if isinstance(steps, list) else []:
            if isinstance(step, dict) and step.get("action") == "set_current" and step.get("app_id"):
                found.update(f"{step['app_id']}.{key}" for key in step.get("state_patch") or {})
    return sorted(found)


def task_designs(folder):
    """The tasks a world must carry, as the seeding calls see them: public brief, decisive scope,
    and the starting situation from the private design (never the feasible path or the criteria)."""
    tasks = []
    for workflow_path in sorted((Path(folder) / "tasks").glob("*/workflow.json")):
        task_dir = workflow_path.parent
        if task_dir.name.startswith("_"):
            continue
        if (task_dir / "workflow.json").is_file():
            design = read(task_dir / "workflow.json")
            tasks.append(
                {
                    "workflow_id": task_dir.name,
                    "public_assignment": read(task_dir / "assignment.json"),
                    # Where the task's decisive work lands: the review's blocking scope.
                    "decisive_collections": list((design.get("feature_cell") or {}).get("collections") or []),
                    # What the grader reads and the golden writes, once those exist: also in scope.
                    "scope_collections": scope_collections(task_dir),
                    # Seeding needs the starting situation, never the answer: the private
                    # feasible path, success criteria and phases stay out of the seed prompt.
                    "private_design": {
                        k: design.get(k)
                        for k in (
                            "id",
                            "title",
                            "objective",
                            "decision_problem",
                            "manager_id",
                            "worker_ids",
                            "team_ids",
                            "initial_materials",
                            "deliverables",
                            "assumptions",
                            "software_requirement_ids",
                        )
                        if k in design
                    }
                    | {
                        # Who holds which system, so a specialist's inputs land where only that
                        # specialist will find them.
                        "worker_apps": {
                            c["worker_id"]: c.get("apps") or [] for c in design.get("contributions", [])
                        }
                    },
                }
            )
    return tasks


def core_payload(root, folder):
    company = read(folder / "company.json")
    apps, contract = app_contract(root, folder)
    tasks = task_designs(folder)
    # The avoid list is frozen on the first attempt: other seeds keep registering names, and a
    # prompt that changes on every retry would never hit the call cache.
    frozen = folder / "world" / "avoid.json"
    if frozen.is_file():
        saved = read(frozen)
        avoid_names, avoid_emails = saved["names"], saved["emails"]
    else:
        avoid_names, avoid_emails = used_person_names(folder.parent, exclude=folder.name)
        write(frozen, {"names": avoid_names[:1500], "emails": avoid_emails[:800]})
    return {
        "call": "world_core",
        # People must be unique across the corpus: no worker name or email reused between companies.
        "avoid_person_names": avoid_names[:1500],
        "avoid_emails": avoid_emails[:800],
        # Seeding needs who the company is and how it operates, not the research
        # apparatus: source quotes and unused task outlines are dropped from the prompt.
        "company": company_view(company),
        "workers": apps.get("workers") or [w["id"] for w in company["workers"]],
        "tasks": tasks,
        **({"upcoming_work_themes": company.get("outlines", [])} if not tasks else {}),
        "apps": [
            {k: v for k, v in app.items() if k != "schema_document"}
            | {"schema_excerpt": app["schema_document"][:6000]}
            for app in contract
        ],
        "output_contract": {
            "identities": "one entry per (worker, app with identity_key); user_json parses to a JSON object",
            "materials": "relative paths only; no task solutions or grading text",
            "entities_json": "one JSON object; include a dated history array. Every entity and history "
            "record must name its destination apps in apps: [app_id, ...] or app_id references. "
            "Unmapped records are excluded from app calls.",
            "scope": "Seed ordinary business only when tasks is empty. Upcoming work themes are "
            "operating context, not assignments or prescribed outcomes.",
        },
    }


def project_world(world, app_id):
    """Retain records with this app in `apps` hints or explicit app-id references.

    Match exact app IDs in nested values/keys (including projection maps). Unmapped
    records are excluded; non-record containers and scalar world metadata survive.
    Nested records are filtered independently, preventing unrelated entity leakage.
    """

    def mentions(node):
        if isinstance(node, dict):
            return app_id in node or any(mentions(v) for v in node.values())
        if isinstance(node, list):
            return any(mentions(v) for v in node)
        return node == app_id

    def project(node, in_collection=False):
        if isinstance(node, dict):
            if in_collection or "id" in node or "apps" in node or "app_id" in node:
                # Explicit hints take precedence over nested references: a child
                # projection must not bring its unrelated parent into this app.
                destinations = node.get("apps", node.get("app_id", node))
                if not mentions(destinations):
                    return None
            return {k: filtered for k, v in node.items() if (filtered := project(v)) is not None}
        if isinstance(node, list):
            return [filtered for v in node if (filtered := project(v, in_collection=True)) is not None]
        return node

    return project(world) or {}


def record_count(value):
    """Records in a collection however the app shapes it: a list, a dict of lists, or a dict of records."""
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        if value and all(isinstance(v, list) for v in value.values()):
            return sum(len(v) for v in value.values())
        return len(value)
    return 0


def population_findings(states, plan):
    """Measure the persisted plan against actual collections; never count targets as data."""
    findings = {}
    for target in plan:
        app_id, collection = target["app_id"], target["collection"]
        value = states.get(app_id, {}).get(collection)
        ids = record_ids(value)
        actual = len(set(ids)) if ids else record_count(value)
        if actual < target["target_records"]:
            findings.setdefault(app_id, []).append(
                {
                    "severity": "error",
                    "evidence": f"/{collection}",
                    "issue": f"Population target requires {target['target_records']} {target['record_unit']}; only {actual} present. Preserve these records and populate the shortfall in bounded calls.",
                    "actual": actual,
                    "target": target["target_records"],
                }
            )
    return findings


def history_eligible(key, value):
    """Directories and interface state do not acquire earlier windows of traffic."""
    static = (
        USER_COLLECTIONS
        | UI_STATE_KEYS
        | {
            "user",
            "currentUser",
            "current_user",
            "account",
            "labels",
            "folders",
            "channels",
            "calendars",
            "settings",
            "permissions",
            "roles",
            "contacts",
            "companies",
            "products",
            "audiences",
            "dealStages",
            "ticketStatuses",
        }
    )
    return key not in static and key not in {"ui", "undoStack", "redoStack", "selectedItems"} and bool(value)


def record_ids(value):
    ids = []

    def take(rec):
        if isinstance(rec, dict):
            for key in ("id", "messageId", "emailId", "eventId", "ticketId", "itemId"):
                if rec.get(key) is not None:
                    ids.append(str(rec[key]))
                    return
            for key, val in rec.items():
                if key.lower().endswith("id") and isinstance(val, (str, int)):
                    ids.append(str(val))
                    return

    if isinstance(value, list):
        for rec in value:
            take(rec)
    elif isinstance(value, dict):
        if value and all(isinstance(v, list) for v in value.values()):
            for rows in value.values():
                for rec in rows:
                    take(rec)
        else:
            ids.extend(str(k) for k in value)
    return ids


PRIMARY_ID_KEYS = ("id", "messageId", "emailId", "eventId", "ticketId", "itemId")


def primary_id(record):
    """A record's own identifier, or None when it carries only references.

    ``record_ids`` falls back to any key ending in "id" so a shard prompt can list what is already
    there. Deduplicating on that fallback is wrong: a chat message whose only id-shaped field is
    channelId reads as a duplicate of every other message in the channel. Whole shards were being
    discarded that way, 455 records generated and 0 inserted, 1,715 rows in one sample.
    """
    if not isinstance(record, dict):
        return None
    for key in PRIMARY_ID_KEYS:
        if record.get(key) is not None:
            return str(record[key])
    return None


def merge_records(existing, addition):
    """Add a shard's records to a collection; on an id collision the existing record wins.

    A record with no identifier of its own is always kept: a shard covers a window the collection
    does not hold yet, so the risk of adding one twice is far smaller than dropping all of them.
    """
    if isinstance(existing, list) and isinstance(addition, list):
        seen = {rid for rec in existing if (rid := primary_id(rec)) is not None}
        merged = list(existing)
        for rec in addition:
            rid = primary_id(rec)
            if rid is not None and rid in seen:
                continue
            if rid is not None:
                seen.add(rid)
            merged.append(rec)
        return merged
    if isinstance(existing, dict) and isinstance(addition, dict):
        if existing and all(isinstance(v, list) for v in existing.values()):
            merged = {k: list(v) for k, v in existing.items()}
            for key, rows in addition.items():
                if isinstance(rows, list):
                    merged[key] = merge_records(merged.get(key, []), rows)
            return merged
        merged = dict(existing)
        for key, rec in addition.items():
            merged.setdefault(key, rec)
        return merged
    return existing


def time_windows(reference_date, shards, months_back=12, recent_weeks=6, *, first_date=None, last_date=None):
    """Equal windows over the older part of the history, the recent weeks being the first call's."""
    from datetime import date, timedelta

    end = date.fromisoformat(str(reference_date)[:10]) - timedelta(weeks=recent_weeks)
    if last_date:
        end = min(end, date.fromisoformat(last_date))
    start = date.fromisoformat(first_date) if first_date else end - timedelta(days=int(months_back * 30.4))
    if start >= end:
        return []
    step = (end - start) / max(1, shards - 1)
    windows = []
    for index in range(max(0, shards - 1)):
        a = start + step * index
        b = start + step * (index + 1)
        windows.append((a.isoformat(), b.isoformat()))
    return windows


ACCESS_RULE = (
    "Only workers_using_this_app have an account in this app. A worker in workers_without_access "
    "cannot log in here, so no record may name one as the person who acted: not as owner, author, "
    "sender, recipient, assignee, reporter, requester, approver, reviewer, commenter or attendee, "
    "and not as a user record in the directory. That holds for the manager too, who is deliberately "
    "kept out of one system so the work has to be delegated. Their part of the work reaches this app "
    "only as text a worker who does hold it wrote."
)

FIELD_RULE = (
    "The field names this app's own records carry, per collection, read from the app's own data. "
    "Write these names: the app reads them, and a renamed field passes validation and renders "
    "nothing (microsoft_teams seeded id/name where the app reads userId/displayName; salesforce "
    "posts carried createdAt where the app reads createdDate, so the feed ordered by page-load "
    "time). The lists are what was observed, not a limit: where the schema documents a field that "
    "is not listed, follow the schema, and add the fields a record needs."
)


WHOLE_CONTRACT = (
    "state_json contains exactly named_collections, including empty collections; "
    "omit all other top-level keys, and never wrap the object in a label of your own. Groups are "
    "merged and validated as one native state, so already_authored_collections is context to "
    "reference and never output. Use canonical IDs for cross-collection references; compact JSON."
)

# The repair contract. Asking for the collection back is what made a repair call emit more output
# than first-pass authoring (mean 12,878 tokens against 8,447, p90 42,756 against 21,945, 861
# core-hours over the tree), because every record the reviewers did not fault had to be rewritten
# to keep it. previous_state_json is a sample under this contract, so the answer must be a patch:
# the records not returned are the ones kept.
PATCH_CONTRACT = (
    "state_json contains exactly named_collections, and under each one ONLY the records you change "
    "or add -- the repair, not the collection. previous_state_json is a sample of what those "
    "collections hold now, so: a record you do not return is kept exactly as it is; a record you "
    "return replaces the record with the same id; a record with an id that is not there yet is "
    'added; and {"id": "<the id>", "_remove": true} deletes that record. Never copy back a record '
    "you are not changing, never change the id of a record you are replacing, and never return a "
    '"_sampled" or "_omitted" marker -- those say how many records the sample left out. Return each '
    "named collection as a key even when your patch for it is empty. already_authored_collections "
    "is context to reference and never output. Use canonical IDs for cross-collection references; "
    "compact JSON."
)


# A counted finding prints three exemplars and describes thousands. When the batch below IS the
# population, the author has to be told so, or it repairs the three ids the prose names and returns
# the rest untouched -- which is the sample-versus-population trap with extra steps.
POPULATION_RULE = (
    "records_to_repair is how many records the findings are about, and every record in "
    "previous_state_json's collections is one of them -- not an example of them. Repair every one "
    "and return every one you repaired. The ids named in revision_feedback are examples of the same "
    "defect, never the extent of it."
)


def feedback_for_prompt(feedback):
    """The findings as the author reads them: a population's size, never its id list.

    ``records`` exists so the *repair* can cover the population; the author never reads it, because
    the batch it is working on is already in ``previous_state_json``. Serialising it as well spends
    the very ceiling this work exists to stay under, once per batch: measured over the 60 worlds now
    that the checkers declare populations, the id lists are a mean of 35.9 KB per world, a median of
    17.8 KB and 162.2 KB at hammond-power-solutions, whose gmail /emails population alone is 4,040
    ids. A finding that declares no population serialises byte for byte as it did, so every call
    already cached stays cached.
    """
    out = []
    for item in feedback:
        records = item.get("records")
        if not records:
            out.append(item)
            continue
        out.append({k: v for k, v in item.items() if k != "records"} | {"population_records": len(records)})
    return out


def app_payload(
    core,
    app,
    identities,
    worker_apps,
    company,
    tasks,
    feedback=None,
    previous_state=None,
    collections=None,
    *,
    record_fields=None,
    authored=None,
    patch=False,
    population=None,
):
    """One ``app_state`` call's payload: this app, these collections, this company's world.

    ``record_fields`` is the app's own field names for the named collections and ``authored`` the
    collections of this app already written in this seed, so a call that covers one collection can
    still reference the ids the earlier calls wrote.

    ``patch`` says ``previous_state`` is a sample rather than the whole collection, so the answer is
    the records to change and not the collection itself. ``population`` says that every record in it
    is one the findings are about, which is the difference between repairing a counted finding and
    repairing the three records it happened to print.
    """
    holders = [w for w, ids in worker_apps.items() if app["app_id"] in ids]
    return {
        "call": "app_state",
        **(
            {
                "revision_feedback": feedback_for_prompt(feedback),
                "previous_state_json": json.dumps(previous_state),
            }
            if feedback
            else {}
        ),
        "app": app,
        "company_summary": {k: company.get(k) for k in ("id", "name", "sector", "operations", "location")},
        "workers": company.get("workers", []),
        "tasks_public": [t["public_assignment"] for t in tasks],
        "tasks_private_records": [
            {
                "workflow_id": t["workflow_id"],
                "initial_materials": t["private_design"].get("initial_materials"),
            }
            for t in tasks
        ],
        "canonical_world": project_world(core["world"], app["app_id"]),
        "named_collections": collections if collections is not None else app["top_level_keys"],
        "reference_date": core["reference_date"],
        "operating_scope": core["operating_scope"],
        **(
            {
                "population_targets": [
                    p
                    for p in core["population_plan"]
                    if p["app_id"] == app["app_id"]
                    and (collections is None or p["collection"] in collections)
                ],
                "population_target_rule": "Targets apply to the final merged collection across all calls, not each response. Preserve canonical IDs and facts; use the specified date window. Do not invent counts or repeat records to satisfy a target.",
            }
            if core.get("population_plan")
            else {}
        ),
        "identities_for_this_app": identities,
        # The access barrier is part of the world's design: worker_apps decides which apps a
        # worker's VM can even reach, and the task binding keeps at least one decisive app away
        # from the manager. Unsaid, the author assigned and reported records as workers with no
        # login here -- 218 jira records at greenbrier-companies, 236 salesforce at
        # federal-home-loan-bank-des-moines, 165 Expensify at treehouse-foods, in several
        # companies the very app their own task turns on.
        "workers_using_this_app": holders,
        "workers_without_access": [w for w in worker_apps if w not in set(holders)],
        "access_rule": ACCESS_RULE,
        **({"record_fields": record_fields, "record_fields_rule": FIELD_RULE} if record_fields else {}),
        **({"already_authored_collections": authored} if authored else {}),
        **({"previous_state_is_a_sample": True} if patch and feedback else {}),
        **(
            {"records_to_repair": population, "records_to_repair_rule": POPULATION_RULE}
            if patch and feedback and population
            else {}
        ),
        "output_contract": PATCH_CONTRACT if patch and feedback else WHOLE_CONTRACT,
    }


def salvage_truncated_json(text, max_cuts=2000):
    """Recover a JSON object whose output was cut off, dropping only the unfinished tail.

    Model output can stop mid-record when it runs out of room. Walking back to each of the
    last ``max_cuts`` commas or opening brackets, the text is cut there and every open string,
    array and object is closed; the first cut that parses wins. Returns None when nothing
    parses. Used for worlds too large for the bounded repair call, where losing the final
    record beats losing a twenty-minute call.
    """

    def closers(prefix):
        stack, in_string, escape = [], False, False
        for ch in prefix:
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]" and stack:
                stack.pop()
        return ('"' if in_string else "") + "".join(reversed(stack))

    def attempt(prefix):
        candidate = prefix.rstrip().rstrip(",") + closers(prefix)
        try:
            value = json.loads(candidate)
        except ValueError:
            return None
        return value if isinstance(value, dict) and value else None

    text = text.rstrip()
    positions, depths = [], {}
    depth, in_string, escape = 0, False, False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            positions.append(i)
            depths[i] = depth
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif ch == ",":
            positions.append(i)
            depths[i] = depth
    # Cut where a record starts (a comma followed by an opening bracket, or the opening of a
    # top-level collection) so the unfinished record is dropped whole; then any comma, dropping
    # an unfinished field; finally close whatever was open at the very end.
    record_starts = [
        i
        for i in positions
        if (text[i] == "," and text[i + 1 : i + 4].lstrip()[:1] in ("{", "["))
        or (text[i] == "[" and depths[i] <= 1)
    ]
    for cuts in (record_starts[-max_cuts:], positions[-max_cuts:]):
        for cut in reversed(cuts):
            value = attempt(text[:cut] if text[cut] == "," else text[: cut + 1])
            if value is not None:
                return value
    return attempt(text)


def parse_json(text, what, *, strict=True, models=None, expected=None):
    """Parse a model-written JSON object.

    ``strict`` rejects duplicate object keys, which matters for app states where a duplicated
    collection key would silently drop records. The free-form canonical world tolerates a
    duplicated scalar key (last value wins) rather than discarding a 20-minute call for a slip.
    ``expected`` names the top-level keys, which the repair call is told and which identify a
    repair that came back wrapped in its own label.
    """

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result and strict:
                raise ValueError(f"duplicate object key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=unique_object)
    except ValueError as exc:
        fixed_csv = escape_csv_quotes(text)
        if fixed_csv is not None:
            value = json.loads(fixed_csv, object_pairs_hook=unique_object)
            if not isinstance(value, dict):
                raise TypeError(f"{what} must be a JSON object")
            return value
        if models is None:
            raise ValueError(f"{what} is not valid JSON: {exc}") from exc
        # A 20-minute authoring call should not be discarded for a syntax slip: repair once.
        try:
            repaired = repair_json(text, what, models, expected=expected)
            value = unwrap_label(json.loads(repaired, object_pairs_hook=unique_object), what, expected)
        except ValueError as exc2:
            salvaged = salvage_truncated_json(text) if len(text) > 20_000 else None
            if salvaged is None:
                raise ValueError(f"{what} is not valid JSON even after repair: {exc2}") from exc2
            print(
                f"warning: {what} was cut off at {len(text)} characters; salvaged the complete records",
                file=sys.stderr,
            )
            value = unwrap_label(salvaged, what, expected)
    if not isinstance(value, dict):
        raise TypeError(f"{what} must be a JSON object")
    return value


def contract_worker_apps(folder):
    """Per-worker apps named by the tasks' contributions, or None when any task leaves them out."""
    result = {}
    tasks = [
        p for p in sorted(Path(folder).glob("tasks/*/workflow.json")) if not p.parent.name.startswith("_")
    ]
    if not tasks:
        return None
    named = False
    for path in tasks:
        workflow = read(path)
        for contribution in workflow.get("contributions", []):
            apps = contribution.get("apps")
            if apps is None:
                return None
            named = named or bool(apps)
            result.setdefault(contribution["worker_id"], set()).update(apps)
    # An empty list is a real binding (standard bundle only) once anyone names an app; a
    # contract where nobody names anything is a contract that was never written.
    return {w: sorted(a) for w, a in result.items()} if named else None


def sync_worker_apps(folder):
    """Rewrite a seeded world's worker_apps.json from the task contract; returns the map or None."""
    folder = Path(folder)
    contract = contract_worker_apps(folder)
    if contract is None or not (folder / "world" / "worker_apps.json").is_file():
        return None
    current = read(folder / "world" / "worker_apps.json")
    apps_here = {a["app_id"] for a in read(folder / "apps.json")["apps"]}
    synced = {w: sorted((set(contract.get(w, [])) | STANDARD_APPS) & apps_here) for w in current}
    write(folder / "world" / "worker_apps.json", synced)
    return synced


def grant_map(core, apps, workers, contract_apps=None):
    """Who may log into which app: the company's binding when it has one, else the core's guess.

    ``contract_apps`` is ``worker_apps.company_grants`` -- the one rule, settled at the binding
    stage before the world is written. The standard workplace bundle is everyone's by policy
    (``design.standard_apps``), so it is unioned in either way, which also makes a raw
    contributions map behave the same as the company-wide one.
    """
    worker_apps = {row.worker_id: sorted(set(row.app_ids)) for row in core.worker_apps}
    if not contract_apps and (
        set(worker_apps) != set(workers) or any(set(v) - apps.keys() for v in worker_apps.values())
    ):
        raise ValueError("worker_apps must cover every worker with known app ids")
    if contract_apps:
        # The task contract decides who holds which system; the model's guess is replaced.
        worker_apps = {w: sorted(set(contract_apps.get(w, []))) for w in workers}
    return {w: sorted((set(worker_apps.get(w, ())) | STANDARD_APPS) & apps.keys()) for w in workers}


def validate_core(core, apps, workers, models=None, contract_apps=None):
    identities = {}
    for item in core.identities:
        if item.app_id not in apps or item.worker_id not in workers:
            raise ValueError(f"identity for unknown worker/app {item.worker_id}/{item.app_id}")
        identities.setdefault(item.worker_id, {})[item.app_id] = parse_json(
            item.user_json, f"{item.worker_id} identity in {item.app_id}"
        )
    worker_apps = grant_map(core, apps, workers, contract_apps)
    # The identity book stays complete -- every worker in every app with an identity_key, 1,671
    # records across the 60 seeded worlds where the grants need 1,522 -- because it is what lets a
    # mined task widen a grant without re-seeding the world: a granted app with no identity makes
    # ``hub_vm._plan`` raise "Missing proxy endpoint", and 0 granted pairs lack one today. What
    # narrows is the author's view of it: ``make_author`` hands each app only the identities of the
    # workers the grant map gives that app, so the app is never written for people who cannot open
    # it (the 8.9% surplus is exactly the 149 (worker, app) pairs in 40 worlds that have no login).
    for app_id, app in apps.items():
        if app.get("identity_key"):
            for worker in workers:
                if app_id not in identities.get(worker, {}):
                    raise ValueError(f"missing identity for worker {worker} in {app_id}")
    if errors := distinct_identity_errors(identities, {a for a in apps if apps[a].get("identity_key")}):
        raise ValueError("identities are not distinct people: " + "; ".join(errors[:6]))
    materials = []
    for item in core.materials:
        path = PurePosixPath(item.path)
        if (
            item.worker_id not in workers
            or path.is_absolute()
            or ".." in path.parts
            or str(path) != item.path
        ):
            raise ValueError(f"invalid material {item.worker_id}:{item.path}")
        materials.append(item)
    try:
        date.fromisoformat(str(core.reference_date)[:10])
    except ValueError as exc:
        raise ValueError(f"reference_date must be an ISO date, got {core.reference_date!r}") from exc
    world = parse_json(core.entities_json, "entities_json", strict=False, models=models)
    if rules := check_literal_records("world", world):
        raise ValueError(
            "entities_json contains generator rules instead of literal records: "
            + "; ".join(f"{f['path']}: {f['message']}" for f in rules[:4])
        )
    return world, identities, materials, worker_apps


def validate_app_state(result, app, root, identities, models=None):
    state = parse_json(result.state_json, f"{app['app_id']} state", models=models)
    schema_text = (root / app["schema"]).read_text()
    validate_state({"id": app["app_id"], "state_keys": app.get("top_level_keys") or []}, state, schema_text)
    if app.get("identity_key"):
        # A worker missing from the app's directory is repaired here rather than raised: the record
        # is known, and raising discarded every other app this seed had already authored.
        splice_identities(state, identities)
        findings = check_state_identities(app["app_id"], state, identities)
        if findings:
            raise ValueError("identity mismatch: " + "; ".join(f["message"] for f in findings))
        # Keep the native ID type for runtime consumers that compare IDs exactly.
        # These records are shared with the core's exported identities map.
        for record in identities.values():
            hits = identity_members(state, record) if record.get("id") is not None else []
            if hits:  # single-account apps have no directory entry to align with
                record["id"] = hits[0][1]["id"]
    return state


@cache
def record_field_catalogue(root):
    """Per app, the field names its own records carry, by collection; empty when unrecorded."""
    return app_record_fields(Path(root) / "catalogs" / "app_record_fields.json")


def reference_collections(state, exclude, limit=60, budget=None):
    """Collections already written for this app, small enough to carry into the next call.

    ``budget`` caps the total JSON they add to a prompt; without one a single collection of 60
    documents can be megabytes.
    """
    picked, spent = {}, 0
    for key, value in state.items():
        if key in exclude or not 0 < record_count(value) <= limit:
            continue
        if budget is not None:
            size = len(json.dumps(value, ensure_ascii=False))
            if spent + size > budget:
                continue
            spent += size
        picked[key] = value
    return picked


def collection_groups(keys, fields, group_size, small_collections=()):
    """How an app's collections are split into calls: a record collection is a call of its own.

    A call's wall time is its output: over the 2,208 group calls on disk the correlation between
    output tokens and seconds is 0.975, and one call authoring four collections wrote a mean of
    30,885 tokens. That is where the stage's worst numbers come from -- 400 of the 601 world_states
    timeouts were four-collection calls, 193 core-hours at a mean of 1,736 s, each one losing the
    whole group.

    The app's own record-field catalogue says which collections actually hold records, and it
    separates the expensive from the trivial almost perfectly: of the 817 single-collection calls
    on disk, the 268 for a catalogued collection ran a median 493 s (48% over 600 s) and the 549
    for an uncatalogued one a median 7 s with a p90 of 10 s and none over 600 s. So each catalogued
    collection is authored alone, and the rest are batched ``group_size`` at a time as before. An
    app the catalogue does not cover keeps today's fixed grouping: there is no evidence to split on.
    """
    if not fields:
        return [list(keys[offset : offset + group_size]) for offset in range(0, len(keys), group_size)]
    groups, batch = [], []
    for key in keys:
        if key in fields and key not in small_collections:
            groups.append([key])
            continue
        batch.append(key)
        if len(batch) >= group_size:
            groups.append(batch)
            batch = []
    if batch:
        groups.append(batch)
    return groups


REFERENCE_BUDGET = 40_000

# A repair was sent the collection it repairs whole and had to answer with it whole. Measured over
# the 31,886 world_states attempts on disk: repair calls emit a mean 12,878 output tokens against
# first-pass authoring's 8,447 and a p90 of 42,756 against 21,945 -- 72.6 M output tokens and 861
# core-hours, and 323 of the 479 calls that ran past 1,500 s without finishing are repairs. The
# cause is the contract, not the unit: the worst prompt on disk is macerich gmail_mock /emails at
# 3,859,614 characters, of which 3,726,147 is the one previous collection, and that call is already
# a single-collection call. So a repair is sent a sample of what is there and answers with the
# records it changes. This budget is what the sample may occupy; the rest of the prompt measures
# 133 KB in that worst case, so it leaves the provider's ceiling a wide margin.
REPAIR_STATE_BUDGET = 300_000

SAMPLE_MARKERS = ("_sampled", "_omitted")


def sampling_marker(record):
    """Whether this row is one of ``bound_states``'s "N more records not shown" placeholders."""
    return isinstance(record, dict) and any(key in record for key in SAMPLE_MARKERS)


def patch_removes(record):
    """Whether a patch record asks for the record with its id to be deleted."""
    return isinstance(record, dict) and record.get("_remove") is True


def apply_patch(previous, patch):
    """``previous`` with a repair's records laid over it: replace by id, append what is new, drop
    what the patch marks ``_remove``, and ignore the sampling placeholders.

    This is the merge the patch contract needs, and it is deliberately not ``merge_records``: a
    shard covers a window the collection does not hold, so there the existing record wins; a repair
    is a correction of a record that is already there, so here the patch wins. It is also the
    tolerant reading of a bad answer -- a model that echoes back only the sample it was shown now
    corrects those records and leaves the rest of the inbox alone, where the whole-collection
    contract would have read the same answer as "delete the other 3,000 emails".
    """
    if isinstance(previous, list) and isinstance(patch, list):
        replace, extra = {}, []
        for record in patch:
            if sampling_marker(record):
                continue
            rid = primary_id(record)
            if rid is None:
                extra.append(record)
            else:
                replace.setdefault(rid, record)
        out, used = [], set()
        for record in previous:
            rid = primary_id(record)
            if rid is not None and rid in replace and rid not in used:
                used.add(rid)
                if not patch_removes(replace[rid]):
                    out.append(replace[rid])
                continue
            out.append(record)
        out.extend(r for rid, r in replace.items() if rid not in used and not patch_removes(r))
        out.extend(r for r in extra if not patch_removes(r))
        return out
    if isinstance(previous, dict) and isinstance(patch, dict):
        if previous and all(isinstance(v, list) for v in previous.values()):
            merged = {key: list(value) for key, value in previous.items()}
            for key, rows in patch.items():
                if isinstance(rows, list):
                    merged[key] = apply_patch(merged.get(key, []), rows)
            return merged
        if previous and all(isinstance(v, dict) for v in previous.values()):
            # A map of records keyed by id, the shape ``record_count`` calls "a dict of records".
            merged = dict(previous)
            for key, record in patch.items():
                if key in SAMPLE_MARKERS:
                    continue
                if patch_removes(record):
                    merged.pop(key, None)
                else:
                    merged[key] = record
            return merged
    # Not a collection of records: one record (``currentUser``), a view (``ui``), a scalar, or a
    # shape the answer does not match. Those are small, they are never what a sample leaves out,
    # and a repair that has to drop a key from one must be able to. Take the answer as it stands,
    # which is what the whole-collection contract always did.
    return patch


def patch_cited_ids(feedback, previous):
    """The ids of ``previous``'s own records that ``feedback`` names, so a sample still carries them.

    Sampling a collection the reviewers cited by id and then asking for those records to be
    corrected is asking for a guess. ``cited_ids`` only accepts tokens that are ids of records
    actually present, so a collection name or a date in the prose cannot widen this.
    """
    holders = {rid for value in (previous or {}).values() for rid in record_ids(value)}
    if not holders or not feedback:
        return set()
    text = " ".join(f"{f.get('evidence') or ''} {f.get('issue') or ''}" for f in feedback)
    return cited_ids(text, holders)


# How many population batches one repair of one group may spend. Re-measured over every population
# the checkers now declare, unioned per collection group the way the author really batches them: 189
# population-bearing groups fleet-wide holding 45,866 records, a median of 1 batch per group, a p90
# of 2 and a maximum of 17.
#
# The batch *size* is not the knob. It is sized by output, not input, because output is 97.8% of wall
# clock: 240,000 characters is about 200 drive items, and a patch of 200 such records is already
# 42,000 output tokens, the p90 of every repair call on disk. Raising it buys coverage by recreating
# the timeouts this work exists to remove.
#
# So the cap is a call-budget decision, and the budget turns out not to bind, because nearly every
# group needs one batch:
#
#   cap  8: one round covers 175 of 189 groups,  9,793 of 45,866 records | calls/world median 3 max 16
#   cap 12: 178 of 189,                         14,777                  | median 3 max 20
#   cap 16: 183 of 189,                         27,874                  | median 3 max 21
#   cap 24: 189 of 189,                         45,866                  | median 3 max 22
#
# 24 it is: one round covers every population on disk, and the worst world spends 22 calls against
# design.seed_call_budget = 500, where a median world spends 263 in total. The cost is wall clock on
# the 13 worlds with a big population -- a group's batches are sequential, so the six 17-batch groups
# add roughly an hour each at the p50 of 235 s a call -- against those records not being repaired at
# all today. Worth it per record; ``design.seed_population_batches`` is there to say otherwise.
REPAIR_POPULATION_BATCHES = 24

# How many records may point at one absent id before the defect is read as a missing container
# rather than as a population of broken children. See ``population_ids`` for the distribution.
REFERRER_RATIO = 4


def population_ids(feedback, previous):
    """Every record id the findings name as part of the defect, not only the ones they print.

    A counted finding cites exemplars: ``world_check.MAX_EXAMPLES`` is 3, and its own comment says
    the citations are there "so a repair can be narrowed". For a *uniformity* finding that is right.
    For a *population* finding it is the sample-versus-population trap: "3,350 of 4,960 threads name
    a root message that does not exist (e.g. a, b, c)" describes 3,350 records and prints 3, so a
    repair narrowed to the citations fixes 3 and the count it was measured against does not move.
    Measured over the 66 reachability findings on disk, 62 of them name a population larger than
    their exemplars -- 27,649 records against 186 printed ones.

    So a finding may carry ``records``: the ids of the whole population. This is the field a checker
    fills in; ``evidence`` and ``issue`` stay the human-readable citation.

    Two readings of that field are in use and both have to work. ``check_reachability``,
    ``check_containment`` and ``check_counts`` declare the ids of the broken *records*, which is what
    can be sent. ``check_references`` declares "the ids that resolve to nothing, which is what a
    repair has to write or re-point" -- ids of records that by definition do not exist, 28,608 of them
    on disk, the largest declared population in the tree. An absent id cannot be put in front of the
    author, so under the first reading alone that whole population resolved to **zero** records, the
    repair changed nothing and recorded that it had: the same trap one level up. What can be sent is
    the record that points at the absent id, so that is what is resolved here.

    Ids that neither name a record of this group nor are pointed at by one belong to another group and
    are dropped, which is what keeps one finding from widening a repair into collections it does not
    name.
    """
    named = {str(rid) for f in feedback or () for rid in (f.get("records") or ())}
    if not named:
        return set()
    own = {rid for value in (previous or {}).values() for rid in record_ids(value)}
    resolved = named & own
    absent = named - own
    if absent:
        referrers = referring_ids(previous, absent)
        # A thousand records pointing at two ids that were never written is a missing *container*,
        # and its repair is to write the container, not to re-point a thousand children. Measured
        # over the 185 reference findings whose absent ids have referrers at all, the ratio is
        # sharply bimodal -- median 1.00, p75 3.00, then p90 286.75 and a maximum of 2,175 (2,175
        # cruise-america drive items under one folder that does not exist) -- so the cut sits in an
        # empty gap: every threshold from 4 to 8 keeps the same 148 findings and 19,775 records, and
        # the 37 it drops would have rewritten 49,416 records to supply one or two missing parents.
        # Those findings keep the ordinary path, where the author gets the sample and the finding's
        # own instruction to "write the parent or point the child at one that exists".
        if len(referrers) <= REFERRER_RATIO * len(absent):
            resolved |= referrers
    return resolved


def scalars(record):
    """Every string or number a record carries, its own nested lists of scalars included.

    ``cardIds``, ``members`` and ``followers`` hold bare ids in a list, which is how a container
    names its children; a reference that dangles out of one of those is as real as a dangling
    ``parentId``.
    """
    if not isinstance(record, dict):
        return set()
    out = set()
    for value in record.values():
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            out.add(str(value))
        elif isinstance(value, list):
            out |= {str(v) for v in value if isinstance(v, (str, int)) and not isinstance(v, bool)}
    return out


def referring_ids(previous, targets):
    """The ids of the records that carry one of ``targets`` as a value.

    One pass over the collection, because a reference finding's population is the absent ids and the
    repairable records are the ones pointing at them. A record with no id of its own cannot be named
    in a batch and is left to the whole-collection path.
    """
    found = set()
    for value in (previous or {}).values():
        for candidates, record in record_candidates(value):
            if candidates and scalars(record) & targets:
                found |= candidates
    return found


def record_candidates(value):
    """``(the ids this record could be known by, the record)`` for each record of a collection.

    Every shape an app keeps a collection in, because the populations on disk use all of them: a
    list (drive ``items``), a dict of lists (slack messages keyed by channel) and a dict of records
    keyed by id (trello ``cards``, 3,265 of blastx-consulting's 3,288). A checker names the record by
    whichever id it saw, so both the key and the record's own id count -- reading only lists found 0
    of the 27,658 records the reachability findings name.
    """
    if isinstance(value, list):
        for record in value:
            rid = primary_id(record)
            yield ({rid} if rid is not None else set()), record
    elif isinstance(value, dict):
        if value and all(isinstance(v, list) for v in value.values()):
            for rows in value.values():
                for record in rows:
                    rid = primary_id(record)
                    yield ({rid} if rid is not None else set()), record
        else:
            for key, record in value.items():
                rid = primary_id(record) if isinstance(record, dict) else None
                yield {str(key)} | ({rid} if rid is not None else set()), record


def population_batches(previous, ids, budget=None, cap=REPAIR_POPULATION_BATCHES):
    """``ids`` split into batches each small enough to be sent whole, and how many are left over.

    A population repair is not one call and it is not one call per record: it is as many calls as it
    takes to put every named record in front of the author once. The batches follow collection and
    record order so a resumed or re-run repair sends the same batches and hits the call cache.
    ``budget`` is shared with the reference collections that travel with every call, so batches are
    filled to four fifths of it.
    """
    batches, batch, spent = [], set(), 0
    # Resolved here, not in the signature: a default bound at definition time cannot be adjusted,
    # and both of this module's size constants were reached that way by mistake.
    room = int((REPAIR_STATE_BUDGET if budget is None else budget) * 0.8)
    for value in (previous or {}).values():
        for candidates, record in record_candidates(value):
            hit = candidates & ids
            if not hit:
                continue
            size = len(json.dumps(record, ensure_ascii=False))
            if batch and spent + size > room:
                batches.append(batch)
                batch, spent = set(), 0
            batch |= hit
            spent += size
    if batch:
        batches.append(batch)
    return batches[:cap], sum(len(b) for b in batches[cap:])


def bound_previous(app_id, previous, cited, limit=None, *, only_cited=False):
    """One repair's previous state sampled to ``limit`` characters, every cited record kept whole.

    ``bound_states`` keeps small collections entire and samples big ones, leaving its own count
    marker behind; the cited records are then put back in front of that marker, the way
    ``bound_for_authoring`` does it for the grader authors.

    ``only_cited`` is the population case: ``cited`` is then one batch of the records a counted
    finding is actually about, and sending a representative spread of the collection beside it would
    spend the budget on records the finding does not name.

    The budget can only be defeated by a single record larger than it, which ``bound_states`` cannot
    sample away. There is no such record: over the 406 app states on disk the largest single record
    is under 100,000 characters, none at all above it. If one ever appears, the author's pre-dispatch
    check shrinks once and then refuses terminally, which is the backstop and not a silent failure.
    """
    limit = REPAIR_STATE_BUDGET if limit is None else limit
    if only_cited:
        # The population is known, so the call carries it and nothing else of the big collections:
        # a representative spread beside it would only dilute the records the finding is about.
        # Every shape, for the reason ``record_candidates`` gives, and the "_sampled" marker is the
        # one ``bound_states`` leaves so the two kinds of sample read the same to the author.
        bounded = {}
        for key, value in previous.items():
            if isinstance(value, list):
                kept = [r for c, r in record_candidates(value) if c & cited]
                left = len(value) - len(kept)
                bounded[key] = (
                    value
                    if not left
                    else [*kept, {"_sampled": f"{left} more records not shown of {len(value)}"}]
                )
            elif isinstance(value, dict) and value and all(isinstance(v, list) for v in value.values()):
                inner = {}
                for name, rows in value.items():
                    kept = [r for c, r in record_candidates(rows) if c & cited]
                    left = len(rows) - len(kept)
                    inner[name] = (
                        rows
                        if not left
                        else [*kept, {"_sampled": f"{left} more records not shown of {len(rows)}"}]
                    )
                bounded[key] = inner
            elif isinstance(value, dict) and value and all(isinstance(v, dict) for v in value.values()):
                kept = {k: v for k, v in value.items() if ({str(k)} | {primary_id(v) or ""}) & cited}
                left = len(value) - len(kept)
                bounded[key] = (
                    value
                    if not left
                    else kept | {"_sampled": f"{left} more records not shown of {len(value)}"}
                )
            else:
                bounded[key] = value
        return bounded
    bounded = bound_states({app_id: previous}, limit=limit, seed=f"repair:{app_id}")[app_id]
    for key, value in previous.items():
        if not isinstance(value, list) or bounded.get(key) is value or not cited:
            continue
        kept = {primary_id(r) for r in bounded[key] if isinstance(r, dict)}
        named = [r for r in value if (rid := primary_id(r)) is not None and rid not in kept and rid in cited]
        if named:
            bounded[key] = [
                *[r for r in bounded[key] if not sampling_marker(r)],
                *named,
                *[r for r in bounded[key] if sampling_marker(r)],
            ]
    return bounded


def oversized_prompt(app_id, group, size, ceiling):
    """The refusal an over-ceiling authoring prompt earns: a receipt line, never an exception.

    ``PromptTooLarge`` is raised before dispatch and was caught nowhere on this path, so the caller
    retried it unchanged: macerich's gmail_mock /emails repair reached 162 attempts that each failed
    in 0.4 s, and over the whole tree 15 world_states call ids ran 213 such attempts. The wording
    follows ``world_repair``'s own oversized receipt so a driver reads one sentence for both.
    """
    return (
        f"{app_id} {list(group)}: a prompt of {size:,} characters is beyond the {ceiling:,} the "
        "provider accepts in one call, after sampling the state it repairs; not sent"
    )


def group_evidence(group):
    return f"named_collections={list(group)}"


# The whole merged state, told apart from the group that happens to hold every key.
STATE_EVIDENCE = "merged state"


def group_finding(app_id, issue, evidence, severity="error"):
    """One authoring failure, shaped like every other piece of author feedback.

    ``severity`` is "warning" for a failure no stage can repair -- a prompt over the provider's
    ceiling is the only one so far. A gate that blocks on a defect the pipeline cannot repair is a
    permanent stall, and ``mechanical_feedback`` only sends errors back, so a warning is recorded
    and never retried.
    """
    return {"target": app_id, "severity": severity, "issue": issue, "evidence": evidence}


# A repaired collection that keeps less than this share of the records it was given, having been
# asked about fewer, is refused rather than written. Measured over the 8,972 collection answers on
# disk: only 207 (2.3%) made a collection smaller at all, and the losses are not trims but wipes --
# the same 110 answers breach every floor from 90% down to 50%, and the twelve worst are 761 -> 0,
# 755 -> 0, 592 -> 0 with *zero* surviving ids. At this floor, with the minimum below, the guard
# fires on 110 of 8,972 answers (1.2%) and saves 27,533 records.
COLLAPSE_FLOOR = 0.5
COLLAPSE_MIN_RECORDS = 20  # below this a shrink is ordinary editing; every one of the 110 is above it


def collapsed_collections(before, after, allowance):
    """Collections the answer shrank past the floor while removing more records than it was asked
    about; for each, how many it held and how many came back.

    The defect this catches, traced to the call that did it: cort-business-services-corporation's
    contractbook_mock repair was given 540 activities and findings about ``contracts`` -- a
    collection outside its group -- and answered ``{}``, saying so in its rationale ("the reported
    defects require editing contracts, which this collection group excludes"). The author read that
    as a keys mismatch, retried with the previous state dropped, and wrote the 28 activities the
    retry invented over the 540 it had. Zero id overlap. That is one of 45 isolated collapses across
    26 companies, 10,581 records, and no contract the author can state will stop a model answering
    short -- so the check belongs at the moment of writing, where every path converges.

    ``allowance`` is how many records the findings name: the most a narrowed repair has any business
    removing. A uniformity finding that genuinely consolidates a collection will breach this and be
    refused once, which is the right trade: the records are kept and the refusal is recorded, where
    the alternative is a silent wipe no later stage can tell from a world that never had them.
    """
    collapsed = {}
    for key, value in (before or {}).items():
        had = record_count(value)
        if had < COLLAPSE_MIN_RECORDS or not isinstance(after, dict) or key not in after:
            continue
        now = record_count(after[key])
        if now >= had * COLLAPSE_FLOOR or (had - now) <= allowance:
            continue
        collapsed[key] = (had, now)
    return collapsed


def names_collection(text, keys):
    """The collections of ``keys`` that ``text`` names as a whole word, not as part of one."""
    return {k for k in keys if re.search(rf"(?<![\w.]){re.escape(k)}(?![\w])", text)}


def findings_for_group(feedback, group, keys):
    """The findings a group can act on: those naming one of its collections, or naming none at all.

    A group used to be sent every finding the app had. cort's contractbook_mock group
    ``[activities, notifications, comments, savedViews]`` was sent three findings about ``contracts``
    and answered ``{}`` -- correctly, since it could neither see nor return that collection -- and the
    retry that followed destroyed 540 records. Asking a group only what it can answer removes the
    wrong question instead of coping with the right answer to it.
    """
    kept = []
    for item in feedback or ():
        named = names_collection(f"{item.get('evidence') or ''} {item.get('issue') or ''}", keys)
        if not named or named & set(group):
            kept.append(item)
    return kept


def salvage_group(group, partial, previous):
    """What a failed group is filled with: what the call returned, else what the group already had.

    Every documented key has to be present for the state to validate at all, so a group with
    nothing to show for it still leaves its collections empty rather than absent.
    """
    filled = {}
    for key in group:
        if isinstance(partial, dict) and key in partial:
            filled[key] = partial[key]
        elif isinstance(previous, dict) and key in previous:
            filled[key] = previous[key]
        else:
            filled[key] = []
    return filled


def make_author(
    root,
    folder,
    config,
    models,
    instructions,
    *,
    world,
    reference_date,
    operating_scope,
    identities,
    worker_apps,
    company,
    tasks,
    apps,
    receipts,
    problems=None,
    population_plan=None,
):
    """The app-state author for one world: ``author(app, feedback, previous, only)`` writes or
    repairs one app's state through bounded collection-group calls.

    Seeding builds it from the freshly authored core; the bulk stage rebuilds the same author
    from the seeded folder (``folder_author``) so a mechanical repair after bulk follows the
    path seeding took. ``receipts`` collects one entry per call, keyed by app, and ``problems``
    the groups the author could not write, as feedback findings keyed by app: the author never
    raises over one group, because raising lost every other app in the same seed.
    """
    group_size = config["design"].get("seed_collections_per_call", 4)
    problems = {} if problems is None else problems

    def note(app_id, issue, evidence, severity="error"):
        problems.setdefault(app_id, []).append(group_finding(app_id, issue, evidence, severity))

    def forget(app_id, evidence):
        """Drop an earlier failure with this evidence; this pass has just rewritten it."""
        kept = [p for p in problems.get(app_id, []) if p["evidence"] != evidence]
        if kept:
            problems[app_id] = kept
        else:
            problems.pop(app_id, None)

    # The earlier-history records this author has already generated, keyed by (app, collection).
    # They are bought once per author and re-merged on every later repair of that app.
    shard_records = {}
    # (app, group) pairs whose prompt will not fit the provider even sampled, with the receipt line
    # that says so. Terminal for the life of this author: the refusal is recorded once and the group
    # is never dispatched again, which is the whole difference from the 396 attempts on disk.
    refused = {}
    # Volume: a collection the first call fills to shard_min records or more is extended by
    # shards-1 further calls, each covering an earlier window of the history. One response
    # holds a few dozen emails; an inbox holds hundreds.
    shards = config["design"].get("seed_shards", 3)
    shard_min = config["design"].get("seed_shard_min_records", 15)
    # How much of a declared population one repair round may cover, in batches. A knob because the
    # table above is a budget trade rather than a fact: see REPAIR_POPULATION_BATCHES.
    population_batches_per_round = config["design"].get("seed_population_batches", REPAIR_POPULATION_BATCHES)
    core_view = {"world": world, "reference_date": reference_date, "operating_scope": operating_scope}
    if population_plan:
        core_view["population_plan"] = population_plan

    def author(app, feedback=None, previous=None, only=None):
        """Author one app's state; ``only`` limits a repair to the groups holding those keys."""
        # Only the workers the grant map gives this app. The identity book covers every worker in
        # every app with an identity_key, as insurance for a grant a mined task widens later, but
        # an identity the author is shown is a person it writes records for -- and it was shown all
        # of them: greenbrier's jira users held all 7 workers where 2 hold jira, and the 5 who
        # cannot open it were named 108 times in the state against 91 for the two who can. This is
        # also what ``splice_identities`` puts back and ``check_state_identities`` demands, so
        # narrowing here narrows the whole chain instead of contradicting it.
        ids = {
            w: recs[app["app_id"]]
            for w, recs in identities.items()
            if app["app_id"] in recs and app["app_id"] in worker_apps.get(w, ())
        }
        if feedback is not None and previous is not None and only is None:
            # A repair touches the collections the findings name; the rest keep their records,
            # sharded volume included. Findings without a path redo every group.
            named = set()
            for f in feedback:
                path = str(f.get("evidence") or "") + " " + str(f.get("issue") or "")
                named |= names_collection(path, app["top_level_keys"])
            only = named or None
        # Real record collections already get separate calls. Context size must not
        # turn small interface/directory groups into many identical-context calls.
        size = group_size
        keys = app["top_level_keys"]
        fields = record_field_catalogue(root).get(app["app_id"]) or {}
        if previous:
            # A repair has the state in hand, so it need not take the catalogue's word for which
            # collections hold records: 91 of the 658 collections holding 15 records or more in a
            # seeded world are not in the catalogue, slack_mock's channel-keyed messages among
            # them, in all 60 worlds. Those get a call of their own here too.
            fields = dict(fields) | {
                k: fields.get(k, []) for k in previous if record_count(previous[k]) >= shard_min
            }
        state = {}
        compiled = {}
        if (
            config["design"].get("seed_compile_directories")
            and app["app_id"] == "hubspot_mock"
            and not previous
        ):
            from .population_directories import hubspot_directories

            canonical = canonical_people(world, list(worker_apps))
            owners = [
                ids.get(w) or canonical.get(w, {})
                for w, grants in worker_apps.items()
                if app["app_id"] in grants
            ]
            compiled = hubspot_directories(world, population_plan or [], owners)
            state.update(compiled)
        small_limit = config["design"].get("seed_small_collection_limit", 0)
        small = {
            p["collection"]
            for p in population_plan or []
            if p["app_id"] == app["app_id"] and p["target_records"] <= small_limit
        }
        for group in collection_groups(keys, fields, size, small):
            group = [k for k in group if k not in compiled]
            if not group:
                continue
            if only is not None and previous is not None and not set(group) & only:
                state.update({k: previous[k] for k in group if k in previous})
                continue
            held = {k: previous[k] for k in group} if previous is not None else None
            # A repair is sent a sample of what it is repairing and answers with a patch; the
            # records it does not return are the records it keeps. Only a genuine repair patches:
            # the retries below hand the call something other than ``held`` and take the answer
            # whole, exactly as they did before.
            # Only the findings this group can act on. A group sent a finding about a collection it
            # does not hold answers correctly that it cannot act on it, and the retry that followed
            # such an answer is what destroyed cort's 540 activities; see ``findings_for_group``.
            group_findings = (
                findings_for_group(feedback, group, app["top_level_keys"]) if feedback else feedback
            )
            if feedback and held is not None and not group_findings:
                # Nothing here to repair. Keeping the records is the answer, and it costs no call.
                state.update({k: previous[k] for k in group if k in previous})
                continue
            cited = patch_cited_ids(group_findings, held) if group_findings and held else set()
            # A counted finding names a population and prints three of it. One call per batch of that
            # population puts every record it names in front of the author once, where narrowing to
            # the citations repaired 3 of 3,350 and left the count it is measured against unmoved.
            # Without a population this is the single call it always was.
            pop = population_ids(group_findings, held) if group_findings and held else set()
            batches, uncovered = (
                population_batches(held, pop, cap=population_batches_per_round) if pop else ([cited], 0)
            )
            covered, spent = set(), 0
            # One bounded retry per group: an empty or mis-keyed group is a cheap slip, not a
            # reason to discard every other group and the 20-minute core call.
            partial, problem, receipt, collapse = None, None, None, None
            refusal = refused.get((app["app_id"], tuple(group)))
            for batch in batches:
                group_previous = (
                    bound_previous(app["app_id"], held, batch, only_cited=bool(pop))
                    if group_findings and held
                    else held
                )
                patching = group_previous is not held
                group_feedback = group_findings
                for attempt in range(2):
                    if refusal is not None:
                        break
                    prompt = (
                        instructions
                        + "\n"
                        + json.dumps(
                            app_payload(
                                core_view,
                                app,
                                ids,
                                worker_apps,
                                company,
                                tasks,
                                group_feedback,
                                group_previous,
                                group,
                                record_fields={k: fields[k] for k in group if fields.get(k)},
                                # A call that covers one collection cannot see the ids its siblings
                                # were given, so the small collections already written go with it.
                                authored=reference_collections(state, group, budget=REFERENCE_BUDGET),
                                patch=patching,
                                population=len(batch) if pop else None,
                            ),
                            ensure_ascii=False,
                        )
                    )
                    if len(prompt) > PROMPT_CEILING and group_previous is not None:
                        # Shrink once. The sample is already bounded, so what is left over the ceiling
                        # is the group itself: author it fresh rather than refuse it outright.
                        group_previous, patching = None, False
                        continue
                    if len(prompt) > PROMPT_CEILING:
                        # Terminal, and recorded: a refusal raised past the caller is a refusal the
                        # caller retries unchanged, which is how one call id reached 162 attempts that
                        # each failed in 0.4 s. Nothing here can shrink the prompt further, so the group
                        # is never dispatched again and the world keeps what it already had.
                        refusal = oversized_prompt(app["app_id"], group, len(prompt), PROMPT_CEILING)
                        refused[(app["app_id"], tuple(group))] = refusal
                        break
                    try:
                        result, receipt = models.call("world_states", prompt, AppStateResult)
                    except PromptTooLarge as exc:
                        # A ceiling this path did not measure: a provider ceiling below PROMPT_CEILING,
                        # or a skill that grew. Shrink once the same way, then refuse terminally rather
                        # than let an exception nothing catches become a retry nothing can satisfy.
                        if group_previous is not None:
                            group_previous, patching = None, False
                            continue
                        refusal = oversized_prompt(
                            app["app_id"],
                            group,
                            getattr(exc, "size", len(prompt)),
                            getattr(exc, "ceiling", PROMPT_CEILING),
                        )
                        refused[(app["app_id"], tuple(group))] = refusal
                        break
                    partial, problem = None, None
                    # An answer for another app is a confusion about what was asked, not a slip in one
                    # group, and it has never happened in the 99 companies on disk; the repair stages
                    # read it as a reason to stop before they spend a round on it.
                    if result.app_id != app["app_id"]:
                        raise ValueError(f"app_state call for {app['app_id']} returned {result.app_id}")
                    try:
                        partial = parse_json(
                            result.state_json,
                            f"{app['app_id']} collection group",
                            models=models,
                            expected=group,
                        )
                    except (ValueError, TypeError) as exc:
                        # Malformed JSON, or a payload that is not an object at all, is a group slip
                        # like any other: retry the group once with the parser's complaint. The
                        # TypeError used to escape this handler uncaught -- "gmail_mock collection
                        # group must be a JSON object" took piper-sandler, eaglebrook and
                        # ultimus-fund-solutions with every app they had already authored.
                        problem = f"invalid JSON: {exc}"
                    if problem is not None:
                        if attempt == 1:
                            break
                        group_feedback = [group_finding(app["app_id"], problem, group_evidence(group))]
                        # The same rule as the keys retry below: only a first pass retries with a
                        # clean slate. A repair that answered with unparseable JSON still has a
                        # collection to repair, and handing the retry ``null`` is how a retry came to
                        # write a freshly invented collection over a real one.
                        if held is None:
                            group_previous, patching = None, False
                        continue
                    keys_match = set(partial) == set(group)
                    all_empty = keys_match and all(partial[k] in (None, "", [], {}) for k in group)
                    # Exact keys, with something in them, is the normal case. Exact keys but every
                    # collection empty gets one retry (a dropped Sheets group looked like this); if
                    # the author insists, the collections really are empty (selection state, an
                    # upload queue) and the downstream checks judge the whole state.
                    if keys_match and (not all_empty or attempt == 1):
                        if patching:
                            # The patch is the answer; the collection is what it is laid over.
                            partial = {k: apply_patch(held.get(k), partial.get(k)) for k in group}
                        # The write-time guard. No output contract can stop a model answering short,
                        # so the records it dropped without being asked about them are put back here,
                        # once, before anything reaches disk.
                        collapsed = collapsed_collections(held, partial, max(3, len(pop | cited)))
                        if collapsed:
                            partial = {**partial, **{k: held[k] for k in collapsed}}
                            collapse = (
                                f"{app['app_id']} kept "
                                + ", ".join(
                                    f"{now:,} of {had:,} {k}" for k, (had, now) in sorted(collapsed.items())
                                )
                                + f" while the findings name {max(3, len(pop | cited))} record(s); the "
                                "records it dropped were put back and the repair did not land"
                            )
                            if attempt == 0:
                                group_feedback = [
                                    group_finding(app["app_id"], collapse, group_evidence(group))
                                ]
                                continue
                        break
                    problem = (
                        (
                            f"{app['app_id']} repair of {group} returned no records to change; return the "
                            "corrected records themselves"
                            if patching
                            else f"{app['app_id']} group returned every collection empty: {group}; populate them from the canonical world"
                        )
                        if all_empty
                        else f"{app['app_id']} group missing documented keys or returned extra keys: "
                        f"expected {group}, got {sorted(partial)}"
                    )
                    if attempt == 1:
                        break
                    group_feedback = [group_finding(app["app_id"], problem, group_evidence(group))]
                    # A first pass is shown its own bad answer to write over. A repair is not: the
                    # previous state IS the thing being repaired, and dropping it is how a retry came
                    # to author cort's collection fresh and write 28 records over 540. The repair
                    # keeps what it was given, and its contract with it.
                    if held is None:
                        group_previous, patching = partial or None, False
                if refusal is not None or problem is not None:
                    break
                # The batch landed. It becomes what the next batch of the same population is laid
                # over, so the patches accumulate instead of each one reverting the last.
                if held is not None and patching:
                    held = partial
                covered |= batch
                spent += 1
                receipts.setdefault(app["app_id"], []).append(
                    {
                        **receipt,
                        "collections": group,
                        **({"population": len(pop), "batch": spent, "of": len(batches)} if pop else {}),
                    }
                )
            # A group the author could not write after its retry is one collection's worth of
            # records, and raising here discarded every other app's finished calls along with the
            # whole core -- nothing was written at all. It becomes a finding the repair loop and
            # CHECKS.json carry, and the group keeps the best content there is.
            forget(app["app_id"], group_evidence(group))
            if refusal is not None:
                # Recorded as a warning, not an error. Nothing in the pipeline can shrink this
                # prompt, so an error here is a gate blocking on a defect no stage can repair --
                # a permanent stall -- and ``mechanical_feedback`` sends only errors back, which
                # is what keeps the group from being dispatched again.
                note(app["app_id"], refusal, group_evidence(group), severity="warning")
                state.update(salvage_group(group, None, held if held is not None else previous))
                receipts.setdefault(app["app_id"], []).append(
                    {
                        "job": "world_states",
                        "status": "refused",
                        "collections": group,
                        "reason": refusal,
                    }
                )
                continue
            if problem is not None:
                note(app["app_id"], problem, group_evidence(group))
                # A patch that failed its checks is not a collection: taking it as one would write
                # the handful of records it holds over the thousands it was laid against. Under the
                # whole-collection contract the faulted answer *was* the collection, so it was worth
                # salvaging; here the best content is what the batches before it already merged.
                partial = salvage_group(
                    group, None if patching else partial, held if held is not None else previous
                )
            if collapse is not None:
                # An error, so the round that follows asks again, and the receipt says the repair did
                # not land. It is not a refusal: the world keeps every record it had.
                note(app["app_id"], collapse, group_evidence(group))
            if pop and len(covered) < len(pop):
                # The honest receipt for a population repair that did not reach the whole population.
                # Without it a repair that changed 3 records of 3,350 writes what a complete repair
                # writes, the checker's count does not move, and the bounded rounds read as a repair
                # that failed rather than one that is most of the way done. It is an error, so the
                # next round asks for the rest -- the checker recomputes the population each round,
                # so the batches that landed are not sent again.
                note(
                    app["app_id"],
                    f"{app['app_id']} {list(group)}: repaired {len(covered):,} of the {len(pop):,} "
                    f"records the findings name; the rest are unchanged and still match the finding "
                    f"({spent} of {len(batches)} batches sent"
                    + (f", {uncovered:,} records beyond this pass's batch cap)" if uncovered else ")"),
                    group_evidence(group),
                )
            state.update(partial)
        if shards > 1:
            redone = set(keys) if feedback is None else (only or set(keys))
            for key in keys:
                if (
                    key in redone
                    and history_eligible(key, state.get(key))
                    and record_count(state.get(key)) >= shard_min
                ):
                    state[key] = extend_collection(app, key, state, ids)
        result = AppStateResult(app_id=app["app_id"], rationale="merged groups", state_json=json.dumps(state))
        forget(app["app_id"], STATE_EVIDENCE)
        try:
            outcome = validate_app_state(result, apps[app["app_id"]], root, ids, models=models)
        except (ValueError, TypeError) as exc:
            # Workers missing from the app's own directory is a slip in the user collection,
            # not a reason to lose the app: redo those groups once with the exact complaint.
            if str(exc).startswith("identity mismatch") and only is None:
                user_keys = {k for k in keys if k in USER_COLLECTIONS} or set(keys)
                complaint = [
                    {
                        "target": app["app_id"],
                        "severity": "error",
                        "issue": str(exc),
                        # Name every field the validator compares. The complaint used to stop at
                        # email while the check also read username, so a username disagreement was
                        # retried with instructions that never mentioned it and failed the same way.
                        "evidence": (
                            "every identity in identities_for_this_app must appear as a record in the "
                            "user collection with the same id, name, email and username, copied "
                            "character for character from that identity"
                        ),
                    }
                ]
                return author(app, complaint, state, only=user_keys)
            # The same reasoning one level up: a state its own validator faults is still every
            # other app's finished work. The fault is recorded against this app and the state is
            # returned, so the world reaches disk and a repair stage can reach it.
            note(app["app_id"], f"state validation failed: {exc}", STATE_EVIDENCE)
            outcome = state
        register_people(folder.parent, folder.name, *people_in(outcome))
        return app["app_id"], outcome

    def extend_collection(app, key, state, ids):
        """Earlier months of the same collection, one call per window, merged by id.

        The generated records are kept per (app, collection) for the life of this author, so a
        repair re-merges the history it already paid for instead of buying it again. Repairs
        rewrite the recent, task-relevant layer; these records sit weeks earlier and outside it,
        and 5,179 duplicate shard calls inside single generations cost 442 model-hours.
        """
        merged = state[key]
        cached = shard_records.get((app["app_id"], key))
        if cached is not None:
            for records in cached:
                merged = merge_records(merged, records)
            return merged
        produced = []
        target = next(
            (p for p in population_plan or [] if p["app_id"] == app["app_id"] and p["collection"] == key), {}
        )
        windows = time_windows(
            reference_date, shards, first_date=target.get("first_date"), last_date=target.get("last_date")
        )
        for index, (start, end) in enumerate(windows, 1):
            shard = {
                "collection": key,
                "window": {"from": start, "to": end},
                "index": index,
                "of": len(windows),
                "existing_count": record_count(merged),
                "existing_ids": record_ids(merged)[-300:],
                "reference_collections": {
                    k: v for k, v in state.items() if k != key and 0 < record_count(v) <= 60
                },
            }
            body = app_payload(core_view, app, ids, worker_apps, company, tasks, None, None, [key])
            body["shard"] = shard
            body["output_contract"] = (
                f"state_json contains exactly one key, {key!r}, holding only NEW records dated inside "
                "shard.window, none of existing_ids, shaped like the schema, consistent with "
                "reference_collections and the canonical world; compact JSON."
            )
            prompt = skill_for_call(instructions, "app_state") + "\n" + json.dumps(body, ensure_ascii=False)
            try:
                result, receipt = models.call("world_states", prompt, AppStateResult)
                partial = parse_json(result.state_json, f"{app['app_id']} {key} shard {index}", models=models)
            except ValueError as exc:
                # A verdict about this shard, not a fault: the model's exceptions split that way
                # already -- PromptTooLarge and ModelOutputInvalid are ValueErrors, ModelUnavailable
                # and CallBudgetExhausted are RuntimeErrors and pass straight through to stop the
                # step. The receipt is what was missing: a skipped shard left nothing but a line on
                # stderr, so a collection that lost its history read exactly like one that never had
                # any, which is the "a stage with no failure receipt" rule in miniature.
                answer = Receipt.from_exception(exc, f"{app['app_id']} {key} shard {index}")
                print(f"warning: {answer.reason}", file=sys.stderr)
                receipts.setdefault(app["app_id"], []).append(
                    {
                        "job": "world_states",
                        # The shared outcome, so a reader counting what a seed really measured does
                        # not have to know that this stage spells a refusal "skipped". ``status`` is
                        # kept beside it because report.py counts these receipts by that field.
                        **answer.body,
                        "status": "skipped",
                        "collections": [key],
                        "shard": index,
                    }
                )
                continue
            produced.append(partial.get(key))
            merged = merge_records(merged, partial.get(key))
            receipts.setdefault(app["app_id"], []).append(
                {**Receipt.passed().body, **receipt, "collections": [key], "shard": index}
            )
        shard_records[(app["app_id"], key)] = produced
        return merged

    return author


def seed_outcome(status):
    """The shared outcome for a SEED.json ``status``, so both writers of that file agree on it.

    ``seed_world`` writes SEED.json and ``world_repair.review_repair`` rewrites its verdict after a
    repair round, which is two places deciding what the same receipt says. The outcome lives here so
    a status flipped in one of them cannot leave the other's word behind -- a stale marker and a
    lying receipt are the same defect wearing different hats.

    ``status`` folds the review verdict and the mechanical checks into one word and three gates
    parse it; the outcome answers only which of the four this run reached. A world seeded with no
    review asked for is a pass *of the seed*: whether the review accepted it is ``review_verdict``
    and ``mechanical_ok``, which is where every gate already looks.
    """
    if status == "seeded_review_failed":
        return Receipt.refused(f"the seeded world was not accepted: {status}")
    return Receipt.passed()


def folder_author(root, folder, models, instructions, config=None, receipts=None, problems=None):
    """``make_author`` for a seeded folder, from its written artifacts. Returns (author, contract)."""
    root, folder = Path(root), Path(folder)
    if config is None:
        config = load_config(root)
    config.setdefault("design", {})
    seed = read(folder / "world" / "SEED.json")
    apps_manifest, contract = app_contract(root, folder)
    author = make_author(
        root,
        folder,
        config,
        models,
        instructions,
        world=read(folder / "world" / "world.json"),
        reference_date=seed["reference_date"],
        operating_scope=seed.get("operating_scope", ""),
        identities=read(folder / "world" / "identities.json"),
        worker_apps=read(folder / "world" / "worker_apps.json"),
        company=company_view(read(folder / "company.json")),
        tasks=task_designs(folder),
        apps={app["app_id"]: app for app in apps_manifest["apps"]},
        receipts=receipts if receipts is not None else {},
        problems=problems,
        population_plan=read(folder / "world/population.json")
        if (folder / "world/population.json").is_file()
        else None,
    )
    return author, contract


MECHANICAL_REPAIR_ROUNDS = 2


def with_problems(checks, problems):
    """The mechanical check result with the author's unwritten groups counted as its own errors.

    The author no longer raises when a collection group defeats its retry, so the world reaches
    disk with every other app's work intact; the group that failed is a defect of that world and
    is carried where the repair loop, CHECKS.json and the gates already look, rather than in a
    field of its own that nothing reads.

    Each problem keeps its own severity. A group refused because its prompt is over the provider's
    ceiling is a warning: no stage can shrink it, and counting it as an error makes the world block
    forever on a defect nothing can repair -- which is also why only the errors here move ``ok``.
    """
    if not problems:
        return checks
    findings = [
        finding(item.get("severity", "error"), app_id, item["evidence"], item["issue"])
        for app_id, items in sorted(problems.items())
        for item in items
    ]
    errors = [f for f in findings if f["severity"] == "error"]
    return {
        **checks,
        "ok": checks.get("ok", False) and not errors,
        "errors": checks.get("errors", 0) + len(errors),
        "warnings": checks.get("warnings", 0) + len(findings) - len(errors),
        "findings": list(checks.get("findings") or []) + findings,
    }


def mechanical_feedback(checks, apps):
    """Error findings of a check result as author feedback, grouped by the app they name.

    A drift finding names a pair ("google_docs_mock~google_drive_mock"); both copies get it.
    Findings on the world, the identities or the materials name no app and are left out: the
    app author cannot fix those.

    ``records`` is carried through when the checker sets it: the ids of the whole population a
    counted finding is about, as against the three exemplars its message prints. This is the one
    field that separates "repair these three records" from "repair these 3,350"; see
    ``population_ids`` for why the distinction decides whether the repair moves the count at all.
    A checker that does not set it behaves exactly as before.
    """
    by_app = {}
    for f in checks.get("findings", []):
        if f.get("severity") != "error":
            continue
        for source in str(f.get("source", "")).split("~"):
            if source in apps:
                by_app.setdefault(source, []).append(
                    {
                        "target": source,
                        "severity": "error",
                        "issue": f["message"],
                        "evidence": f.get("path", ""),
                        **({"records": list(f["records"])} if f.get("records") else {}),
                    }
                )
    return by_app


def repair_mechanical(contract, states, checks, check, author, max_rounds=MECHANICAL_REPAIR_ROUNDS):
    """Send app-sourced mechanical errors back to the app author, at most ``max_rounds`` times.

    ``states`` is repaired in place through ``author(app, feedback, previous)``; ``check()``
    re-runs the mechanical check over the current states. Stops as soon as the check passes or
    no error names an app. Returns the last check result and the number of rounds run.
    """
    rounds = 0
    while not checks["ok"] and rounds < max_rounds:
        by_app = mechanical_feedback(checks, states)
        if not by_app:
            break
        rounds += 1
        for app in contract:
            if app["app_id"] in by_app:
                _, states[app["app_id"]] = author(app, by_app[app["app_id"]], states[app["app_id"]])
        checks = check()
    return checks, rounds


def seed_world(root, folder, *, timeout_seconds=1500, models=None, concurrency=8, review_rounds=1):
    """Author and write the world; every model call is cached under world/calls like other stages."""
    root, folder = Path(root), Path(folder)
    config = load_config(root)
    config.setdefault("design", {}).update(getattr(models, "config", {}).get("design", {}))
    group_size = config["design"].get("seed_collections_per_call", 4)
    if type(group_size) is not int or group_size < 1:
        raise ValueError("design.seed_collections_per_call must be a positive integer")
    manifest = read(folder / "MANIFEST.json")
    apps_manifest, contract = app_contract(root, folder)
    apps = {app["app_id"]: app for app in apps_manifest["apps"]}
    workers = apps_manifest.get("workers") or []
    instructions, skill_record = load_skill(root)
    if models is None:
        config.setdefault("models", {}).setdefault("world_states", config["models"].get("expand"))
        config.setdefault("generation", {})["completion_timeout_seconds"] = timeout_seconds
        # A world with no ceiling can run away: the most expensive one authored so far spent 653
        # calls over 43 hours while a median world spends 263. Zero or absent leaves it uncapped.
        models = Models(
            config,
            folder / "world",
            max_calls=config["design"].get("seed_call_budget") or None,
            cumulative=True,
        )

    payload = core_payload(root, folder)
    # The canonical world is the one long generation of a seed; give it half again the time
    # an app group gets rather than losing forty minutes of output to the clock.
    core_timeout = int(timeout_seconds * 1.5)
    core_reused = (folder / "world/CORE.json").is_file()
    if core_reused:
        from .blueprint import load_core

        core, core_receipt = load_core(root, folder)
    else:
        core, core_receipt = models.call(
            "world_states",
            skill_for_call(instructions, "world_core") + "\n" + json.dumps(payload, ensure_ascii=False),
            WorldCore,
            timeout_seconds=core_timeout,
        )
    # The company-wide grant, settled at the binding stage: one rule in one place, covering the
    # whole roster and already narrowed to the apps this company declares. Imported here because
    # worker_apps imports this module.
    from .worker_apps import company_grants

    contract_apps = company_grants(folder)

    def finalize(core):
        # Identities are derived from the canonical world's own staff records: the app authors
        # copy those into every directory, document and message, so an identity that names
        # anyone else makes two people of one worker (jabil, Pennsylvania DGS).
        world, identities, materials, worker_apps = validate_core(
            core, apps, workers, models=models, contract_apps=contract_apps
        )
        reconcile_identities(identities, canonical_people(world, workers))
        return world, identities, materials, worker_apps

    def staff_of(core):
        # The same staff records for the identities-only repair, read from the rejected core.
        try:
            return canonical_people(parse_json(core.entities_json, "entities_json", strict=False), workers)
        except (ValueError, TypeError):
            return {}

    try:
        world, identities, materials, worker_apps = finalize(core)
    except ValueError as exc:
        if "not distinct people" in str(exc) or "missing identity" in str(exc):
            # Bounded repair: rewrite identities only, keep the 20-minute world and materials.
            core = repair_identities(
                core, apps, workers, payload["company"], str(exc), models, instructions, people=staff_of(core)
            )
        elif "generator rules" in str(exc):
            # A compact-table world has no data at all. Redo the call once, with the rejection.
            core, core_receipt = models.call(
                "world_states",
                instructions
                + "\n"
                + json.dumps({**payload, "previous_attempt_rejected": str(exc)}, ensure_ascii=False),
                WorldCore,
                timeout_seconds=core_timeout,
            )
        else:
            raise
        world, identities, materials, worker_apps = finalize(core)
    # One worker with two emails across apps, or two workers sharing one, is a core slip the
    # identities-only call fixes cheaply; app states copy identities, so fix it before them.
    identity_errors = [
        f["message"]
        for f in check_identities(identities, payload["company"].get("workers", []))
        if f["severity"] == "error"
    ]
    if identity_errors:
        core = repair_identities(
            core,
            apps,
            workers,
            payload["company"],
            "; ".join(identity_errors[:6]),
            models,
            instructions,
            people=canonical_people(world, workers),
        )
        world, identities, materials, worker_apps = finalize(core)
    names, emails = people_in([world, identities])
    register_people(folder.parent, folder.name, names, emails)
    company = payload["company"]
    population_plan = [p.model_dump() for p in getattr(core, "population_plan", [])]
    receipts, problems = {}, {}
    author = make_author(
        root,
        folder,
        config,
        models,
        instructions,
        world=world,
        reference_date=core.reference_date,
        operating_scope=core.operating_scope,
        identities=identities,
        worker_apps=worker_apps,
        company=company,
        tasks=payload["tasks"],
        apps=apps,
        receipts=receipts,
        problems=problems,
        population_plan=population_plan,
    )

    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(contract)))) as pool:
        states = dict(pool.map(author, contract))

    def mechanical(materials):
        # The check stage's world-check, run on the world as it will be written: the parts not
        # on disk yet are passed in, keyed the way the files will be; the app list, the dossier
        # workers and the bulk exclusion come from the folder exactly as they will then.
        return check_folder(
            root,
            folder,
            states,
            world=world,
            identities=identities,
            materials={f"{m.worker_id}/{m.path}": m.content for m in materials},
            reference_date=core.reference_date,
            # The grant map: without it check_actor_logins has no contract to read and does not
            # run, so a record assigned to a worker who cannot open the app went unseen until a
            # model reviewer happened to notice it (2,054 such records over the seeded worlds).
            worker_apps=worker_apps,
            # Uniformity rules judge the world being written; they never re-judge one on disk.
            authoring=True,
        )

    def checked(materials):
        """The same check, plus the collection groups the author could not write."""
        return with_problems(mechanical(materials), problems)

    # Mechanical errors that name an app (stub messages, generator rules, bad references) get one
    # repair pass before any reviewer sees the world; the reviewer's time goes to judgement. The
    # author's own unwritten groups join them: that is the repair they used to be raised instead of.
    repair_mechanical(contract, states, checked(materials), lambda: checked(materials), author, 1)

    # Volume shortfalls need bounded population work, not a broad rewrite or a
    # semantic vote that cannot establish the missing records. Preserve outputs.
    population_errors = population_findings(states, population_plan)
    if population_errors:
        verdict, history = None, []
    else:
        verdict, history, materials = review_world(
            root,
            config,
            models,
            review_rounds=review_rounds,
            core=core,
            company=company,
            tasks=payload["tasks"],
            world=world,
            identities=identities,
            worker_apps=worker_apps,
            materials=materials,
            states=states,
            apps=apps,
            contract=contract,
            author=author,
            mechanical=mechanical,
            instructions=instructions,
        )
    checks = with_problems(checked(materials), population_errors)
    # A world the reviewers accepted but the mechanical rules still fault (a directory entry
    # without an email, a stray pipeline word) goes back to the app author with those exact
    # findings, twice at most, instead of being thrown away over a repairable detail.
    mechanical_rounds = 0
    if verdict is not None and verdict.verdict == "accept":
        checks, mechanical_rounds = repair_mechanical(
            contract, states, checks, lambda: checked(materials), author, MECHANICAL_REPAIR_ROUNDS
        )
    # A semantic repair may replace a collection; check its final population too.
    population_errors = population_findings(states, population_plan)
    checks = with_problems(checks if not population_errors else checked(materials), population_errors)
    status = (
        "seeded_review_failed"
        if population_errors
        else (
            "seeded_not_verified"
            if not review_rounds
            else "seeded_reviewed"
            if verdict.verdict == "accept" and checks["ok"]
            else "seeded_review_failed"
        )
    )

    world_dir = folder / "world"
    for app_id, state in states.items():
        write(world_dir / f"{app_id}.state.json", state)
    write(world_dir / "identities.json", identities)
    write(world_dir / "worker_apps.json", worker_apps)
    write(world_dir / "world.json", world)
    for item in materials:
        target = world_dir / "materials" / item.worker_id / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item.content)
    write(world_dir / "CHECKS.json", checks)
    if history:
        write(world_dir / "REVIEW.json", {"rounds": history, "final_verdict": verdict.verdict})
    write(
        world_dir / "SEED.json",
        {
            "seeded_at": now(),
            **seed_outcome(status).body,
            "skill": skill_record,
            "reference_date": core.reference_date,
            "operating_scope": core.operating_scope,
            "rationale": core.rationale,
            "assumptions": core.assumptions,
            "receipts": {"world_core": core_receipt, "app_states": receipts},
            "mechanical_checks": {"errors": checks["errors"], "warnings": checks["warnings"]},
            "mechanical_repair_rounds": mechanical_rounds,
            # Collection groups the author could not write, each already counted in the checks.
            "author_problems": problems,
            "review": verdict.verdict if verdict else None,
            # "status" folds review and mechanics into one word for compatibility; gates and
            # scoreboards read these two fields, which say which of the two held.
            "review_verdict": verdict.verdict if verdict else None,
            "mechanical_ok": checks["ok"],
            "status": status,
            "population_plan": population_plan,
            "population_shortfalls": population_errors,
        },
    )
    manifest["stages"]["stage2_world"] = status
    manifest["hashes"] = {
        str(p.relative_to(folder)): digest(p.read_bytes())
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p != folder / "MANIFEST.json"
    }
    write(folder / "MANIFEST.json", manifest)
    return {
        "apps": sorted(states),
        "workers": sorted(identities),
        "materials": len(materials),
        # Calls, not receipts: a group refused before dispatch writes a receipt saying so and
        # spends nothing, and counting it as a call is the "an empty run looks like a real one"
        # mistake in miniature.
        "calls": int(not core_reused)
        + sum(1 for r in receipts.values() for item in r if item.get("call_id"))
        + len(history),
        "mechanical": {"errors": checks["errors"], "warnings": checks["warnings"]},
        "review": verdict.verdict if verdict else None,
        "review_verdict": verdict.verdict if verdict else None,
        "mechanical_ok": checks["ok"],
        "status": status,
    }
