"""Public command line; every generation run is independently resumable."""

import argparse
import json
import sys
from pathlib import Path

from .catalogs import bootstrap
from .config import load_config, selected_config
from .pipeline import new_run, run
from .report import build_report, label_difficulty
from .storage import read, write


def positive_integer(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, help="TOML configuration; overrides COMPANY_ENVS_CONFIG")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="read-only prerequisite and dossier checks; no model calls")
    doctor.add_argument("--folder", type=Path, help="also inspect an exported company and its research")
    doctor.add_argument("--profile", choices=["inspect", "generate", "desktop"], default="inspect")
    preflight = commands.add_parser(
        "preflight", help="probe TCP and temporary profile writes; no model calls or app builds"
    )
    preflight.add_argument("--scope", choices=["models", "runtime", "all"], default="all")
    preflight.add_argument("--output", type=Path, help="also save the measured capability report")
    commands.add_parser("permissions-config", help="print host Codex permissions for the selected config")
    boot = commands.add_parser("bootstrap", help="import versioned reference data once")
    boot.add_argument("--source", type=Path, required=True)
    gen = commands.add_parser("generate", help="research, expand, review and select designs")
    gen.add_argument("--companies", type=int)
    gen.add_argument("--tasks", type=int)
    gen.add_argument("--workers", type=int)
    gen.add_argument("--seed", type=int)
    gen.add_argument("--resume", help="existing run id")
    report = commands.add_parser("report", help="read-only integrity and coverage report")
    report.add_argument("run_id")
    label = commands.add_parser("label-difficulty", help="backfill difficulty labels for accepted tasks")
    label.add_argument("run_id")
    audit = commands.add_parser("audit", help="read-only, per-stage artifact and provenance audit")
    audit.add_argument("run_id")
    smoke = commands.add_parser(
        "hub-smoke", help="prove one hub app is seedable and inspectable through the generic contract"
    )
    smoke.add_argument("app_id", help="catalog app id, e.g. Zendesk_mock")
    smoke.add_argument("--state", type=Path, required=True, help="full native state JSON to seed")
    smoke.add_argument("--sid", default="smoke")
    smoke.add_argument("--hub-root", type=Path, help="defaults to design.hub_root in config.toml")
    smoke.add_argument("--work", type=Path, help="build/serve directory (default experiments/hub-cache)")
    smoke.add_argument("--expect", help="text that must appear in the rendered app after seeding")
    serve = commands.add_parser(
        "hub-serve", help="seed and serve a company folder's hub apps behind worker proxies"
    )
    serve.add_argument("folder", type=Path, help="companies/<company_id> with world/<app>.state.json files")
    serve.add_argument(
        "--episode", default="ep1", help="episode label; the shared session id derives from it"
    )
    serve.add_argument(
        "--host", default="0.0.0.0", help="interface worker VMs reach; harness ports stay on loopback"
    )
    serve.add_argument("--hub-root", type=Path)
    serve.add_argument("--work", type=Path)
    serve.add_argument(
        "--check", action="store_true", help="seed, verify readback, write runtime files, then stop"
    )
    render = commands.add_parser(
        "render-check",
        help="render every served app in headless Chrome and write runtime/RENDER.json (needs hub-serve)",
    )
    render.add_argument("folder", type=Path, help="companies/<company_id> with runtime/endpoints.json")
    render.add_argument("--work", type=Path, help="directory for the throwaway browser profiles")
    render.add_argument(
        "--host", help="interface to reach the proxies on (default: theirs; 0.0.0.0 -> 127.0.0.1)"
    )
    visual = commands.add_parser(
        "visual-judge",
        help="judge the rendered apps and worker start pages for realism; writes runtime/VISUAL.json",
    )
    visual.add_argument("folder", type=Path, help="companies/<company_id> with runtime/RENDER.json")
    visual.add_argument("--task", help="task id whose worker start pages are judged (needs hub-serve)")
    visual.add_argument("--votes", type=positive_integer, default=3, help="judge votes per screen")
    visual.add_argument("--work", type=Path, help="directory for the throwaway browser profiles")
    seedw = commands.add_parser(
        "seed-world", help="author world/<app>.state.json, identities and materials for a company folder"
    )
    core = commands.add_parser(
        "seed-core", help="author and review canonical history and desktops; stop before app population"
    )
    core.add_argument("folder", type=Path)
    population = commands.add_parser(
        "populate-world", help="populate native apps from an accepted core; stop before world acceptance"
    )
    population.add_argument("folder", type=Path)
    population.add_argument("--app", action="append", help="populate only this app; repeat for several")
    patchpop = commands.add_parser(
        "patch-population", help="apply a baseline-bound native repair; preserve originals"
    )
    patchpop.add_argument("folder", type=Path)
    patchpop.add_argument("patch", type=Path)
    recheckpop = commands.add_parser(
        "recheck-population", help="revalidate saved native output after compiler changes; no model calls"
    )
    recheckpop.add_argument("folder", type=Path)
    reviewworld = commands.add_parser(
        "review-world", help="fresh coherence review of a mechanically valid population"
    )
    reviewworld.add_argument("folder", type=Path)
    reviewworld.add_argument("--round", type=int, default=0)
    verifyworld = commands.add_parser(
        "verify-world", help="build, browser-test and reset a population; no model calls"
    )
    verifyworld.add_argument("folder", type=Path)
    verifyworld.add_argument("--work", type=Path, help="private app build cache")
    freezeworld = commands.add_parser(
        "freeze-world", help="freeze only an accepted, tested, unchanged population"
    )
    freezeworld.add_argument("folder", type=Path)
    core.add_argument("--timeout", type=positive_integer, default=2700)
    core.add_argument("--review-rounds", type=int, choices=range(3), default=2)
    core.add_argument(
        "--resume",
        action="store_true",
        help="review edited canonical sources without reauthoring; archive prior checks",
    )
    seedw.add_argument("folder", type=Path, help="companies/<company_id> produced by export-company")
    seedw.add_argument("--timeout", type=positive_integer, default=1500, help="model call budget in seconds")
    seedw.add_argument(
        "--verify", action="store_true", help="then seed, read back and proxy every app (hub-serve --check)"
    )
    seedw.add_argument("--hub-root", type=Path)
    seedw.add_argument("--work", type=Path)
    seedw.add_argument("--host", default="0.0.0.0", help="interface for the verification proxies")
    seedw.add_argument(
        "--review-rounds",
        type=int,
        choices=range(7),
        default=1,
        help="fresh-session coherence review plus bounded per-app repair rounds (0 disables)",
    )
    wcheck = commands.add_parser(
        "world-check", help="mechanical coherence checks on a company folder's world/"
    )
    wcheck.add_argument("folder", type=Path)
    wrepair = commands.add_parser(
        "world-repair",
        help="send a checked world's mechanical errors back to its authors (bounded rounds, bulk re-laid), then check again",
    )
    wrepair.add_argument("folder", type=Path)
    wrepair.add_argument("--rounds", type=int, default=2, help="author rounds at most (default 2)")
    rrepair = commands.add_parser(
        "review-repair",
        help="send a rejected world's review findings to the apps owning the cited records, then review again",
    )
    rrepair.add_argument("folder", type=Path)
    rrepair.add_argument("--rounds", type=int, default=2, help="repair-and-review rounds at most (default 2)")
    launch = commands.add_parser(
        "launch-vms",
        help="one worker VM per task participant, wired to that worker's proxy URLs and materials",
    )
    launch.add_argument("folder", type=Path)
    launch.add_argument("task_id")
    launch.add_argument(
        "--base-image", type=Path, required=True, help="qcow2 base image with desktop and browser"
    )
    launch.add_argument(
        "--browser-dir", type=Path, required=True, help="browser distribution directory delivered to guests"
    )
    launch.add_argument(
        "--host-ip", required=True, help="host address the VMs use to reach the worker proxies"
    )
    launch.add_argument(
        "--workers", nargs="*", help="subset of worker ids (default: the task's participants)"
    )
    launch.add_argument("--dry-run", action="store_true", help="write every VM file without booting QEMU")
    stopvms = commands.add_parser("stop-vms", help="stop a company folder's running worker VMs")
    stopvms.add_argument("folder", type=Path)
    resetvms = commands.add_parser(
        "reset-vms", help="stop, discard overlays and relaunch the same VMs (apps are not reseeded)"
    )
    resetvms.add_argument("folder", type=Path)
    episode = commands.add_parser(
        "run-episode", help="run the multi-agent worker harness for one task (fake backend = simulation only)"
    )
    episode.add_argument("folder", type=Path)
    episode.add_argument("task_id")
    episode.add_argument("--backend", choices=("fake", "mypcbench"), default="fake")
    episode.add_argument("--seconds", type=positive_integer, default=60)
    calib = commands.add_parser(
        "calibrate",
        help="author (if missing) and calibrate a task grader: initial world must score 0, reference must pass",
    )
    calib.add_argument("folder", type=Path)
    calib.add_argument("task_id")
    calib.add_argument(
        "--golden",
        action="store_true",
        help="calibrate by replaying tasks/<id>/golden.json in a fresh session",
    )
    calib.add_argument(
        "--repair",
        type=int,
        choices=(0, 1, 2),
        default=2,  # one grader revision and one golden revision before rejecting the task
        help="on calibration failure, re-author the grader with the findings this many times (bounded)",
    )
    goldenp = commands.add_parser(
        "author-golden", help="author a private reference trajectory in a separate model call"
    )
    goldenp.add_argument("folder", type=Path)
    goldenp.add_argument("task_id")
    verifierp = commands.add_parser(
        "author-verifier", help="author an independent private Python verifier for a staged task"
    )
    verifierp.add_argument("folder", type=Path)
    verifierp.add_argument("task_id")
    verifier_calib = commands.add_parser(
        "calibrate-verifier", help="replay and calibrate an accepted staged task with saved checkpoints"
    )
    verifier_calib.add_argument("folder", type=Path)
    verifier_calib.add_argument("task_id")
    verifier_calib.add_argument("--work", type=Path, required=True)
    verifier_calib.add_argument("--reference-checkpoint", type=Path)
    trialp = commands.add_parser("trial-task", help="run or reopen one calibrated real desktop trial")
    trialp.add_argument("folder", type=Path)
    trialp.add_argument("task_id")
    trialp.add_argument(
        "--kind", choices=("teacher", "ordinary", "worker-ablation", "input-ablation"), required=True
    )
    trialp.add_argument("--work", type=Path, required=True)
    trialp.add_argument("--attempt", type=int, default=1)
    trialp.add_argument("--unavailable-worker")
    trialp.add_argument("--input-worker")
    packagep = commands.add_parser("prepare-collaborator", help="prepare an accepted pilot checkout locally")
    packagep.add_argument("folder", type=Path)
    packagep.add_argument("--output", type=Path, required=True)
    packagep.add_argument(
        "--inspection",
        action="store_true",
        help="package accepted world/calibration and retained failures without certifying team gates",
    )
    verifyp = commands.add_parser(
        "verify-collaborator", help="verify an extracted checkout and replay/reset without models"
    )
    verifyp.add_argument("--work", type=Path, required=True)
    statusp = commands.add_parser("pilot-status", help="inspect current checkpoint evidence without models")
    statusp.add_argument("folder", type=Path)
    statusp.add_argument("--output", type=Path)
    diagnosticp = commands.add_parser(
        "diagnose-trial",
        help="grade saved trial outputs for diagnosis without certifying a failed environment",
    )
    diagnosticp.add_argument("folder", type=Path)
    diagnosticp.add_argument("task_id")
    diagnosticp.add_argument(
        "--kind", choices=("teacher", "ordinary", "worker-ablation", "input-ablation"), required=True
    )
    diagnosticp.add_argument("--attempt", type=int, required=True)
    regradep = commands.add_parser(
        "regrade-trial", help="resume verification of a healthy saved desktop episode after a grader repair"
    )
    regradep.add_argument("folder", type=Path)
    regradep.add_argument("task_id")
    regradep.add_argument(
        "--kind", choices=("teacher", "ordinary", "worker-ablation", "input-ablation"), required=True
    )
    regradep.add_argument("--attempt", type=int, required=True)
    readp = commands.add_parser("readability", help="write REPORT-readability for a company folder")
    readp.add_argument("folder", type=Path)
    gradep = commands.add_parser(
        "grade", help="grade a task against the running apps' final state (needs hub-serve endpoints)"
    )
    gradep.add_argument("folder", type=Path)
    gradep.add_argument("task_id")
    gradep.add_argument(
        "--judge", action="store_true", help="also score judgment checks with a fresh-session model call"
    )
    export = commands.add_parser(
        "export-company", help="assemble a selected company into companies/<id>/ for Stage 2"
    )
    export.add_argument("run_id")
    export.add_argument("company_id", nargs="?", help="omit to export every selected company of the run")
    export.add_argument("--output", type=Path)
    export.add_argument("--no-tasks", action="store_true", help="allow export before tasks exist")
    export.add_argument(
        "--dossier-only", action="store_true", help="exclude existing tasks for a fresh world"
    )
    author = commands.add_parser("author-tasks", help="author tasks against a seeded company world")
    author.add_argument("folder", type=Path)
    author.add_argument("--count", type=int, required=True)
    candidate = commands.add_parser(
        "review-task", help="independently review a supplied task repair against its frozen world"
    )
    candidate.add_argument("folder", type=Path)
    candidate.add_argument("candidate", type=Path)
    release = commands.add_parser(
        "release", help="bundle a pinned company and its seeded world into a self-contained archive"
    )
    release.add_argument("folder", type=Path)
    release.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify-release", help="verify a release archive's hashes and pins offline")
    verify.add_argument("archive", type=Path)
    transform = commands.add_parser(
        "transform",
        help="rename entity ids and shift dates on a seeded company; verify checks and grader still hold",
    )
    transform.add_argument("folder", type=Path)
    transform.add_argument("output", type=Path)
    transform.add_argument("--rename-seed", type=int)
    transform.add_argument("--shift-days", type=int, default=0)
    runp = commands.add_parser(
        "run-company",
        help=(
            "one command: load, check, serve, calibrate, teacher, launch, then episode:<policy> and "
            "grade:<policy> for every [worker_policy] policies entry, teardown; resumable. Each task "
            "runs in its own runtime, runtime/tasks/<task_id>/ (checkpoint CONTROLLER.json, working "
            "copy under controller/); run it once per task"
        ),
    )
    runp.add_argument("folder", type=Path)
    runp.add_argument(
        "task_id", help="tasks/<task_id>/workflow.json; its runtime is runtime/tasks/<task_id>/"
    )
    runp.add_argument("--backend", choices=("fake", "mypcbench"), default="fake")
    restart = runp.add_mutually_exclusive_group()
    restart.add_argument(
        "--from",
        dest="from_step",
        metavar="STEP",
        help=(
            "restart at a controller step: load, check, serve, calibrate, teacher, launch, "
            "episode:<policy>, grade:<policy>, teardown (plain episode/grade: the first policy)"
        ),
    )
    restart.add_argument(
        "--reset",
        action="store_true",
        help="reseed to the initial episode state with a new session and relaunch",
    )
    runp.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="no QEMU; every file is still written (default for the fake backend; mypcbench boots)",
    )
    runp.add_argument("--base-image", type=Path)
    runp.add_argument("--browser-dir", type=Path)
    runp.add_argument("--host-ip")
    runp.add_argument(
        "--model-calls",
        type=positive_integer,
        default=0,
        help="worker policy model-call budget; required with the mypcbench backend",
    )
    runp.add_argument("--no-teacher", action="store_true", help="skip the teacher rollout safeguard")
    runp.add_argument("--teacher-model", help="model spec for the teacher policy (default: the expand model)")
    runp.add_argument("--seconds", type=positive_integer, default=1500, help="per-step budget in seconds")
    amplifyp = commands.add_parser(
        "amplify", help="derive task variants from an accepted task and its seeded world"
    )
    amplifyp.add_argument("folder", type=Path)
    amplifyp.add_argument("task_id")
    amplifyp.add_argument("--count", type=positive_integer, required=True)
    amplifyp.add_argument("--seed", type=int, default=0)
    sheet = commands.add_parser("review-sheet", help="one-page human review sheet for a company folder")
    sheet.add_argument("folder", type=Path)
    cohort = commands.add_parser("cohort-sheet", help="one table over every company folder for human review")
    cohort.add_argument("--companies", type=Path, default=Path("companies"))
    cohort.add_argument("--output", type=Path, required=True)
    ab = commands.add_parser(
        "add-bulk",
        help="add the bulk layer to a seeded world: model-written generator specs, records built by code",
    )
    ab.add_argument("folder", type=Path)
    ab.add_argument("--again", action="store_true")
    replay = commands.add_parser(
        "verify-cache-replay", help="reseed from cached calls and compare world files"
    )
    replay.add_argument("folder", type=Path)
    replay.add_argument("--output", type=Path)
    cohort_reportp = commands.add_parser("cohort-report", help="summarize recorded evidence for a batch")
    cohort_reportp.add_argument("companies_dir", type=Path)
    cohort_reportp.add_argument("--output", type=Path)
    ag = commands.add_parser(
        "agreement",
        help="write AGREEMENT.json: every gate a company must pass, with evidence; exit 3 when any fails",
    )
    ag.add_argument("folder", type=Path)
    dn = commands.add_parser(
        "dedupe-names",
        help="rename people this world shares with an earlier company, everywhere, and register the new names",
    )
    dn.add_argument("folder", type=Path)
    sw = commands.add_parser(
        "sync-worker-apps", help="rewrite a seeded world's worker_apps.json from the task contract"
    )
    sw.add_argument("folder", type=Path)
    aa = commands.add_parser(
        "assign-apps",
        help="bind each contribution to the apps its worker's VM holds and fix decisive collections; seeding then enforces the barrier",
    )
    aa.add_argument("folder", type=Path)
    aa.add_argument("--again", action="store_true")
    aa.add_argument(
        "--only",
        nargs="+",
        help="bind these task ids alone and leave ASSIGN.json (the seed stage's input) untouched",
    )
    rb = commands.add_parser(
        "rewrite-briefs",
        help="reword a company's task briefs in the manager's plain voice; facts, cells and grading unchanged",
    )
    rb.add_argument("folder", type=Path)
    rb.add_argument(
        "--again", action="store_true", help="restore the original text first and rewrite from it"
    )
    rb.add_argument(
        "--only",
        nargs="+",
        help="reword these task ids alone and leave REWRITE.json (the apps stage's input) untouched",
    )
    rc = commands.add_parser(
        "repair-criteria",
        help="drop interface-state sentences from a company's task criteria (mechanical, recorded)",
    )
    rc.add_argument("folder", type=Path)
    inspect = commands.add_parser("inspect", help="read an artifact relative to the repository")
    inspect.add_argument("path", type=Path)
    args = parser.parse_args()
    with selected_config(args.config):
        _dispatch(args, parser)


def _dispatch(args, parser):
    root = args.root.resolve()
    if args.command == "doctor":
        from .doctor import inspect_prerequisites

        result = inspect_prerequisites(root, folder=args.folder, profile=args.profile)
        print(json.dumps(result, indent=2))
        if not result["ok"]:
            raise SystemExit(1)
    elif args.command == "preflight":
        from .preflight import inspect_environment

        result = inspect_environment(load_config(root), scope=args.scope)
        if args.output:
            write(args.output, result)
        print(json.dumps(result, indent=2))
        if not result["ok"]:
            raise SystemExit(1)
    elif args.command == "permissions-config":
        from .preflight import permissions_config

        print(permissions_config(load_config(root)), end="")
    elif args.command == "bootstrap":
        manifest = bootstrap(root, args.source.resolve())
        pinned = root / "catalogs" / "embedding.json"
        if not pinned.exists():
            from huggingface_hub import model_info

            model = "sentence-transformers/all-mpnet-base-v2"
            write(pinned, {"model": model, "revision": model_info(model).sha})
        print(f"Pinned {len(manifest['files'])} reference files.")
    elif args.command == "generate":
        if args.resume and args.seed is not None:
            parser.error("a resumed run preserves its original seed")
        directory = (
            (root / "runs" / args.resume)
            if args.resume
            else new_run(root, args.companies, args.tasks, args.workers, args.seed)
        )
        state = run(root, directory, args.companies, args.tasks, args.workers)
        raise SystemExit(0 if state["status"] == "complete" else 2)
    elif args.command == "report":
        print(json.dumps(build_report(root, root / "runs" / args.run_id), indent=2))
    elif args.command == "label-difficulty":
        print(json.dumps(label_difficulty(root, args.run_id), indent=2))
    elif args.command == "audit":
        from .audit import audit_run

        result = audit_run(root, root / "runs" / args.run_id)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["status"] == "pass" else 2)
    elif args.command == "hub-smoke":
        from .world.hub_app import hub_apps
        from .world.hub_app import smoke as hub_smoke

        design = load_config(root).get("design", {})
        hub_root = args.hub_root or Path(design.get("hub_root", ""))
        catalog = hub_apps(read(root / "catalogs" / "apps.json")["apps"])
        if args.app_id not in catalog:
            raise SystemExit(f"{args.app_id} is not a hub app with a schema document")
        app = catalog[args.app_id]
        schema_text = (root / "catalogs" / "app_schemas" / Path(app["schema"]).name).read_text()
        report = hub_smoke(
            app,
            hub_root / args.app_id,
            args.work or root / "experiments" / "hub-cache",
            read(args.state),
            sid=args.sid,
            schema_text=schema_text,
            expect_text=args.expect,
        )
        record = root / "experiments" / "hub-smoke" / f"{args.app_id}.json"
        write(record, report)
        print(json.dumps({**report, "recorded": str(record.relative_to(root))}, indent=2))
    elif args.command == "transform":
        from .world.transform import transform_company, verify_transform

        transform_company(
            args.folder.resolve(),
            args.output.resolve(),
            rename_seed=args.rename_seed,
            shift_days=args.shift_days,
        )
        result = verify_transform(args.folder.resolve(), args.output.resolve())
        print(json.dumps(result, indent=2, default=str))
        raise SystemExit(0 if result["ok"] else 2)
    elif args.command == "hub-serve":
        from .world.hub_world import serve_company

        design = load_config(root).get("design", {})
        report = serve_company(
            args.folder,
            args.hub_root or Path(design.get("hub_root", "")),
            args.work or root / "experiments" / "hub-cache",
            episode=args.episode,
            host=args.host,
            check_only=args.check,
            root=root,
        )
        if args.check:
            print(json.dumps(report, indent=2))
    elif args.command == "render-check":
        from .world.render_check import admissible, format_lines, run_render_check

        report = run_render_check(args.folder.resolve(), host=args.host, work=args.work)
        print("\n".join(format_lines(report)))
        # ``admissible``, not ``ok``: a host that cannot render is a fault about the host and the
        # pipeline goes on without that evidence (the VM stage takes it again in a real browser),
        # while a render that measured nothing stops the company. The rule lives in render_check so
        # this exit and the driver's gate cannot drift apart.
        raise SystemExit(0 if admissible(report) else 1)
    elif args.command == "visual-judge":

        def first_task(folder):
            """The folder's first real task; the start pages differ per task's roster."""
            tasks = sorted(p.name for p in (folder / "tasks").iterdir() if not p.name.startswith("_"))
            return tasks[0] if tasks else None

        from .world.grader_author import _models
        from .world.visual_judge import admissible, format_lines, run_visual_judge

        folder = args.folder.resolve()
        design = load_config(root).get("design", {})
        report = run_visual_judge(
            folder,
            _models(root, folder / "runtime" / "visual_calls", "visual_judge"),
            task_id=args.task or first_task(folder),
            votes=args.votes,
            host_ip=str(design.get("vm_host_ip", "10.0.2.2")),
            work=args.work,
        )
        print("\n".join(format_lines(report)))
        raise SystemExit(0 if admissible(report) else 1)
    elif args.command == "run-company":
        from .world.controller import ControllerError, configured_policies, run_company, step_names

        policies = configured_policies(root)
        names = step_names(policies)
        if args.from_step and args.from_step not in (*names, "episode", "grade"):
            parser.error(f"--from must be one of {', '.join(names)} (or episode/grade for the first policy)")
        if args.backend == "mypcbench" and not args.model_calls:
            # A zero budget is not a quiet no-op: every worker's first step raises "budget
            # exhausted", the teacher scores 0.0, and the run reads exactly like a task the
            # reference solution could not do. Boot the VMs only when there is something to spend.
            parser.error("--model-calls must be given with --backend mypcbench")
        budgets = {
            "seconds": dict.fromkeys((*names, "reset"), args.seconds),
            "model_calls": args.model_calls,
        }
        try:
            result = run_company(
                root,
                args.folder.resolve(),
                args.task_id,
                budgets=budgets,
                backend=args.backend,
                base_image=args.base_image,
                browser_dir=args.browser_dir,
                host_ip=args.host_ip,
                dry_run=args.dry_run,
                from_step=args.from_step,
                reset=args.reset,
                no_teacher=args.no_teacher,
                teacher_model=args.teacher_model,
                policies=policies,
            )
        except ControllerError as exc:
            raise SystemExit(f"run-company failed: {exc}") from exc
        print(json.dumps(result, indent=2, default=str))
    elif args.command == "amplify":
        from .world.amplify import amplify

        print(
            json.dumps(
                amplify(root, args.folder.resolve(), args.task_id, args.count, seed=args.seed),
                indent=2,
                default=str,
            )
        )
    elif args.command == "seed-core":
        from .world.blueprint import seed_core

        result = seed_core(
            root,
            args.folder.resolve(),
            timeout_seconds=args.timeout,
            review_rounds=args.review_rounds,
            resume=args.resume,
        )
        print(json.dumps(result, indent=2))
        if result["status"] != "core_reviewed":
            raise SystemExit(2)
    elif args.command == "populate-world":
        from .world.population import populate

        result = populate(root, args.folder.resolve(), app_ids=args.app)
        print(json.dumps({k: v for k, v in result.items() if k not in {"apps", "checks"}}, indent=2))
        if result["status"] != "populated_for_review":
            raise SystemExit(2)
    elif args.command == "patch-population":
        from .world.population_repair import apply_native_patch

        print(json.dumps(apply_native_patch(root, args.folder, read(args.patch)), indent=2))
    elif args.command == "recheck-population":
        from .world.population_repair import revalidate_population

        result = revalidate_population(root, args.folder)
        print(
            json.dumps(
                {k: result[k] for k in ("status", "failures", "shortfalls", "native_errors")}, indent=2
            )
        )
        if result["status"] != "populated_for_review":
            raise SystemExit(2)
    elif args.command == "review-world":
        from .world.world_acceptance import review_population

        result = review_population(root, args.folder, round_index=args.round)
        print(json.dumps(result["review"], indent=2))
        if result["review"]["verdict"] != "accept":
            raise SystemExit(2)
    elif args.command == "verify-world":
        from .world.runtime_acceptance import verify_runtime

        result = verify_runtime(root, args.folder, work=args.work)
        print(json.dumps({"checks": result["checks"], "seconds": result["seconds"]}, indent=2))
    elif args.command == "freeze-world":
        from .world.world_acceptance import freeze_population

        print(json.dumps(freeze_population(root, args.folder), indent=2))
    elif args.command == "seed-world":
        from .world.state_seed import seed_world

        report = seed_world(
            root, args.folder.resolve(), timeout_seconds=args.timeout, review_rounds=args.review_rounds
        )
        if args.verify:
            from .world.hub_world import serve_company

            design = load_config(root).get("design", {})
            report["verify"] = serve_company(
                args.folder.resolve(),
                args.hub_root or Path(design.get("hub_root", "")),
                args.work or root / "experiments" / "hub-cache",
                host=args.host,
                check_only=True,
                root=root,
            )
            manifest = read(args.folder / "MANIFEST.json")
            manifest["stages"]["stage2_world"] = "seeded_readback_verified"
            write(args.folder / "MANIFEST.json", manifest)
        print(json.dumps(report, indent=2))
    elif args.command == "world-check":
        from .world.world_check import check_folder

        folder = args.folder.resolve()
        result = check_folder(root, folder)
        write(folder / "world" / "CHECKS.json", result)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["ok"] else 2)
    elif args.command == "world-repair":
        from .world.world_repair import repair_world

        report = repair_world(root, args.folder.resolve(), rounds=args.rounds)
        print(json.dumps(report, indent=2))
        if not report["ok"]:
            print(
                f"world-repair: {report['errors']} error(s) remain after {report['rounds']} round(s); "
                "see world/REPAIR.json unrepaired",
                file=sys.stderr,
            )
        raise SystemExit(0 if report["ok"] else 1)
    elif args.command == "review-repair":
        from .storage import now as stamp
        from .world.world_repair import both_ends, repair_review

        folder = args.folder.resolve()
        try:
            report = repair_review(root, folder, rounds=args.rounds)
        except Exception as exc:
            # Without a receipt the driver would attempt this repair on every loop forever.
            report = {
                "repaired_at": stamp(),
                "ok": False,
                "rounds": 0,
                "verdict": None,
                "error": both_ends(f"{type(exc).__name__}: {exc}"),
                "unrepaired": [],
            }
            write(folder / "world" / "REVIEW-REPAIR.json", report)
            print(json.dumps(report, indent=2))
            raise SystemExit(1) from exc
        print(json.dumps(report, indent=2))
        if not report["ok"]:
            print(
                f"review-repair: the world is {report['verdict'] or 'unreviewed'} after "
                f"{report['rounds']} round(s); see world/REVIEW-REPAIR.json unrepaired",
                file=sys.stderr,
            )
        raise SystemExit(0 if report["ok"] else 1)
    elif args.command == "launch-vms":
        from .world.hub_vm import launch_company

        receipt = launch_company(
            args.folder.resolve(),
            args.task_id,
            base_image=args.base_image,
            browser_dir=args.browser_dir,
            host_ip=args.host_ip,
            workers=args.workers or None,
            dry_run=args.dry_run if args.dry_run is not None else args.backend != "mypcbench",
        )
        print(json.dumps(receipt, indent=2, default=str))
    elif args.command == "stop-vms":
        from .world.hub_vm import stop_company

        print(json.dumps(stop_company(args.folder.resolve()), indent=2, default=str))
    elif args.command == "reset-vms":
        from .world.hub_vm import reset_company

        print(json.dumps(reset_company(args.folder.resolve()), indent=2, default=str))
    elif args.command == "run-episode":
        import asyncio

        from .world.harness import Action, Budget, Episode, FakeBackend, WorkerAgent

        if args.backend != "fake":
            raise SystemExit(
                "mypcbench backend adapter is not implemented yet; refusing to fall back to fake"
            )
        folder = args.folder.resolve()
        task_dir = folder / "tasks" / args.task_id
        assignment = read(task_dir / "assignment.json")
        workflow = read(task_dir / "workflow.json")
        roster = list(workflow["worker_ids"])
        boss = workflow.get("manager_id") or roster[0]
        titles = {w["id"]: w.get("title", w["id"]) for w in read(folder / "company.json")["workers"]}
        specialists = [w for w in roster if w != boss]
        delegations = iter(
            [
                Action(
                    "send_message",
                    {"recipient": who, "text": f"Please start on your part: {titles.get(who, who)}"},
                )
                for who in specialists
            ]
            + [Action("done")]
        )
        workers = [
            WorkerAgent(
                boss,
                titles.get(boss, "boss"),
                lambda obs: next(delegations),
                FakeBackend(),
                Budget(len(specialists) + 1, args.seconds),
            )
        ]
        workers += [
            WorkerAgent(
                who, titles.get(who, who), lambda obs: Action("done"), FakeBackend(), start_on_message=True
            )
            for who in specialists
        ]
        result = asyncio.run(
            Episode(
                workers,
                boss_id=boss,
                brief=assignment["brief"],
                runtime=folder / "runtime",
                seconds=args.seconds,
            ).run()
        )
        print(
            json.dumps(
                {"backend": "fake", "simulation_only": True, "boss": boss, "result": result},
                indent=2,
                default=str,
            )
        )
    elif args.command == "review-task":
        from .world.task_author import review_candidate

        result = review_candidate(root, args.folder, args.candidate)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["accepted"] else 1)
    elif args.command == "author-verifier":
        from .world.python_verifier import author_verifier

        result = author_verifier(root, args.folder, args.task_id)
        print(json.dumps(result, indent=2))
    elif args.command == "calibrate-verifier":
        from .world.staged_calibration import calibrate_verifier

        result = calibrate_verifier(
            root, args.folder, args.task_id, args.work, reference_checkpoint=args.reference_checkpoint
        )
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["status"] == "accepted" else 1)
    elif args.command == "trial-task":
        from .world.staged_trials import run_trial

        result = run_trial(
            root,
            args.folder,
            args.task_id,
            kind=args.kind,
            work=args.work,
            attempt=args.attempt,
            unavailable_worker=args.unavailable_worker,
            input_worker=args.input_worker,
        )
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["result"]["class"] == args.kind + "_passed" else 1)
    elif args.command == "prepare-collaborator":
        from .collaborator import build_checkout

        print(
            json.dumps(build_checkout(root, args.folder, args.output, inspection=args.inspection), indent=2)
        )
    elif args.command == "pilot-status":
        from .pilot_status import inspect_pilot

        print(json.dumps(inspect_pilot(root, args.folder, args.output), indent=2))
    elif args.command == "diagnose-trial":
        from .world.trial_diagnostics import diagnose_trial

        print(
            json.dumps(
                diagnose_trial(root, args.folder, args.task_id, kind=args.kind, attempt=args.attempt),
                indent=2,
            )
        )
    elif args.command == "regrade-trial":
        from .world.regrade_trial import regrade_trial

        result = regrade_trial(root, args.folder, args.task_id, kind=args.kind, attempt=args.attempt)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["class"] == args.kind + "_passed" else 1)
    elif args.command == "verify-collaborator":
        from .collaborator import verify_checkout

        print(json.dumps(verify_checkout(root, work=args.work), indent=2))
    elif args.command == "author-golden":
        from .world.golden import author_golden

        print(json.dumps(author_golden(root, args.folder.resolve(), args.task_id), indent=2, default=str))
    elif args.command == "readability":
        from .world.readability import write_readability_report

        report = write_readability_report(args.folder.resolve())
        print(json.dumps({k: v for k, v in report.items() if k != "items"}, indent=2, default=str)[:4000])
    elif args.command == "release":
        from .world.release import bundle_company

        print(json.dumps(bundle_company(root, args.folder.resolve(), args.output), indent=2, default=str))
    elif args.command == "verify-release":
        from .world.release import verify_bundle

        result = verify_bundle(args.archive)
        print(json.dumps(result, indent=2, default=str))
        raise SystemExit(0 if result.get("ok", True) else 2)
    elif args.command == "add-bulk":
        from .world.bulk_layer import add_bulk

        print(json.dumps(add_bulk(root, args.folder.resolve(), again=args.again)))
    elif args.command == "verify-cache-replay":
        from .world.replay_verify import replay_verify

        folder = args.folder.resolve()
        output = args.output or root / "experiments/cache-replay" / folder.name
        report = replay_verify(root, folder, output)
        print(json.dumps(report, indent=2))
        sys.exit(0 if report["identical"] else 3)
    elif args.command == "cohort-report":
        from .world.cohort_report import cohort_report

        output = args.output or args.companies_dir
        print(json.dumps(cohort_report(args.companies_dir, output), indent=2))
    elif args.command == "agreement":
        from .world.agreement import agreement

        report = agreement(args.folder.resolve())
        print(
            json.dumps(
                {
                    "done": report["done"],
                    **{k: v["ok"] for k, v in report["conditions"].items()},
                    "difficulty": report["difficulty"]["label"],
                }
            )
        )
        if not report["done"]:
            sys.exit(3)
    elif args.command == "dedupe-names":
        from .world.names import dedupe_people

        print(json.dumps(dedupe_people(args.folder.resolve())))
    elif args.command == "sync-worker-apps":
        from .world.state_seed import sync_worker_apps

        print(json.dumps(sync_worker_apps(args.folder.resolve())))
    elif args.command == "assign-apps":
        from .world.worker_apps import assign_worker_apps

        print(json.dumps(assign_worker_apps(root, args.folder.resolve(), again=args.again, only=args.only)))
    elif args.command == "rewrite-briefs":
        from .workflows import repair_criteria
        from .world.brief_rewrite import rewrite_briefs

        # Criteria that describe interface state are repaired first, so the plain brief and the
        # grader are authored from criteria about records.
        repaired = repair_criteria(args.folder.resolve())
        result = rewrite_briefs(root, args.folder.resolve(), again=args.again, only=args.only)
        print(json.dumps({**result, "criteria_repaired": repaired} if isinstance(result, dict) else result))
    elif args.command == "repair-criteria":
        from .workflows import repair_criteria

        print(json.dumps(repair_criteria(args.folder.resolve()), indent=2))
    elif args.command in ("calibrate", "grade"):
        from .world import grader as grader_module
        from .world.hub_app import HubClient

        folder = args.folder.resolve()
        endpoints = read(folder / "runtime" / "endpoints.json")["apps"]
        clients = {app_id: HubClient(entry["harness_url"]) for app_id, entry in endpoints.items()}
        if args.command == "calibrate":
            if not (folder / "tasks" / args.task_id / "grader.json").is_file():
                try:
                    grader_module.author_grader(root, folder, args.task_id)
                except ValueError as exc:
                    # The author refused to write checks that meet the contract: a selector naming
                    # a whole collection, or one pointing at interface state rather than records.
                    # That is a real answer about this task and it has to leave a receipt, or the
                    # driver finds no grader, calls this step again, and calls it again forever.
                    # 955 such failures were logged across eight companies before this existed,
                    # 390 of them on one world that had already passed every earlier gate.
                    from .storage import now

                    write(
                        folder / "tasks" / args.task_id / "GRADER_FAILED.json",
                        {"at": now(), "task_id": args.task_id, "reason": str(exc)[:2000]},
                    )
                    print(json.dumps({"graded": False, "reason": str(exc)[:2000]}, indent=2))
                    raise SystemExit(2) from exc
            attempts = []
            for round_index in range(args.repair + 1):
                try:
                    result = grader_module.calibrate(
                        folder,
                        args.task_id,
                        clients,
                        golden=args.golden,
                        models=grader_module._models(
                            root, folder / "tasks" / args.task_id / "calibration_calls", "task_judgment"
                        ),
                    )
                    result["repair_rounds"] = attempts
                    break
                except grader_module.CalibrationError as exc:
                    attempts.append(str(exc)[:2000])
                    if round_index >= args.repair:
                        write(
                            folder / "tasks" / args.task_id / "CALIBRATION_FAILED.json",
                            {"attempts": attempts},
                        )
                        print(json.dumps({"calibrated": False, "attempts": attempts}, indent=2))
                        raise SystemExit(2) from exc
                    if args.golden and "judge must pass the reference" in str(exc):
                        # The judge rejected the golden itself (a referenced record never written, a
                        # deliverable missing): the golden author revises with the judge's reasons.
                        from .world import golden as golden_module

                        feedback = {
                            "round": round_index + 1,
                            "failure": str(exc)[:3500],
                            "earlier": attempts[:-1],
                        }
                        golden_module.author_golden(root, folder, args.task_id, feedback=feedback)
                    else:
                        # The grader author never sees the golden, so the judge's reasons (which
                        # quote what the golden wrote) are stripped before feedback crosses over.
                        feedback = {
                            "round": round_index + 1,
                            "failure": grader_module.grader_safe_feedback(str(exc)),
                            "earlier": [grader_module.grader_safe_feedback(a) for a in attempts[:-1]],
                        }
                        grader_module.author_grader(root, folder, args.task_id, feedback=feedback)
        else:
            models = grader_module._models(root, folder / "runtime", "task_judgment") if args.judge else None
            result = grader_module.grade(folder, args.task_id, clients, models=models)
        print(json.dumps(result, indent=2, default=str))
        raise SystemExit(0 if (result.get("ok", result.get("calibrated", True)) is not False) else 2)
    elif args.command == "author-tasks":
        from .world.task_author import author_tasks

        print(json.dumps(author_tasks(root, args.folder, args.count), indent=2))
    elif args.command == "export-company":
        from .company_layout import export_company, export_run

        if args.company_id:
            output, manifest = export_company(
                root,
                args.run_id,
                args.company_id,
                args.output,
                tasks_required=not args.no_tasks,
                dossier_only=args.dossier_only,
            )
            print(json.dumps({"output": str(output), "tasks": manifest["tasks"]}, indent=2))
        else:
            print(
                json.dumps(
                    [
                        str(p)
                        for p in export_run(
                            root,
                            args.run_id,
                            args.output,
                            tasks_required=not args.no_tasks,
                            dossier_only=args.dossier_only,
                        )
                    ],
                    indent=2,
                )
            )
    elif args.command == "review-sheet":
        from .world.review_sheet import write_review_sheet

        print(write_review_sheet(args.folder.resolve()))
    elif args.command == "cohort-sheet":
        from .world.review_sheet import write_cohort_sheet

        print(write_cohort_sheet(args.companies.resolve(), args.output.resolve()))
    elif args.command == "inspect":
        print(json.dumps(read(root / args.path), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
