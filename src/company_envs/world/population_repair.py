"""Explicit migration of saved Stage 3 outputs; no re-seeding or acceptance review."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from company_envs.config import load_config
from company_envs.storage import digest, now, read, run_lock, write

from .blueprint import load_core
from .bulk_layer import BULK_VERSION
from .population import additive_world, missing_targets
from .population_amendments import corrected_world
from .population_contract import app_fingerprint
from .population_quality import instant, materialize_drive, normalize_slack, retime_bulk, sync_documents
from .seed_calls import load_skill
from .state_seed import app_contract, validate_core


def revalidate_population(root, folder):
    """Explicitly recheck unchanged saved output under the current compiler, without model calls."""
    from .population import populate

    root, folder = Path(root).resolve(), Path(folder).resolve()
    work = folder / "world/population"
    with run_lock(work):
        core, _ = load_core(root, folder)
        _, contract = app_contract(root, folder)
        config = load_config(root)
        base = read(folder / "world/world.json")
        extension = (
            read(work / "EXTENSION.json")
            if (work / "EXTENSION.json").exists()
            else {"parent_world_hash": digest(base)}
        )
        world = corrected_world(folder, additive_world(base, extension))
        if read(work / "EFFECTIVE-WORLD.json") != world:
            raise ValueError("Effective world differs from explicit source amendments")
        entries = {}
        for app in contract:
            aid = app["app_id"]
            entry = read(work / f"{aid}.json")
            if digest(read(folder / f"world/{aid}.state.json")) != entry["state_hash"]:
                raise ValueError(f"Unrecorded native drift: {aid}")
            initial = work / f"{aid}.initial.json"
            if entry.get("initial_state_hash") != (digest(read(initial)) if initial.exists() else None):
                raise ValueError(f"Changed supplied initial state: {aid}")
            entries[aid] = entry
        archive = work / "revalidation" / digest({"at": now(), "entries": entries})
        if (folder / "world/POPULATION.json").exists():
            write(archive / "POPULATION.json", read(folder / "world/POPULATION.json"))
        for app in contract:
            aid = app["app_id"]
            entry = entries[aid]
            write(archive / f"{aid}.json", entry)
            entry.setdefault("generation_inputs", entry["inputs"])
            entry["inputs"] = app_fingerprint(
                read(folder / "world/CORE.json")["hashes"],
                world,
                read(work / "targets.json")
                if (work / "targets.json").exists()
                else [p.model_dump() for p in core.population_plan],
                app,
                read(folder / "world/identities.json"),
                read(folder / "world/worker_apps.json"),
                config,
            )
            entry["revalidation_source"] = str((archive / f"{aid}.json").relative_to(folder))
            write(work / f"{aid}.json", entry)

    class NoModelCalls:
        def call(self, *args, **kwargs):
            raise ValueError("Revalidation cannot author or repair missing output")

    return populate(root, folder, models=NoModelCalls())


def apply_native_patch(root, folder, patch):
    """Apply a reviewed, exact-value repair to saved native records; never re-author a world.

    A patch binds the previous population and names every changed field, its old/new
    values and a reason. Originals and the patch remain inspectable. Run populate-world
    afterward to repeat all gates and produce the next handoff.
    """
    from copy import deepcopy

    from .population_snapshot import population_snapshot

    root, folder = Path(root).resolve(), Path(folder).resolve()
    work = folder / "world/population"
    with run_lock(work):
        if patch["baseline"] != population_snapshot(root, folder, require_complete=False):
            raise ValueError("Repair patch belongs to a different population")
        if "known_issues" in patch and (
            not isinstance(patch["known_issues"], list)
            or not all(isinstance(s, str) for s in patch["known_issues"])
        ):
            raise ValueError("known_issues must be a list of plain-English findings")
        _, contract = app_contract(root, folder)
        recorded = read(folder / "world/POPULATION.json")["states"]
        states = {aid: read(folder / entry["path"]) for aid, entry in recorded.items()}
        contract = [a for a in contract if a["app_id"] in states]
        before = deepcopy(states)
        for change in patch["changes"]:
            if not change.get("reason") or not change.get("path") or change["app"] not in states:
                raise ValueError("Every native repair needs an app, field path and reason")
            node = states[change["app"]]
            for key in change["path"][:-1]:
                node = node[key]
            key = change["path"][-1]
            if node[key] != change["before"]:
                raise ValueError(f"Native repair precondition failed: {change['app']} {change['path']}")
            node[key] = change["after"]
        archive = work / "review-repairs" / digest(patch)
        write(archive / "PATCH.json", patch)
        config = load_config(root)
        world = read(work / "EFFECTIVE-WORLD.json")
        for app in contract:
            aid = app["app_id"]
            entry = read(work / f"{aid}.json")
            write(archive / f"{aid}.json", entry)
            if states[aid] != before[aid]:
                write(archive / f"{aid}.state.json", before[aid])
            entry.setdefault("generation_inputs", entry["inputs"])
            if aid in patch.get("bulk_ids", {}):
                entry["bulk_ids"] = patch["bulk_ids"][aid]
            entry.update(
                inputs=app_fingerprint(
                    read(folder / "world/CORE.json")["hashes"],
                    world,
                    read(work / "targets.json")
                    if (work / "targets.json").exists()
                    else [p.model_dump() for p in load_core(root, folder)[0].population_plan],
                    app,
                    read(folder / "world/identities.json"),
                    read(folder / "world/worker_apps.json"),
                    config,
                ),
                state_hash=digest(states[aid]),
                bytes=len(json.dumps(states[aid], ensure_ascii=False).encode()),
                review_repair=str((archive / "PATCH.json").relative_to(folder)),
            )
            write(folder / f"world/{aid}.state.json", states[aid])
            write(work / f"{aid}.json", entry)
        if "known_issues" in patch:
            handoff = read(folder / "world/POPULATION.json")
            handoff["known_issues"] = patch["known_issues"]
            write(folder / "world/POPULATION.json", handoff)
        return {"patch": str((archive / "PATCH.json").relative_to(folder)), "changes": len(patch["changes"])}


def repair_population(root, folder, *, comment_file=None, drive_sources=(), timezone="UTC"):
    """Archive before mutation; retain the original generation provenance on each app."""
    root, folder = Path(root), Path(folder)
    work = folder / "world/population"
    with run_lock(work):
        core, _ = load_core(root, folder)
        manifest, contract = app_contract(root, folder)
        config = load_config(root)
        base, identities, _, grants = validate_core(
            core, {a["app_id"]: a for a in manifest["apps"]}, manifest["workers"]
        )
        extension = (
            read(work / "EXTENSION.json")
            if (work / "EXTENSION.json").exists()
            else {"parent_world_hash": digest(base)}
        )
        world = corrected_world(folder, additive_world(base, extension))
        plan = (
            read(work / "targets.json")
            if (work / "targets.json").exists()
            else [p.model_dump() for p in core.population_plan]
        )
        states = {a["app_id"]: read(folder / "world" / f"{a['app_id']}.state.json") for a in contract}
        entries = {a: read(work / f"{a}.json") for a in states}
        core_hashes = read(folder / "world/CORE.json")["hashes"]
        _, skill = load_skill(root)
        sources = {
            "core": core_hashes,
            "extension": extension,
            "plan": plan,
            "skill": skill,
            "schemas": contract,
        }
        legacy_inputs = digest(sources)
        source_contract = digest({**sources, "seed": config["generation"].get("seed", 0)})
        for aid, state in states.items():
            app = next(a for a in contract if a["app_id"] == aid)
            current_inputs = app_fingerprint(core_hashes, world, plan, app, identities, grants, config)
            if (
                entries[aid]["inputs"] != legacy_inputs
                and entries[aid]["inputs"] != current_inputs
                and entries[aid].get("source_contract") != source_contract
            ):
                raise ValueError(f"saved population belongs to different source inputs: {aid}")
            if digest(state) != entries[aid]["state_hash"]:
                raise ValueError(f"unrecorded native drift: {aid}")
            initial = work / f"{aid}.initial.json"
            if entries[aid].get("initial_state_hash") != (
                digest(read(initial)) if initial.exists() else None
            ):
                raise ValueError(f"initial population changed: {aid}")
        before = {a: digest(s) for a, s in states.items()}
        archive = work / "before-audit-fixes" / digest(before)
        for aid, state in states.items():
            if not (archive / f"{aid}.state.json").exists():
                write(archive / f"{aid}.state.json", state)
                write(archive / f"{aid}.json", entries[aid])
        changes = {}
        if "hubspot_mock" in states and entries["hubspot_mock"].get("bulk_version", 4) < BULK_VERSION:
            changes["history_dates"] = retime_bulk(
                states["hubspot_mock"],
                read(work / "hubspot_mock.human.json"),
                entries["hubspot_mock"]["specs"],
                config["generation"].get("seed", 0),
            )
            events = {e["id"]: e for e in states.get("google_calendar_mock", {}).get("events", [])}
            changed_events = []
            for meeting in states["hubspot_mock"].get("meetings", []):
                if meeting["id"] not in events:
                    continue
                start = datetime.fromisoformat(meeting["date"])
                if not start.tzinfo:
                    start = start.replace(tzinfo=ZoneInfo(timezone))
                event = events[meeting["id"]]
                if instant(event["start"]) != start:
                    event.update(
                        start=start.isoformat(),
                        end=(start + timedelta(minutes=meeting["duration"])).isoformat(),
                    )
                    changed_events.append(meeting["id"])
            changes["calendar_projections"] = changed_events
        if "slack_mock" in states:
            changes["slack"] = normalize_slack(states["slack_mock"])
        if "google_docs_mock" in states and "google_drive_mock" in states:
            changes["document_registry"] = sync_documents(
                states["google_docs_mock"], states["google_drive_mock"], drive_sources=drive_sources
            )
        if comment_file is not None:
            saved = read(comment_file)
            if saved["source_state_hash"] != before["google_docs_mock"] or saved["output_hash"] != digest(
                saved["comments"]
            ):
                raise ValueError("grounded comments do not match this native source")
            replacements = {c["id"]: c for c in saved["comments"]}
            docs = states["google_docs_mock"]
            for comment in replacements.values():
                version_date = docs["documents"][comment["docId"]]["updated"]
                if instant(comment["created"]) < instant(version_date):
                    comment["created"] = version_date
            docs["comments"] = [replacements.get(c["id"], c) for c in docs["comments"]]
            changes["grounded_comments"] = {
                "count": len(replacements),
                "source": str(comment_file),
                "permissions": "author must already have comment access",
                "dates": "no earlier than the supplied document revision",
            }
        if "google_drive_mock" in states:
            changes["download_files"] = materialize_drive(folder, states["google_drive_mock"])
        report = {
            "at": now(),
            "archive": str(archive.relative_to(folder)),
            "before": before,
            "after": {a: digest(s) for a, s in states.items()},
            "changes": changes,
            "review": "Stage 4 not run",
            "runtime": "not started",
        }
        report_path = work / "audit-repairs" / f"{digest(report)}.json"
        write(report_path, report)
        for app in contract:
            aid = app["app_id"]
            entry, state = entries[aid], states[aid]
            entry.setdefault("generation_inputs", entry["inputs"])
            entry.update(
                inputs=app_fingerprint(
                    read(folder / "world/CORE.json")["hashes"], world, plan, app, identities, grants, config
                ),
                state_hash=digest(state),
                bytes=len(json.dumps(state, ensure_ascii=False).encode()),
                shortfalls=missing_targets(state, plan, aid),
                audit_repair=str(report_path.relative_to(folder)),
                source_contract=source_contract,
            )
            # Only CRM's saved templates were migrated; other outputs retain their generation version.
            if aid == "hubspot_mock":
                entry["bulk_version"] = BULK_VERSION
            write(folder / "world" / f"{aid}.state.json", state)
            write(work / f"{aid}.json", entry)
        load_core(root, folder)
        return report
