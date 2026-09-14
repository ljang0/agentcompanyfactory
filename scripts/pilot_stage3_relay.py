"""Re-expand already-paid template specs with the corrected worker identity lookup."""

import json
from pathlib import Path

from company_envs.config import load_config
from company_envs.storage import digest, now, read, write
from company_envs.world.bulk import BulkSpecs
from company_envs.world.bulk_layer import BULK_VERSION, check_specs, lay_bulk, max_bytes_for
from company_envs.world.population import additive_world, check_bulk_targets, missing_targets
from company_envs.world.state_seed import app_contract, canonical_people


def main():
    root = Path(__file__).resolve().parents[1]
    folder = root / "experiments/pilot-sanmar/company"
    work = folder / "world/population"
    config = load_config(root)
    _, contract = app_contract(root, folder)
    world = additive_world(read(folder / "world/world.json"), read(work / "EXTENSION.json"))
    grants, identities = read(folder / "world/worker_apps.json"), read(folder / "world/identities.json")
    canonical = canonical_people(world, list(grants))
    plan = read(work / "targets.json")
    candidates = []
    for path in (folder / "world/calls").glob("*/result.json"):
        result = read(path)
        if "specs" in result.get("data", {}):
            candidates.append((result, str(path.relative_to(folder))))
    report = {}
    for app in contract:
        app_id = app["app_id"]
        marker = work / f"{app_id}.json"
        human = work / f"{app_id}.human.json"
        if not marker.exists() or not human.exists() or app_id == "google_docs_mock":
            continue
        entry, baseline = read(marker), read(human)
        if entry.get("bulk_version") == BULK_VERSION:
            continue
        targets = missing_targets(baseline, plan, app_id)
        if not targets:
            continue
        roster = []
        for worker, apps in grants.items():
            if app_id in apps:
                person = identities.get(worker, {}).get(app_id) or canonical[worker]
                roster.append(
                    {
                        "id": person.get("id") or person.get("userId"),
                        "name": person.get("name") or person.get("username"),
                        "email": person.get("email"),
                    }
                )
        options = []
        for result, source in candidates:
            specs = BulkSpecs.model_validate(result["data"]).specs
            if not specs or any(s.app_id != app_id for s in specs):
                continue
            errors = check_bulk_targets(specs, targets) + check_specs(
                specs,
                app,
                baseline,
                [t["collection"] for t in targets],
                max_bytes_for(config, app_id),
                people=roster,
            )
            if errors:
                options.append({"source": source, "errors": errors})
                continue
            state = json.loads(json.dumps(baseline))
            added, ids, short = lay_bulk(state, specs, 0, max_bytes_for(config, app_id), people=roster)
            if short:
                options.append({"source": source, "errors": short})
                continue
            previous = read(folder / "world" / f"{app_id}.state.json")
            archive = work / "before-identity-fix" / f"{app_id}.state.json"
            if not archive.exists():
                write(archive, previous)
            write(folder / "world" / f"{app_id}.state.json", state)
            entry.update(
                state_hash=digest(state),
                bulk_ids=sorted(ids),
                specs=[s.model_dump() for s in specs],
                problems=[p for p in entry["problems"] if isinstance(p, dict)],
                bytes=len(json.dumps(state, ensure_ascii=False).encode()),
                shortfalls=missing_targets(state, plan, app_id),
                bulk_version=BULK_VERSION,
                bulk_people=roster,
                deterministic_repair={
                    "at": now(),
                    "source": source,
                    "previous_hash": digest(previous),
                    "model_calls": 0,
                },
            )
            write(marker, entry)
            report[app_id] = {"added": added, "bytes": entry["bytes"], "source": source, "model_calls": 0}
            break
        else:
            report[app_id] = {"no_acceptable_saved_specs": options}
    previous_report = read(work / "IDENTITY-RELAY.json") if (work / "IDENTITY-RELAY.json").exists() else {}
    write(work / "IDENTITY-RELAY.json", {**previous_report, **report})
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
