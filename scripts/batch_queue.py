"""Run selected companies with shared claims, bounded retries and account pauses.

Run: .venv/bin/python scripts/batch_queue.py NAME --runs RUN [RUN ...] --parallel 6
Inspect: .venv/bin/python scripts/batch_queue.py status [--runs RUN [RUN ...]]

experiments/batch/queue/<company>.json holds each company's progress. A file lock protects the claim for the
whole attempt; atomic replacement publishes complete snapshots to other drivers.
A dead driver's claim expires after ten minutes. Live owners keep their lock even
if their heartbeat is late. Commands inherit that lock so an orphan cannot overlap
with a replacement driver. Retries can repeat effects of an interrupted command;
this queue prevents concurrent execution, not all repeated external effects.

"""

# ruff: noqa: SIM115 -- prepare_work is copied unchanged from batch_companies.py

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = [str(ROOT / ".venv" / "bin" / "python"), "-m", "company_envs"]
STEPS = (
    "export",
    "rewrite",
    "apps",
    "seed",
    "names",
    "barrier",
    "bulk",
    "readability",
    "check",
    "calibrate",
    "vm",
    "controller",
    "sheet",
    "agreement",
)
BACKOFF = (120, 600, 1800)
HEARTBEAT = 60
STALE = 600
_local = threading.local()


def now(seconds=None):
    return datetime.fromtimestamp(time.time() if seconds is None else seconds, UTC).isoformat()


def seconds(value):
    return datetime.fromisoformat(value).timestamp()


def state_path(folder):
    """Queue state lives beside the batch logs, never inside the company folder it describes."""
    return folder.parent.parent / "experiments" / "batch" / "queue" / f"{folder.name}.json"


def read_state(folder):
    path = state_path(folder)
    if path.is_file():
        return json.loads(path.read_text())
    return {
        "steps": {
            name: {"status": "pending", "attempts": 0, "started_at": None, "ended_at": None, "note": ""}
            for name in STEPS
        },
        "claim": None,
    }


def save_state(folder, state):
    """Publish the state in one rename while the caller holds the company lock."""
    target = state_path(folder)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".BATCH-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def terminal(row):
    note = row["note"].lower()
    return row["status"] == "failed" and (
        row["attempts"] >= 4 or "review_not_accepted" in note or "agreement failed" in note
    )


def finished(state):
    rows = state["steps"].values()
    return any(terminal(row) for row in rows) or all(row["status"] in ("ok", "skipped") for row in rows)


def ready(row, timestamp):
    if row["status"] in ("ok", "skipped") or terminal(row):
        return False
    if row["status"] == "failed":
        return timestamp >= seconds(row["ended_at"]) + BACKOFF[row["attempts"] - 1]
    return row["status"] == "pending"


class AccountLimit(Exception):
    """No more commands should start until the driver's pause ends."""


def run(cmd, log, timeout):
    # prepare_work uses this same entry point for its cache warmup command.
    return _local.queue.call(cmd, log, timeout)


class BatchQueue:
    def __init__(self, name, work, *, root=ROOT, runner=None, clock=time.time, sleep=time.sleep):
        self.root = Path(root)
        self.dir = self.root / "experiments" / "batch" / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.work = Path(work)
        self.driver = f"{name}:{uuid.uuid4().hex}"
        self.clock, self.sleep = clock, sleep
        self.runner = runner or self.run_command
        self.pause_until = 0
        self.pause_lock = threading.Lock()

    def run_command(self, cmd, log, timeout):
        """Run a CLI call, or start hub-serve when timeout is None."""
        env = dict(os.environ, PYTHONPATH=str(self.root / "src"))
        with open(log, "a") as stream:
            stream.write(f"\n$ {' '.join(map(str, cmd))}  [{now(self.clock())}]\n")
            stream.flush()
            options = {
                "cwd": self.root,
                "stdout": stream,
                "stderr": subprocess.STDOUT,
                "env": env,
                "pass_fds": (_local.claim_fd,),
            }
            if timeout is None:
                return subprocess.Popen(cmd, **options)
            proc = subprocess.Popen(cmd, **options)
            try:
                return proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Interrupt first so the CLI kills its own model calls (they run in their own
                # sessions and would otherwise outlive it and duplicate work); then kill.
                proc.send_signal(signal.SIGINT)
                try:
                    return proc.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    raise

    def paused(self):
        with self.pause_lock:
            return self.clock() < self.pause_until

    def call(self, cmd, log, timeout):
        if self.paused():
            raise AccountLimit("usage limit; driver paused")
        offset = log.stat().st_size if log.exists() else 0
        error = ""
        try:
            return self.runner(cmd, log, timeout)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if timeout is not None and self.account_limited(log, offset, error):
                raise AccountLimit("ModelUnavailable: usage limit; driver paused for ten minutes")

    def account_limited(self, log, offset, error=""):
        with log.open("rb") if log.exists() else open(os.devnull, "rb") as stream:
            stream.seek(offset)
            output = stream.read().decode(errors="replace") + error
        if "modelunavailable" not in output.lower() or "usage limit" not in output.lower():
            return False
        with self.pause_lock:
            self.pause_until = max(self.pause_until, self.clock() + 600)
        return True

    @contextmanager
    def claim(self, company):
        folder = self.root / "companies" / company
        locks = self.root / "experiments" / "batch" / "claims"
        locks.mkdir(parents=True, exist_ok=True)
        with (locks / f"{company}.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield None
                return
            state = read_state(folder)
            previous = state["claim"]
            if previous and self.clock() - seconds(previous["heartbeat"]) <= STALE:
                yield None
                return
            if finished(state):
                yield None
                return
            for row in state["steps"].values():
                if row["status"] == "running":
                    row.update(
                        status="failed",
                        ended_at=previous["heartbeat"] if previous else now(self.clock()),
                        note="driver stopped during this step",
                    )
            state["claim"] = {"driver": self.driver, "pid": os.getpid(), "heartbeat": now(self.clock())}
            mutex = threading.Lock()
            stopped = threading.Event()
            save_state(folder, state)

            def heartbeat():
                while not stopped.wait(HEARTBEAT):
                    with mutex:
                        state["claim"]["heartbeat"] = now(self.clock())
                        save_state(folder, state)

            thread = threading.Thread(target=heartbeat, daemon=True)
            thread.start()
            _local.claim_fd, _local.queue = handle.fileno(), self
            try:
                yield state, mutex
            finally:
                stopped.set()
                thread.join()
                state["claim"] = None
                save_state(folder, state)
                # Closing the descriptor releases the lock only after any orphan
                # command holding an inherited descriptor also exits.
                del _local.claim_fd, _local.queue

    def process(self, run_id, company):
        if self.paused():
            return
        folder = self.root / "companies" / company
        log = self.dir / f"{company}.log"
        with self.claim(company) as claimed:
            if claimed is None:
                return
            state, mutex = claimed
            for step in STEPS:
                row = state["steps"][step]
                if row["status"] in ("ok", "skipped"):
                    continue
                if self.paused() or not ready(row, self.clock()):
                    return
                with mutex:
                    row.update(
                        status="running",
                        attempts=row["attempts"] + 1,
                        started_at=now(self.clock()),
                        ended_at=None,
                        note="",
                    )
                    save_state(folder, state)
                offset = log.stat().st_size if log.exists() else 0
                try:
                    rc = self.step(step, run_id, folder, log)
                    status, note = ("ok", "") if rc == 0 else ("failed", f"rc={rc}")
                    if step == "agreement" and rc != 0:
                        note = f"agreement failed: rc={rc}"
                except AccountLimit as exc:
                    status, note = "pending", str(exc)
                except subprocess.TimeoutExpired:
                    status, note = "failed", "timeout"
                except Exception as exc:  # noqa: BLE001 - keep the failure for the next driver
                    status, note = "failed", f"{type(exc).__name__}: {exc}"
                if self.account_limited(log, offset, note):
                    status, note = "pending", "ModelUnavailable: usage limit; driver paused for ten minutes"
                with mutex:
                    if status == "pending":
                        row["attempts"] -= 1
                    row.update(status=status, ended_at=now(self.clock()), note=note)
                    save_state(folder, state)
                if status != "ok":
                    return

    def review_accepted(self, folder):
        try:
            return json.loads((folder / "world" / "SEED.json").read_text()).get("status") == "seeded_reviewed"
        except (OSError, ValueError):
            return False

    def step(self, step, run_id, folder, log):
        if step == "export":
            if (folder / "MANIFEST.json").is_file():
                return 0
            return self.call(CLI + ["export-company", run_id, folder.name], log, 300)
        if step == "seed":
            seed_file = folder / "world" / "SEED.json"
            if seed_file.is_file():
                return 0
            work = self.prepare_work(folder, log)
            rc = self.call(
                CLI
                + [
                    "seed-world",
                    str(folder),
                    "--timeout",
                    "2400",
                    "--verify",
                    "--host",
                    "127.0.0.1",
                    "--work",
                    str(work),
                    "--review-rounds",
                    "2",
                ],
                log,
                5 * 3600,
            )
            if seed_file.is_file() and rc != 0:
                # The world exists but --verify readback failed: that is a real failure, not a pass.
                try:
                    stage = (
                        json.loads((folder / "MANIFEST.json").read_text())
                        .get("stages", {})
                        .get("stage2_world")
                    )
                except (OSError, ValueError):
                    stage = None
                seeded = (
                    "seeded_reviewed",
                    "seeded_review_failed",
                    "seeded_readback_verified",
                    "seeded_not_verified",
                )
                return 0 if stage is None or stage in seeded else rc
            return 0 if seed_file.is_file() else rc
        if step in ("calibrate", "vm", "controller") and not self.review_accepted(folder):
            return "review_not_accepted"
        if step == "calibrate":
            self.prepare_work(folder, log)
            return self.calibrate(folder, log)
        if step == "vm":
            return self.vm_episodes(folder, log)
        if step == "controller":
            rc = 0
            for task in self.tasks(folder):
                rc |= self.call(
                    CLI + ["run-company", str(folder), task, "--backend", "fake", "--dry-run"], log, 1800
                )
            return rc
        command, timeout = {
            "rewrite": ("rewrite-briefs", 1800),
            "apps": ("assign-apps", 1800),
            "names": ("dedupe-names", 600),
            "barrier": ("sync-worker-apps", 120),
            "bulk": ("add-bulk", 3600),
            "readability": ("readability", 600),
            "check": ("world-check", 600),
            "sheet": ("review-sheet", 300),
            "agreement": ("agreement", 120),
        }[step]
        return self.call(CLI + [command, str(folder)], log, timeout)

    def vm_episodes(self, folder, log):
        """Every task on real worker VMs, companies taking turns on a fixed number of host slots."""
        import fcntl
        import tomllib

        try:
            design = tomllib.loads((self.root / "config.toml").read_text()).get("design", {})
        except (OSError, ValueError):
            design = {}
        settings = {k: design.get(k) for k in ("vm_base_image", "vm_browser_dir", "vm_host_ip")}
        if not all(settings.values()):
            # Not a failure of the company: the agreement's vm_verified condition stays false
            # until a configured driver runs the stage.
            with open(log, "a") as stream:
                stream.write("vm: skipped, no vm_base_image/vm_browser_dir/vm_host_ip in config.toml\n")
            return 0
        slot_dir = self.root / "experiments" / "batch" / "vm-slots"
        slot_dir.mkdir(parents=True, exist_ok=True)
        handle = None
        while handle is None:
            for index in range(int(design.get("vm_slots", 4))):
                candidate = open(slot_dir / f"{index}.lock", "w")
                try:
                    fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    handle = candidate
                    break
                except BlockingIOError:
                    candidate.close()
            if handle is None:
                time.sleep(30)
        try:
            rc = 0
            for task in self.tasks(folder):
                rc |= (
                    self.call(
                        CLI
                        + [
                            "run-company",
                            str(folder),
                            task,
                            "--backend",
                            "mypcbench",
                            "--no-dry-run",
                            "--base-image",
                            str(settings["vm_base_image"]),
                            "--browser-dir",
                            str(settings["vm_browser_dir"]),
                            "--host-ip",
                            str(settings["vm_host_ip"]),
                            "--model-calls",
                            str(int(design.get("vm_model_calls", 60))),
                            "--seconds",
                            "1500",
                        ],
                        log,
                        3 * 3600,
                    )
                    or 0
                )
            return rc
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    def tasks(self, folder):
        return sorted(p.name for p in (folder / "tasks").iterdir() if (p / "workflow.json").is_file())

    def calibrate(self, folder, log):
        """Serve the world until every task has been authored and calibrated."""
        started_at = self.clock()
        serve = self.call(
            CLI
            + [
                "hub-serve",
                str(folder),
                "--host",
                "127.0.0.1",
                "--work",
                str(self.dir / "work" / folder.name),
            ],
            log,
            None,
        )
        try:
            deadline = self.clock() + 900
            endpoints = folder / "runtime" / "endpoints.json"
            while not endpoints.is_file() or endpoints.stat().st_mtime < started_at:
                if serve.poll() is not None or self.clock() > deadline:
                    return "serve_failed"
                if self.paused():
                    raise AccountLimit("usage limit; driver paused")
                self.sleep(2)
            self.sleep(3)
            rc = 0
            for task in self.tasks(folder):
                if not (folder / "tasks" / task / "golden.json").is_file():
                    rc |= self.call(CLI + ["author-golden", str(folder), task], log, 3600)
                rc |= self.call(CLI + ["calibrate", str(folder), task, "--golden"], log, 3600)
            return rc
        finally:
            serve.terminate()
            try:
                serve.wait(30)
            except subprocess.TimeoutExpired:
                serve.kill()
                serve.wait()

    def prepare_work(self, folder, log):
        """Per-company app build directory, hardlinked from the shared warm cache.

        Concurrent companies must not share one built app directory: HubProcess writes
        .mock-states inside it and removes it on stop. Hardlink copies cost no disk and keep
        builds isolated. The warm cache gets node_modules installed once per app under a lock.
        """
        work = self.dir / "work" / folder.name
        work.mkdir(parents=True, exist_ok=True)
        apps = json.loads((folder / "apps.json").read_text())["apps"]
        for app in apps:
            app_id = app["app_id"]
            source = self.work / app_id
            target = work / app_id
            if target.exists():
                continue
            if not (source / "dist" / "index.html").is_file():
                run(
                    CLI
                    + [
                        "hub-smoke",
                        app_id,
                        "--state",
                        str(ROOT / "experiments/hub-smoke/states" / f"{app_id}.json"),
                        "--work",
                        str(self.work),
                    ],
                    log,
                    1200,
                )
            lock = self.work / f".{app_id}.install.lock"
            with open(lock, "w") as handle:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX)
                if not (source / "node_modules" / "vite" / "bin" / "vite.js").is_file():
                    install = (
                        ["npm", "ci"] if (source / "package-lock.json").is_file() else ["npm", "install"]
                    )
                    subprocess.run(
                        [*install, "--ignore-scripts", "--no-audit", "--no-fund"],
                        cwd=source,
                        stdout=open(log, "a"),
                        stderr=subprocess.STDOUT,
                        timeout=900,
                        check=False,
                    )
                fcntl.flock(handle, fcntl.LOCK_UN)
            subprocess.run(["cp", "-al", str(source), str(target)], check=True)
            # A hardlink inherits the warm cache's serving marker, and HubProcess then refuses this
            # private copy as already served by the process holding the cache.
            for stale in (".mock-states", ".mock-files", ".hub-serving"):
                subprocess.run(["rm", "-rf", str(target / stale)], check=False)
        return work

    def scope(self, runs):
        companies = {}
        for run_id in runs:
            dataset = self.root / "runs" / run_id / "dataset.json"
            try:
                selected = json.loads(dataset.read_text())["companies"]
            except (FileNotFoundError, ValueError, KeyError):
                continue
            for company in sorted(selected):
                companies.setdefault(company, run_id)
        return companies

    def drive(self, runs, parallel=6, once=False):
        """Watch the runs, taking only as much work as this driver can execute."""
        if parallel < 1:
            raise ValueError("parallel must be at least one")
        scope = self.scope(runs)
        attempted = set()
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {}
            while True:
                for company, future in list(futures.items()):
                    if future.done():
                        future.result()
                        del futures[company]
                if not once:
                    scope = self.scope(runs)
                if not self.paused():
                    for company, run_id in scope.items():
                        if len(futures) >= parallel:
                            break
                        if company in futures or (once and company in attempted):
                            continue
                        state = read_state(self.root / "companies" / company)
                        if finished(state):
                            attempted.add(company)
                            continue
                        row = next(
                            state["steps"][s]
                            for s in STEPS
                            if state["steps"][s]["status"] not in ("ok", "skipped")
                        )
                        if not ready(row, self.clock()) and row["status"] != "running":
                            attempted.add(company)
                            continue
                        attempted.add(company)
                        futures[company] = pool.submit(self.process, run_id, company)
                if not futures:
                    if once and (self.paused() or attempted.issuperset(scope)):
                        return
                    if scope and all(finished(read_state(self.root / "companies" / c)) for c in scope):
                        return
                    if not scope and self.runs_finished(runs):
                        return
                self.sleep(1)

    def runs_finished(self, runs):
        for run_id in runs:
            try:
                state = json.loads((self.root / "runs" / run_id / "run.json").read_text())
            except (FileNotFoundError, ValueError):
                return False
            if state.get("status") in (None, "running", "ready"):
                return False
        return True

    def status(self, runs=()):
        companies = (
            self.scope(runs)
            if runs
            else {p.stem: "" for p in (self.root / "experiments" / "batch" / "queue").glob("*.json")}
        )
        for company in sorted(companies):
            state = read_state(self.root / "companies" / company)
            started = [
                (s, state["steps"][s])
                for s in STEPS
                if state["steps"][s]["status"] != "pending" or state["steps"][s]["note"]
            ]
            step, row = started[-1] if started else (STEPS[0], state["steps"][STEPS[0]])
            outcome = "terminal" if any(terminal(r) for r in state["steps"].values()) else row["status"]
            note = " ".join(row["note"].split())
            print(
                f"{company}: {step} {outcome} (attempts={row['attempts']})" + (f" — {note}" if note else "")
            )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="driver name, or status to inspect the queue")
    parser.add_argument("--runs", nargs="+")
    parser.add_argument("--parallel", type=int, default=6)
    parser.add_argument("--work", type=Path, default=ROOT / "experiments" / "hub-cache")
    parser.add_argument(
        "--once", action="store_true", help="try each currently selected company once, then exit"
    )
    args = parser.parse_args(argv)
    if args.parallel < 1:
        parser.error("--parallel must be at least one")
    if args.name != "status" and not args.runs:
        parser.error("--runs is required when running the queue")
    queue = BatchQueue(args.name, args.work.resolve())
    if args.name == "status":
        queue.status(args.runs or ())
    else:
        queue.drive(args.runs, args.parallel, args.once)


if __name__ == "__main__":
    sys.exit(main())
