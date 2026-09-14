"""Deterministic, offline generalization of a seeded UTF-8 company folder.

Entity identifiers are discovered in id/ids fields, worker maps and code-like
tokens (e.g. L310-v2) in text. App identifiers and schema/collection names are
contracts, not entities. Numeric identifiers are replaced only in identifier
fields, keyed tables, selectors and explicit textual references, never quantities
or list indices. Spreadsheet cell coordinates remain coordinates.

Calendar shifts preserve strict ordering of distinct calendar dates. In shift
direction, each date takes the nearest available date at/after its requested
shift (at/before for negative shifts), skipping weekends for source weekdays.
This can propagate adjustments beyond the original weekend. Times and offsets
are preserved verbatim; this is a calendar shift, not timezone/DST conversion.

Runtime evidence and model-call caches are excluded. TRANSFORM.json is provenance,
not a new human review or a calibration certificate. Recalibrate before grading.
"""

import hashlib
import json
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from company_envs.storage import digest

from . import grader
from .hub_world import load_world
from .world_check import check_world

_ID_FIELD = re.compile(r"(?:^id$|_ids?$|Ids?$|^workers$|^members$)")
_CODE = re.compile(r"(?<![\w-])[A-Z][A-Z0-9]*(?:-(?:[A-Z0-9]+|[A-Za-z0-9]*\d[A-Za-z0-9]*))*(?![\w])")
_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
_CELL = re.compile(r"[A-Z]+[1-9][0-9]*$")
_SKIP = {"runtime", "calls", "grader_calls", ".git", "__pycache__"}


def _read_files(folder):
    files = {}
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder)
        if set(relative.parts) & _SKIP or relative.as_posix() == "TRANSFORM.json":
            continue
        if path.is_symlink():
            raise ValueError(f"symlinks are unsupported: {relative}")
        if path.is_file():
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"transform requires UTF-8 files: {relative}") from exc
            files[relative.as_posix()] = (raw, json.loads(text) if path.suffix == ".json" else text)
    return files


def _id_field(key):
    return bool(_ID_FIELD.search(key)) and key not in {"app_id", "app_ids", "call_id", "session_id"}


def _discover(files):
    ids, integers, structural, app_ids = set(), set(), set(), set()
    manifest = files["apps.json"][1]
    for app in manifest["apps"]:
        app_ids.add(app["app_id"])
        structural.update(app.get("top_level_keys", []))
    structural.update(files["world/world.json"][1])

    def visit(node, key="", coordinates=False):
        if isinstance(node, dict):
            if "id" in node:
                structural.update(node)
            for k, value in node.items():
                if not (coordinates and _CELL.fullmatch(k)):
                    codes(k)
                visit(value, k, k == "data")
        elif isinstance(node, list):
            for value in node:
                visit(value, key)
        elif isinstance(node, str):
            if key == "selector":
                for kind, arg in grader._tokens(node):
                    if kind == "filter" and _id_field(arg[0]) and type(arg[1]) in (str, int):
                        ids.add(str(arg[1]))
                        if type(arg[1]) is int:
                            integers.add(str(arg[1]))
            if _id_field(key) and re.fullmatch(r"[A-Za-z0-9_.-]+", node):
                ids.add(node)
            codes(node)
        elif type(node) is int and _id_field(key):
            ids.add(str(node))
            integers.add(str(node))

    def codes(text):
        ids.update(token for token in _CODE.findall(_DATE.sub("", text)) if any(c.isdigit() for c in token))

    for name, (_, node) in files.items():
        visit(node)
        if name.endswith(("identities.json", "worker_apps.json")):
            ids.update(node)
        if "/materials/" in name:
            codes(name)
    ids.difference_update(app_ids)
    return ids, integers, structural


def _make_map(ids, integers, supplied, seed):
    if supplied is not None and seed is not None:
        raise ValueError("supply id_map or rename_seed, not both")
    if supplied is not None:
        mapping = {str(k): v for k, v in supplied.items()}
    elif seed is not None:
        mapping = {}
        for key in sorted(ids):
            token = hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()
            mapping[key] = (
                100_000_000 + int(token[:12], 16) % 900_000_000
                if key in integers or key.isdecimal()
                else "entity_" + token[:16]
            )
    else:
        mapping = {}
    targets = [str(v) for v in mapping.values()]
    if len(set(targets)) != len(targets) or set(targets) & ids:
        raise ValueError("id_map must be injective and targets must not overlap source IDs")
    for key, value in mapping.items():
        if key not in ids:
            raise ValueError(f"unknown source ID: {key}")
        if type(value) not in (str, int) or not re.fullmatch(r"[A-Za-z0-9_-]+", str(value)):
            raise ValueError(f"invalid target ID: {value!r}")
        if key in integers and (type(value) is not int or value < 0):
            raise ValueError(f"numeric ID {key} requires a nonnegative integer target")
    return dict(sorted(mapping.items()))


def _dates(files, shift_days, business_calendar):
    days = set()
    for name, (raw, _) in files.items():
        for token in _DATE.findall(name + "\n" + raw.decode()):
            try:
                days.add(date.fromisoformat(token))
            except ValueError:
                pass
    direction = -1 if shift_days < 0 else 1
    previous = None
    mapping, adjustments = {}, []
    for day in sorted(days, reverse=direction < 0):
        requested = day + timedelta(days=shift_days)
        target = requested
        reasons = []
        if business_calendar and shift_days:
            if previous is not None and (target - previous).days * direction <= 0:
                target = previous + timedelta(days=direction)
                reasons.append("relative_order")
            if day.weekday() < 5 and target.weekday() >= 5:
                reasons.append("weekday")
                while target.weekday() >= 5:
                    target += timedelta(days=direction)
        mapping[day.isoformat()] = target.isoformat()
        if target != requested:
            adjustments.append(
                {
                    "source": day.isoformat(),
                    "requested": requested.isoformat(),
                    "target": target.isoformat(),
                    "adjustment_days": (target - requested).days,
                    "reasons": reasons,
                }
            )
        previous = target
    return dict(sorted(mapping.items())), adjustments


class _Rewrite:
    def __init__(self, mapping, dates, structural):
        self.mapping, self.dates, self.structural = mapping, dates, structural
        self.counts = Counter()
        strings = [re.escape(k) for k in sorted(mapping, key=lambda k: (-len(k), k)) if not k.isdecimal()]
        self.pattern = re.compile(r"(?<![\w])(?:" + "|".join(strings) + r")(?![\w])") if strings else None

    def identifier(self, value):
        key = str(value)
        if key in self.mapping:
            self.counts[key] += 1
            target = self.mapping[key]
            return target if type(value) is int else str(target)
        return value

    def text(self, text):
        # Protect ISO dates from numeric IDs and from generated strings.
        text = _DATE.sub(lambda m: self.dates.get(m[0], m[0]), text)
        if self.pattern:
            text = self.pattern.sub(lambda m: self.identifier(m[0]), text)
        # Numeric references have explicit context; bare numbers remain quantities.
        return re.sub(
            r"(?P<prefix>\b(?:ticket|user|group|organization|record)s?[ #:/]+|#|"
            r"\[(?:\\?\")|@\.(?:id|[A-Za-z_]+_id|[A-Za-z_]+Id)\s*==\s*)"
            r"(?P<id>\d+)(?![\w-]|\.\d)",
            lambda m: m["prefix"] + str(self.identifier(m["id"])),
            text,
            flags=re.IGNORECASE,
        )

    def selector(self, selector):
        result, previous = "$", ""
        for kind, arg in grader._tokens(selector):
            if kind == "key":
                key = arg
                if arg not in self.structural and not (previous == "data" and _CELL.fullmatch(arg)):
                    key = self.identifier(arg)
                result += "[" + json.dumps(key) + "]"
                previous = arg
            elif kind == "index":
                result += f"[{arg}]"
            elif kind == "wild":
                result += "[*]"
            else:
                field, value = arg
                value = self.node(value, field)
                result += f"[?(@.{field}=={json.dumps(value)})]"
        return result

    def node(self, node, key=""):
        if isinstance(node, dict):
            result = {}
            for k, value in node.items():
                new_key = _DATE.sub(lambda m: self.dates.get(m[0], m[0]), k)
                if k not in self.structural and not (key == "data" and _CELL.fullmatch(k)):
                    new_key = str(self.identifier(k)) if k in self.mapping else self.text(k)
                if new_key in result:
                    raise ValueError(f"renaming collides with object key {new_key}")
                if k == "value_json" and value is not None:
                    operand = json.loads(value)
                    tokens = grader._tokens(node["selector"])
                    last = tokens[-1]
                    field = last[1] if last[0] == "key" else ""
                    # Counts and numeric quantities must not turn into numeric IDs.
                    if node["operator"] != "count_gte":
                        operand = self.node(operand, field)
                    result[new_key] = json.dumps(operand, ensure_ascii=False)
                else:
                    result[new_key] = self.node(value, k)
            return result
        if isinstance(node, list):
            return [self.node(value, key) for value in node]
        if _id_field(key) and type(node) in (str, int) and str(node) in self.mapping:
            return self.identifier(node)
        if isinstance(node, str):
            if key == "selector":
                return self.selector(node)
            return self.text(node)
        return node


def _schema_path(folder, relative):
    path = Path(relative)
    for root in [folder, *folder.parents, Path(__file__).resolve().parents[3]]:
        candidate = root / path
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError(f"cannot resolve app schema: {relative}")


def _states(nodes):
    return {app["app_id"]: nodes[app["state_file"]] for app in nodes["apps.json"]["apps"]}


def _refresh_hashes(nodes):
    initial = _states(nodes)
    materials = {
        name.removeprefix("world/materials/"): digest(_encode(name, node))
        for name, node in nodes.items()
        if name.startswith("world/materials/")
    }
    for name, draft in nodes.items():
        if not re.fullmatch(r"tasks/[^/]+/grader.json", name) or "grader" not in draft:
            continue
        task = name.rsplit("/", 1)[0]
        workflow = nodes[f"{task}/workflow.json"]
        draft.update(
            initial_hash=digest(initial),
            criteria_hash=digest(workflow["success_criteria"]),
            brief_hash=digest(nodes[f"{task}/assignment.json"]),
            grading_context_hash=digest(
                {
                    "worker_ids": workflow.get("worker_ids", []),
                    "completion_outcomes": workflow.get("completion", {}).get("outcomes", []),
                }
            ),
            initial_material_hashes=materials,
        )
    if "MANIFEST.json" in nodes:
        nodes["MANIFEST.json"]["hashes"] = {
            name: digest(_encode(name, node))
            for name, node in sorted(nodes.items())
            if name != "MANIFEST.json"
        }


def _encode(name, node):
    return (
        json.dumps(node, ensure_ascii=False, indent=2) + "\n" if name.endswith(".json") else node
    ).encode()


def _render(folder, files, mapping, dates, structural):
    rewrite = _Rewrite(mapping, dates, structural)
    nodes, paths = {}, {}
    for name, (_, node) in files.items():
        target = rewrite.text(name)
        if target in nodes:
            raise ValueError(f"renaming collides with file {target}")
        paths[name] = target
        nodes[target] = rewrite.node(node)
    # App schema paths refer to the pinned catalog outside the copied company.
    # Absolute paths let load_world accept output anywhere without a root argument.
    for original, app in zip(files["apps.json"][1]["apps"], nodes["apps.json"]["apps"], strict=True):
        if original.get("schema"):
            app["schema"] = str(_schema_path(folder, original["schema"]))
    _refresh_hashes(nodes)
    return nodes, paths, dict(sorted(rewrite.counts.items()))


def transform_company(
    folder, output_folder, *, id_map=None, rename_seed=None, shift_days=0, business_calendar=True
):
    """Write a new company and return its deterministic TRANSFORM.json manifest.

    Existing/overlapping output folders are refused. An explicit partial map is
    applied as supplied; verification reports missing IDs as leaks. With neither
    map nor seed, this is a date-only transform and renaming checks are disabled.
    """
    folder, output = Path(folder).resolve(), Path(output_folder).resolve()
    if output.exists() or output.is_relative_to(folder) or folder.is_relative_to(output):
        raise ValueError("output must be a new folder outside the source")
    if type(shift_days) is not int:
        raise ValueError("shift_days must be an integer")
    files = _read_files(folder)
    ids, integers, structural = _discover(files)
    mapping = _make_map(ids, integers, id_map, rename_seed)
    dates, adjustments = _dates(files, shift_days, business_calendar)
    nodes, paths, counts = _render(folder, files, mapping, dates, structural)
    manifest = {
        "version": 1,
        "rename": id_map is not None or rename_seed is not None,
        "rename_seed": rename_seed,
        "id_map": mapping,
        "shift_days": shift_days,
        "business_calendar": business_calendar,
        "date_map": dates,
        "adjustments": adjustments,
        "occurrences": counts,
        "paths": paths,
        "source_hashes": {name: digest(raw) for name, (raw, _) in files.items()},
        "output_hashes": {name: digest(_encode(name, node)) for name, node in sorted(nodes.items())},
    }
    output.mkdir(parents=True)
    for name, node in nodes.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_encode(name, node))
    (output / "TRANSFORM.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _grader_checks(source, target, paths, rewrite):
    checks = []
    before, after = _states(source), _states(target)
    for name, draft in source.items():
        if not re.fullmatch(r"tasks/[^/]+/grader.json", name):
            continue
        new_draft = target[paths[name]]
        old = grader.TaskGrader.model_validate(draft.get("grader", draft))
        new = grader.TaskGrader.model_validate(new_draft.get("grader", new_draft))
        for left, right in zip(old.checks, new.checks, strict=True):
            old_paths = [left.predicate, *left.guards] if left.kind == "state" else left.app_paths
            new_paths = [right.predicate, *right.guards] if right.kind == "state" else right.app_paths
            for a, b in zip(old_paths, new_paths, strict=True):
                selected = grader.select(before[a.app_id], a.selector)
                actual = grader.select(after[b.app_id], b.selector)
                # Selecting scalar IDs loses their field context; preserve it here.
                field = grader._tokens(a.selector)[-1]
                key = field[1] if field[0] == "key" else ""
                expected = [rewrite.node(value, key) for value in selected]
                resolved = expected == actual
                same = True
                if isinstance(a, grader.Predicate):
                    same = grader.evaluate_predicate(a, before, before) == grader.evaluate_predicate(
                        b, after, after
                    )
                checks.append(
                    {"task": name, "check": left.id, "selector": a.selector, "ok": resolved and same}
                )
            for a, b in zip(left.material_paths, right.material_paths, strict=True):
                old_name, new_name = "world/materials/" + a, "world/materials/" + b
                checks.append(
                    {
                        "task": name,
                        "check": left.id,
                        "material": a,
                        "ok": (old_name in source) == (new_name in target),
                    }
                )
    return checks


def verify_transform(folder, output_folder):
    """Return an offline report; ``ok`` requires all integrity/seed/grader checks.

    Verification rediscovers source IDs, regenerates each expected occurrence and
    compares every output file, rather than trusting manifest counts or hashes.
    Identifier-like quantities, schema keys and spreadsheet coordinates are not
    identifier occurrences. TRANSFORM.json intentionally retains source IDs.
    """
    folder, output = Path(folder).resolve(), Path(output_folder).resolve()
    errors, checks, world = [], [], None
    try:
        manifest = json.loads((output / "TRANSFORM.json").read_text())
        source_files, output_files = _read_files(folder), _read_files(output)
        ids, integers, structural = _discover(source_files)
        mapping = _make_map(ids, integers, manifest["id_map"], None)
        if manifest["rename"]:
            errors.extend(
                f"source ID leak: incomplete map omits {key}" for key in sorted(ids - mapping.keys())
            )
        elif mapping:
            errors.append("date-only transform contains an ID map")
        dates, adjustments = _dates(source_files, manifest["shift_days"], manifest["business_calendar"])
        expected, paths, counts = _render(folder, source_files, mapping, dates, structural)
        for key, value in {
            "date_map": dates,
            "adjustments": adjustments,
            "paths": paths,
            "occurrences": counts,
        }.items():
            if manifest.get(key) != value:
                errors.append(f"manifest {key} mismatch")
        for label, files in [("source", source_files), ("output", output_files)]:
            if manifest[f"{label}_hashes"] != {name: digest(raw) for name, (raw, _) in files.items()}:
                errors.append(f"{label} hashes mismatch")
        if set(expected) != set(output_files):
            errors.append("output file set mismatch")
        for name, node in expected.items():
            if name not in output_files or output_files[name][0] != _encode(name, node):
                errors.append(f"occurrence/date/content mismatch (possible source ID leak): {name}")
        source = {name: node for name, (_, node) in source_files.items()}
        target = {name: node for name, (_, node) in output_files.items()}
        if manifest["rename"]:
            remaining = _Rewrite(mapping, {}, structural)
            for name, node in target.items():
                remaining.text(name)
                remaining.node(node)
            errors.extend(f"source ID leak in output: {key}" for key in sorted(remaining.counts))
        load_world(output)
        reference = target.get("world/SEED.json", {}).get("reference_date")
        reference = (
            reference
            or target["world/world.json"].get("reference_date")
            or target["world/world.json"].get("snapshot_at")
        )
        if not reference:
            raise ValueError("world has no reference date")
        world = check_world(
            target["world/world.json"],
            _states(target),
            target.get("world/identities.json", {}),
            target.get("company.json", {}).get("workers", []),
            reference,
            {k: v for k, v in target.items() if k.startswith("world/materials/") and isinstance(v, str)},
        )
        if not world["ok"]:
            errors.append("world_check failed")
        checks = _grader_checks(source, target, paths, _Rewrite(mapping, dates, structural))
        if any(not check["ok"] for check in checks):
            errors.append("grader resolution changed")
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    return {"ok": not errors, "errors": errors, "world_check": world, "grader_checks": checks}
