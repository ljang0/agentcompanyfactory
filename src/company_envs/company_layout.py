"""One folder per company: each stage writes its deliverables where the next stage reads them.

Layout, mirroring CUA-Gym's per-task output folders and gym-anything's env/tasks tree:

    companies/<company_id>/
      MANIFEST.json            provenance, stage status, hashes
      company.json             Stage 1 researched dossier (workers, teams, software, evidence)
      review.json              Stage 1 fresh-session review of the company and its tasks
      apps.json                required hub apps: schema path and state keys = the seed contract
      tasks/<workflow_id>/
        workflow.json          full private design (builder/reviewer material, never on a VM)
        assignment.json        public boss brief, the only task text a worker sees
      world/                   Stage 2 output: <app_id>.state.json per app, world.json, materials/
      runtime/                 Stage 3 output: session ids, ports, VM assignments, traces, grades

Stage 1 fills the top level and ``tasks/``; the seed skill fills ``world/``; the
runtime fills ``runtime/``. Nothing here is an acceptance claim.
"""

import re
import shutil
import subprocess
from pathlib import Path

from .config import load_config
from .research_bundle import export_research
from .storage import digest, now, read, write
from .world.hub_app import hub_apps, top_level_keys
from .world.hub_identity import identity_key

WORLD_README = """# world/ — Stage 2 deliverables (seed skill output)

For every app in ../apps.json write `<app_id>.state.json`: the complete native state
with every documented state key (see the schema path in apps.json). The hydrator posts
it verbatim to the app's `/post {action: set}`; nothing else seeds the app.

Also write:
- `world.json`        canonical entities and history that the app states project from
- `identities.json`   {worker_id: {app_id: user record}} for every app with an identity_key;
                      each record must also appear in that app's users collection
- `materials/<worker_id>/...`   per-worker desktop files delivered only to that worker's VM

Do not put the private feasible path, grading key or delegation plan in any of these.
"""

RUNTIME_README = """# runtime/ — Stage 3 deliverables

- `sessions.json`  one shared session id per app for this company world
- `endpoints.json` per worker, per app proxy URLs (each worker is logged in as itself); harness ports stay on the host
- `attribution/<app_id>.jsonl`  which worker changed which state keys, when
- `vms/<worker_id>/` VM identity, materials delivered, trace of actions
- `grades/<workflow_id>.json` final independent assessment against the public brief
"""


def current_pins(root, *, config=None):
    """Capture configured versions and the exact catalog manifest bytes.

    Partial export configuration may override pins; otherwise use root's config.
    Missing versions remain explicit nulls and fail verification, never inferred pins.
    """
    root = Path(root)
    design = load_config(root, missing_ok=True).get("design", {})
    design.update((config or {}).get("design", {}))
    return {
        "hub_revision": design.get("hub_revision"),
        "model_pin": design.get("model_pin"),
        "catalog_manifest_sha256": digest((root / "catalogs" / "manifest.json").read_bytes()),
    }


def pin_errors(pins):
    """Validate immutable git/catalog digests and an explicit provider/model name."""
    if not isinstance(pins, dict):
        return ["pins must be an object"]
    errors = []
    for key, pattern in (
        ("hub_revision", r"[0-9a-f]{40}|[0-9a-f]{64}"),
        ("catalog_manifest_sha256", r"[0-9a-f]{64}"),
        ("model_pin", r"[^\s/]+/[^\s]+"),
    ):
        if not isinstance(pins.get(key), str) or not re.fullmatch(pattern, pins[key]):
            errors.append(f"invalid or missing pin: {key}")
    return errors


def verify_pins(root, folder=None):
    """Report export/config/catalog drift and the configured hub checkout's HEAD.

    This checks the git revision, not uncommitted hub edits or model-provider alias
    resolution. Missing configuration, pins or checkout are verification failures.
    """
    root = Path(root)
    pins = read(Path(folder) / "MANIFEST.json").get("pins") if folder is not None else current_pins(root)
    drift = pin_errors(pins)
    current, revision, parent_revision, gitlink = None, None, None, None
    try:
        current = current_pins(root)
        drift.extend(f"current {error}" for error in pin_errors(current))
        for key, value in current.items():
            if not isinstance(pins, dict) or pins.get(key) != value:
                recorded = pins.get(key) if isinstance(pins, dict) else None
                drift.append(f"{key}: recorded {recorded!r}, current {value!r}")
        design = load_config(root)["design"]
        hub = Path(design["hub_root"]).expanduser()
        hub = hub if hub.is_absolute() else root / hub
        revision = subprocess.check_output(
            ["git", "-C", str(hub), "rev-parse", "HEAD"], text=True, stderr=subprocess.PIPE, timeout=10
        ).strip()
        if revision != current["hub_revision"] and not drift:

            def git(*args, directory=hub):
                return subprocess.check_output(
                    ["git", "-C", str(directory), *args], text=True, stderr=subprocess.PIPE, timeout=10
                ).strip()

            parent = git("rev-parse", "--show-superproject-working-tree")
            if parent:
                parent_revision = git("rev-parse", "HEAD", directory=parent)
                submodule = Path(git("rev-parse", "--show-toplevel")).relative_to(Path(parent))
                entry = git("ls-tree", current["hub_revision"], "--", str(submodule), directory=parent)
                fields = entry.split()
                gitlink = fields[2] if len(fields) >= 3 and fields[:2] == ["160000", "commit"] else None
            if parent_revision != current["hub_revision"] or gitlink != revision:
                drift.append(f"hub checkout: HEAD {revision!r}, configured {current['hub_revision']!r}")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        drift.append(f"cannot inspect current pins: {exc}")
    return {
        "ok": not drift,
        "pins": pins,
        "current": current,
        "hub_revision": revision,
        "superproject_revision": parent_revision,
        "pinned_submodule_revision": gitlink,
        "drift": drift,
    }


def export_company(
    root, run_id, company_id, output=None, *, config=None, tasks_required=True, dossier_only=False
):
    """Assemble a selected company from a finished Stage 1 run into its own folder."""
    root = Path(root)
    if config is None:
        config = load_config(root, missing_ok=True)
    standard_ids = set(config.get("design", {}).get("standard_apps", []))
    run_dir = root / "runs" / run_id
    dataset = read(run_dir / "dataset.json")
    companies = dataset["companies"]
    if company_id not in companies:
        raise ValueError(f"{company_id} is not a selected company in run {run_id}")
    company = read(root / companies[company_id])
    workflows = [] if dossier_only else [w for w in dataset["workflows"] if w["company_id"] == company_id]
    if not workflows and tasks_required and not dossier_only:
        raise ValueError(f"{company_id} has no selected workflows in run {run_id}")
    assignments = read(run_dir / "assignments.json") if (run_dir / "assignments.json").is_file() else {}
    catalog = hub_apps(read(root / "catalogs" / "apps.json")["apps"])
    pins = current_pins(root, config=config)

    output = Path(output) if output else root / "companies" / company_id
    if output.exists():
        raise ValueError(f"{output} already exists; remove it or choose another output")
    (output / "tasks").mkdir(parents=True)
    (output / "world").mkdir()
    (output / "runtime").mkdir()

    write(output / "company.json", company)
    review_paths = {w["review_path"] for w in workflows}
    if len(review_paths) == 1:
        shutil.copyfile(root / review_paths.pop(), output / "review.json")

    required_ids = {
        app_id
        for w in workflows
        for software in company["software"]
        if software["id"] in read(root / w["workflow_path"])["software_requirement_ids"]
        for app_id in software["catalog_app_ids"]
    }
    if not workflows:
        # Before task design, every dossier domain app is a candidate surface.
        required_ids.update(a for s in company["software"] for a in s["catalog_app_ids"])
    apps = []
    for app_id in sorted(required_ids | standard_ids):
        entry = catalog.get(app_id)
        schema = root / "catalogs" / "app_schemas" / Path(entry["schema"]).name if entry else None
        apps.append(
            {
                "app_id": app_id,
                "role": "standard" if app_id in standard_ids else "task",
                "hub_seedable": entry is not None,
                "schema": str(schema.relative_to(root)) if schema else None,
                # The seed contract: the state file must have exactly these top-level keys.
                "top_level_keys": top_level_keys(schema.read_text()) if schema and schema.is_file() else None,
                # Which top-level key the app treats as the logged-in user; None = single-account app.
                "identity_key": identity_key(schema.read_text()) if schema and schema.is_file() else None,
                "state_file": f"world/{app_id}.state.json",
            }
        )
    write(
        output / "apps.json",
        {
            "identity": "per_worker_proxy",
            "workers": [w["id"] for w in company["workers"]],
            "identities_file": "world/identities.json",
            "apps": apps,
        },
    )

    task_ids = []
    for w in workflows:
        task_dir = output / "tasks" / w["workflow_id"]
        task_dir.mkdir()
        workflow = read(root / w["workflow_path"])
        write(task_dir / "workflow.json", workflow)
        assignment = assignments.get(w["workflow_id"]) or {
            "workflow_id": w["workflow_id"],
            "company_id": company_id,
            "title": workflow["title"],
            "brief": workflow["brief"],
        }
        write(task_dir / "assignment.json", assignment)
        task_ids.append(w["workflow_id"])
    (output / "world" / "README.md").write_text(WORLD_README)
    (output / "runtime" / "README.md").write_text(RUNTIME_README)
    research = export_research(
        root, output, {"source_run": run_id, "source_paths": {"company": companies[company_id]}}, company
    )

    manifest = {
        "company_id": company_id,
        "exported_at": now(),
        "source_run": run_id,
        "export_mode": "dossier_only" if dossier_only else "selected_tasks",
        "research": {"path": "research/PROVENANCE.json", "status": research["status"]},
        "pins": pins,
        "source_paths": {
            "company": companies[company_id],
            "evidence": research["source_evidence"],
            "workflows": [w["workflow_path"] for w in workflows],
        },
        "stages": {
            "stage1_design": "selected" if workflows else "pending",
            "stage2_world": "pending",
            "stage3_runtime": "pending",
        },
        "tasks": task_ids,
        "hashes": {
            str(p.relative_to(output)): digest(p.read_bytes())
            for p in sorted(output.rglob("*"))
            if p.is_file()
        },
    }
    write(output / "MANIFEST.json", manifest)
    return output, manifest


def export_run(root, run_id, output_root=None, *, config=None, tasks_required=True, dossier_only=False):
    """Export every selected company of a run; returns the folders written."""
    dataset = read(Path(root) / "runs" / run_id / "dataset.json")
    results = []
    for company_id in sorted(dataset["companies"]):
        output = (Path(output_root) / company_id) if output_root else None
        results.append(
            export_company(
                root,
                run_id,
                company_id,
                output,
                config=config,
                tasks_required=tasks_required,
                dossier_only=dossier_only,
            )[0]
        )
    return results
