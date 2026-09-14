"""Compact human review sheets from recorded company artifacts, with no live checks.

Only the requested Markdown output is written. Missing artifacts say ``not yet``;
invalid JSON remains an error. Recorded calibration is not revalidated for drift.
Briefs are never shortened: inputs too large for 119 lines raise ValueError before
writing, rather than silently dropping task text or reviewer evidence.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from company_envs.storage import decided_by

from .controller import task_runtimes

_MISSING = "not yet"
_QUESTIONS = (
    "Is this a real job?",
    "Could a new hire understand the brief?",
    "Does the data look like a real desk?",
    "Is anything a giveaway?",
    "Would three people really be needed?",
)


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _text(value) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return _MISSING
    if isinstance(value, dict):
        return "; ".join(f"{key}: {_text(item)}" for key, item in value.items())
    if isinstance(value, list):
        return ", ".join(_text(item) for item in value)
    return " ".join(str(value).split())


def _cell(value) -> str:
    return (
        _text(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("`", "\\`")
    )


def _row(*values) -> str:
    return "| " + " | ".join(_cell(value) for value in values) + " |"


def _checks(folder: Path, seed: dict) -> str:
    for relative in ("world/CHECKS.json", "runtime/CHECKS.json"):
        report = _read(folder / relative)
        if report:
            break
    else:
        report = seed.get("mechanical_checks") or {}
        relative = "world/SEED.json"
    if not report:
        return _MISSING
    return f"{_text(report.get('errors'))} errors, {_text(report.get('warnings'))} warnings ({relative})"


def _readability(report: dict) -> str:
    categories = report.get("categories", {})
    if not categories:
        return _MISSING
    return "; ".join(
        f"{name}: {_text(category.get('verdict'))} "
        f"({_text(category.get('item_count'))} items, "
        f"{_text(category.get('flagged_count'))} flagged)"
        for name, category in categories.items()
    )


def _calibration(folder: Path, task_id: str) -> tuple[str, bool]:
    proof = _read(folder / "runtime/grades" / task_id / "calibration.json")
    accepted = proof.get("accepted")
    status = "accepted" if accepted is True else "rejected" if accepted is False else _MISSING
    if proof:
        status += f" (scope: {_text(proof.get('scope'))})"
    return status, accepted is True


def _snapshot(folder: Path) -> dict:
    company = _read(folder / "company.json")
    seed = _read(folder / "world/SEED.json")
    review = _read(folder / "world/REVIEW.json")
    rounds = review.get("rounds") or []
    latest = rounds[-1].get("verdict", {}) if rounds else review
    app_ids = {a["app_id"] for a in _read(folder / "apps.json").get("apps", [])}
    states = {}
    for path in sorted(folder.glob("world/*.state.json")):
        app_id = path.name.removesuffix(".state.json")
        app_ids.add(app_id)
        states[app_id] = _read(path)
    tasks = []
    for path in sorted((folder / "tasks").glob("*")):
        if (
            path.is_dir()
            and not path.name.startswith("_")
            and any((path / f).is_file() for f in ("workflow.json", "assignment.json"))
        ):
            status, accepted = _calibration(folder, path.name)
            tasks.append(
                {
                    "id": path.name,
                    "assignment": _read(path / "assignment.json"),
                    "workflow": _read(path / "workflow.json"),
                    "calibration": status,
                    "accepted": accepted,
                }
            )
    return {
        "company": company,
        "name": company.get("name") or folder.name,
        "apps": sorted(app_ids),
        "states": states,
        "tasks": tasks,
        "seed": seed.get("status"),
        "review": review.get("final_verdict") or latest.get("verdict") or seed.get("review"),
        "findings": latest.get("findings", []),
        "checks": _checks(folder, seed),
        "readability": _read(folder / "REPORT-readability.json"),
        "stage": company_stage(folder),
    }


# The driver's step markers in pipeline order, so the first missing one is what a company is
# waiting for. A failure marker is not progress and is read separately.
_PROGRESS_MARKERS = (
    "tasks/_plain_brief/REWRITE.json",
    "tasks/_worker_apps/ASSIGN.json",
    "world/SEED.json",
    "world/NAMES.json",
    "world/BULK.json",
    "REPORT-readability.json",
    "world/CHECKS.json",
    "runtime/RENDER.json",
    "runtime/VISUAL.json",
    "REVIEW-SHEET.md",
    "AGREEMENT.json",
)


def company_stage(folder: Path) -> dict:
    """Where a company actually stands, distinguishing dead from blocked from not yet started.

    Every report rendered the fleet through the agreement file, and 97 of the 98 companies without
    one came out as the single word "seeded": a company three seed attempts dead, a company whose
    one review repair is spent, and a company queued for tonight were the same row. The driver has
    the discriminators on disk -- SEED_FAILED.json, REVIEW-REPAIR.json, the step markers -- and no
    report read any of them. This names the marker it read so the claim is checkable.
    """
    folder = Path(folder)
    agreement = _read(folder / "AGREEMENT.json")
    seed = _read(folder / "world/SEED.json")
    review = _read(folder / "world/REVIEW.json")
    failed = _read(folder / "world/SEED_FAILED.json")
    repair = (folder / "world/REVIEW-REPAIR.json").is_file()
    if agreement.get("done") is True:
        stage, detail = "done", "AGREEMENT.json says done"
    elif (folder / "world/SEED_FAILED.json").is_file():
        stage = "seed_failed"
        detail = _text(failed.get("reason") or failed.get("status") or "world/SEED_FAILED.json")
    elif seed.get("status") == "seeded_reviewed" or (review.get("final_verdict") == "accept"):
        stage = "in_flight"
        detail = f"seeded and reviewed; agreement {'failing' if agreement else 'not written'}"
    elif seed:
        stage = "review_not_accepted"
        detail = f"review: {_text(review.get('final_verdict') or seed.get('review'))}"
    elif (folder / "tasks/_worker_apps/ASSIGN.json").is_file():
        stage, detail = "awaiting_seed", "apps assigned, no world yet"
    else:
        stage, detail = "awaiting_apps", "no ASSIGN.json yet"
    waiting = next((path for path in _PROGRESS_MARKERS if not (folder / path).is_file()), None)
    return {
        "stage": stage,
        "detail": detail,
        "repair_recorded": repair or bool(failed.get("attempts")),
        "waiting_on": waiting,
        # A world moved aside is not a world: cort and cresa both keep one under another name,
        # and a report that counted the directory would call them seeded.
        "world_on_disk": (folder / "world/world.json").is_file(),
        "worlds_set_aside": sorted(p.name for p in folder.glob("world.*") if p.is_dir()),
    }


def _position(stage: dict) -> str:
    """One cell: the stage, plus the fact that distinguishes dead from waiting."""
    text = stage["stage"]
    if stage["repair_recorded"] and stage["stage"] in ("seed_failed", "review_not_accepted"):
        text += " (repair recorded)"
    if stage["worlds_set_aside"] and not stage["world_on_disk"]:
        text += f" (world set aside: {', '.join(stage['worlds_set_aside'])})"
    return text


def _collections(state: dict) -> str:
    # Lists count their entries. Object maps count records, not profile/UI fields.
    counts = [
        f"{key}: {len(value)}"
        for key, value in state.items()
        if isinstance(value, list)
        or (isinstance(value, dict) and all(isinstance(v, dict) for v in value.values()))
    ]
    return "; ".join(counts) if counts else "no top-level collections"


# What decides a review sheet: this module. The same file is scripts/batch_companies.py's SHEET_CODE,
# and the digest recorded in a sheet is compared against this tuple, so the two have to name the same
# files. The sheet is the one marker that is not JSON, and a recorded value has to live inside the
# marker rather than beside it -- a sidecar would be driver state, and that driver keeps none -- so it
# goes in an HTML comment: invisible in rendered Markdown, greppable on disk, and it also tells a
# human reader which code drew the page they are signing.
DECIDES_THIS = (Path(__file__),)
DECIDED_BY_LINE = "<!-- decided_by: %s -->"
_DECIDED_BY = re.compile(r"<!-- decided_by: ([0-9a-f]{64}) -->")


def current(marker):
    """Whether a REVIEW-SHEET.md was drawn by this module as it stands now.

    The driver's accept slot, bulk_layer.current's contract. It is handed ``{"text": ...}`` because
    the marker is Markdown and the driver's reader cannot parse it as JSON; a sheet with no recorded
    digest is stale, which is all three on disk on 2026-09-10, at 1 s each to redraw.
    """
    found = _DECIDED_BY.search(str((marker or {}).get("text", "")))
    return bool(found) and found.group(1) == decided_by(DECIDES_THIS)


def write_review_sheet(folder: str | Path, output: str | Path | None = None) -> Path:
    """Write the sheet (under 120 lines) and return its path; FOLDER/REVIEW-SHEET.md by default.

    ``output`` sends it somewhere else. REVIEW-SHEET.md is also the driver's stage-18 marker, so
    reading a company by writing its sheet into the company advanced its apparent position: cort
    carries a REVIEW-SHEET.md while it is still waiting for assign-apps at stage 2. An inspection
    should not move a state machine.
    """
    folder = Path(folder)
    data = _snapshot(folder)
    company = data["company"]
    lines = [
        f"# Review sheet: {_cell(data['name'])}",
        "",
        (
            f"Real analogue: {_cell(company.get('real_firm'))}; sector: {_cell(company.get('sector'))}; "
            f"location: {_cell(company.get('location'))}."
        ),
        "",
        "## Roster",
        "",
        _row("Worker", "Title", "Team", "Apps"),
        "| --- | --- | --- | --- |",
    ]
    worker_apps = _read(folder / "world/worker_apps.json")
    for worker in company.get("workers", []):
        worker_id = worker["id"]
        teams = [
            t.get("name") or t.get("id")
            for t in company.get("teams", [])
            if worker_id in t.get("worker_ids", [])
        ]
        name = worker_id + (f" ({worker['name']})" if worker.get("name") else "")
        apps = worker_apps.get(worker_id)
        lines.append(_row(name, worker.get("title"), teams, "none" if apps == [] else apps))
    if not company.get("workers"):
        lines.append(_row(*([_MISSING] * 4)))
    lines += [
        "",
        "## Apps — top-level collection counts",
        "",
        _row("App", "Records / entries (nested records excluded)"),
        "| --- | --- |",
    ]
    for app in data["apps"]:
        counts = _collections(data["states"][app]) if app in data["states"] else _MISSING
        lines.append(_row(app, counts))
    if not data["apps"]:
        lines.append(_row(_MISSING, _MISSING))
    lines += ["", "## Tasks — public briefs", ""]
    for task in data["tasks"]:
        assignment, workflow = task["assignment"], task["workflow"]
        boss = workflow.get("manager_id") or assignment.get("manager_id")
        cell = workflow.get("feature_cell") or assignment.get("feature_cell")
        lines += [
            (
                f"**{_cell(task['id'])}** — Feature cell: {_cell(cell)}; boss: {_cell(boss)}; "
                f"recorded calibration: {_cell(task['calibration'])}."
            ),
            "",
        ]
        brief = assignment.get("brief")
        if brief is None or brief == "":
            lines += [_MISSING, ""]
        else:
            # A longer fence preserves even Markdown/code fences in the public text.
            fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", brief)), default=0))
            lines += [fence + "text", brief, fence, ""]
    if not data["tasks"]:
        lines += [_MISSING, ""]
    # One controller runtime per task (runtime/tasks/<task>/), or the single-checkpoint layout.
    controllers = [_read(runtime / "CONTROLLER.json") for runtime in task_runtimes(folder).values()]
    parts = []
    for controller in sorted(controllers, key=lambda c: _text(c.get("task_id"))):
        text = f"{_text(controller.get('status'))}; task: {_text(controller.get('task_id'))}; "
        text += f"responsible step: {_text(controller.get('responsible_step'))}"
        if controller.get("options", {}).get("dry_run"):
            text += "; dry run"
        parts.append(text)
    controller_text = " / ".join(parts) if parts else _text(None)
    lines += [
        "## Recorded results",
        "",
        f"- Position: {_cell(data['stage']['stage'])} — {_cell(data['stage']['detail'])}"
        + (f"; waiting on {_cell(data['stage']['waiting_on'])}" if data["stage"]["waiting_on"] else "")
        + ("; a repair is already recorded" if data["stage"]["repair_recorded"] else "")
        + (
            f"; world set aside as {_cell(', '.join(data['stage']['worlds_set_aside']))}"
            if data["stage"]["worlds_set_aside"] and not data["stage"]["world_on_disk"]
            else ""
        )
        + ".",
        f"- Seed: {_cell(data['seed'])}; world review: {_cell(data['review'])}.",
        f"- Mechanical checks: {_cell(data['checks'])}.",
        f"- Readability: {_cell(_readability(data['readability']))}.",
        f"- Controller: {_cell(controller_text)}.",
    ]
    items = [
        item
        for category in data["readability"].get("categories", {}).values()
        for item in category.get("items", [])
    ]
    worst = max(
        items,
        key=lambda item: (
            {"plain": 0, "dense": 1, "system-log": 2}.get(item.get("verdict"), -1),
            item.get("jargon_density") or 0,
            item.get("average_sentence_length") or 0,
        ),
        default={},
    )
    lines.append(
        f"- Worst readability sample ({_cell(worst.get('source'))}): {_cell(worst.get('sample_sentence'))}"
    )
    findings = sorted(data["findings"], key=lambda f: f.get("severity") != "error")
    for finding in findings[:5]:
        lines.append(
            f"- Review finding [{_cell(finding.get('severity'))}, {_cell(finding.get('target'))}]: "
            f"{_cell(finding.get('issue'))} (evidence: {_cell(finding.get('evidence'))})"
        )
    if not findings:
        lines.append("- Review findings: " + ("none recorded" if data["review"] else _MISSING) + ".")
    elif len(findings) > 5:
        lines.append(f"- {len(findings) - 5} more findings in world/REVIEW.json.")
    lines += [
        "",
        "Recorded results only; calibration acceptance does not establish a successful worker episode.",
        "",
        "## Human decision",
        "",
        *[f"- [ ] Yes / [ ] No — {question}" for question in _QUESTIONS],
    ]
    markdown = "\n".join(lines) + f"\n\n{DECIDED_BY_LINE % decided_by(DECIDES_THIS)}\n"
    if len(markdown.splitlines()) >= 120:
        raise ValueError("Review sheet exceeds 119 lines; public briefs and roster were not truncated")
    output = Path(output) if output else folder / "REVIEW-SHEET.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")
    return output


def write_cohort_sheet(companies_dir: str | Path, output: str | Path) -> Path:
    """Write one row per immediate company folder; leave human decisions blank."""
    lines = [
        "# Company cohort review",
        "",
        "Recorded artifacts only; calibrated counts are accepted reports, not fresh validation.",
        "",
        _row(
            "Company",
            "Sector",
            "Position",
            "Apps",
            "Tasks",
            "Seed status",
            "Review verdict",
            "Checks",
            "Readability",
            "Calibrated tasks",
            "Human decision",
        ),
        "| " + " | ".join(["---"] * 11) + " |",
    ]
    for folder in sorted(Path(companies_dir).glob("*")):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        data = _snapshot(folder)
        tasks = data["tasks"]
        calibrated = sum(t["accepted"] for t in tasks)
        calibration = f"{calibrated}/{len(tasks)}" if tasks else _MISSING
        lines.append(
            _row(
                f"{data['name']} ({folder.name})",
                data["company"].get("sector"),
                _position(data["stage"]),
                data["apps"],
                [t["id"] for t in tasks],
                data["seed"],
                data["review"],
                data["checks"],
                _readability(data["readability"]),
                calibration,
            )
            + "  |"
        )
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output
