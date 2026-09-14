"""Build a local collaborator checkout from explicit source and accepted-world inputs."""

import json
import os
import re
import shutil
import tarfile
import tempfile
import uuid
from collections import Counter
from pathlib import Path

from .storage import digest, now, read, write


def _json(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def _document_bytes(name, value):
    if name.endswith(".jsonl"):
        return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in value).encode()
    return _json(value)


def _map_strings(value, transform):
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, list):
        return [_map_strings(item, transform) for item in value]
    if isinstance(value, dict):
        return {key: _map_strings(item, transform) for key, item in value.items()}
    return value


def portable_metadata(files, *, repository, user_home):
    """Redact machine paths and rebind dependent metadata hashes, recording every change.

    Native state and canonical business records must remain byte-for-byte identical.
    The export records original hashes so the world lineage is still identifiable.
    """
    original = dict(files)
    parsed = {}
    for name, data in files.items():
        if name.endswith(".json"):
            parsed[name] = json.loads(data)
        elif name.endswith(".jsonl"):
            parsed[name] = [json.loads(line) for line in data.splitlines() if line.strip()]

    def redact(text):
        return text.replace(str(repository), "<repository>").replace(str(user_home), "<user-home>")

    transformed = {name: _map_strings(value, redact) for name, value in parsed.items()}
    for _ in range(32):
        mapping = {}
        for name, value in transformed.items():
            current = original[name] if value == parsed[name] else _document_bytes(name, value)
            mapping[digest(original[name])] = digest(current)
            mapping[digest(parsed[name])] = digest(value)
        updated = {
            name: _map_strings(
                _map_strings(parsed[name], redact), lambda s, mapping=mapping: mapping.get(s, s)
            )
            for name in transformed
        }
        if updated == transformed:
            break
        transformed = updated
    else:
        raise ValueError("Metadata hashes did not converge; inspect a possible provenance cycle")
    result = dict(files)
    for name, value in transformed.items():
        if value != parsed[name]:
            result[name] = _document_bytes(name, value)
    for name, data in original.items():
        if (
            name.endswith((".state.json", "/world/world.json", "/EFFECTIVE-WORLD.json"))
            and result[name] != data
        ):
            raise ValueError(f"Personal-path redaction would change business records: {name}")
    changes = {
        name: {"original_sha256": digest(original[name]), "export_sha256": digest(data)}
        for name, data in result.items()
        if data != original[name]
    }
    return result, changes


def company_inputs(folder, trials=None):
    """Close over the accepted native resources and evidence, excluding failed attempts and model logs."""
    folder = Path(folder)
    core, population = read(folder / "world/CORE.json"), read(folder / "world/POPULATION.json")
    runtime = read(folder / "world/acceptance/RUNTIME.json")
    names = {
        "MANIFEST.json",
        "company.json",
        "apps.json",
        "world-spec.json",
        "world/CORE.json",
        "world/POPULATION.json",
        "world/FROZEN.json",
        "world/acceptance/REVIEW.json",
        "world/acceptance/RUNTIME.json",
        population["effective_world"],
    }
    names.update(core["hashes"])
    names.update(population["artifacts"])
    names.update(entry["path"] for entry in population["states"].values())
    names.update(runtime["evidence"])
    for relative in (
        "EXTENSION.json",
        "AMENDMENTS.json",
        "ASSETS.json",
        "CAMPAIGNS.json",
        "transactions/RESOURCES.json",
        "files/RESOURCES.json",
    ):
        if (folder / "world/population" / relative).exists():
            names.add("world/population/" + relative)
    names.update(str(p.relative_to(folder)) for p in (folder / "research").rglob("*") if p.is_file())
    task_files = {
        "assignment.json",
        "workflow.json",
        "assessment.json",
        "WORLD.json",
        "review.json",
        "golden.json",
        "golden.author.json",
        "verifier.py",
        "verifier.json",
        "verifier-calibration.json",
        "verifier-cases.json",
    }
    for task_id in read(folder / "MANIFEST.json")["tasks"]:
        names.update(
            f"tasks/{task_id}/{name}" for name in task_files if (folder / "tasks" / task_id / name).is_file()
        )
        calibration = read(folder / "tasks" / task_id / "verifier-calibration.json")
        for path in (folder / "tasks" / task_id / "verifier-case-targets").glob("*/TARGETS.json"):
            if {row["kind"]: row for row in read(path).get("targets", [])} == calibration.get(
                "failure_targets"
            ):
                names.add(str(path.relative_to(folder)))
        references = folder / "runtime/verifier" / task_id / "references"
        matched = [
            p
            for p in references.glob("*/REFERENCE.json")
            if read(p)["data_hash"] == calibration["reference_hash"]
        ]
        if len(matched) != 1:
            raise ValueError(f"Need one measured reference checkpoint for {task_id}")
        names.update(
            str((matched[0].parent / name).relative_to(folder)) for name in ("REFERENCE.json", "data.json")
        )
        case_dir = folder / "runtime/verifier" / task_id / "calibration"
        required = {row.get("report_hash") for row in calibration["cases"]} - {None}
        for path in case_dir.glob("*/report.json"):
            if digest(read(path)) in required:
                names.update(
                    str((path.parent / name).relative_to(folder)) for name in ("report.json", "RESULT.json")
                )
    for task, kinds in (trials or {}).items():
        for kind, row in kinds.items():
            receipt = row["receipt"]
            receipt_name = "TEACHER.json" if kind == "teacher" else "TRIAL.json"
            names.add(f"runtime/{kind}/{task}/{receipt_name}")
            runtime = folder / receipt["trajectory_runtime"]
            for checkpoint in (folder / "runtime/trials" / task / kind).glob("*/RESULT.json"):
                if read(checkpoint)["result"].get("trajectory_runtime") == receipt["trajectory_runtime"]:
                    names.add(str(checkpoint.relative_to(folder)))
                    if (checkpoint.parent / "INPUT.json").is_file():
                        names.add(str((checkpoint.parent / "INPUT.json").relative_to(folder)))
            attempt = runtime.parent.parent
            names.update(
                str((attempt / name).relative_to(folder))
                for name in (receipt_name, "diagnostics.json", "budget.json")
                if (attempt / name).is_file()
            )
            for path in runtime.rglob("*"):
                relative = path.relative_to(runtime)
                # Keep grade inputs, native writes, delivered files, all action traces and screens.
                # VM disks, staged duplicate materials and raw model histories are unnecessary.
                if not path.is_file() or path.is_symlink():
                    continue
                if (
                    relative.parts[0] in {"grades", "exports", "evidence", "attribution"}
                    or (relative.parts[0] == "vms" and "trace" in relative.parts)
                    or path.name
                    in {
                        "episode.json",
                        "episode-result.json",
                        "CLOCK.json",
                        "NATIVE-FINAL.json",
                        "sessions.json",
                        "INPUT-ABLATION.json",
                    }
                ):
                    names.add(str(path.relative_to(folder)))
    result = {}
    for name in sorted(names):
        source = folder / name
        if (
            source.is_symlink()
            or not source.resolve().is_relative_to(folder.resolve())
            or not source.is_file()
        ):
            raise ValueError(f"Unsafe or missing collaborator input: {name}")
        result[name] = source.read_bytes()
    # The development manifest also indexed temporary files. The portable manifest
    # will index exactly the included company payload after metadata redaction.
    manifest = json.loads(result["MANIFEST.json"])
    manifest["hashes"] = {}
    result["MANIFEST.json"] = _json(manifest)
    return result


def require_team_evidence(root, folder):
    from .world.staged_grading import proof_key
    from .world.task_assessment import require_task_world

    rows = {}
    tasks = read(folder / "MANIFEST.json")["tasks"]
    if not tasks:
        raise ValueError("A collaborator example requires accepted tasks and real team evidence")
    for task in tasks:
        require_task_world(root, folder, task)
        key = proof_key(root, folder, task)
        reports = {}
        for kind in ("teacher", "ordinary", "worker-ablation", "input-ablation"):
            name = "TEACHER.json" if kind == "teacher" else "TRIAL.json"
            report = read(folder / "runtime" / kind / task / name)
            if report.get("simulation_only") or report.get("reset", {}).get("status") != "pass":
                raise ValueError(f"{task}/{kind} lacks real trial and reset evidence")
            if kind in {"teacher", "ordinary"} and report.get("class") != f"{kind}_passed":
                raise ValueError(f"{task}/{kind} has not completed a successful real trial")
            if report.get("class") not in {f"{kind}_passed", f"{kind}_failed"}:
                raise ValueError(f"{task}/{kind} did not reach a complete real trial judgment")
            if kind in {"worker-ablation", "input-ablation"}:
                dependencies = read(folder / "tasks" / task / "assessment.json")["dependencies"]
                roster = {row["worker_id"] for row in dependencies}
                actual = set(report["trajectory_summary"]["episode"]["workers"])
                if kind == "worker-ablation":
                    removed = report.get("unavailable_worker")
                    if removed not in roster or actual != roster - {removed}:
                        raise ValueError(f"{task}: worker ablation must remove exactly its named worker")
                else:
                    withheld = report.get("withheld_inputs") or {}
                    worker = withheld.get("worker_id")
                    dependency = next((row for row in dependencies if row["worker_id"] == worker), None)
                    if (
                        actual != roster
                        or dependency is None
                        or withheld.get("references") != dependency["inputs"]
                    ):
                        raise ValueError(f"{task}: input ablation does not match the accepted worker inputs")
                    probe = read(folder / report["trajectory_runtime"] / "INPUT-ABLATION.json")
                    if probe.get("status") != "view_probe_passed" or any(
                        probe.get(key) != value for key, value in withheld.items()
                    ):
                        raise ValueError(f"{task}: missing measured input-ablation view check")
            grade_path = report.get("grade_report") or str(
                Path(report["trajectory_runtime"]) / "grades" / task / "staged/report.json"
            )
            if not (folder / grade_path).resolve().is_relative_to(folder.resolve()):
                raise ValueError("Trial grade must stay within the company folder")
            grade = read(folder / grade_path)
            if grade.get("proof_key") != key or not grade.get("complete"):
                raise ValueError(f"{task}/{kind} has incomplete or stale grading")
            if kind in {"teacher", "ordinary"} and not grade.get("passed"):
                raise ValueError(f"{task}/{kind} has not passed")
            reports[kind] = {"receipt": report, "grade": grade}
        if reports["worker-ablation"]["grade"]["score"] >= reports["ordinary"]["grade"]["score"]:
            raise ValueError(f"{task}: worker ablation has not demonstrated weaker business results")
        rows[task] = reports
    return rows


def inspection_evidence(root, folder):
    """Require the accepted world and calibration, retaining real trial failures honestly."""
    from .world.staged_grading import proof_key
    from .world.task_assessment import require_task_world

    root, folder = Path(root).resolve(), Path(folder).resolve()
    tasks = read(folder / "MANIFEST.json")["tasks"]
    if not tasks:
        raise ValueError("Inspection requires accepted tasks")
    rows = {}
    for task in tasks:
        require_task_world(root, folder, task)
        calibration = read(folder / "tasks" / task / "verifier-calibration.json")
        key = proof_key(root, folder, task)
        if (
            calibration.get("status") != "accepted"
            or calibration.get("proof_key") != key
            or calibration.get("golden_hash") != digest(read(folder / "tasks" / task / "golden.json"))
        ):
            raise ValueError(f"{task}: inspection requires current accepted calibration")
        rows[task] = {}
        for kind in ("teacher", "ordinary", "worker-ablation", "input-ablation"):
            name = "TEACHER.json" if kind == "teacher" else "TRIAL.json"
            path = folder / "runtime" / kind / task / name
            if not path.exists():
                continue
            receipt = read(path)
            if receipt.get("simulation_only") or receipt.get("reset", {}).get("status") != "pass":
                raise ValueError(f"{task}/{kind}: inspection requires a real, reset trial")
            runtime = folder / receipt["trajectory_runtime"]
            grade_path = folder / (
                receipt.get("grade_report")
                or str(runtime.relative_to(folder) / "grades" / task / "staged/report.json")
            )
            if not runtime.resolve().is_relative_to(folder) or not grade_path.resolve().is_relative_to(
                folder
            ):
                raise ValueError("Inspection trial evidence must stay within the company folder")
            grade = read(grade_path) if grade_path.exists() else None
            if grade and grade.get("proof_key") != key:
                raise ValueError(f"{task}/{kind}: retained grading is stale")
            rows[task][kind] = {"receipt": receipt, "grade": grade}
    return rows


def inspection_attempts(folder):
    """Keep earlier receipts and grades without duplicating every development VM trace."""
    folder = Path(folder)
    files, rows = {}, []
    for checkpoint in sorted((folder / "runtime/trials").glob("*/*/*/RESULT.json")):
        original = read(checkpoint)["result"]
        runtime = folder / original["trajectory_runtime"]
        if not runtime.resolve().is_relative_to(folder.resolve()):
            raise ValueError("Historical trial must stay within the company folder")
        paths = {checkpoint, checkpoint.parent / "INPUT.json"}
        paths.update((runtime / "grades").glob("*/staged/report.json"))
        paths.update((runtime / "grades").glob("*/regraded/*/report.json"))
        regrades = sorted((runtime / "grades").glob("*/regraded/*/RESULT.json"))
        paths.update(regrades)
        for path in paths:
            if path.is_file() and not path.is_symlink():
                files[str(path.relative_to(folder))] = path.read_bytes()
        rows.append(
            {
                "task": checkpoint.parents[2].name,
                "kind": checkpoint.parents[1].name,
                "attempt": int(checkpoint.parent.name),
                "original_class": original["class"],
                "checkpoint": str(checkpoint.relative_to(folder)),
                "regraded_receipts": [str(path.relative_to(folder)) for path in regrades],
            }
        )
    return files, rows


def inspection_index(relative, trials, attempts):
    lines = [
        "# Inspect the pipeline and measured outputs",
        "",
        (
            "This is an inspection handoff of an accepted world and calibrated tasks. "
            "This mode does not require successful ordinary teams or ablations; the retained outcomes "
            "are listed below. No release certification is implied."
        ),
        "",
        (
            f"Start with the [pilot results]({relative}/../RESULTS.md), "
            "[setup instructions](docs/COLLABORATOR.md), and [pipeline plan](docs/PILOT.md)."
        ),
        "",
        "## Tasks and latest real trials",
        "",
    ]
    for task, kinds in trials.items():
        task_dir = f"{relative}/tasks/{task}"
        lines.extend(
            [
                f"### {task}",
                "",
                (
                    f"[Assignment]({task_dir}/assignment.json) · [Assessment]({task_dir}/assessment.json) · "
                    f"[Reference]({task_dir}/golden.json) · [Calibration]({task_dir}/verifier-calibration.json)"
                ),
                "",
            ]
        )
        for kind, row in kinds.items():
            receipt = row["receipt"]
            runtime = f"{relative}/{receipt['trajectory_runtime']}"
            scored = receipt["class"] in {f"{kind}_passed", f"{kind}_failed"}
            score = str(receipt["score"]) if scored else "unscored"
            grade_path = receipt.get("grade_report") or str(
                Path(receipt["trajectory_runtime"]) / "grades" / task / "staged/report.json"
            )
            lines.append(
                f"- **{kind}: {receipt['class']}**, business score {score}. "
                f"[Native final state]({runtime}/NATIVE-FINAL.json) · "
                f"[Delivered files]({runtime}/exports/{task}) · [Desktop traces]({runtime}/vms)"
                + (f" · [Grade]({relative}/{grade_path})" if row["grade"] else "")
            )
        lines.append("")
    lines.extend(
        [
            "## Earlier attempts and scope",
            "",
            (
                f"[All {len(attempts)} retained attempts]({relative}/proofs/TRIAL-ATTEMPTS.json) "
                "include original receipts and available repaired grades. Complete state, delivered files "
                "and screenshots accompany the latest teacher per task and latest ordinary trial. "
                "Earlier development traces, VM disks, model prompts, account files and build caches are omitted."
            ),
            "",
            (
                "Private assessments, references and verifiers are for collaborators inspecting the benchmark; "
                "they must remain off worker desktops. Tasks from this company share one world lineage."
            ),
            "",
            (
                "`COLLABORATOR-VERIFIED.json`, when present, states the actual clean-extraction replay/reset "
                "result. An inspection verification does not promote the unfinished team gates."
            ),
        ]
    )
    return ("\n".join(lines) + "\n").encode()


def source_inputs(root):
    """Source plus the small offline fixtures the shipped tests actually read."""
    root = Path(root)
    files = {}
    directories = (
        "src",
        "examples/sanmar",
        "tests",
        "scripts",
        "catalogs",
        ".agents/skills",
        "companies/cort/world.v1-compact",
        "companies/cort/tasks",
        "experiments/hub-smoke/states",
        "experiments/interact/drift-20260909",
        "experiments/interact/drift-20260910",
    )
    for directory in directories:
        for path in (root / directory).rglob("*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and not {
                    "__pycache__",
                    ".pytest_cache",
                    "calls",
                    ".ruff_cache",
                }.intersection(path.parts)
            ):
                files[str(path.relative_to(root))] = path.read_bytes()
    for name in (
        "pyproject.toml",
        "uv.lock",
        ".gitignore",
        "config.toml",
        "configs/pilot-sanmar.toml",
        "data/person_names.json",
        "experiments/app-audit/probes.json",
        "companies/cort/company.json",
        "companies/cort/apps.json",
        "companies/cort/MANIFEST.json",
        "companies/cort/review.json",
        "docs/README.md",
        "docs/ARCHITECTURE.md",
        "docs/PILOT.md",
        "docs/END-TO-END-PLAN.md",
        "docs/SANDBOX-TROUBLESHOOTING.md",
        "docs/COLLABORATOR.md",
        "docs/GATE4-RUNBOOK.md",
        "docs/PIPELINE-REPAIRS.md",
        "README.md",
        "CONTRIBUTING.md",
    ):
        files[name] = (root / name).read_bytes()
    # Keep the default test config's behavior while making its machine paths portable.
    files["config.toml"] = files["config.toml"].replace(str(Path.home()).encode(), b"~")
    if (root / "LICENSE").is_file():
        files["LICENSE"] = (root / "LICENSE").read_bytes()
    return files


def measured_trial_usage(folder, trials):
    """Retain measured counters without copying prompts, profile paths or account files."""
    measured = {}
    for task, kinds in trials.items():
        measured[task] = {}
        for kind, row in kinds.items():
            receipt = row["receipt"]
            runtime = Path(folder) / receipt["trajectory_runtime"]
            paths = list((runtime / "vms").glob("*/policy/*/calls/*/attempt-*/receipt.json"))
            paths += list((runtime.parent.parent / "grading-models").glob("calls/*/attempt-*/receipt.json"))
            attempts = [read(path) for path in paths]
            usage = Counter()
            for attempt in attempts:
                usage.update(
                    {
                        key: value
                        for key, value in (attempt.get("usage") or {}).items()
                        if isinstance(value, (int, float))
                    }
                )
            measured[task][kind] = {
                "worker_calls_reserved": receipt["model_calls_used"],
                "provider_attempts_with_receipts": len(attempts),
                "provider_statuses": dict(Counter(attempt.get("status", "unknown") for attempt in attempts)),
                "recorded_usage": dict(usage),
                "usage_complete": bool(attempts) and all(attempt.get("usage") for attempt in attempts),
                "cost_usd": sum(attempt["cost_usd"] for attempt in attempts)
                if attempts and all(isinstance(attempt.get("cost_usd"), (int, float)) for attempt in attempts)
                else None,
                "wall_seconds": receipt["elapsed_seconds"],
                "note": "Incomplete or cancelled provider attempts may have no token counters. Recorded usage is not an estimate for those missing counters.",
            }
    return measured


def write_payload(output, files):
    """Share identical immutable screenshots, keeping every evidence path and byte."""
    images, saved = {}, 0
    for name, data in files.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        screenshot = name.endswith(".png") and "/runtime/" in name and "/trace/" in name
        key = digest(data) if screenshot else None
        if key is not None and key in images:
            os.link(images[key], target)
            saved += len(data)
        else:
            target.write_bytes(data)
            if key is not None:
                images[key] = target
    return {"duplicate_screenshot_bytes_shared": saved, "unique_screenshots": len(images)}


def build_checkout(root, folder, output, *, inspection=False):
    """Prepare files locally; no publishing, overwrites, repository cleanup or credential copying."""
    root, folder, output = Path(root).resolve(), Path(folder).resolve(), Path(output).resolve()
    trials = inspection_evidence(root, folder) if inspection else require_team_evidence(root, folder)
    if output.exists():
        raise FileExistsError(output)
    archive = output.with_suffix(".tar.gz")
    if archive.exists():
        raise FileExistsError(archive)
    files = source_inputs(root)
    relative = folder.relative_to(root).as_posix()
    files.update({f"{relative}/{name}": data for name, data in company_inputs(folder, trials).items()})
    if inspection:
        retained, attempts = inspection_attempts(folder)
        files.update({f"{relative}/{name}": data for name, data in retained.items()})
        files[f"{relative}/proofs/TRIAL-ATTEMPTS.json"] = _json(attempts)
        files["INSPECT.md"] = inspection_index(relative, trials, attempts)
    files[f"{relative}/proofs/TEAM-TRIALS.json"] = _json(trials)
    files[f"{relative}/proofs/MODEL-USAGE.json"] = _json(measured_trial_usage(folder, trials))
    files[f"{relative}/proofs/OMITTED-DEVELOPMENT-FILES.json"] = _json(
        {
            task: {
                kind: {
                    "reason": "VM disks, build inputs, raw model histories and incidental logs are not required to inspect the retained grade inputs, actor writes, desktop files and action traces.",
                    "paths": [
                        path
                        for path in row["receipt"].get("evidence_refs", [])
                        if f"{relative}/{path}" not in files
                    ],
                }
                for kind, row in kinds.items()
            }
            for task, kinds in trials.items()
        }
    )
    for path in (folder.parent / "stages").glob("*/STAGE.*"):
        if path.is_file():
            files[str(path.relative_to(root))] = path.read_bytes()
    for name in (
        "STAGE.md",
        "STAGE4-STATUS.md",
        "stage4-general-checks.json",
        "PIPELINE-STATUS.json",
        "RESULTS.md",
        "RESUME.md",
        "CLEANUP-PLAN.json",
        "PRESERVATION-CHECK.json",
    ):
        path = folder.parent / name
        if path.is_file():
            files[str(path.relative_to(root))] = path.read_bytes()
    files, changes = portable_metadata(files, repository=root, user_home=Path.home())
    for name, content in files.items():
        if name.endswith((".json", ".jsonl", ".toml", ".md", ".py", ".txt", ".log", ".html")) and (
            str(Path.home()).encode() in content
            or re.search(rb"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----", content)
        ):
            raise ValueError(f"Machine-specific or private content remains: {name}")
    manifest_name = f"{relative}/MANIFEST.json"
    manifest = json.loads(files[manifest_name])
    manifest["hashes"] = {
        name[len(relative) + 1 :]: digest(data)
        for name, data in files.items()
        if name.startswith(relative + "/") and name != manifest_name
    }
    files[manifest_name] = _json(manifest)
    storage = write_payload(output, files)
    record = {
        "at": now(),
        "status": "prepared_not_published",
        "mode": "inspection" if inspection else "validated",
        "certified": False,
        "company": relative,
        "origin_frozen_hash": digest(read(folder / "world/FROZEN.json")),
        "files": {name: digest(data) for name, data in sorted(files.items())},
        "metadata_redactions": changes,
        "storage": storage,
        "note": "Original world lineage and business bytes are preserved. Only machine paths and dependent metadata hashes are rebound. Clean-extraction runtime verification is still required.",
    }
    write(output / "COLLABORATOR.json", record)
    with tarfile.open(archive, "w:gz") as stream:
        stream.add(output, arcname=output.name)
    return {"checkout": str(output), "archive": str(archive), "files": len(files), "published": False}


def verify_checkout(root, *, work):
    """Verify the extracted payload and perform fresh native reference replay/reset; no models."""
    from .world.python_verifier import run_verifier
    from .world.staged_calibration import reference_replay

    root, work = Path(root).resolve(), Path(work).resolve()
    record = read(root / "COLLABORATOR.json")
    for name, expected in record["files"].items():
        path = root / name
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(root)
            or digest(path.read_bytes()) != expected
        ):
            raise ValueError(f"Collaborator payload changed: {name}")
    folder = root / record["company"]
    mode = record.get("mode", "validated")
    if mode == "inspection":
        inspection_evidence(root, folder)
    elif mode == "validated":
        require_team_evidence(root, folder)
    else:
        raise ValueError(f"Unknown collaborator package mode: {mode}")
    work.mkdir(parents=True, exist_ok=True)
    measured = {}
    proof_root = root / "verification" / uuid.uuid4().hex
    proof_files = {}
    with tempfile.TemporaryDirectory(prefix="collaborator-proof-", dir=work) as temporary:
        replay = Path(temporary) / "company"
        shutil.copytree(
            folder,
            replay,
            ignore=lambda directory, names: (
                {"runtime", "proofs"}.intersection(names) if Path(directory) == folder else set()
            ),
        )
        for task in read(folder / "MANIFEST.json")["tasks"]:
            data = reference_replay(root, replay, task, work / "hub-cache")
            expected = [
                i + 1
                for i, criterion in enumerate(
                    read(replay / "tasks" / task / "workflow.json")["success_criteria"]
                )
                if criterion["method"] == "state"
            ]
            mechanics = run_verifier(replay / "tasks" / task / "verifier.py", data, expected)
            if not all(check["passed"] for check in mechanics["checks"].values()):
                raise ValueError(f"Extracted reference verification failed: {task}")
            references = list((replay / "runtime/verifier" / task / "references").glob("*/REFERENCE.json"))
            if len(references) != 1:
                raise ValueError(f"Fresh extraction proof needs exactly one measured reference: {task}")
            for name in ("REFERENCE.json", "data.json"):
                destination = proof_root / task / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(references[0].parent / name, destination)
                proof_files[str(destination.relative_to(root))] = digest(destination.read_bytes())
            measured[task] = {
                "status": "replayed_verified_and_reset",
                "data_hash": digest(data),
                "mechanical": mechanics,
            }
    result = {
        "at": now(),
        "status": "inspection_verified_not_published" if mode == "inspection" else "verified_not_published",
        "mode": mode,
        "certified": mode == "validated",
        "payload_hash": digest(record["files"]),
        "model_calls": 0,
        "tasks": measured,
        "proof_files": proof_files,
        "scope": (
            "Inspection payload, accepted world/tasks/calibration and retained trial records checked; "
            "fresh native reference replay, isolated mechanics and reset passed. "
            "Ordinary-team and ablation acceptance is not established. No models or desktop trials were run."
            if mode == "inspection"
            else "Payload, frozen world and saved team evidence verified; fresh native reference replay and reset passed. Semantic judgments and desktop teams are retained evidence, not rerun here."
        ),
    }
    write(root / "COLLABORATOR-VERIFIED.json", result)
    return result
