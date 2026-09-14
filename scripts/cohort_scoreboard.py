"""Gate scoreboard per cohort, read from artifacts only.

For each cohort in experiments/batch/COHORTS.json: how many worlds finished seeding, how many
the reviewers accepted (SEED.json review_verdict, or REVIEW.json's final verdict for older
seeds), how many passed the mechanical check (CHECKS.json), how many companies have every task
fully calibrated, and how many teacher runs passed out of how many ran (counted per task: each
task has its own controller runtime).

    .venv/bin/python scripts/cohort_scoreboard.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def teacher_reports(folder):
    """One TEACHER.json per task that ran: from the per-task runtimes, runtime/tasks/<task>/, and
    from the single-checkpoint layout's runtime/controller/ when it is still in place."""
    layouts = (
        "runtime/tasks/*/controller/company/runtime/teacher/*/TEACHER.json",
        "runtime/controller/company/runtime/teacher/*/TEACHER.json",
    )
    return [load(p) for pattern in layouts for p in sorted(folder.glob(pattern))]


def review_verdict(folder):
    """The reviewers' final verdict: SEED.json's explicit field, else REVIEW.json's last round."""
    seed = load(folder / "world/SEED.json")
    if not seed:
        return None
    if "review_verdict" in seed:
        return seed["review_verdict"]
    rounds = load(folder / "world/REVIEW.json").get("rounds") or []
    return (rounds[-1].get("verdict") or {}).get("verdict") if rounds else None


def world(company):
    folder = ROOT / "companies" / company
    seed = load(folder / "world/SEED.json")
    rounds = load(folder / "world/REVIEW.json").get("rounds") or []
    verdict = review_verdict(folder)
    checks = load(folder / "world/CHECKS.json")
    proofs = [load(p) for p in (folder / "runtime/grades").glob("*/calibration.json")]
    teachers = teacher_reports(folder)
    return {
        "seeded": bool(seed),
        "accepted": verdict == "accept",
        "rounds": len(rounds),
        "checked": checks.get("ok") if checks else None,
        "calibrated": bool(proofs)
        and all(p.get("accepted") and p.get("scope") == "all_checks" for p in proofs),
        "teacher_passed": sum(t.get("class") == "teacher_passed" for t in teachers),
        "teacher_ran": len(teachers),
    }


def main():
    cohorts = load(ROOT / "experiments/batch/COHORTS.json")
    print(
        f"{'cohort':26} {'n':>3} {'seeded':>6} {'accepted':>8} {'checked':>7} {'calibrated':>10} {'teacher':>8}"
    )
    for name, spec in cohorts.items():
        rows = [world(c) for c in spec["companies"]]
        seeded = sum(r["seeded"] for r in rows)
        accepted = sum(r["accepted"] for r in rows)
        checked = sum(r["checked"] is True for r in rows)
        calibrated = sum(r["calibrated"] for r in rows)
        teacher = f"{sum(r['teacher_passed'] for r in rows)}/{sum(r['teacher_ran'] for r in rows)}"
        rate = f" ({accepted / seeded:.0%})" if seeded else ""
        print(
            f"{name:26} {len(rows):3d} {seeded:6d} {accepted:8d}{rate:7} {checked:7d} {calibrated:10d} {teacher:>8}"
        )


if __name__ == "__main__":
    main()
