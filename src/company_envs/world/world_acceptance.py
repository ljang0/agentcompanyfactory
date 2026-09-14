"""Stage 4 acceptance of the final population, with content-bound review evidence."""

from pathlib import Path

from company_envs.config import load_config
from company_envs.models import Models
from company_envs.storage import digest, now, read, write

from .blueprint import load_core
from .population_amendments import corrected_materials
from .population_quality import quality_errors, resource_errors
from .population_snapshot import population_snapshot
from .seed_calls import WorkerMaterial
from .world_check import check_folder
from .world_review import make_reviewer


def review_population(root, folder, *, models=None, round_index=0):
    root, folder = Path(root).resolve(), Path(folder).resolve()
    snapshot = population_snapshot(root, folder)
    work = folder / "world/acceptance"
    config = load_config(root)
    core, _ = load_core(root, folder)
    manifest = read(folder / "apps.json")
    apps = {a["app_id"]: a for a in manifest["apps"]}
    states = {a: read(folder / item["state_file"]) for a, item in apps.items()}
    checks = check_folder(root, folder, states)
    native_errors = quality_errors(states) + resource_errors(folder)
    if not checks["ok"] or native_errors:
        write(work / "CHECKS.json", {"checks": checks, "native_errors": native_errors})
        raise ValueError("Population requires mechanical repair before independent acceptance")
    materials = []
    source = folder / "world/materials"
    for path in sorted(source.rglob("*")):
        if path.is_file():
            relative = path.relative_to(source)
            materials.append(
                WorkerMaterial(
                    worker_id=relative.parts[0], path=str(Path(*relative.parts[1:])), content=path.read_text()
                )
            )
    materials = corrected_materials(folder, materials)
    # CSV values are mechanically checked in full. Show their header/first rows rather
    # than displacing the app content with thousands of repeated ledger cells.
    sampled = [
        m.model_copy(
            update={
                "content": "\n".join(m.content.splitlines()[:12])
                + f"\n[CSV sample; {len(m.content.splitlines()) - 1} data rows; full content SHA256 {digest(m.content)}]"
            }
        )
        if m.path.lower().endswith(".csv") and len(m.content.splitlines()) > 12
        else m
        for m in materials
    ]
    models = models or Models(
        config, folder / "world", max_calls=config["design"].get("seed_call_budget"), cumulative=True
    )
    previous = work / "REVIEW.json"
    reported = []
    if previous.exists():
        from .world_review import ReviewFinding

        reported = [ReviewFinding.model_validate(f) for f in read(previous)["review"]["findings"]]
    resolutions = folder / "world/population/REVIEW-RESOLUTIONS.json"
    if resolutions.exists():
        from .world_review import ReviewFinding

        # Keep the supporting records in the next independent sample. A repair
        # author's disposition never substitutes for the reviewers' verdict.
        reported.extend(
            ReviewFinding(target=r["target"], severity="warning", issue=r["decision"], evidence=r["evidence"])
            for r in read(resolutions).get("findings", [])
        )
    reviewer = make_reviewer(
        root,
        config,
        models,
        # A repair first gets one fresh reading; a potential accept is always
        # confirmed by the full configured panel. Avoid three paid rejections
        # of the same unresolved defect on every repair attempt.
        review_rounds=round_index + 1,
        reference_date=core.reference_date,
        operating_scope=core.operating_scope,
        company=read(folder / "company.json"),
        tasks=[],
        world=read(folder / "world/population/EFFECTIVE-WORLD.json"),
        identities=read(folder / "world/identities.json"),
        worker_apps=read(folder / "world/worker_apps.json"),
        apps=apps,
        mechanical=lambda _: checks,
        reported=reported,
    )
    try:
        verdict, meta = reviewer(round_index, states, sampled)
    except Exception as exc:
        # Provider/permission failure is not a verdict about the company's data.
        write(
            work / f"REVIEW-ATTEMPT-{round_index}-{digest(snapshot)[:12]}.json",
            {
                "at": now(),
                "round": round_index,
                "baseline": snapshot,
                "status": "review_unavailable",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        raise
    if population_snapshot(root, folder) != snapshot:
        raise ValueError("Population changed during acceptance review")
    result = {
        "at": now(),
        "round": round_index,
        "baseline": snapshot,
        "review": verdict.model_dump(),
        "meta": meta,
        "material_content_hash": digest([m.model_dump() for m in materials]),
        "checks": checks,
        "native_errors": native_errors,
        "runtime": "not yet verified",
    }
    write(work / f"REVIEW-{round_index}-{digest(snapshot)[:12]}.json", result)
    write(previous, result)
    return result


def freeze_population(root, folder, runtime=None, *, refresh_tasks=False):
    """Fail closed on stale, skipped, failed or incomplete acceptance evidence."""
    root, folder = Path(root).resolve(), Path(folder).resolve()
    snapshot = population_snapshot(root, folder)
    runtime = runtime or read(folder / "world/acceptance/RUNTIME.json")
    review = read(folder / "world/acceptance/REVIEW.json")
    if review["baseline"] != snapshot or review["review"]["verdict"] != "accept":
        raise ValueError("A fresh accepted world review is required")
    required = {"build", "render", "writes", "persistence", "access", "resources", "reset"}
    if (
        runtime.get("baseline") != snapshot
        or set(runtime.get("checks", {})) != required
        or any(v is not True for v in runtime["checks"].values())
    ):
        raise ValueError("Complete runtime acceptance evidence is required")
    validate_runtime_evidence(root, folder, runtime, snapshot)
    frozen_path = folder / "world/FROZEN.json"
    if frozen_path.exists():
        previous = read(frozen_path)
        if (
            previous.get("status") == "world_accepted"
            and previous.get("baseline") == snapshot
            and previous.get("review_hash") == digest(review)
            and previous.get("runtime_hash") == digest(runtime)
        ):
            return previous
    receipt = {
        "schema_version": 1,
        "status": "world_accepted",
        "at": now(),
        "baseline": snapshot,
        "review_hash": digest(review),
        "runtime_hash": digest(runtime),
        "reference_date": read(folder / "world/POPULATION.json")["reference_date"],
    }
    bindings = sorted((folder / "tasks").glob("*/WORLD.json"))
    if bindings:
        if not refresh_tasks:
            raise ValueError("Tasks already bind this world; use --refresh-tasks after runtime-only repair")
        _refresh_task_bindings(folder, receipt, runtime, bindings)
    elif refresh_tasks:
        raise ValueError("No existing task bindings to refresh")
    write(folder / "world/acceptance/RUNTIME.json", runtime)
    write(folder / "world/FROZEN.json", receipt)
    return receipt


def _refresh_task_bindings(folder, receipt, runtime, bindings):
    """Carry unchanged tasks across a measured runtime repair, retaining their old proof."""
    previous = read(folder / "world/FROZEN.json")
    if previous.get("status") != "world_accepted" or any(
        previous[key] != receipt[key] for key in ("baseline", "review_hash", "reference_date")
    ):
        raise ValueError("Task binding refresh requires unchanged population and accepted content review")
    old_hash = digest(previous)
    saved = {str(p.relative_to(folder)): read(p) for p in bindings}
    if any(b.get("frozen_hash") != old_hash for b in saved.values()):
        raise ValueError("Task binding does not match the previous frozen world")
    # verify-world writes a new current receipt but retains every native run.
    candidates = [folder / "world/acceptance/RUNTIME.json"]
    candidates += sorted((folder / "world/acceptance/runtime").glob("*/RESULT.json"))
    prior_runtime = next(
        (read(p) for p in candidates if p.exists() and digest(read(p)) == previous["runtime_hash"]), None
    )
    if prior_runtime is None or prior_runtime.get("baseline") != previous["baseline"]:
        raise ValueError("The previous runtime receipt must be retained before refreshing task bindings")
    for relative, expected in prior_runtime.get("evidence", {}).items():
        path = (folder / relative).resolve()
        if not path.is_relative_to(folder) or not path.is_file() or digest(path.read_bytes()) != expected:
            raise ValueError(f"Previous runtime evidence changed: {relative}")
    archive = (
        folder
        / "world/acceptance/task-binding-refreshes"
        / digest({"previous": previous, "runtime": runtime})
    )
    if archive.exists():
        raise ValueError(f"Task binding refresh already started; inspect retained journal: {archive}")
    task_files = {
        str(p.relative_to(folder)): digest(p.read_bytes())
        for binding in bindings
        for p in sorted(binding.parent.iterdir())
        if p.is_file() and p != binding
    }
    journal = {
        "at": now(),
        "previous_frozen_hash": old_hash,
        "frozen_hash": digest(receipt),
        "previous_runtime_hash": previous["runtime_hash"],
        "runtime_hash": receipt["runtime_hash"],
        "task_files": task_files,
        "bindings": saved,
        "scope": "runtime acceptance and task bindings; task content, calibration and trials unchanged",
    }
    write(archive / "FROZEN.json", previous)
    write(archive / "RUNTIME.json", prior_runtime)
    write(archive / "NEXT-FROZEN.json", receipt)
    write(archive / "REFRESH.json", journal)
    for path in bindings:
        write(
            path,
            {
                "frozen_hash": digest(receipt),
                "runtime_refresh": str((archive / "REFRESH.json").relative_to(folder)),
            },
        )


def validate_runtime_evidence(root, folder, runtime, snapshot):
    """Boolean claims alone cannot freeze a world; require coverage and intact receipts."""
    from .runtime_acceptance import implementation_snapshot

    if runtime.get("implementation") != implementation_snapshot(root):
        raise ValueError("Runtime implementation changed since acceptance")
    grants = read(folder / "world/worker_apps.json")
    pairs = {(a, w) for w, apps in grants.items() for a in apps}
    apps = set(snapshot["states"])
    for name in ("rendered", "access"):
        rows = runtime.get(name, [])
        if {(r["app"], r["worker"]) for r in rows} != pairs or not all(r.get("ok") is True for r in rows):
            raise ValueError(f"Incomplete runtime {name} coverage")
    for name in ("builds", "reset"):
        rows = runtime.get(name, {})
        if set(rows) != apps or not all(r.get("ok") is True for r in rows.values()):
            raise ValueError(f"Incomplete runtime {name} coverage")
    from .hub_app import source_hash

    hub = Path(load_config(root)["design"]["hub_root"])
    for aid, record in runtime["builds"].items():
        if record.get("native_source_hash") != source_hash(hub / aid) or not record.get("build", {}).get(
            "dist_hash"
        ):
            raise ValueError(f"Native app implementation changed: {aid}")
    if set(runtime.get("actors", {})) != apps or any(n < 1 for n in runtime["actors"].values()):
        raise ValueError("Missing trusted actor write evidence")
    writes = runtime.get("writes", [])
    if {r["app"] for r in writes} != apps or not all(r.get("ok") is True for r in writes):
        raise ValueError("Incomplete runtime write coverage")
    if not runtime.get("evidence"):
        raise ValueError("Missing runtime evidence files")
    for relative, expected in runtime["evidence"].items():
        path = (folder / relative).resolve()
        if not path.is_relative_to(folder) or not path.is_file() or digest(path.read_bytes()) != expected:
            raise ValueError(f"Runtime evidence changed: {relative}")


def accepted_world(root, folder):
    """Read a frozen population only while its data, code, review and proof still agree."""
    root, folder = Path(root).resolve(), Path(folder).resolve()
    frozen = read(folder / "world/FROZEN.json")
    snapshot = population_snapshot(root, folder)
    review = read(folder / "world/acceptance/REVIEW.json")
    runtime = read(folder / "world/acceptance/RUNTIME.json")
    if frozen.get("status") != "world_accepted" or frozen["baseline"] != snapshot:
        raise ValueError("Frozen world baseline changed")
    if frozen["review_hash"] != digest(review) or frozen["runtime_hash"] != digest(runtime):
        raise ValueError("Frozen world acceptance receipts changed")
    if (
        review["baseline"] != snapshot
        or review["review"]["verdict"] != "accept"
        or runtime["baseline"] != snapshot
    ):
        raise ValueError("World acceptance does not describe this population")
    validate_runtime_evidence(root, folder, runtime, snapshot)
    return frozen
