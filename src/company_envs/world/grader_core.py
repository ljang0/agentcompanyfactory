"""The grader contract and its mechanical evaluation.

Check schemas, selectors, predicate evaluation over app states and exported files, task
paths, and the attribution of passing checks to workers. No model call happens here; the
author (``grader_author``), the calibration proof (``calibration``) and the judgment model
(``judge``) build on this module, and ``grader`` re-exports it.
"""

import fnmatch
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from company_envs.schemas import STANDARD_APPS
from company_envs.storage import digest, read

from .documents import GRADED_FOLDERS, guest_path
from .hub_app import UI_STATE_KEYS

SKILL = "company-task-grader"
VERSION = 1


_NAME = r"[A-Za-z_][A-Za-z0-9_-]*"
_TOKEN = re.compile(
    rf"\.(?P<key>{_NAME})|\[(?P<index>0|[1-9][0-9]*)\]|\[(?P<wild>\*)\]"
    rf'|\[(?P<quoted>"(?:[^"\\]|\\.)*")\]'
    rf"|\[\?\(@\.(?P<field>{_NAME})\s*==\s*"
    r'(?P<value>"(?:[^"\\]|\\.)*"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?|true|false|null)\)\]'
)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _tokens(selector):
    if not selector.startswith("$") or len(selector) > 2048:
        raise ValueError("selector must start with $ and be at most 2048 characters")
    result, pos = [], 1
    while pos < len(selector):
        match = _TOKEN.match(selector, pos)
        if not match:
            raise ValueError(f"unsupported selector syntax at {selector[pos:]!r}")
        row = match.groupdict()
        if row["key"] is not None or row["quoted"] is not None:
            result.append(("key", row["key"] or json.loads(row["quoted"])))
        elif row["index"] is not None:
            result.append(("index", int(row["index"])))
        elif row["wild"]:
            result.append(("wild", None))
        else:
            result.append(("filter", (row["field"], json.loads(row["value"]))))
        pos = match.end()
    return result


def _equal(left, right):
    # Exact JSON equality also keeps true distinct from 1.
    return digest(left) == digest(right)


def select(state, selector):
    """Return matches, preserving missing ([]) versus JSON null ([None]).

    Supported: $.tickets[0], $.tickets[*].status, $.comments["1310"],
    $.tickets[?(@.id==1310)].status. Filters compare a direct field to a JSON
    scalar. No recursive descent, expressions, script evaluation or functions.
    """
    nodes = [state]
    for kind, arg in _tokens(selector):
        found = []
        for node in nodes:
            if (kind == "key" and isinstance(node, dict) and arg in node) or (
                kind == "index" and isinstance(node, list) and arg < len(node)
            ):
                found.append(node[arg])
            elif kind == "wild" and isinstance(node, (dict, list)):
                found.extend(node.values() if isinstance(node, dict) else node)
            elif kind == "filter" and isinstance(node, list):
                key, value = arg
                found.extend(
                    row for row in node if isinstance(row, dict) and key in row and _equal(row[key], value)
                )
        nodes = found
    return nodes


class AppPath(Contract):
    app_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    selector: str

    @model_validator(mode="after")
    def valid_selector(self):
        tokens = _tokens(self.selector)
        if not tokens or tokens[0][0] != "key":
            raise ValueError("selectors must name a top-level collection")
        return self


# Every format the pipeline renders needs a way to check a value inside it, or a task whose work
# lands in that format cannot be graded at all. Measured over the 2,026 materials on disk:
# pdf_contains covers 635, docx_contains 392, file_contains the 473 plain-text ones -- while the
# 71 decks had no operator of any kind, and the 266 workbooks had only sheet_cell, which needs the
# author to guess the exact cell the worker will use. sheet_contains and slide_contains close both.
FILE_OPERATORS = {
    "file_exists",
    "file_absent",
    "file_contains",
    "sheet_cell",
    "sheet_contains",
    "pdf_contains",
    "docx_contains",
    "slide_contains",
    "image_size",
}
# The file operators that read a document's text rather than one addressed cell. They share one
# rule -- nonempty text, an exact path, case-sensitive substring -- so they are named once.
TEXT_FILE_OPERATORS = {"file_contains", "pdf_contains", "docx_contains", "sheet_contains", "slide_contains"}
# Operators whose path may be a glob. Presence and absence are about a *set* of files -- "no scan
# is left in Downloads" names a pattern, not one name -- while anything that reads inside a file
# needs to know which file it read.
GLOB_OPERATORS = {"file_exists", "file_absent", "file_contains"}
VALUE_JSON_OPERATORS = {"equals", "contains", "count_gte", "sheet_cell", "image_size"}


class Predicate(Contract):
    app_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    selector: str | None = None
    operator: Literal[
        "exists",
        "equals",
        "contains",
        "count_gte",
        "changed",
        "unchanged",
        "file_exists",
        "file_absent",
        "file_contains",
        "sheet_cell",
        "sheet_contains",
        "pdf_contains",
        "docx_contains",
        "slide_contains",
        "image_size",
    ]
    path: str | None = None
    text: str | None = None
    sheet: str | None = None
    cell: str | None = None
    value_json: str | None = Field(
        default=None, description="JSON-encoded operand; null for operators without an operand"
    )

    @model_validator(mode="after")
    def valid_operand(self):
        if self.operator in FILE_OPERATORS:
            if self.app_id is not None or self.selector is not None or not self.path:
                raise ValueError("file predicates require path and forbid app_id/selector")
            parts = Path(self.path).parts
            if (
                Path(self.path).is_absolute()
                or ".." in parts
                or len(parts) < 3
                or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[0])
                or parts[1] not in set(GRADED_FOLDERS)
            ):
                raise ValueError(
                    "file path must be worker/<" + "|".join(GRADED_FOLDERS) + "> plus a filename"
                )
            if self.operator not in GLOB_OPERATORS and any(c in self.path for c in "*?["):
                raise ValueError("document and cell predicates require an exact path")
            if self.operator in TEXT_FILE_OPERATORS:
                if not self.text:
                    raise ValueError("contains requires nonempty text")
            elif self.text is not None:
                raise ValueError("text is only valid for file content checks")
            if self.operator == "sheet_cell":
                if not self.sheet or not self.cell or not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]*", self.cell):
                    raise ValueError("sheet_cell requires a sheet and an A1 cell address")
            elif self.sheet is not None or self.cell is not None:
                raise ValueError("sheet/cell are only valid for sheet_cell")
        else:
            if any(value is not None for value in (self.path, self.text, self.sheet, self.cell)):
                raise ValueError("app predicates forbid file fields")
            AppPath(app_id=self.app_id, selector=self.selector)
        needs_value = self.operator in VALUE_JSON_OPERATORS
        if needs_value != (self.value_json is not None):
            raise ValueError(
                "equals/contains/count_gte/sheet_cell/image_size require value_json; others forbid it"
            )
        if needs_value:
            value = json.loads(self.value_json)
            # Reject NaN/Infinity, including within containers.
            json.dumps(value, allow_nan=False)
            if self.operator == "count_gte" and (type(value) is not int or value < 1):
                raise ValueError("count_gte requires a positive JSON integer")
            if self.operator == "image_size" and not (
                isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,4}x[1-9][0-9]{0,4}", value)
            ):
                raise ValueError('image_size requires a JSON string like "1200x628"')
            if self.operator == "sheet_cell" and isinstance(value, (list, dict)):
                raise ValueError("sheet_cell requires a JSON scalar")
        return self


class Check(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    criterion_ref: str = Field(pattern=r"^/success_criteria/(0|[1-9][0-9]*)$")
    kind: Literal["state", "artifact", "judgment"]
    description: str = Field(min_length=1)
    predicate: Predicate | None = None
    guards: list[Predicate] = Field(default_factory=list, description="AND preconditions; no separate credit")
    rubric: str | None = None
    app_paths: list[AppPath] = Field(default_factory=list)
    material_paths: list[str] = Field(default_factory=list, description="Paths relative to world/materials")

    @model_validator(mode="before")
    @classmethod
    def drop_retired_fields(cls, value):
        # The screenshot judge is retired. Older graders on disk still carry its field; a free-form
        # object in the authoring schema is also what Codex strict mode rejects (invalid_json_schema).
        if isinstance(value, dict) and "screen_evidence" in value:
            value = {k: v for k, v in value.items() if k != "screen_evidence"}
        return value

    @model_validator(mode="after")
    def valid_kind(self):
        if self.kind == "state":
            if self.predicate is None or self.rubric or self.app_paths or self.material_paths:
                raise ValueError("state checks require a predicate and forbid rubric/evidence paths")
        elif (
            self.predicate is not None
            or self.guards
            or not self.rubric
            or not (self.app_paths or self.material_paths)
        ):
            raise ValueError("artifact/judgment checks require rubric and paths, with no predicates")
        for path in self.material_paths:
            _safe_path(Path("/materials"), path)
        return self


class TaskGrader(Contract):
    checks: list[Check] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_checks(self):
        if len({c.id for c in self.checks}) != len(self.checks):
            raise ValueError("duplicate check ids")
        if not any(c.kind == "state" for c in self.checks):
            raise ValueError("at least one mechanical state check is required")
        return self


def evaluate_predicate(predicate, initial_states, final_states, *, exports=None):
    """Missing apps are errors; missing paths cannot satisfy equals/unchanged.

    equals requires exactly one match. contains is exact membership for arrays
    and object keys, substring containment for strings. count_gte counts a sole
    collection's entries, otherwise selector matches. changed compares selected
    values against the baseline, including deletion; unchanged requires presence.
    """
    if predicate.operator in FILE_OPERATORS:
        files = exports if isinstance(exports, dict) else _export_files(exports)
        if predicate.operator == "file_absent":
            # An empty export is not proof that a file is gone; it is proof of nothing. Answering
            # True here would be a check that passes hardest when the episode produced least,
            # which is the same defect as a receipt that always says done.
            if not files:
                raise ValueError("file_absent cannot be judged: no files were exported")
            return not any(fnmatch.fnmatchcase(name, predicate.path) for name in files)
        error = None
        for name, path in files.items():
            if not fnmatch.fnmatchcase(name, predicate.path):
                continue
            try:
                if _file_matches(predicate, path):
                    return True
            except ValueError as exc:
                error = exc
        if error is not None:
            raise error
        return False
    app = predicate.app_id
    before = select(initial_states[app], predicate.selector)
    after = select(final_states[app], predicate.selector)
    op = predicate.operator
    value = json.loads(predicate.value_json) if predicate.value_json is not None else None
    if op == "exists":
        return bool(after)
    if op == "equals":
        return len(after) == 1 and _equal(after[0], value)
    if op == "contains":
        if len(after) != 1:
            return False
        container = after[0]
        if isinstance(container, list):
            return any(_equal(item, value) for item in container)
        if isinstance(container, (dict, str)) and isinstance(value, str):
            return value in container
        return False
    if op == "count_gte":
        selects_many = any(kind in {"wild", "filter"} for kind, _ in _tokens(predicate.selector))
        count = (
            len(after[0])
            if not selects_many and len(after) == 1 and isinstance(after[0], (list, dict))
            else len(after)
        )
        return count >= value
    if op == "changed":
        return not _equal(before, after)
    return bool(before) and bool(after) and _equal(before, after)


def _export_files(base):
    """List regular files without following links supplied by a guest."""
    if base is None:
        return {}
    base = Path(base)
    files = {}
    for path in sorted(base.rglob("*")):
        if path.is_file() and not any(p.is_symlink() for p in (path, *path.parents)):
            relative = str(path.relative_to(base))
            files[relative] = _safe_path(base, relative)
    return files


def _staged_files(folder):
    """Every file on the workers' desktops before the episode: the baseline exports compare against."""
    files = {}
    # Before VM launch, ordinary world materials are the desktop baseline.
    for materials in sorted((Path(folder) / "world/materials").glob("*")):
        if not (Path(folder) / "runtime/vms" / materials.name / "guest").exists():
            files.update(
                {
                    f"{materials.name}/{guest_path(name)}": path
                    for name, path in _export_files(materials).items()
                }
            )
    for guest in sorted((Path(folder) / "runtime/vms").glob("*/guest")):
        files.update({f"{guest.parent.name}/{name}": path for name, path in _export_files(guest).items()})
    return files


def _initial_files(folder, grader):
    """Staged files that a file predicate names; the calibration proof hashes exactly these."""
    patterns = [
        p.path
        for check in grader.checks
        if check.kind == "state"
        for p in [check.predicate, *check.guards]
        if p.operator in FILE_OPERATORS
    ]
    if not patterns:
        return {}
    files = _staged_files(folder)
    return {
        name: path
        for name, path in files.items()
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)
    }


def _file_hashes(files):
    return {name: digest(path.read_bytes()) for name, path in files.items()}


def _file_matches(predicate, path):
    try:
        if predicate.operator == "file_exists":
            return True
        if predicate.operator == "file_contains":
            return predicate.text in path.read_text(encoding="utf-8")
        if predicate.operator == "sheet_cell":
            from openpyxl import load_workbook

            workbook = load_workbook(path, read_only=True, data_only=False)
            try:
                return _equal(
                    workbook[predicate.sheet][predicate.cell].value, json.loads(predicate.value_json)
                )
            finally:
                workbook.close()
        if predicate.operator == "image_size":
            # A picture's one property a task can fix in advance and a worker can be asked to
            # produce: "export the banner at 1200x628". Exact, so it neither drifts nor needs a
            # tolerance nobody could defend.
            from PIL import Image

            with Image.open(path) as image:
                return f"{image.width}x{image.height}" == json.loads(predicate.value_json)
        if predicate.operator == "sheet_contains":
            # Both of a workbook's answers. data_only=True is the cached value the application
            # wrote when it last saved -- what the worker saw -- and is None in a workbook no
            # application has opened; data_only=False is the literal or the formula's own text.
            # Reading one of them would answer "not there" for every workbook of the other kind,
            # which is a silent miss rather than a verdict.
            from .desktop_app import read_sheets

            return any(
                predicate.text in read_sheets(path, data_only=cached)["text"] for cached in (True, False)
            )
        if predicate.operator == "slide_contains":
            # Titles, every text frame, table cells and speaker notes, which is a deck's whole
            # readable surface -- the same reach docx_contains has over paragraphs and tables.
            from .desktop_app import read_slides

            text = read_slides(path)["text"]
        elif predicate.operator == "pdf_contains":
            from pypdf import PdfReader

            with path.open("rb") as stream:
                text = "\n".join(page.extract_text() or "" for page in PdfReader(stream).pages)
        else:
            from docx import Document

            document = Document(path)
            text = "\n".join(
                [p.text for p in document.paragraphs]
                + [cell.text for table in document.tables for row in table.rows for cell in row.cells]
            )
        return predicate.text in text
    except Exception as exc:
        raise ValueError(f"cannot read exported file {path.name}: {exc}") from exc


def _safe_path(base, relative):
    path = Path(relative)
    if not relative or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe relative path: {relative}")
    target = (base / path).resolve()
    if not target.is_relative_to(base.resolve()):
        raise ValueError(f"path escapes {base}: {relative}")
    return target


def _task(folder, task_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        raise ValueError("invalid task id")
    return _safe_path(Path(folder) / "tasks", task_id)


def _criteria(folder, task_id):
    workflow = read(_task(folder, task_id) / "workflow.json")
    return workflow["success_criteria"]


def _grading_context(folder, task_id):
    workflow = read(_task(folder, task_id) / "workflow.json")
    return {
        "worker_ids": workflow.get("worker_ids", []),
        "completion_outcomes": workflow.get("completion", {}).get("outcomes", []),
        **({"assessment": _assessment(folder, task_id)} if workflow.get("assessment") is not None else {}),
    }


def _assessment(folder, task_id):
    """Keep the accepted assessment and its separately stored private copy in sync."""
    from company_envs.schemas import TaskAssessment

    task = _task(folder, task_id)
    declared = read(task / "workflow.json").get("assessment")
    saved = task / "assessment.json"
    if declared is None:
        if saved.exists():
            raise ValueError("Assessment is not part of the accepted workflow")
        return None
    assessment = TaskAssessment.model_validate(declared).model_dump()
    if not saved.is_file() or TaskAssessment.model_validate(read(saved)).model_dump() != assessment:
        raise ValueError("Saved assessment differs from the accepted workflow")
    return assessment


def assessment_score(assessment, checks):
    """Weight criteria once, preserving explicit must-pass and incomplete-evidence gates."""
    grouped = {}
    for check in checks:
        grouped.setdefault(check["criterion_ref"], []).append(check["status"])
    expected = {f"/success_criteria/{c['criterion'] - 1}" for c in assessment["criteria"]}
    if set(grouped) != expected or len(expected) != len(assessment["criteria"]):
        raise ValueError("Assessment and graded criteria do not match")
    criteria = []
    for criterion in assessment["criteria"]:
        ref = f"/success_criteria/{criterion['criterion'] - 1}"
        statuses = grouped[ref]
        if any(s not in {"pass", "fail", "pending", "error"} for s in statuses):
            raise ValueError("Unknown assessment check status")
        criteria.append(
            {
                "criterion_ref": ref,
                "weight": criterion["weight"],
                "must_pass": criterion["must_pass"],
                "score": statuses.count("pass") / len(statuses),
                "passed": all(s == "pass" for s in statuses),
                "complete": all(s in {"pass", "fail"} for s in statuses),
            }
        )
    return {
        "score": sum(c["score"] * c["weight"] for c in criteria) / sum(c["weight"] for c in criteria),
        "must_pass_satisfied": all(c["passed"] for c in criteria if c["must_pass"]),
        "complete": all(c["complete"] for c in criteria),
        "criteria": criteria,
    }


def _contributions(
    folder, task_id, grader, rows, snapshots, sid, *, attribution_dir=None, exports=None, initial_files=None
):
    workflow = read(_task(folder, task_id) / "workflow.json")
    workers = sorted(set(workflow.get("worker_ids", [])))
    table = {worker: {"worker_id": worker, "passing_checks": [], "changes": []} for worker in workers}
    passed = {row["id"] for row in rows if row["status"] == "pass"}
    relevant = {}
    for check in grader.checks:
        if check.id not in passed:
            continue
        paths = [check.predicate, *check.guards] if check.kind == "state" else check.app_paths
        for path in paths:
            if isinstance(path, Predicate) and path.operator in FILE_OPERATORS:
                for name, target in (exports or {}).items():
                    worker = name.split("/", 1)[0]
                    before = (initial_files or {}).get(name)
                    if worker not in table or not fnmatch.fnmatchcase(name, path.path):
                        continue
                    if before is not None and digest(before.read_bytes()) == digest(target.read_bytes()):
                        continue
                    try:
                        if not _file_matches(path, target):
                            continue
                    except ValueError:
                        continue
                    if check.id not in table[worker]["passing_checks"]:
                        table[worker]["passing_checks"].append(check.id)
                    table[worker]["changes"].append({"path": name, "check_ids": [check.id]})
                continue
            if path.app_id not in snapshots:
                continue
            snap = snapshots[path.app_id]
            if _equal(
                select(snap["initial_state"], path.selector), select(snap["current_state"], path.selector)
            ):
                continue
            key = _tokens(path.selector)[0][1]
            relevant.setdefault((path.app_id, key), set()).add(check.id)
    warnings = []
    attribution_dir = (
        Path(attribution_dir) if attribution_dir is not None else folder / "runtime" / "attribution"
    )
    for app in snapshots:
        path = attribution_dir / f"{app}.jsonl"
        if not path.exists():
            warnings.append(f"no attribution log for {app}")
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            try:
                entry = json.loads(line)
                if entry["sid"] != sid:
                    continue
                worker, keys = entry["worker_id"], entry["changed_keys"]
                if worker not in table:
                    continue
                if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
                    raise ValueError("changed_keys must be strings")
                timestamp = entry["at"]
                if not isinstance(timestamp, str) or not timestamp:
                    raise ValueError("attribution requires a timestamp")
                for key in sorted(set(keys)):
                    checks = sorted(relevant.get((app, key), []))
                    if checks:
                        table[worker]["passing_checks"] = sorted(
                            set(table[worker]["passing_checks"]) | set(checks)
                        )
                    # Every attributed change is kept: milestones also count writes that
                    # artifact and judgment checks read, which no state check names.
                    table[worker]["changes"].append(
                        {"app_id": app, "key": key, "at": timestamp, "check_ids": checks}
                    )
            except (ValueError, KeyError, TypeError) as exc:
                warnings.append(f"{path.name}:{number}: invalid attribution: {exc}")
    required = 3 if len(workers) >= 3 else 0
    observed = sum(bool(row["passing_checks"]) for row in table.values())
    # Milestones: with a task contract, every contribution's worker must have done consequential
    # work, only in systems that worker holds, and every decisive collection must have changed.
    # Without a contract the older rule stands: three distinct contributors when the roster has three.
    contract = {
        c["worker_id"]: set(c.get("apps") or []) | set(STANDARD_APPS)
        for c in workflow.get("contributions", [])
        if c.get("apps") is not None
    }
    milestones, problems = {}, []
    if contract:
        cells = set((workflow.get("feature_cell") or {}).get("collections", []))
        # ... or in any collection the grader reads: artifact and judgment checks select the
        # documents specialists write, even though no state check names them.
        for check in getattr(grader, "checks", []):
            paths = [check.predicate, *check.guards] if check.kind == "state" else check.app_paths
            for path in paths:
                if path is None or getattr(path, "operator", None) in FILE_OPERATORS:
                    continue
                try:
                    cells.add(f"{path.app_id}.{_tokens(path.selector)[0][1]}")
                except (ValueError, IndexError, TypeError):
                    continue
        for worker, held in contract.items():
            row = table.get(worker)
            # Consequential: a change that made a state check pass, or a change in a decisive
            # collection (specialist work that artifact or judgment checks read rather than a
            # state check).
            done = bool(
                row
                and (
                    row["passing_checks"] or any(f"{c['app_id']}.{c['key']}" in cells for c in row["changes"])
                )
            )
            outside = sorted(
                {c["app_id"] for c in (row["changes"] if row else []) if c["app_id"] not in held}
            )
            milestones[worker] = {"done": done, "outside_held_apps": outside}
            if not done:
                problems.append(f"{worker} made no consequential change")
            if outside:
                problems.append(f"{worker} wrote in apps they do not hold: {outside}")
        decisive = {}
        for cell in (workflow.get("feature_cell") or {}).get("collections", []):
            app, _, key = cell.partition(".")
            decisive[cell] = any(
                c["app_id"] == app and c["key"] == key for row in table.values() for c in row["changes"]
            )
            if not decisive[cell]:
                problems.append(f"decisive collection {cell} was not changed by any worker")
    status = "pass" if (not problems if contract else observed >= required) else "fail"
    return {
        "workers": list(table.values()),
        "required_workers": len(contract) if contract else required,
        "observed_workers": observed,
        "milestones": milestones,
        "decisive_collections": decisive if contract else {},
        "problems": problems,
        "status": status,
        "basis": "collection-level overlap of proxy labels only; logs do not authenticate callers "
        "or prove field authorship or surviving writes; changed exported files identify the worker's "
        "desktop, not who authored their contents",
        "warnings": warnings,
    }


WIPE_MIN_RECORDS = 4  # a workbook's sheet list is small and still tells a reset from an edit
WIPE_SHARE = 0.5  # losing at least this share of seeded records is a reset, not editing


def _wiped_collections(initial, final):
    """app.collection names whose seeded records mostly vanished between initial and final."""
    from .world_check import records

    wiped = []
    for app, before in initial.items():
        after = final.get(app) or {}
        for key, value in before.items():
            if not isinstance(value, (list, dict)) or key in UI_STATE_KEYS:
                continue
            seeded = {str(r["id"]) for _p, r in records(value) if isinstance(r, dict) and "id" in r}
            if len(seeded) < WIPE_MIN_RECORDS:
                continue
            current = {str(r["id"]) for _p, r in records(after.get(key)) if isinstance(r, dict) and "id" in r}
            lost = len(seeded - current) / len(seeded)
            if len(seeded - current) >= 3 and lost >= WIPE_SHARE:
                wiped.append(
                    {"collection": f"{app}.{key}", "seeded": len(seeded), "lost": len(seeded - current)}
                )
    return wiped
