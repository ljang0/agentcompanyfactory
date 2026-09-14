"""Objective health reading for an unattended run: run it at the start and the end.

    .venv/bin/python scripts/health.py

Reports driver processes, seeds and model calls in flight, controller runs and VMs without a
live owner, hub app servers (hub-serve, the mock apps' vite servers) without a live driver or
controller run above them, docker containers named for a company and older than a day, lock files
held by dead pids, disk headroom, usage-limit hits in the last hour, and the newest line each
driver wrote to experiments/batch/<driver>/health.jsonl. Exit code 1 when anything needs a hand;
the reasons are printed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / "experiments" / "batch"
DAY = 86400
STALE_HEALTH = 900  # a live driver that has not written a health line in 15 minutes is stuck


def procs(pattern):
    out = subprocess.run(["pgrep", "-af", pattern], capture_output=True, text=True, check=False).stdout
    return [line.split(" ", 1) for line in out.splitlines() if line.strip()]


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def parent(pid):
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().split(")")[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def ancestors(pid, parent=parent):
    """PID's parent chain up to init, nearest first."""
    chain = []
    while pid and int(pid) > 1 and len(chain) < 32:
        pid = parent(int(pid))
        if pid is None:
            break
        chain.append(pid)
    return chain


def cwd(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def app_server(pid, args, root=ROOT, cwd=cwd):
    """A hub app server of ours: hub-serve, or a mock app's vite server run from the warm cache
    (experiments/hub-cache) or a driver's work directory (experiments/batch/<driver>/work)."""
    if "hub-serve" in args:
        return str(root) in args
    if "vite" not in args:
        return False
    where = cwd(pid)
    batch = str(root / "experiments" / "batch") + "/"
    return where.startswith(str(root / "experiments" / "hub-cache")) or (
        where.startswith(batch) and "/work/" in where[len(batch) :]
    )


def orphan_servers(servers, owners, ancestors=ancestors):
    """App servers with no live driver or controller run (OWNERS, pids) among their ancestors."""
    return [(pid, args) for pid, args in servers if not set(ancestors(pid)) & set(owners)]


def containers():
    """(name, created) for every docker container, or nothing when docker is absent or slow."""
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.CreatedAt}}"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [tuple(line.split("\t", 1)) for line in out.splitlines() if "\t" in line]


def stale_containers(rows, companies, now, max_age=DAY):
    """Container names that mention a company id and were created more than MAX_AGE seconds ago."""
    stale = []
    for name, created in rows:
        try:
            when = datetime.strptime(" ".join(created.split()[:3]), "%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            continue
        if now - when.timestamp() > max_age and any(c in name for c in companies):
            stale.append(name)
    return stale


def driver_health(batch=BATCH):
    """(driver, newest complete health line) for every driver that wrote a health.jsonl."""
    rows = []
    for path in sorted(batch.glob("*/health.jsonl")):
        try:
            with open(path, "rb") as handle:
                handle.seek(max(0, handle.seek(0, 2) - 50_000))
                lines = handle.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):  # the last line may be half-written
            try:
                rows.append((path.parent.name, json.loads(line)))
                break
            except ValueError:
                continue
    return rows


def health_age(line, now):
    try:
        return now - datetime.fromisoformat(line["time"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def describe(driver, line, now):
    age = health_age(line, now)
    since = "?" if age is None else f"{age / 60:.0f} min ago"
    parts = [
        f"driver {driver} (pid {line.get('pid')}): {since}",
        f"stages {line.get('stages')}",
        f"outcomes {line.get('outcomes')}",
        f"seeds {line.get('seeds_running')}",
        f"guests {line.get('guests_held')}",
        f"disk {line.get('disk_free_gb')} GB",
        f"limit hits {line.get('usage_limit_hits_last_hour')}",
    ]
    return " | ".join(parts)


def main():
    problems = []
    now = time.time()
    drivers = [
        p
        for p in procs("scripts/batch_companies.py")
        if "batch_companies.py" in p[1] and Path(f"/proc/{p[0]}/comm").read_text().strip() == "python"
    ]
    driver_pids = {int(p[0]) for p in drivers}
    seeds = procs("seed-worl[d]")
    calls = procs("codex.*exe[c]")
    runs = [p for p in procs("company_envs run-company") if "run-company" in p[1]]
    run_pids = {int(p[0]) for p in runs}
    orphan_runs = [
        p for p in runs if parent(p[0]) not in driver_pids and parent(parent(p[0]) or 0) not in driver_pids
    ]
    vms = [p for p in procs("^qemu-system") if str(ROOT / "companies") in p[1]]  # ours, not other users'
    servers = [p for p in procs("hub-serve|vite") if app_server(p[0], p[1])]
    orphan_apps = orphan_servers(servers, driver_pids | run_pids)
    company_ids = sorted(p.name for p in (ROOT / "companies").glob("*") if p.is_dir())
    stale = stale_containers(containers(), company_ids, now)
    controllers_running = 0
    # One checkpoint per task under runtime/tasks/<task>/, plus any company-level one from the
    # single-checkpoint layout that has not been moved.
    checkpoints = [
        *ROOT.glob("companies/*/runtime/tasks/*/CONTROLLER.json"),
        *ROOT.glob("companies/*/runtime/CONTROLLER.json"),
    ]
    for state in checkpoints:
        try:
            controllers_running += json.loads(state.read_text()).get("status") == "running"
        except (OSError, ValueError):
            pass
    dead_locks = []
    for lock in (BATCH / "locks").glob("*"):
        try:
            pid = int(lock.read_text().strip() or 0)
        except (OSError, ValueError):
            pid = 0
        if not alive(pid):
            dead_locks.append(lock.name)
    disk = shutil.disk_usage("/")
    free_gb = disk.free / 1e9
    hour_ago = now - 3600
    limit_hits = 0
    for log in BATCH.glob("*/*.log"):
        try:
            if log.stat().st_mtime < hour_ago:
                continue
            with open(log, "rb") as f:
                f.seek(max(0, f.seek(0, 2) - 200_000))
                limit_hits += f.read().count(b"hit your usage limit")
        except OSError:
            continue
    names = [
        next(
            (
                w
                for w in p[1].split()
                if not w.startswith("-") and "python" not in w and not w.endswith(".py")
            ),
            "?",
        )
        for p in drivers
    ]
    print(f"drivers: {len(drivers)} {names}")
    print(f"seeds: {len(seeds)} | model calls in flight: {len(calls)}")
    print(
        f"controller runs: {len(runs)} (orphans: {len(orphan_runs)}) | controllers marked running: {controllers_running} | VMs: {len(vms)}"
    )
    print(
        f"hub app servers: {len(servers)} (orphans: {len(orphan_apps)}) | stale company containers: {len(stale)}"
    )
    print(
        f"locks held by dead pids: {len(dead_locks)} | disk free: {free_gb:.0f} GB | usage-limit hits in last hour: {limit_hits}"
    )
    for driver, line in driver_health():
        print(describe(driver, line, now))
        age = health_age(line, now)
        if alive(line.get("pid", 0)) and (age is None or age > STALE_HEALTH):
            problems.append(
                f"driver {driver} is alive but wrote no health line for {(age or 0) / 60:.0f} min"
            )
    for pid, args in orphan_apps:
        print(f"  orphan app server {pid}: {args[:120]}")
    if orphan_runs:
        problems.append(f"{len(orphan_runs)} controller run(s) without a driver parent")
    if orphan_apps:
        problems.append(f"{len(orphan_apps)} hub app server(s) without a live driver or controller run")
    if stale:
        problems.append(f"{len(stale)} docker container(s) older than a day named for a company: {stale}")
    if vms and not runs:
        problems.append(f"{len(vms)} VM(s) with no controller run")
    if free_gb < 15:
        problems.append(f"disk below 15 GB ({free_gb:.0f} GB)")
    if limit_hits:
        problems.append(f"{limit_hits} usage-limit hit(s) in the last hour")
    if not drivers:
        problems.append("no driver running")
    for p in problems:
        print("PROBLEM:", p)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
