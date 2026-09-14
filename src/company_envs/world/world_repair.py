"""Repair a checked world in place, the way seeding and the bulk stage repair theirs.

The check stage runs ``world-check`` on worlds the reviewers accepted and a mechanical error there
stopped the world for good, even one a checker rule that landed after the seed would have sent
back to the author. Seeding repairs such errors with ``repair_mechanical`` (app-sourced findings
go back to the app author, bounded rounds) and the bulk stage does the same after laying bulk;
this module gives the check stage that path, on a folder whose author is rebuilt from its
artifacts (``folder_author``).

Two kinds of error are repaired, in at most ``rounds`` rounds:

* an error naming an app goes to that app's author with the exact finding. The author rewrites
  the human layer (the seeded state without the records BULK.json names) and the bulk is laid
  again on top from the specs BULK.json holds, as ``add_bulk`` does: the human layer is what
  the author wrote and what it can answer for; the bulk repeats by design;
* an error naming the canonical world (``source: "world"``: pipeline vocabulary in a policy, a
  reply dated before its parent) goes to one small ``world_records_repair`` call that returns
  the faulty records only, spliced back where their JSON pointers say. world.json is written
  before the app author is built, so the authors see the repaired world.

Errors no author owns (identities, materials, the prose gate over every app) and errors that
appear only with the bulk records (a spec's one-line bodies) are left as they are and named in
REPAIR.json. Model calls go through ``Models`` and are cached under world/calls.

``repair_review`` is the same recovery for the *reviewers'* findings, which kill a world one
stage earlier: a rejected world's blocking error findings go back to the apps that own the
records they cite, through the very machinery seeding repairs with, and the reviewers read the
repaired world again. It writes REVIEW-REPAIR.json and, on acceptance, SEED.json's verdict. Each
app's rewrite is narrowed to the records the findings named (``narrow_repair``), and the receipt
carries how many records were put back, the reason every app that could not be repaired was not,
and ``stale``: what the verdict this step started from no longer covers (``stale_verdict``).
"""

import json
import sys
from copy import deepcopy
from pathlib import Path, PurePosixPath

from company_envs.config import load_config
from company_envs.models import ModelOutputInvalid, Models
from company_envs.storage import digest, now, read, write

from .bulk import BulkSpec
from .bulk_layer import bulk_ids_for, lay_bulk, max_bytes_for
from .hub_app import validate_state
from .seed_calls import WorkerMaterial, WorldRecordsRepair
from .state_seed import (
    MECHANICAL_REPAIR_ROUNDS,
    company_view,
    folder_author,
    load_skill,
    mechanical_feedback,
    parse_json,
    repair_mechanical,
    seed_outcome,
    skill_for_call,
    task_designs,
)
from .world_check import check_folder, record_identifiers, records
from .world_review import (
    ReviewFinding,
    cited_ids,
    dedupe_findings,
    make_reviewer,
    materials_diff,
    narrow_repair,
    plan_repair,
    record_holders,
    repair_materials,
    review_coverage,
    review_drift,
    review_inputs,
)

SKILL = "company-world-states"
MAX_RECORD_CHARS = 30_000  # a world object larger than this is a collection, not a record


def strip_bulk(state, ids):
    """The human layer of a layered state: every record carrying an identifier in ``ids`` goes.

    Records are removed from the three shapes ``add_to_state`` appends to: a list, a map of
    lists (channels, threads) and a map keyed by id.
    """
    if not ids:
        return deepcopy(state)

    def keep(record):
        return not (isinstance(record, dict) and record_identifiers(record) & ids)

    out = {}
    for key, value in state.items():
        if isinstance(value, list):
            out[key] = [r for r in value if keep(r)]
        elif isinstance(value, dict) and value and all(isinstance(v, list) for v in value.values()):
            out[key] = {k: [r for r in v if keep(r)] for k, v in value.items()}
        elif isinstance(value, dict):
            out[key] = {k: v for k, v in value.items() if k not in ids and keep(v)}
        else:
            out[key] = value
    return deepcopy(out)


def state_path(folder, app):
    """Where an app's state file lives in a company folder."""
    return Path(folder) / (app.get("state_file") or f"world/{app['app_id']}.state.json")


def read_layers(root, folder):
    """A folder's app states as the two layers a repair works on.

    Returns (the apps.json entries, the BULK.json marker or None, bulk ids per app, the layered
    states on disk, the human layer under them). The human layer is what an author wrote and what
    it can answer for; the bulk repeats by design and is laid again afterwards.
    """
    apps = read(Path(folder) / "apps.json")["apps"]
    marker = Path(folder) / "world" / "BULK.json"
    bulk = read(marker) if marker.is_file() else None
    bulk_ids = bulk_ids_for(root, folder)
    layered = {app["app_id"]: read(state_path(folder, app)) for app in apps}
    human = {a: strip_bulk(s, bulk_ids.get(a, set())) for a, s in layered.items()}
    return apps, bulk, bulk_ids, layered, human


def lay_layers(root, folder, config, apps, bulk, bulk_ids, layered, human):
    """Write every human layer that changed with its bulk laid again on top, as ``add_bulk`` does.

    ``bulk`` and ``bulk_ids`` are updated in place for the records the new laying produced.
    Returns (the states now on disk, whether anything was written).

    Every layer is built and validated before any of them is written. It used to lay, validate and
    write one app at a time with BULK.json last, so an app whose laid state failed its schema left
    the apps before it on disk under a BULK.json naming the layer before *them*: a re-laid drive
    under bookkeeping for the drive it replaced. That is a defect of this exact shape found in the
    post-lay repair, and the window closes by doing all the work that can fail first.
    """
    # Resolved per app below: design.bulk_max_bytes_by_app overrides the default, and
    # bulk_layer.HARD_MAX_BYTES clamps the one app a wrong budget would corrupt.
    generation_seed = config.get("generation", {}).get("seed", 0)
    out, pending, entries = {}, [], {}
    for app in apps:
        app_id = app["app_id"]
        state = deepcopy(human[app_id])
        entry = ((bulk or {}).get("apps") or {}).get(app_id)
        if isinstance(entry, dict) and entry.get("specs"):
            specs = [BulkSpec.model_validate(s) for s in entry["specs"]]
            added, ids, _short = lay_bulk(state, specs, generation_seed, max_bytes_for(config, app_id))
            validate_state(
                {"id": app_id, "state_keys": app.get("top_level_keys") or []},
                state,
                (Path(root) / app["schema"]).read_text(),
            )
            entries[app_id] = (
                entry,
                {"added": added, "bytes": len(json.dumps(state)), "ids": sorted(ids)},
                ids,
            )
        out[app_id] = state
        if state != layered[app_id]:
            pending.append((app, state))
    # Nothing above this line has written a byte. From here it is only file writes.
    for app_id, (entry, update, ids) in entries.items():
        entry.update(update)
        bulk_ids[app_id] = ids
    for app, state in pending:
        write(state_path(folder, app), state)
    changed = bool(pending)
    if changed and bulk:
        write(Path(folder) / "world" / "BULK.json", bulk)
    return out, changed


def refresh_manifest(folder):
    """Re-hash the folder after a repair rewrote files, when it carries a manifest."""
    folder = Path(folder)
    if not (folder / "MANIFEST.json").is_file():
        return
    manifest = read(folder / "MANIFEST.json")
    manifest["hashes"] = {
        str(p.relative_to(folder)): digest(p.read_bytes()) for p in sorted(folder.rglob("*")) if p.is_file()
    }
    write(folder / "MANIFEST.json", manifest)


def world_errors(checks):
    """Error findings on the canonical world alone, each with a JSON pointer into world.json."""
    return [
        f
        for f in checks.get("findings", [])
        if f.get("severity") == "error"
        and f.get("source") == "world"
        and str(f.get("path", "")).startswith("/")
    ]


def record_at(world, path):
    """The record a finding's pointer lies in: (pointer, object) for the longest prefix of
    ``path`` naming a record-sized object, or (None, None) for the root or a whole collection."""
    tokens = path.split("/")[1:]
    while tokens:
        node = world
        for token in tokens:
            key = token.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict):
                node = node.get(key)
            elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
                node = node[int(key)]
            else:
                node = None
                break
        if isinstance(node, dict) and len(json.dumps(node)) <= MAX_RECORD_CHARS:
            return "/" + "/".join(tokens), node
        tokens.pop()
    return None, None


def repair_world_records(world, findings, models, instructions, reference_date, company):
    """One call that rewrites only the world records the findings point at, in place.

    Returns (pointers of the records replaced, receipt). A finding whose pointer names no
    record-sized object is left out; ids are kept whatever the call returns.
    """
    records, feedback = {}, []
    for f in findings:
        pointer, record = record_at(world, f["path"])
        if pointer is None:
            continue
        records[pointer] = record
        feedback.append({"record": pointer, "path": f["path"], "issue": f["message"]})
    if not records:
        return [], None
    payload = {
        "call": "world_records_repair",
        "reference_date": reference_date,
        "company_summary": {k: company.get(k) for k in ("id", "name", "sector", "operations")},
        "feedback": feedback,
        "records": records,
        "rule": (
            "Return records_json: a JSON object whose keys are exactly the pointers in records and "
            "whose values are the corrected records. Change only what feedback names (a date, a parent "
            "link, a phrase); keep every id, every other field and the company's own words. Nothing in "
            "the company's data knows it is generated: no pipeline vocabulary."
        ),
    }
    prompt = skill_for_call(instructions, "world_core") + "\n" + json.dumps(payload, ensure_ascii=False)
    result, receipt = models.call("world_states", prompt, WorldRecordsRepair)
    fixed = parse_json(result.records_json, "world records repair", models=models)
    repaired = []
    for pointer, record in records.items():
        new = fixed.get(pointer)
        if not isinstance(new, dict) or new == record:
            continue
        if "id" in record:
            new["id"] = record["id"]
        record.clear()
        record.update(new)
        repaired.append(pointer)
    return repaired, receipt


def _reason(finding, apps, human_keys, rounds, sampled):
    """Why one error is still in CHECKS.json.

    ``sampled`` maps an app to the size that makes its prompt a sample of its biggest collection
    rather than the whole thing. It used to be the reason the app was never sent at all, and so
    outranked the round count; now the app is sent, so it qualifies the round count instead.
    """
    parts = set(str(finding.get("source", "")).split("~"))
    if not (parts & set(apps) or "world" in parts):
        return "no author owns this source"
    if (finding.get("source"), finding.get("path")) not in human_keys:
        return "appears only with the bulk layer"
    if parts & set(sampled):
        return f"{sampled[min(parts & set(sampled))]}; not repaired in {rounds} round(s)"
    return f"not repaired in {rounds} round(s)"


def repair_world(root, folder, rounds=MECHANICAL_REPAIR_ROUNDS, models=None):
    """Check FOLDER; on errors, repair for at most ``rounds`` rounds, re-lay the bulk, write the
    states, world.json, CHECKS.json and REPAIR.json. Returns the report REPAIR.json holds, with
    ``ok`` saying whether the final check passed."""
    root, folder = Path(root), Path(folder)
    world_dir = folder / "world"
    checks = check_folder(root, folder)
    if checks["ok"]:
        write(world_dir / "CHECKS.json", checks)
        return {"ok": True, "errors": 0, "rounds": 0, "world_records": [], "unrepaired": []}
    config = load_config(root)
    config.setdefault("models", {}).setdefault("world_states", config["models"].get("expand"))
    models = models if models is not None else Models(config, world_dir)
    skill_text, skill = load_skill(root, SKILL)
    seed = read(world_dir / "SEED.json")
    company = read(folder / "company.json")
    apps, bulk, bulk_ids, layered, human = read_layers(root, folder)

    def human_check():
        """The check over the human layers alone: what the authors can answer for."""
        return check_folder(root, folder, states=human, bulk_ids={})

    def relay():
        """Write the repaired human layers with their bulk laid again on top; check the folder."""
        _states, changed = lay_layers(root, folder, config, apps, bulk, bulk_ids, layered, human)
        return check_folder(root, folder, bulk_ids=bulk_ids), changed

    report = {
        "repaired_at": now(),
        "skill": skill,
        "max_rounds": rounds,
        "rounds": 0,
        "before": {"errors": checks["errors"], "warnings": checks["warnings"]},
        "world_records": [],
        "receipts": {"world": [], "apps": {}},
    }
    human_checks = human_check()
    # An app whose biggest collection is larger than a prompt can hold used to be named and not
    # sent, here and in ``repair_review``. That was right while the author had to be given a
    # collection whole and hand it back whole: the alternative was a refusal it answered by
    # discarding every record the reviewers had read.
    #
    # It is now the guard that cancels the repair. Measured over the 406 app layers on disk, 84 of
    # them in 41 of the 60 companies are past this limit -- gmail_mock and google_drive_mock at
    # 1.7-4.0 million characters, macerich terminal on exactly this -- and those 84 are precisely
    # the layers the sampled repair exists for. The author now samples the collection it repairs
    # and takes a patch back (``bound_previous``, ``apply_patch`` in ``state_seed``), which turns
    # macerich's 4,363,037-character prompt into 226,958 and keeps all 3,089 of its emails. So they
    # are sent, and the receipt records that their prompt was a sample.
    #
    # What a sample cannot do is rewrite a whole collection in one round. A collection-wide finding
    # is repaired as far as the sample reaches, round by round, where before it was not repaired at
    # all; ``_reason`` says so beside the round count rather than instead of it.
    sampled = {
        app_id: (
            f"a collection of {largest_collection(state):,} characters is repaired from a sample, "
            f"a patch at a time, because the whole of it is past the {PROMPT_LIMIT:,} "
            "the provider accepts in one call"
        )
        for app_id, state in human.items()
        if too_large_to_send(state)
    }
    if sampled:
        report["receipts"]["sampled"] = sampled
    author = contract = None
    changed = False
    try:
        while not human_checks["ok"] and report["rounds"] < rounds:
            faults = world_errors(human_checks)
            by_app = mechanical_feedback(human_checks, human)
            sendable = set(by_app)
            if not faults and not sendable:
                break  # nothing left that a call could repair
            report["rounds"] += 1
            repaired = []
            if faults:
                world = read(world_dir / "world.json")
                repaired, receipt = repair_world_records(
                    world, faults, models, skill_text, seed.get("reference_date"), company
                )
                if repaired:
                    write(world_dir / "world.json", world)
                    changed = True
                    report["world_records"] += repaired
                    report["receipts"]["world"].append(receipt)
                    author = None  # the app authors read the world from disk
            if not sendable:
                if not repaired:
                    break  # world findings with no record to rewrite: another round changes nothing
                human_checks = human_check()
                continue
            if author is None:
                author, contract = folder_author(
                    root, folder, models, skill_text, config, report["receipts"]["apps"]
                )
            asked = [app for app in contract if app["app_id"] in sendable]
            human_checks, _ = repair_mechanical(asked, human, human_checks, human_check, author, 1)
        checks, relaid = relay()
        changed = changed or relaid
    except (ModelOutputInvalid, ValueError) as exc:
        # A verdict about *this world*: the model's draft was unusable, or a laid state failed its
        # own schema. Record it and write the receipt, because a repair that crashed silently is
        # indistinguishable from a world that needed no repair.
        #
        # Anything else -- an OSError, a provider outage, a bug -- is a fault, and a fault says
        # nothing about this world. It is deliberately not caught: no CHECKS.json and no REPAIR.json
        # are written, so the marker stays stale and the step runs again, instead of a fresh receipt
        # standing over a world the fault left half-written. OSError was in this list and is the
        # reason the distinction is worth stating: a disk error is not a fact about a company.
        print(f"warning: world repair failed: {exc}", file=sys.stderr)
        report["error"] = str(exc)[:300]
        checks = check_folder(root, folder)
    human_keys = {(f.get("source"), f.get("path")) for f in human_checks.get("findings", [])}
    report["unrepaired"] = [
        f"{f['source']} {f['path']}: {_reason(f, human, human_keys, report['rounds'], sampled)}"
        for f in checks["findings"]
        if f["severity"] == "error"
    ]
    report["after"] = {"ok": checks["ok"], "errors": checks["errors"], "warnings": checks["warnings"]}
    write(world_dir / "CHECKS.json", checks)
    write(world_dir / "REPAIR.json", report)
    if changed:
        refresh_manifest(folder)
    return {
        "ok": checks["ok"],
        "errors": checks["errors"],
        "rounds": report["rounds"],
        "world_records": report["world_records"],
        "unrepaired": report["unrepaired"],
        **({"error": report["error"]} if "error" in report else {}),
    }


REVIEW_REPAIR_ROUNDS = 2


def read_materials(folder):
    """The worker desktop files as the reviewer reads them: world/materials/<worker_id>/<path>."""
    base = Path(folder) / "world" / "materials"
    out = []
    for path in sorted(p for p in base.rglob("*") if p.is_file()):
        parts = path.relative_to(base).parts
        content = path.read_text(errors="replace")
        if len(parts) > 1 and content.strip():
            out.append(
                WorkerMaterial(worker_id=parts[0], path=str(PurePosixPath(*parts[1:])), content=content)
            )
    return out


def folder_inputs(folder):
    """``review_inputs`` recomputed from the files a folder holds now."""
    folder = Path(folder)
    apps = read(folder / "apps.json")["apps"]
    states = {app["app_id"]: read(state_path(folder, app)) for app in apps}
    return review_inputs(read(folder / "world" / "world.json"), states, read_materials(folder))


def recorded_inputs(folder):
    """What the reading this folder's world ships on recorded reading, or None when none did.

    The rounds of a seeding review after the one ``review_world`` marked ``kept`` describe a state
    that was discarded, so they are dropped: the inputs to compare are the kept round's. A later
    ``review-repair`` reading supersedes all of them, because it read the world as it is now.
    """
    folder = Path(folder)
    rounds = []
    for name in ("REVIEW.json", "REVIEW-REPAIR.json"):
        path = folder / "world" / name
        if path.is_file():
            data = read(path)
            # REVIEW.json keeps its readings under "rounds"; in REVIEW-REPAIR.json "rounds" is a
            # count and the readings are "review_rounds", so the key is checked, never assumed.
            readings = [
                entry
                for key in ("rounds", "review_rounds")
                if isinstance(data.get(key), list)
                for entry in data[key]
                if isinstance(entry, dict)
            ]
            kept = next((i for i, entry in enumerate(readings) if entry.get("kept")), None)
            rounds += readings if kept is None else readings[: kept + 1]
    return next((r.get("inputs") for r in reversed(rounds) if r.get("inputs")), None)


def verdict_coverage(folder):
    """What share of the records on disk the recorded verdict read, or None when it recorded none.

    This is the answer to "what does a verdict mean when the world grows by three times afterwards":
    it means exactly what it read, and the share is the number a driver thresholds. Measured over the
    60 seeded folders, the human layers the reviewers read hold 124,708 records against 331,577 on
    disk (37.6%), and the worst world 8.8% of 8,669. Growth is not drift -- ``stale_verdict`` names
    only the records the verdict read that have since moved, because a flag that fires on all 42
    bulked worlds of the 60 is a flag nothing can act on.
    """
    recorded = recorded_inputs(folder)
    return review_coverage(recorded, folder_inputs(folder)) if recorded else None


def stale_verdict(folder):
    """Why a folder's recorded verdict no longer covers the world on disk; [] when it still does.

    The review is the last step of seeding, and two stages run after it: ``dedupe-names`` renames
    people and ``add-bulk`` lays 88-90% of every mailbox. Timestamps in four companies put the
    last review call at 03:47, the renaming at 03:50 and the bulk layer at 03:53, so the verdict
    that gates the world covers roughly a tenth of what ships, and two worlds were stamped accept
    on a world that did not exist yet. The ordering is the driver's (scripts/batch_companies.py
    runs ``review_accepted`` before ``dedupe-names`` and ``add-bulk``); what belongs here is the
    refusal, and it needs this: every round records the digest and the record count of what it
    read, so a verdict whose inputs have moved can be told from one whose have not.

    A verdict that recorded no inputs at all returns []: a reading from before this existed is
    unknown, not stale, and refusing all sixty of them would stop the fleet over a fact none of
    them can establish. Records *added* after the verdict are ``verdict_coverage``'s business, not
    staleness: what is named here is a record the verdict read that has since gone or been
    rewritten, which is the only kind of movement that makes the reading itself untrue.
    """
    recorded = recorded_inputs(folder)
    return review_drift(recorded, folder_inputs(Path(folder))) if recorded else []


def review_errors(review, holders):
    """The blocking findings of the round a REVIEW.json's world actually ships: its error-severity
    findings, run through the reviewer's own dedupe rule so three ballots paraphrasing one slip
    stay one issue.

    The majority rule has already been applied when the round was written: ``merge_ballots``
    downgrades to a warning every error a majority did not raise, so what is still an error here
    is what a majority of the ballots agreed on.

    The shipping round is not always the last. ``review_world`` keeps the best of the rounds the
    decisive panel read and marks it ``kept``, because the last round's state was the one kept and
    37 of the 38 worlds held at revise end above their best round. Reading the last round's findings
    against the kept round's state would hand the author a worklist for records it is not looking at.
    """
    rounds = [r for r in (review.get("rounds") or []) if r.get("verdict")]
    if not rounds:
        return []
    shipped = next((r for r in rounds if r.get("kept")), rounds[-1])
    findings = [ReviewFinding.model_validate(f) for f in shipped["verdict"].get("findings") or []]
    return dedupe_findings([f for f in findings if f.severity == "error"], holders)


def owner_apps(finding, apps, holders):
    """The apps that own a finding: the one it names, and every app holding a record it cites."""
    cited = cited_ids(f"{finding.evidence} {finding.issue}", holders)
    return ({finding.target} & set(apps)) | {a for rid in cited for a in holders[rid] if a in apps}


def unowned_reason(finding, apps, holders):
    """Why nothing can be asked to repair a finding, in plain words."""
    if finding.target == "materials":
        return "a worker's desktop file, and this world ships none"
    if not cited_ids(f"{finding.evidence} {finding.issue}", holders):
        return "cites no record of this world, only a collection or a judgement"
    return "cites only records no app serves"


def write_materials(folder, changed):
    """Write the desktop files a repair changed; returns the paths written."""
    base = Path(folder) / "world" / "materials"
    for m in changed:
        path = base / m.worker_id / m.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(m.content)
    return [f"{m.worker_id}/{m.path}" for m in changed]


def rewritten_records(before, after):
    """Ids of the records an author changed, added or dropped between two states."""
    old = {str(r["id"]): r for _path, r in records(before)}
    new = {str(r["id"]): r for _path, r in records(after)}
    return sorted(rid for rid in old | new if old.get(rid) != new.get(rid))


def both_ends(message, limit=500):
    """A long message trimmed from the middle, because a subprocess names its reason at the end.

    Cutting the head off a failed command keeps the word "Command" and the flags, and loses
    "timed out after 600 seconds" -- which is the only part worth reading.
    """
    message = str(message)
    if len(message) <= limit:
        return message
    head = limit // 3
    return message[:head] + " ... " + message[-(limit - head - 5) :]


PROMPT_LIMIT = 1_048_576  # the provider refuses a prompt longer than this
PROMPT_BUDGET = 0.8  # the rest of the prompt is instructions, the world view and the findings


def largest_collection(state):
    """The size in characters of the biggest collection in one app's state."""
    return max((len(json.dumps(v, ensure_ascii=False)) for v in (state or {}).values()), default=0)


def too_large_to_send(state):
    """Whether this app's biggest collection leaves no room for the rest of an author's prompt."""
    return largest_collection(state) > PROMPT_LIMIT * PROMPT_BUDGET


def repair_review(root, folder, rounds=REVIEW_REPAIR_ROUNDS, models=None):
    """Recover a world the reviewers rejected: at most ``rounds`` rounds of repair and re-reading.

    Each round sends the blocking findings of the last review to the apps that own the records
    they cite (``plan_repair``, so every copy of a cited record is rewritten together), splices
    the corrected records into the human layer through the seeding author, lays the bulk again
    and has the reviewers read the whole world afresh. Findings nothing owns are recorded, never
    guessed at. Writes world/REVIEW-REPAIR.json in every case: it is the receipt that keeps one
    world to one attempt. Returns a report whose ``ok`` says whether the world ends accepted.
    """
    root, folder = Path(root), Path(folder)
    world_dir = folder / "world"
    report = {
        "repaired_at": now(),
        "max_rounds": rounds,
        "rounds": 0,
        "before": {},
        "verdict": None,
        "addressed": [],
        "rewritten": {},
        "materials_rewritten": [],
        "unrepaired": [],
        "review_rounds": [],
        "receipts": {"apps": {}},
    }
    findings, rewritten, orphans, seen = [], {}, [], set()
    changed, checks, seed, bulk_ids, concurrent = False, None, None, None, False
    # The author's own receipts and the reasons an app was not repaired are two different things
    # that shared one dict. ``folder_author`` appends to receipts["apps"][app_id], so writing a
    # reason there left a string where the author's next call expected its list: three worlds died
    # on "AttributeError: 'str' object has no attribute 'append'" in round two, the exception
    # escaped every guard here, and the receipt the driver read said rounds 0, unrepaired [].
    failed = report["receipts"]["failed"] = {}

    def set_aside(finding, reason):
        """Record a finding this step will not touch, once, with why."""
        key = (finding.target, finding.issue[:80])
        if key not in seen:
            seen.add(key)
            orphans.append({"target": finding.target, "issue": finding.issue[:300], "reason": reason})

    # Everything the step reads is inside the guard: the receipt is written whatever goes wrong,
    # because it is what stops the driver attempting the same world again.
    try:
        config = load_config(root)
        config.setdefault("models", {}).setdefault("world_states", config["models"].get("expand"))
        models = models if models is not None else Models(config, world_dir)
        skill_text, report["skill"] = load_skill(root, SKILL)
        seed = read(world_dir / "SEED.json")
        if seed.get("review_verdict") == "accept":
            # Nothing to recover, and rewriting records here would leave an accepted world in a
            # state the reviewers never saw. Seen live: a repair ran against a world another pass
            # had already recovered, rewrote four apps, and ended on "revise" while SEED.json still
            # said accept.
            report["ok"] = True
            report["verdict"] = "accept"
            report["note"] = "already accepted; nothing repaired"
            report["stale"] = stale_verdict(folder)
            report["coverage"] = verdict_coverage(folder)
            write(world_dir / "REVIEW-REPAIR.json", report)
            return report
        world = read(world_dir / "world.json")
        review_path = world_dir / "REVIEW.json"
        review = read(review_path) if review_path.is_file() else {}
        apps, bulk, bulk_ids, layered, human = read_layers(root, folder)
        apps_by_id = {app["app_id"]: app for app in apps}
        materials = read_materials(folder)
        holders = record_holders(world, layered)
        findings = review_errors(review, holders)
        report["stale"] = stale_verdict(folder)
        report["coverage"] = verdict_coverage(folder)
        report["before"] = {"verdict": review.get("final_verdict"), "blocking_findings": len(findings)}
        report["verdict"] = review.get("final_verdict")
        author, contract = folder_author(root, folder, models, skill_text, config, report["receipts"]["apps"])

        def mechanical(_materials):
            """The check the review packet carries, over the states now on disk."""
            nonlocal checks
            checks = check_folder(root, folder, bulk_ids=bulk_ids)
            return checks

        reviewer = make_reviewer(
            root,
            config,
            models,
            review_rounds=rounds,
            reference_date=seed.get("reference_date"),
            operating_scope=seed.get("operating_scope", ""),
            company=company_view(read(folder / "company.json")),
            tasks=task_designs(folder),
            world=world,
            identities=read(world_dir / "identities.json"),
            worker_apps=read(world_dir / "worker_apps.json"),
            apps=apps_by_id,
            mechanical=mechanical,
            reported=findings,
        )

        def taken_elsewhere():
            """Whether another pass has accepted this world since this one read SEED.json.

            The entry guard reads the flag once, which leaves the whole repair as a window. Seen
            live on ust: one repair started 05:50 and wrote ``review_verdict: accept`` at 09:44, a
            second started 08:55 -- before that write, so the entry guard saw a rejected world --
            and finished at 12:38 having rewritten five apps to a world the reviewers read as
            ``revise`` with two findings outstanding in the task's own decisive collection. The
            company still passes the review gate, on an accept that covers a world that is no longer
            on disk. The flag is re-read before anything is written, every round.
            """
            return read(world_dir / "SEED.json").get("review_verdict") == "accept"

        while report["rounds"] < rounds:
            owned = []
            for finding in findings:
                if owner_apps(finding, apps_by_id, holders) or (finding.target == "materials" and materials):
                    owned.append(finding)
                else:
                    set_aside(finding, unowned_reason(finding, apps_by_id, holders))
            if not owned:
                break
            if taken_elsewhere():
                concurrent = True
                break  # before the round is counted: nothing of it happened
            report["rounds"] += 1
            # ``materials`` rather than []: a finding on a worker's desktop file had no author here,
            # and ``merge_ballots`` lets one block a world, so a gate blocked on a class of defect
            # with no repair route by construction. Measured over the 35 recorded receipts: 25
            # material findings across 8 worlds left unrepaired, 10 of them refused at entry for
            # being "a worker's desktop file, not an app record", and hammond-power-solutions
            # terminal with all three of its unrepaired findings naming materials. The seeding loop
            # has repaired these since it was written (``repair_materials``); this path now uses the
            # same call rather than the gate dropping material findings from the blocking set.
            targets, _cited, material_findings, feedback = plan_repair(owned, apps_by_id, holders, materials)
            report["addressed"] += [
                {
                    "round": report["rounds"],
                    "target": f.target,
                    "issue": f.issue[:300],
                    "evidence": f.evidence[:300],
                    "apps": sorted(owner_apps(f, apps_by_id, holders)),
                }
                for f in owned
            ]
            changed_apps, repaired_files = set(), []
            fixed_materials, changed_files = None, []
            if material_findings:
                try:
                    fixed_materials = repair_materials(
                        materials, material_findings, world, models, skill_text
                    )
                except Exception as exc:  # noqa: BLE001 -- one failed materials call is not the round
                    failed["materials"] = both_ends(f"{type(exc).__name__}: {exc}", 300)
                else:
                    changed_files = materials_diff(materials, fixed_materials)
                    if not changed_files:
                        failed["materials"] = "the repair returned every desktop file unchanged"
            for app in contract:
                app_id = app["app_id"]
                if app_id not in targets:
                    continue
                # A copy, not the live layer: the narrowing and the rewritten-record count both
                # compare against what was there before the author ran.
                before = deepcopy(human[app_id])
                if too_large_to_send(before):
                    # This used to skip the app: the author had to be given the collection whole and
                    # return it whole, so a collection past the provider's ceiling could never be
                    # repaired. It is sampled and patched now (see ``repair_world`` above for the
                    # measurement), so the app is sent and the receipt records only that its prompt
                    # was a sample -- which is what says a collection-wide finding here is repaired
                    # as far as the sample reaches rather than all at once.
                    report["receipts"].setdefault("sampled", {})[app_id] = (
                        f"a collection of {largest_collection(before):,} characters is repaired from "
                        f"a sample, a patch at a time, because the whole of it is past the "
                        f"{PROMPT_LIMIT:,} the provider accepts in one call"
                    )
                try:
                    _, rewrote = author(app, feedback[app_id], before)
                except Exception as exc:  # noqa: BLE001 -- an unforeseen author fault is that app's
                    # One app's author failing is that app's findings left unrepaired, not the end
                    # of the round: the others are still worth rewriting, and the receipt below
                    # keeps this world to its one attempt either way. Any exception, not a named
                    # few: an author that fails in a way this list did not foresee took the whole
                    # step down with it and the receipt then said nothing was outstanding.
                    failed[app_id] = both_ends(f"{type(exc).__name__}: {exc}", 300)
                    human[app_id] = before
                    continue
                human[app_id], note = narrow_repair(
                    before, rewrote, owned, holders, widen=report["rounds"] > 1
                )
                if note:
                    report.setdefault("narrowed", {}).setdefault(str(report["rounds"]), {})[app_id] = note
                touched = rewritten_records(before, human[app_id])
                if not touched:
                    # A narrowing to zero is a failed author, not a repair. The author rewrote
                    # nothing a finding named (or only records that were put back), the reviewer duly
                    # reported the same defect, and the driver read that as the loop diverging.
                    # Measured over the 35 recorded receipts: 25 of the 98 app repairs changed no
                    # record at all (26%, in 16 companies), 29 of the 52 reader passes were paid
                    # inside a receipt holding one, and 5 of the 25 repaired-and-rejected worlds are
                    # terminal on a round whose repair changed nothing in an app the errors name.
                    failed[app_id] = "the author returned no change to any record the findings named"
                    continue
                changed_apps.add(app_id)
                rewritten.setdefault(app_id, set()).update(touched)
            if not changed_apps and not changed_files:
                # Nothing landed this round, whether the authors failed or narrowed to nothing.
                # Reading the world again would pay the reviewers to re-read what they rejected.
                report["error"] = (
                    "; ".join(f"{app}: {why}" for app, why in sorted(failed.items()))[:500]
                    or "no author changed a record the findings named"
                )
                break
            # Everything above this line is in memory. One last reading of the flag before the first
            # byte lands, so a world another pass accepted in the meantime is never written over.
            if taken_elsewhere():
                concurrent = True
                break
            if changed_files:
                repaired_files = write_materials(folder, changed_files)
                materials = fixed_materials
                report["materials_rewritten"] += repaired_files
                changed = True
            layered, wrote = lay_layers(root, folder, config, apps, bulk, bulk_ids, layered, human)
            changed = changed or wrote
            verdict, meta = reviewer(report["rounds"], layered, materials)
            report["review_rounds"].append(
                {
                    "round": report["rounds"],
                    "repaired": sorted(changed_apps),
                    "unchanged": sorted(targets - changed_apps),
                    "materials": repaired_files,
                    "verdict": verdict.model_dump(),
                    **meta,
                }
            )
            report["verdict"] = verdict.verdict
            if verdict.verdict == "accept":
                break
            holders = record_holders(world, layered)
            findings = dedupe_findings([f for f in verdict.findings if f.severity == "error"], holders)
    except Exception as exc:  # noqa: BLE001 -- the receipt must be written whatever stopped the step
        # Whatever reached disk stands; the receipt says what stopped, and the world stays rejected.
        # Every exception, not a named few. An AttributeError from inside the author escaped this
        # guard in three worlds and the caller's fallback receipt wrote rounds 0 and unrepaired [],
        # which is what a clean run writes: a crashed repair was indistinguishable from a repaired
        # world. The ``set_aside`` loop below is what makes the difference visible, so it has to be
        # reached, and that means nothing may get past this.
        print(f"warning: review repair failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        report["error"] = both_ends(f"{type(exc).__name__}: {exc}", 300)
    if concurrent:
        # A different note from the entry guard's on purpose: this one means a repair was under way
        # and was abandoned before it wrote, which is the thing worth finding in a receipt later.
        report["note"] = "another pass accepted this world while this one was repairing; nothing written"
        report["verdict"] = "accept"
    accepted = report["verdict"] == "accept" and seed is not None
    if not accepted:
        for finding in findings:
            set_aside(finding, f"still reported after {report['rounds']} round(s)")
    report["unrepaired"] = orphans
    report["rewritten"] = {
        app_id: {"records": len(ids), "ids": sorted(ids)[:40]} for app_id, ids in sorted(rewritten.items())
    }
    report["ok"] = accepted
    if accepted and not concurrent:
        # The seeding path's own bookkeeping, so the driver's review gate reads one field as always.
        checks = checks if checks is not None else check_folder(root, folder, bulk_ids=bulk_ids)
        # Re-read rather than write back the copy taken at entry: a concurrent pass that finished in
        # between owns whatever it put there, and this step's fields are the only ones it may set.
        seed = read(world_dir / "SEED.json")
        seed["review"] = seed["review_verdict"] = "accept"
        seed["mechanical_checks"] = {"errors": checks["errors"], "warnings": checks["warnings"]}
        seed["mechanical_ok"] = checks["ok"]
        seed["status"] = "seeded_reviewed" if checks["ok"] else "seeded_review_failed"
        # The shared outcome moves with the status it is derived from. Rewriting one and not the
        # other is exactly how a marker comes to contradict its own receipt: this file is the second
        # writer of SEED.json, and ``seed_outcome`` is the single place that decides the word.
        seed.update(seed_outcome(seed["status"]).body)
        seed["review_repaired_at"] = report["repaired_at"]
        write(world_dir / "SEED.json", seed)
        if (folder / "MANIFEST.json").is_file():
            manifest = read(folder / "MANIFEST.json")
            manifest.setdefault("stages", {})["stage2_world"] = seed["status"]
            write(folder / "MANIFEST.json", manifest)
    write(world_dir / "REVIEW-REPAIR.json", report)
    if changed or (accepted and not concurrent):
        refresh_manifest(folder)
    return {
        "ok": accepted,
        "verdict": report["verdict"],
        "rounds": report["rounds"],
        "addressed": len(report["addressed"]),
        "rewritten": {app_id: entry["records"] for app_id, entry in report["rewritten"].items()},
        "unrepaired": report["unrepaired"],
        # What the verdict this step started from no longer covers, so a driver reading one field
        # can tell a world the reviewers judged from one they judged a tenth of, and the share of
        # the records on disk that verdict actually read.
        "stale": report.get("stale") or [],
        "coverage": report.get("coverage"),
        "materials_rewritten": report["materials_rewritten"],
        **({"error": report["error"]} if "error" in report else {}),
    }
