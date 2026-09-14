"""Judge benchmark from calibration: how often the judge passes golden evidence and refuses
unchanged evidence, across every calibrated task.

Each full calibration writes runtime/grades/<task>/judge_bench.json with one row per judged
check: the majority verdict on the golden packet (expected pass) and a single-vote verdict on
the untouched packet (expected fail or pending). This script aggregates them.

    .venv/bin/python scripts/judge_benchmark.py [companies_dir]
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "companies")
    rows = []
    for path in sorted(root.glob("*/runtime/grades/*/judge_bench.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for row in data.get("rows", []):
            rows.append({**row, "company": path.parts[-5], "task": data.get("task_id")})
    if not rows:
        print("no judge_bench.json files yet; full calibration writes them")
        return
    by_kind = {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row)
    print(f"{len(rows)} judged checks across {len({r['company'] for r in rows})} companies\n")
    print(f"{'kind':10} {'n':>4} {'golden pass':>12} {'initial refused':>16}")
    for kind, group in sorted(by_kind.items()):
        golden = sum(r["golden_verdict"] == "pass" for r in group) / len(group)
        refused = sum(r["initial_verdict"] != "pass" for r in group) / len(group)
        print(f"{kind:10} {len(group):4d} {golden:11.0%} {refused:15.0%}")
    misses = [r for r in rows if r["golden_verdict"] != "pass" or r["initial_verdict"] == "pass"]
    if misses:
        print("\nmisses:")
        for r in misses:
            print(
                f"  {r['company']}/{r['check_id']}: golden={r['golden_verdict']} initial={r['initial_verdict']}"
            )
    print("\nverdict mix on golden:", dict(Counter(r["golden_verdict"] for r in rows)))


if __name__ == "__main__":
    main()
