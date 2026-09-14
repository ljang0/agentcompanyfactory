"""Checkpoint the canonical company world before native app population."""

import json
import shutil
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from pydantic import Field

from company_envs.config import load_config
from company_envs.models import Models
from company_envs.storage import digest, now, read, write

from .core_checks import history_checks, material_check, record_checks
from .documents import render_material
from .seed_calls import Contract, WorldCore, load_skill, skill_for_call
from .state_seed import (
    app_contract,
    canonical_people,
    core_payload,
    people_in,
    reconcile_identities,
    register_people,
    repair_identities,
    validate_core,
)
from .world_check import check_identities
from .world_review import WorldReview, merge_ballots, repair_materials


class Population(Contract):
    app_id: str
    collection: str
    target_records: int = Field(ge=1)
    record_unit: str = Field(min_length=1)
    first_date: str
    last_date: str
    purpose: str = Field(min_length=1)
    canonical_sources: list[str] = Field(min_length=1)


class Blueprint(WorldCore):
    population_plan: list[Population] = Field(min_length=1)


def inputs(root, folder):
    _, contract = app_contract(root, folder)
    _, skill = load_skill(root)
    return {
        "company": digest((folder / "company.json").read_bytes()),
        "apps": digest((folder / "apps.json").read_bytes()),
        "spec": digest(read(folder / "world-spec.json")) if (folder / "world-spec.json").is_file() else None,
        "skill": skill["hash"],
        "schemas": {a["app_id"]: digest(a["schema_document"]) for a in contract},
        "implementation": digest(Path(__file__).read_bytes()),
    }


def load_core(root, folder):
    """Only consume the exact accepted checkpoint; drift never silently regenerates it."""
    root, folder = Path(root), Path(folder)
    marker = read(folder / "world/CORE.json")
    current = inputs(root, folder)
    if marker["status"] != "core_reviewed" or any(
        marker["inputs"].get(k) != value for k, value in current.items() if k != "implementation"
    ):
        raise ValueError("canonical checkpoint is unaccepted or its inputs changed")
    for relative, expected in marker["hashes"].items():
        path = folder / relative
        if not path.is_file() or digest(path.read_bytes()) != expected:
            raise ValueError(f"canonical checkpoint drift: {relative}")
    core = read_core(folder, marker)
    apps = {a["app_id"]: a for a in read(folder / "apps.json")["apps"]}
    workers = read(folder / "company.json")["workers"]
    world, identities, materials, grants = validate_core(core, apps, [w["id"] for w in workers])
    spec = read(folder / "world-spec.json") if (folder / "world-spec.json").is_file() else {}
    checks = check_blueprint(core, world, identities, materials, grants, apps, workers, spec)
    if not checks["ok"]:
        raise ValueError("canonical checkpoint fails current checks: " + "; ".join(checks["errors"]))
    _, review_skill = load_skill(root, "company-world-review")
    if read(folder / "world/CORE-REVIEW.json")["skill"]["hash"] != review_skill["hash"]:
        raise ValueError("canonical checkpoint review skill changed; review again")
    # Checker implementation hashes are provenance, not a reason to buy another
    # world. Unchanged accepted artifacts must pass the current checks on reuse.
    return core, marker["receipt"]


def read_core(folder, marker):
    """Read editable sources; callers decide whether to verify or freshly review them."""
    world_dir = folder / "world"
    identities = read(world_dir / "identities.json")
    grants = read(world_dir / "worker_apps.json")
    core = Blueprint.model_validate(
        {
            **marker["core_metadata"],
            "entities_json": json.dumps(read(world_dir / "world.json")),
            "population_plan": read(world_dir / "population.json"),
            "identities": [
                {"worker_id": worker, "app_id": app, "user_json": json.dumps(user)}
                for worker, apps in identities.items()
                for app, user in apps.items()
            ],
            "worker_apps": [{"worker_id": worker, "app_ids": apps} for worker, apps in grants.items()],
            "materials": [
                {**item, "content": (world_dir / "materials" / item["worker_id"] / item["path"]).read_text()}
                for item in marker["materials"]
            ],
        }
    )
    return core


def check_blueprint(core, world, identities, materials, grants, apps, workers, spec):
    """Count actual history/files, keeping population targets separate from existing data."""
    errors = [f["message"] for f in check_identities(identities, workers) if f["severity"] == "error"]
    people = canonical_people(world, [w["id"] for w in workers])
    if set(people) != {w["id"] for w in workers}:
        errors.append("canonical world must identify every worker with a distinct name and email")
    # WorldCore permits named collections (staff, accounts, products, ...), not
    # just an array called entities. Count top-level records once, not nested
    # workbook copies or rows repeated in historical events.
    entities = []
    for key, value in world.items():
        if key == "history":
            continue
        if isinstance(value, list):
            entities.extend(value)
        elif isinstance(value, dict) and "id" in value:
            entities.append(value)
    history = world.get("history", [])
    if not isinstance(entities, list) or not isinstance(history, list):
        return {"ok": False, "errors": ["entities and history must be literal arrays"]}
    entity_ids = [item.get("id") for item in entities if isinstance(item, dict)]
    if (
        len(entity_ids) != len(entities)
        or any(not isinstance(i, str) or not i for i in entity_ids)
        or len(set(entity_ids)) != len(entity_ids)
    ):
        errors.append("canonical entities need unique nonempty string IDs")
    if spec.get("reference_date") and core.reference_date[:10] != spec["reference_date"][:10]:
        errors.append("reference date differs from the agreed world specification")
    history_errors, months = history_checks(history, spec, core.reference_date)
    errors.extend(history_errors)
    errors.extend(record_checks([*entities, *history], world, set(apps)))
    if len(entities) < spec.get("minimum_entities", 1):
        errors.append("canonical entity population is below the requested minimum")
    accounts = world.get("accounts", [])
    for requirement, status in (("active_reseller_accounts", "active"), ("prospect_accounts", "prospect")):
        if requirement in spec and sum(a.get("status") == status for a in accounts) != spec[requirement]:
            errors.append(f"{requirement}: expected {spec[requirement]} accounts with status {status}")
    paths = [(m.worker_id, m.path) for m in materials]
    if len(paths) != len(set(paths)):
        errors.append("duplicate desktop material path")
    counts = Counter(m.worker_id for m in materials)
    for worker in workers:
        if counts[worker["id"]] < spec.get("minimum_files_per_worker", 1):
            errors.append(f"{worker['id']}: too few desktop files")
    errors.extend(issue for m in materials if (issue := material_check(m)))
    if spec.get("worker_apps") and grants != spec["worker_apps"]:
        errors.append("app grants differ from the agreed role boundaries")
    covered, destinations = set(), set()
    for item in core.population_plan:
        destination = (item.app_id, item.collection)
        if destination in destinations:
            errors.append(f"duplicate population destination {item.app_id}.{item.collection}")
        destinations.add(destination)
        if item.app_id not in apps or item.collection not in apps[item.app_id]["top_level_keys"]:
            errors.append(f"unknown population destination {item.app_id}.{item.collection}")
        try:
            first, last = date.fromisoformat(item.first_date), date.fromisoformat(item.last_date)
            scheduled = (item.app_id, item.collection) in {
                ("google_calendar_mock", "events"),
                ("hubspot_mock", "meetings"),
            }
            if first > last or (not scheduled and last > date.fromisoformat(core.reference_date[:10])):
                errors.append(f"population dates out of order: {item.app_id}.{item.collection}")
        except ValueError:
            errors.append("population dates must be ISO dates")
        covered.add(item.app_id)
    if covered != set(apps):
        errors.append("population plan must cover every declared app")
    return {
        "ok": not errors,
        "errors": errors,
        "entities": len(entities),
        "history_records": len(history),
        "history_months": sorted(months),
        "history_by_month": months,
        "materials_by_worker": dict(counts),
        "planned_streams": len(core.population_plan),
        "native_population": "not_started",
        "material_validation": "rendered and parsed; formula syntax checked, recalculation not run",
    }


def seed_core(root, folder, *, models=None, review_rounds=2, timeout_seconds=2700, resume=False):
    """One core author, fresh review, and bounded targeted repairs. Never author app states."""
    root, folder = Path(root), Path(folder)
    if review_rounds not in range(3):
        raise ValueError("review_rounds must be 0, 1 or 2")
    marker_path = folder / "world/CORE.json"
    if marker_path.exists() and not resume:
        load_core(root, folder)
        return {"status": "core_reviewed", "reused": True, "calls_this_invocation": 0}
    if resume and not marker_path.exists():
        raise ValueError("--resume requires an existing canonical checkpoint")
    if list((folder / "world").glob("*.state.json")) or list((folder / "tasks").glob("*/workflow.json")):
        raise ValueError("seed-core requires a fresh dossier-only company folder")
    config = load_config(root)
    votes = config.get("generation", {}).get("review_votes", 1)
    if type(votes) is not int or votes < 1:
        raise ValueError("review_votes must be a positive integer")
    models = models or Models(
        config, folder / "world", max_calls=config["design"].get("seed_call_budget"), cumulative=True
    )
    instructions, skill = load_skill(root)
    review_instructions, review_skill = load_skill(root, "company-world-review")
    payload = core_payload(root, folder)
    spec = read(folder / "world-spec.json") if (folder / "world-spec.json").is_file() else {}
    payload.update(checkpoint="world_core", requirements=spec)
    fingerprint = inputs(root, folder)
    world_dir = folder / "world"
    previous = None
    if resume:
        previous = read(marker_path)
        # An explicit resume permits source edits and checker fixes, then runs
        # fresh reviews. It never silently substitutes a different company/spec.
        if any(previous["inputs"].get(k) != v for k, v in fingerprint.items() if k != "implementation"):
            raise ValueError("canonical author inputs changed; inspect before resuming")
        core = Blueprint.model_validate(
            {
                **read_core(folder, previous).model_dump(),
                "population_plan": read(world_dir / "population.json"),
            }
        )
        receipt = previous["receipt"]
        archive = world_dir / "revisions" / now().replace(":", "-")
        archive.mkdir(parents=True)
        for name in ("CORE.json", "CORE-REVIEW.json", "CORE-CHECKS.json"):
            shutil.copy2(world_dir / name, archive / name)
        write(archive / "RESUME-INPUT.json", core.model_dump())
    else:
        write(world_dir / "CORE-INPUT.json", payload)
        core, receipt = models.call(
            "world_states",
            skill_for_call(instructions, "world_core") + "\n" + json.dumps(payload),
            Blueprint,
            timeout_seconds=timeout_seconds,
        )
        write(world_dir / "CORE-DRAFT.json", core.model_dump())
    apps = {a["app_id"]: a for a in read(folder / "apps.json")["apps"]}
    workers = read(folder / "company.json")["workers"]
    roster = [w["id"] for w in workers]
    try:
        world, identities, materials, grants = validate_core(core, apps, roster)
    except ValueError as exc:
        if "identity" not in str(exc) and "distinct people" not in str(exc):
            raise
        core = repair_identities(core, apps, roster, payload["company"], str(exc), models, instructions)
        world, identities, materials, grants = validate_core(core, apps, roster)
    reconcile_identities(identities, canonical_people(world, roster))
    reviews = []
    reviewed_current = False
    for round_index in range(review_rounds + 1):
        checks = check_blueprint(core, world, identities, materials, grants, apps, workers, spec)
        if not checks["ok"]:
            verdict = WorldReview(
                verdict="revise", summary="Mechanical checks failed; semantic review not run.", findings=[]
            )
            reviewed_current = False
            write(
                world_dir / "CORE-REVIEW.json",
                {
                    "skill": review_skill,
                    "rounds": reviews,
                    "status": "not_run",
                    "mechanical_errors": checks["errors"],
                },
            )
            break
        packet = {
            "scope": "Stage 2 canonical world and desktop material sources only. Native apps and final tasks do not yet exist; do not require them. Review actual history, meaningful content, factual continuity, role boundaries and fair opportunities for later collaborative tasks. Evidence must be an exact JSON pointer into the provided world (including named collections), or worker_id/material_path. Common greetings may repeat.",
            "company": payload["company"],
            "requirements": spec,
            "world": world,
            "identities": identities,
            "worker_apps": grants,
            "materials": [m.model_dump() for m in materials],
            "population_plan": [p.model_dump() for p in core.population_plan],
            "mechanical_checks": checks,
        }

        def one_vote(index, packet=packet):
            return models.call(
                "world_review",
                review_instructions + "\n" + json.dumps({**packet, "vote": index}),
                WorldReview,
            )

        with ThreadPoolExecutor(max_workers=min(4, votes)) as pool:
            ballots = list(pool.map(one_vote, range(votes)))
        reviewed_current = True
        verdict, tally = merge_ballots([v for v, _ in ballots], votes=votes, scope_apps=set(), holders={})
        reviews.append(
            {
                "round": round_index,
                "review": verdict.model_dump(),
                "tally": tally,
                "ballots": [{"review": v.model_dump(), "receipt": r} for v, r in ballots],
            }
        )
        write(world_dir / "CORE-REVIEW.json", {"skill": review_skill, "rounds": reviews})
        errors = [f for f in verdict.findings if f.severity == "error"]
        if verdict.verdict == "accept" and not errors and checks["ok"]:
            break
        if round_index == review_rounds:
            break
        material_findings = [f.model_dump() for f in verdict.findings if f.target == "materials"]
        before = digest([world, [m.model_dump() for m in materials]])
        if material_findings:
            materials = repair_materials(materials, material_findings, world, models, instructions)
        faults = [{"path": f.evidence, "message": f.issue} for f in verdict.findings if f.target == "world"]
        if faults:
            from .world_repair import repair_world_records

            repair_world_records(world, faults, models, instructions, core.reference_date, payload["company"])
        if digest([world, [m.model_dump() for m in materials]]) == before:
            break  # No repair happened; do not count cached ballots as new reviews.
    checks = check_blueprint(core, world, identities, materials, grants, apps, workers, spec)
    accepted = (
        verdict.verdict == "accept"
        and not any(f.severity == "error" for f in verdict.findings)
        and checks["ok"]
    )
    written = [world_dir / "CORE-REVIEW.json"]

    def save(relative, value):
        path = world_dir / relative
        write(path, value)
        written.append(path)

    save("world.json", world)
    save("identities.json", identities)
    save("worker_apps.json", grants)
    save("population.json", [p.model_dump() for p in core.population_plan])
    save("CORE-CHECKS.json", checks)
    for material in materials:
        source = world_dir / "materials" / material.worker_id / material.path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(material.content)
        native = world_dir / "desktop" / material.worker_id / material.path
        native.parent.mkdir(parents=True, exist_ok=True)
        written.append(source)
        try:
            native.write_bytes(render_material(material.path, material.content.encode()))
            written.append(native)
        except Exception:
            if checks["ok"]:
                raise
    register_people(folder.parent, folder.name, *people_in([world, identities]))
    marker = {
        "status": "core_reviewed" if accepted else "core_needs_revision",
        "created_at": now(),
        "inputs": fingerprint,
        "skill": skill,
        "receipt": receipt,
        "generation_inputs": previous.get("generation_inputs", previous["inputs"])
        if previous
        else fingerprint,
        "resumed": resume,
        "core_metadata": {
            k: getattr(core, k) for k in ("rationale", "reference_date", "operating_scope", "assumptions")
        },
        "materials": [{"worker_id": m.worker_id, "path": m.path} for m in materials],
        "hashes": {str(p.relative_to(folder)): digest(p.read_bytes()) for p in written},
        "checks": checks,
        "review": verdict.verdict if reviewed_current else "not_run",
        "native_apps": "not_started",
    }
    write(world_dir / "CORE.json", marker)
    manifest = read(folder / "MANIFEST.json")
    manifest["stages"]["canonical_world"] = marker["status"]
    manifest["hashes"].update(marker["hashes"])
    write(folder / "MANIFEST.json", manifest)
    return {
        "status": marker["status"],
        "checks": checks,
        "review": marker["review"],
        "repair_rounds": max(0, len(reviews) - 1),
        "native_apps": "not_started",
    }
