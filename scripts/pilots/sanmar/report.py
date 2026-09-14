"""Inspect completed Stage 3 artifacts and record an explicit Stage 4 handoff."""

import csv
import io
import json
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from company_envs.config import load_config
from company_envs.storage import digest, now, read, write
from company_envs.world.blueprint import load_core
from company_envs.world.bulk_layer import max_bytes_for
from company_envs.world.hub_app import validate_state
from company_envs.world.population import additive_world, missing_targets
from company_envs.world.population_quality import quality_errors, resource_errors
from company_envs.world.state_seed import app_contract, record_count
from company_envs.world.world_check import check_folder


def main():
    root = Path(__file__).resolve().parents[3]
    pilot = root / "experiments/pilot-sanmar"
    folder = pilot / "company"
    work = folder / "world/population"
    core, _ = load_core(root, folder)
    config = load_config(root)
    _, contract = app_contract(root, folder)
    world = additive_world(read(folder / "world/world.json"), read(work / "EXTENSION.json"))
    plan = read(work / "targets.json")
    states, entries, errors = {}, {}, []
    volumes = []
    for app in contract:
        aid = app["app_id"]
        state = read(folder / "world" / f"{aid}.state.json")
        states[aid] = state
        entry = read(work / f"{aid}.json")
        entries[aid] = entry
        if digest(state) != entry["state_hash"]:
            errors.append(f"state/checkpoint drift: {aid}")
        try:
            validate_state({"id": aid, "state_keys": app["top_level_keys"]}, state, app["schema_document"])
        except (ValueError, TypeError) as exc:
            errors.append(f"native contract {aid}: {exc}")
        size = len(json.dumps(state, ensure_ascii=False).encode())
        limit = max_bytes_for(config, aid)
        if size > limit:
            errors.append(f"{aid} exceeds {limit} bytes")
        errors.extend(f"{aid}: {p['collection']} shortfall" for p in missing_targets(state, plan, aid))
        counts = {
            p["collection"]: record_count(state.get(p["collection"])) for p in plan if p["app_id"] == aid
        }
        volumes.append({"app": aid, "records": counts, "json_bytes": size, "limit_bytes": limit})

    documents = states["google_docs_mock"]["documents"]
    comments = states["google_docs_mock"]["comments"]
    for comment in comments:
        doc = documents.get(comment["docId"])
        if not doc or datetime.fromisoformat(comment["created"]) < datetime.fromisoformat(doc["created"]):
            errors.append(f"missing/future parent document: {comment['id']}")
    lengths = [len(re.sub("<[^>]+>", " ", d["content"]).split()) for d in documents.values()]
    assets = read(work / "ASSETS.json")
    for export in assets["exports"]:
        native = states["google_drive_mock"]["items"][export["id"]]
        raw = (folder / export["local_path"]).read_bytes()
        if native["content"].encode() != raw or native["size"] != len(raw):
            errors.append(f"CSV content differs in Drive: {export['id']}")
        if len(list(csv.reader(io.StringIO(native["content"])))) - 1 != export["record_count"]:
            errors.append(f"CSV rows differ in Drive: {export['id']}")
    events = {e["id"]: e for e in states["google_calendar_mock"]["events"]}
    for meeting in states["hubspot_mock"]["meetings"]:
        event = events.get(meeting["id"])
        if not event:
            errors.append(f"CRM meeting missing from Calendar: {meeting['id']}")
            continue
        start = datetime.fromisoformat(meeting["date"])
        if start.tzinfo is None:
            start = start.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
        if datetime.fromisoformat(event["start"]) != start:
            errors.append(f"CRM/Calendar time mismatch: {meeting['id']}")
    checks = check_folder(root, folder, states, authoring=True)
    errors.extend(quality_errors(states))
    errors.extend(resource_errors(folder))
    errors.extend(
        f"{f['source']} {f['path']}: {f['message']}" for f in checks["findings"] if f["severity"] == "error"
    )
    write(work / "NATIVE-CHECKS.json", checks)
    write(work / "EFFECTIVE-WORLD.json", world)
    write(work / "VOLUME.json", volumes)
    tz = ZoneInfo("America/Los_Angeles")
    weekends = sum(datetime.fromisoformat(e["start"]).astimezone(tz).weekday() >= 5 for e in events.values())
    issues = [
        "Four resource archives have measured sizes different from the accepted catalog's advertised sizes; reconcile the catalog before acceptance.",
        "Six ad creatives have empty thumbnail/media fields. The four product packages do not establish historically valid campaign artwork.",
        f"Calendar contains {weekends} weekend appointments; inspect whether recurring office/customer traffic is plausible.",
        "Generated CRM deal amounts are independent estimates, not line-priced ledger projections; do not assume every quote reconciles to an order.",
        "Some routine correspondence repeats short templates. Volume does not establish semantic diversity or useful task dependencies.",
        "Pinned Drive PDF preview uses an image element. Embedded PDF bytes and download code have been inspected; preview requires a Stage 4 fix and browser proof.",
        "Canonical resource and invoice URLs have local mappings but are not served. Browser visibility, role isolation, persistence and reset remain unmeasured.",
        "Prototype population scripts are pilot-specific. Legacy run-company/seed-world must not bypass the Stage 4 checkpoint or ignore the extension.",
    ]
    report = {
        "ok": not errors,
        "errors": errors,
        "checked_at": now(),
        "native_contracts": len(states),
        "volumes": volumes,
        "documents": {
            "count": len(documents),
            "words_min": min(lengths),
            "words_median": statistics.median(lengths),
            "words_max": max(lengths),
            "comments": len(comments),
        },
        "joined_crm_meetings": len(states["hubspot_mock"]["meetings"]),
        "native_csv_exports": len(assets["exports"]),
        "warning_count": checks["warnings"],
        "warnings_by_source": dict(
            Counter(f["source"] for f in checks["findings"] if f["severity"] == "warning")
        ),
        "accepted_core_unchanged": True,
        "remaining_quality_issues": issues,
        "semantic_review": "not run; Stage 4",
        "runtime_proof": "not run; Stage 4",
        "tasks": "not created; Stage 5",
    }
    write(pilot / "STAGE3-CHECKS.json", report)
    # Receipts, not cache replays, measure completed provider attempts.
    started = min(read(p)["receipt"]["started_at"] for p in work.glob("profiles-*.json"))
    calls = []
    for path in (folder / "world/calls").glob("*/attempt-*/receipt.json"):
        r = read(path)
        if r["started_at"] < started:
            continue
        calls.append(
            {
                k: r.get(k)
                for k in (
                    "call_id",
                    "attempt",
                    "job",
                    "model",
                    "started_at",
                    "status",
                    "seconds",
                    "usage",
                    "cost_usd",
                )
            }
        )
    calls.sort(key=lambda r: r["started_at"])
    complete = [r for r in calls if r["status"] == "complete"]
    usage = Counter()
    for receipt in complete:
        usage.update(receipt.get("usage") or {})
    ended = max(datetime.fromisoformat(r["started_at"]) + timedelta(seconds=r["seconds"]) for r in complete)
    metrics = {
        "first_provider_call": started,
        "last_completed_call": ended.isoformat(),
        "provider_elapsed_seconds": (ended - datetime.fromisoformat(started)).total_seconds(),
        "attempts_by_status": dict(Counter(r["status"] for r in calls)),
        "completed_provider_seconds_sum": sum(r["seconds"] for r in complete),
        "reported_usage": dict(usage),
        "cost_usd": None,
        "limitations": "Elapsed span includes local work between calls. Interrupted usage and image generation usage are unreported, not zero. Dollar costs were not supplied by the provider.",
        "image_generation_calls": 4,
        "interruptions": read(work / "INTERRUPTIONS.json"),
        "calls": calls,
    }
    write(pilot / "STAGE3-METRICS.json", metrics)
    # This is a content manifest for review, not a runtime SEED/acceptance claim.
    artifacts = {}
    for path in sorted((folder / "world").rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(folder)
        if rel.parts[1] in {"desktop", "materials"} or (
            rel.parts[1] == "population"
            and (
                len(rel.parts) == 3
                and path.name
                in {
                    "EXTENSION.json",
                    "EFFECTIVE-WORLD.json",
                    "targets.json",
                    "ROW-TARGETS.json",
                    "ledger.json",
                    "ASSETS.json",
                }
                or "assets" in rel.parts
                or "files" in rel.parts
                or "exports" in rel.parts
                or "transactions" in rel.parts
                and (path.suffix == ".pdf" or path.name in {"RESOURCES.json", "BINDINGS.json", "CHECKS.json"})
            )
        ):
            artifacts[str(rel)] = digest(path.read_bytes())
    write(
        folder / "world/POPULATION.json",
        {
            "schema_version": 1,
            "status": "populated_for_review" if not errors else "population_needs_repair",
            "created_at": now(),
            "reference_date": core.reference_date,
            "core_hashes": read(folder / "world/CORE.json")["hashes"],
            "effective_world": "world/population/EFFECTIVE-WORLD.json",
            "artifacts": artifacts,
            "states": {
                a: {"path": f"world/{a}.state.json", "state_hash": e["state_hash"], "bulk_ids": e["bulk_ids"]}
                for a, e in entries.items()
            },
            "review": "Stage 4 not run",
            "runtime": "not started",
            "known_issues": issues,
        },
    )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"volumes", "remaining_quality_issues"}}, indent=2
        )
    )
    print(json.dumps({k: v for k, v in metrics.items() if k not in {"calls", "interruptions"}}, indent=2))


if __name__ == "__main__":
    main()
