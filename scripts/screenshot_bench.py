"""Measure 20 captures from an already running worker VM; never boot or stop it.

Run with PYTHONPATH=src .venv/bin/python scripts/screenshot_bench.py VM_RECORD
--save-first experiments/vm-smoke-real/screenshot.png. The record is vm.json
written by launch-vms. Use --method cdp to measure browser page capture.
"""

import argparse
import json
import os
import statistics
import time
from pathlib import Path
from types import SimpleNamespace

from company_envs.storage import read
from company_envs.world.backends.mypcbench import SSHTransport
from company_envs.world.hub_vm import _process_identity
from company_envs.world.screenshot import cdp_screenshot, screenshot


def benchmark(vm, method="guest"):
    """Return timings and the first frame; include connection and PNG decoding time.

    There are no warmup frames. Disk writes are outside the timed region. The
    p95 index matches gym-anything's measure_screenshot_latency.py.
    """
    capture = {"guest": screenshot, "cdp": cdp_screenshot}[method]
    samples = []
    first = None
    for _ in range(20):
        start = time.perf_counter_ns()
        png = capture(vm)
        samples.append((time.perf_counter_ns() - start) / 1_000_000)
        if first is None:
            first = png
    return {
        "method": method,
        "surface": "desktop" if method == "guest" else "browser page",
        "frames": len(samples),
        "warmup": 0,
        "median_ms": statistics.median(samples),
        "p95_ms": sorted(samples)[round(0.95 * (len(samples) - 1))],
        "samples_ms": samples,
    }, first


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vm_record", type=Path)
    parser.add_argument("--method", choices=("guest", "cdp"), default="guest")
    parser.add_argument("--save-first", type=Path)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args(argv)
    path = args.vm_record.resolve()
    record = read(path)
    if record.get("dry_run") or record.get("status") != "running":
        raise ValueError("Screenshot benchmark requires a running VM record")
    if record.get("worker") != path.parent.name:
        raise ValueError("VM record worker does not match its directory")
    identity = record["vm"].get("process")
    if not identity or _process_identity(identity["pid"]) != identity:
        raise ValueError("VM process identity is stale; relaunch the worker")
    vm = SimpleNamespace(
        ssh_base=SSHTransport(record, path.parent).argv,
        env=os.environ | {"SSHPASS": "password123"},
        cdp_port=record["vm"].get("cdp_port"),
    )
    report, first = benchmark(vm, args.method)
    if args.save_first:
        args.save_first.parent.mkdir(parents=True, exist_ok=True)
        args.save_first.write_bytes(first)
        report["screenshot"] = str(args.save_first)
    text = json.dumps(report, indent=2) + "\n"
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
