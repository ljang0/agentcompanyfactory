"""Summarize recorded company evidence without rerunning any gate."""

import html
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

from company_envs.storage import digest, read, write
from company_envs.workflows import DIFFICULTY_MIX, difficulty_distribution

from .agreement import CONDITIONS
from .review_sheet import company_stage


def _read(path):
    try:
        return read(path)
    except FileNotFoundError:
        return {}


def _rate(accepted, total):
    return {"accepted": accepted, "total": total, "pass_rate": accepted / total if total else None}


def _records(value):
    """Count collection records, leaving profile fields and UI selections out."""
    if isinstance(value, list):
        return sum(isinstance(row, dict) for row in value)
    if isinstance(value, dict):
        if all(isinstance(row, list) for row in value.values()):
            return sum(_records(rows) for rows in value.values())
        if all(isinstance(row, dict) for row in value.values()):
            return len(value)
    return 0


def _pattern(round_):
    receipt = round_.get("receipt", {})
    ballots = receipt.get("ballots", [])
    if ballots:
        counts = Counter(ballot["verdict"] for ballot in ballots)
        return ", ".join(f"{counts[v]} {v}" for v in sorted(counts))
    if "votes" in receipt:
        return f"{receipt.get('accepts', 0)} accept / {receipt['votes']} votes"
    # Older reviews recorded just one model receipt and a verdict.
    return f"1 {round_['verdict']['verdict']}"


def teacher_difficulty(folders):
    """Compare labels with the latest teacher result for each task, excluding failed infrastructure."""
    designs = []
    groups = {level: [] for level in (*DIFFICULTY_MIX, "unlabeled")}
    for folder in folders:
        # Standard company exports point back to a run. Its labels leave reviewed
        # source files untouched, and apply only to the exact exported design.
        source_run = _read(folder / "MANIFEST.json").get("source_run")
        entries = {}
        if source_run and Path(source_run).name == source_run and source_run not in {".", ".."}:
            dataset = _read(folder.parent.parent / "runs" / source_run / "dataset.json")
            entries = {
                e["workflow_id"]: e for e in dataset.get("workflows", []) if e.get("verdict") == "accept"
            }
        for path in sorted(folder.glob("tasks/*/workflow.json")):
            if path.parent.name.startswith("_"):
                continue
            design = read(path)
            entry = entries.get(path.parent.name, {})
            if not design.get("difficulty") and entry.get("difficulty_input_hash") == digest(design):
                design = {**design, "difficulty": entry.get("difficulty")}
            level = design.get("difficulty") or "unlabeled"
            if level not in groups:
                raise ValueError(f"{path}: difficulty must be easy, medium or hard")
            designs.append(design)
            groups[level].append(_read(folder / "runtime/teacher" / path.parent.name / "TEACHER.json"))
    rows = {}
    for level, results in groups.items():
        classes = Counter(r.get("class") or "missing" for r in results)
        scores = []
        invalid_scores = 0
        for result in results:
            if result.get("class") not in {"teacher_passed", "teacher_failed"}:
                continue
            score = result.get("score")
            if type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1:
                scores.append(score)
            else:
                invalid_scores += 1
        evaluated = classes["teacher_passed"] + classes["teacher_failed"]
        rows[level] = {
            "tasks": len(results),
            "classes": dict(sorted(classes.items())),
            "evaluated": evaluated,
            "passed": classes["teacher_passed"],
            "pass_rate": classes["teacher_passed"] / evaluated if evaluated else None,
            "mean_score": sum(scores) / len(scores) if scores else None,
            "scored": len(scores),
            "invalid_scores": invalid_scores,
        }
    return {"distribution": difficulty_distribution(designs), "teacher_by_difficulty": rows}


def cohort_report(companies_dir, out_dir):
    """Write COHORT.json and COHORT.md, with missing evidence counted separately."""
    companies_dir, out_dir = Path(companies_dir), Path(out_dir)
    folders = [
        p
        for p in sorted(companies_dir.iterdir())
        if p.is_dir() and not p.name.startswith(".") and p.resolve() != out_dir.resolve()
    ]
    snapshots = [(p, _read(p / "AGREEMENT.json")) for p in folders]
    names = list(CONDITIONS) + sorted(
        {name for _, agreement in snapshots for name in agreement.get("conditions", {})} - set(CONDITIONS)
    )
    conditions = {name: {"passed": 0, "failed": 0, "missing": 0} for name in names}
    rounds, patterns, errors = defaultdict(list), defaultdict(list), defaultdict(list)
    records, companies, not_done, anomalies = {}, [], [], []
    calibration, stages = Counter(), Counter()
    for folder, agreement in snapshots:
        first = None
        for name in names:
            ok = agreement.get("conditions", {}).get(name, {}).get("ok")
            conditions[name]["passed" if ok is True else "failed" if ok is False else "missing"] += 1
            if ok is not True and first is None:
                first = name
        done = agreement.get("done") is True and first is None
        stage = company_stage(folder)
        stages[stage["stage"]] += 1
        if not done:
            # "first_failing_condition" was "seeded" for 97 of the 98 companies without an
            # agreement, which made a dead company, a blocked one and one queued for tonight the
            # same row. The stage says which, and names the marker it read.
            not_done.append(
                {
                    "company": folder.name,
                    "first_failing_condition": first or "done",
                    "stage": stage["stage"],
                    "detail": stage["detail"],
                    "repair_recorded": stage["repair_recorded"],
                    "waiting_on": stage["waiting_on"],
                }
            )
        seed = _read(folder / "world/SEED.json")
        checks = _read(folder / "world/CHECKS.json")
        review = _read(folder / "world/REVIEW.json")
        bulk = _read(folder / "world/BULK.json")
        readability = _read(folder / "REPORT-readability.json")
        for round_ in review.get("rounds", []):
            accepted = round_["verdict"]["verdict"] == "accept"
            rounds[round_["round"]].append(accepted)
            patterns[_pattern(round_)].append(accepted)
        for finding in checks.get("findings", []):
            if finding.get("severity") != "error":
                continue
            message = finding["message"]
            clause = re.split(r":|;|[.!?](?:\s|$)|\n", message, maxsplit=1)[0].strip()
            errors[clause].append({"company": folder.name, **finding})
        app_records = {}
        for state_path in sorted(folder.glob("world/*.state.json")):
            app = state_path.name.removesuffix(".state.json")
            total = sum(_records(value) for value in read(state_path).values())
            added = sum(bulk.get("apps", {}).get(app, {}).get("added", {}).values())
            counts = {"human": total - added, "bulk": added, "total": total}
            if added > total:
                # One company's arithmetic stopped the whole cohort: strive-health's BULK.json
                # claims 2,104 gmail records added to a state holding 409, and 1,107 drive records
                # to 310, so the 99-company report raised instead of reporting anything at all.
                # A report whose job is to count missing evidence has to count this too.
                anomalies.append(
                    {
                        "company": folder.name,
                        "app": app,
                        "bulk_added": added,
                        "records_in_state": total,
                        "note": "BULK.json claims more added records than the state holds",
                    }
                )
                counts = {"human": 0, "bulk": total, "total": total}
            app_records[app] = counts
            totals = records.setdefault(app, {"human": 0, "bulk": 0, "total": 0})
            for key, count in counts.items():
                totals[key] += count
        proofs = {
            path.parent.name: read(path) for path in sorted(folder.glob("runtime/grades/*/calibration.json"))
        }
        task_ids = set(proofs) | {
            path.parent.name
            for path in folder.glob("tasks/*/workflow.json")
            if not path.parent.name.startswith("_")
        }
        task_calibration = {}
        for task in sorted(task_ids):
            accepted = proofs.get(task, {}).get("accepted")
            status = "accepted" if accepted is True else "failed" if accepted is False else "missing"
            task_calibration[task] = status
            calibration[status] += 1
        markers = {
            name: _read(folder / "tasks" / directory / filename)
            for name, directory, filename in (
                ("plain_brief", "_plain_brief", "REWRITE.json"),
                ("worker_apps", "_worker_apps", "ASSIGN.json"),
            )
        }
        companies.append(
            {
                "company": folder.name,
                "done": done,
                "stage": stage,
                "seed_status": seed.get("status"),
                "checks": {key: checks.get(key) for key in ("ok", "errors", "warnings")},
                "review": review.get("final_verdict"),
                "readability": {
                    key: value.get("verdict") if isinstance(value, dict) else value
                    for key, value in sorted(readability.get("categories", {}).items())
                },
                "markers": {key: bool(value) for key, value in markers.items()},
                "records_per_app": app_records,
                "calibration": task_calibration,
            }
        )
    report = {
        "difficulty": teacher_difficulty(folders),
        "company_count": len(companies),
        "done": sum(c["done"] for c in companies),
        "stages": dict(sorted(stages.items())),
        "record_anomalies": anomalies,
        "conditions": conditions,
        "review_by_round": [{"round": r, **_rate(sum(v), len(v))} for r, v in sorted(rounds.items())],
        "review_by_vote_pattern": [
            {"pattern": p, **_rate(sum(v), len(v))} for p, v in sorted(patterns.items())
        ],
        "mechanical_error_classes": [
            {
                "message": message,
                "count": len(examples),
                "examples": sorted(
                    examples,
                    key=lambda f: (f["company"], f.get("source", ""), f.get("path", ""), f["message"]),
                )[:3],
            }
            for message, examples in sorted(errors.items())
        ],
        "records_per_app": dict(sorted(records.items())),
        "calibration": {
            **_rate(calibration["accepted"], sum(calibration.values())),
            "failed": calibration["failed"],
            "missing": calibration["missing"],
        },
        "not_done": not_done,
        "companies": companies,
    }
    write(out_dir / "COHORT.json", report)
    (out_dir / "COHORT.md").write_text(_markdown(report), encoding="utf-8")
    return report


def _markdown(report):
    lines = []

    def table(title, headers, rows):
        lines.extend([f"## {title}", ""])
        for row in [headers, ["---"] * len(headers), *rows]:
            lines.append(
                "| "
                + " | ".join(
                    html.escape(" ".join(str(value).split())).replace("|", "&#124;") for value in row
                )
                + " |"
            )
        lines.append("")

    def rate(value):
        return f"{value:.1%}" if value is not None else "not yet"

    table(
        "Companies",
        ["Total", "Done", "Not done"],
        [[report["company_count"], report["done"], len(report["not_done"])]],
    )
    table(
        "Position on disk",
        ["Stage", "Companies"],
        [[stage, count] for stage, count in report["stages"].items()],
    )
    if report["record_anomalies"]:
        table(
            "Record-count anomalies",
            ["Company", "App", "Bulk added", "Records in state", "Note"],
            [
                [a["company"], a["app"], a["bulk_added"], a["records_in_state"], a["note"]]
                for a in report["record_anomalies"]
            ],
        )
    table(
        "Agreement conditions",
        ["Condition", "Passed", "Failed", "Missing"],
        [[name, c["passed"], c["failed"], c["missing"]] for name, c in report["conditions"].items()],
    )
    for title, field, label in (
        ("Review by round", "review_by_round", "round"),
        ("Review by vote pattern", "review_by_vote_pattern", "pattern"),
    ):
        table(
            title,
            [label.capitalize(), "Accepted", "Reviews", "Acceptance rate"],
            [[r[label], r["accepted"], r["total"], rate(r["pass_rate"])] for r in report[field]],
        )
    table(
        "Mechanical errors",
        ["First clause", "Count", "Examples (up to three)"],
        [
            [
                c["message"],
                c["count"],
                "; ".join(
                    f"{e['company']}/{e.get('source', '')}:{e.get('path', '')}: {e['message']}"
                    for e in c["examples"]
                ),
            ]
            for c in report["mechanical_error_classes"]
        ],
    )
    table(
        "Records per app",
        ["App", "Human layer", "Bulk", "Total"],
        [[app, c["human"], c["bulk"], c["total"]] for app, c in report["records_per_app"].items()],
    )
    c = report["calibration"]
    distribution = report["difficulty"]["distribution"]
    table(
        "Difficulty distribution",
        ["Difficulty", "Tasks", "Share of labeled tasks", "Target", "Difference (percentage points)"],
        [
            [
                level,
                count,
                rate(distribution["shares"].get(level)),
                rate(distribution["target"].get(level)),
                f"{distribution['difference_percentage_points'][level]:+.1f}"
                if distribution["difference_percentage_points"].get(level) is not None
                else "not yet",
            ]
            for level, count in distribution["counts"].items()
        ],
    )
    lines += [
        "Teacher pass rates use teacher_passed and teacher_failed only. Environment and grader errors",
        "and missing results are listed separately. Scores average valid completed teacher results.",
        "",
    ]
    table(
        "Teacher results by difficulty",
        [
            "Difficulty",
            "Tasks",
            "Evaluated",
            "Passed",
            "Pass rate",
            "Mean score",
            "Invalid scores",
            "Classes",
        ],
        [
            [
                level,
                row["tasks"],
                row["evaluated"],
                row["passed"],
                rate(row["pass_rate"]),
                f"{row['mean_score']:.3f}" if row["mean_score"] is not None else "not yet",
                row["invalid_scores"],
                ", ".join(f"{k}: {v}" for k, v in row["classes"].items()) or "none",
            ]
            for level, row in report["difficulty"]["teacher_by_difficulty"].items()
        ],
    )
    table(
        "Recorded calibration",
        ["Accepted", "Failed", "Missing", "Tasks", "Accepted / tasks"],
        [[c["accepted"], c["failed"], c["missing"], c["total"], rate(c["pass_rate"])]],
    )
    table(
        "Company evidence",
        ["Company", "Seed", "Readability", "Brief rewritten", "Apps assigned"],
        [
            [
                c["company"],
                c["seed_status"] or "not yet",
                "; ".join(f"{k}: {v}" for k, v in c["readability"].items()) or "not yet",
                "yes" if c["markers"]["plain_brief"] else "not yet",
                "yes" if c["markers"]["worker_apps"] else "not yet",
            ]
            for c in report["companies"]
        ],
    )
    table(
        "Companies not done",
        ["Company", "First failing condition", "Position", "Detail", "Repair recorded", "Waiting on"],
        [
            [
                c["company"],
                c["first_failing_condition"],
                c.get("stage", ""),
                c.get("detail", ""),
                c.get("repair_recorded", ""),
                c.get("waiting_on") or "",
            ]
            for c in report["not_done"]
        ],
    )
    return "\n".join(lines)
