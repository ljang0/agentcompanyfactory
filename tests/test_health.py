"""The health check's pure parts: orphan app servers, stale containers, drivers' health lines."""

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "health.py"
spec = importlib.util.spec_from_file_location("health", SCRIPT)
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


def test_app_servers_are_ours_by_command_or_working_directory(tmp_path):
    root = tmp_path / "repo"
    cache, work = (
        root / "experiments/hub-cache/gmail_mock",
        root / "experiments/batch/fleet-a/work/acme/slack_mock",
    )
    cwds = {
        "10": str(cache),
        "11": str(work),
        "12": str(tmp_path / "elsewhere"),
        "13": str(root / "experiments/batch/x"),
    }
    where = cwds.get
    vite = "node node_modules/vite/bin/vite.js preview --host 127.0.0.1 --port 41755"
    assert health.app_server("10", vite, root, where)
    assert health.app_server("11", vite, root, where)
    assert not health.app_server("12", vite, root, where)  # somebody else's vite
    assert not health.app_server("13", vite, root, where)  # under batch/ but not a work directory
    serve = f"{root}/.venv/bin/python -m company_envs hub-serve {root}/companies/acme --host 127.0.0.1"
    assert health.app_server("14", serve, root, where)
    assert not health.app_server("15", "python -m other hub-serve /elsewhere", root, where)
    assert not health.app_server("16", "esbuild --service=0.21.5 --ping", root, where)


def test_orphan_servers_have_no_owner_among_their_ancestors():
    tree = {500: 400, 400: 300, 300: 1, 600: 1, 700: 650, 650: 1}  # pid -> parent
    chain = lambda pid: health.ancestors(pid, tree.get)
    assert chain(500) == [400, 300, 1] and chain(600) == [1]
    servers = [("500", "vite"), ("600", "hub-serve"), ("700", "vite")]
    orphans = health.orphan_servers(servers, owners={300}, ancestors=lambda pid: chain(int(pid)))
    assert [pid for pid, _ in orphans] == ["600", "700"]  # 500 descends from the driver 300
    orphans = health.orphan_servers(servers, owners={300, 650}, ancestors=lambda pid: chain(int(pid)))
    assert [pid for pid, _ in orphans] == ["600"]  # 650 is a controller run owning 700


def test_stale_containers_are_old_and_named_for_a_company():
    now = 1_800_000_000  # 2027-01-15T08:00:00Z
    rows = [
        ("mypcbench-acme-qemu", "2027-01-13 09:00:03 -0400 EDT"),  # two days old, ours
        ("mypcbench-acme-2", "2027-01-15 01:00:03 -0400 EDT"),  # hours old
        ("programbench-91d9653bee81", "2027-01-01 09:00:03 -0400 EDT"),  # old, not a company
        ("mypcbench-globex-qemu", "yesterday"),  # unparseable date: skipped
    ]
    assert health.stale_containers(rows, ["acme", "globex"], now) == ["mypcbench-acme-qemu"]
    assert health.stale_containers(rows, [], now) == []


def test_driver_health_reads_the_newest_line_of_each_driver(tmp_path):
    lines = [{"time": "2027-01-15T07:00:00+00:00", "pid": 1, "stages": {"seed": 2}}, {"time": "2027-01-15T07:59:00+00:00", "pid": 1, "stages": {"seed": 1, "done": 1}}]  # fmt: skip
    path = tmp_path / "fleet-a" / "health.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines) + "{trunc")  # a half-written tail
    (tmp_path / "fleet-b").mkdir()
    (tmp_path / "fleet-b" / "health.jsonl").write_text("")
    (tmp_path / "fleet-c").mkdir()
    (tmp_path / "fleet-c" / "health.jsonl").write_text(json.dumps(lines[0]) + "\n")
    rows = health.driver_health(tmp_path)
    assert [name for name, _ in rows] == ["fleet-a", "fleet-c"]
    assert rows[0][1]["stages"] == {"seed": 1, "done": 1}  # the newest complete line, the tail ignored
    now = 1_800_000_000  # 08:00 UTC that day
    assert health.health_age(rows[0][1], now) == 60
    assert health.health_age(rows[1][1], now) == 3600
    assert health.health_age({"time": "junk"}, now) is None
    text = health.describe("fleet-a", rows[0][1], now)
    assert text.startswith("driver fleet-a (pid 1): 1 min ago | stages {'seed': 1, 'done': 1}")
