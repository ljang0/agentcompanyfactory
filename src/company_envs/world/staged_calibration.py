"""Resume reference replay and independently judged positive/negative calibration cases."""

import ast
import inspect
import json
import re
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from company_envs.config import load_config
from company_envs.storage import digest, now, read, write

from .golden import author_golden, initial_states, merge_patch
from .grader_author import _models
from .hub_app import HubClient
from .hub_mail import browser_mail, canonical_mail
from .python_verifier import VerifierUnavailable, run_verifier
from .staged_grading import evaluate, proof_key
from .task_assessment import assessment_sources, require_task_world
from .teacher import _snapshot
from .trial_evidence import ObservedWorld, read_events

KINDS = {
    "partial",
    "alternative",
    "wrong_record",
    "wrong_total",
    "missing_authority",
    "irrelevant_contribution",
    "forged_claim",
    "missing_evidence",
}


def repair_reference(root, folder, task_id, findings):
    """Preserve the authored reference and ask its independent author for a bounded repair."""
    task = Path(folder) / "tasks" / task_id
    previous = read(task / "golden.json")
    key = digest({"previous": previous, "findings": findings})
    archive = task / "golden-repairs" / key
    if (archive / "RESULT.json").exists():
        result = read(archive / "RESULT.json")
        if result["trajectory_hash"] != digest(read(task / "golden.json")):
            raise ValueError("A different reference replaced the saved repair")
        return result
    write(archive / "golden.json", previous)
    write(archive / "golden.author.json", read(task / "golden.author.json"))
    write(archive / "FINDINGS.json", findings)
    repaired = author_golden(
        root,
        folder,
        task_id,
        feedback={
            "previous_reference": previous,
            "findings": findings,
            "scope": "Repair only these defects, preserving valid business decisions. The first manager delegation and each consequential handoff must be native app writes: commentary message steps are not replayed and earn no contribution credit. Each required consumer must perform relevant work after receiving the producer's output. Do not add ceremonial acknowledgements or invent external approvals. No verifier code is supplied.",
        },
    )
    result = {
        "at": now(),
        "prior_hash": digest(previous),
        "trajectory_hash": digest(repaired),
        "findings": findings,
    }
    write(archive / "RESULT.json", result)
    return result


class FixtureEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    value_json: str


class FixtureCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    expected: str
    rationale: str
    edits: list[FixtureEdit]
    remove_worker_events: list[str]


class FixtureDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cases: list[FixtureCase]


class FailureTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    criteria: list[int]
    workers: list[str]
    reason: str


class FailureTargets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    targets: list[FailureTarget]


class FixtureTraceRepair(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edits: list[FixtureEdit]
    explanation: str


def repair_alternative_trace(root, folder, task_id, reference_checkpoint):
    """Repair a counterfactual fixture's trace, preserving its independently chosen business alternative."""
    task = Path(folder) / "tasks" / task_id
    saved = read(task / "verifier-cases.json")
    reference = read(Path(reference_checkpoint) / "data.json")
    alternative = next(c for c in saved["cases"] if c["kind"] == "alternative")
    changed = apply_case(reference, alternative)
    payload = {
        "assessment": read(task / "assessment.json"),
        "alternative": alternative,
        "final_changes": changed_pointers(reference["final"], changed["final"]),
        "reference_events": reference["events"],
    }
    key = digest(payload)
    archive = task / "fixture-trace-repairs" / key
    write(archive / "verifier-cases.json", saved)
    models = _models(root, task / "fixture_trace_calls", "task_calibration_trace_repair")
    draft, receipt = models.call(
        "task_calibration_trace_repair",
        """Repair only the simulated actor trace for an independently authored valid alternative fixture. Its final app records adopt the alternative, but captured reference events still show the original choice, creating a contradictory approval/consumption trace. Return ADDITIONAL JSON Pointer replacement edits to the events' business record before/after values so the counterfactual depicts the same workers actually approving, incorporating and implementing the ALTERNATIVE in their original order. Never change actors, sequence numbers, timestamps, final state, inputs or the alternative choice. All paths must be /events/N/changes/RECORD_REFERENCE/before or /after, optionally followed by record fields. Escape map keys using JSON Pointer rules. Preserve factual analyses and unrelated work. Keep initial unknown approvals unknown; the manager must choose the alternative before specialists apply it. This is synthetic calibration evidence, never a real worker trial. Explain the changes.\n"""
        + json.dumps(payload),
        FixtureTraceRepair,
    )
    edits = [e.model_dump() for e in draft.edits]
    if not edits or any(
        not re.match(r"^/events/[0-9]+/changes/[^/]+/(?:before|after)(?:/|$)", e["path"]) for e in edits
    ):
        raise ValueError("Trace repair may only change existing event business content")
    alternative["edits"].extend(edits)
    apply_case(reference, alternative)
    saved["trace_repair"] = {
        "at": now(),
        "receipt": receipt,
        "explanation": draft.explanation,
        "prior_hash": digest(read(archive / "verifier-cases.json")),
    }
    write(task / "verifier-cases.json", saved)
    write(archive / "RESULT.json", saved["trace_repair"])
    return saved["trace_repair"]


def failure_targets(root, task, fixtures):
    """Identify broken requirements without looking at verifier or judge output."""
    assessment = read(task / "assessment.json")
    payload = {
        "assessment": assessment,
        "cases": [
            {"kind": c["kind"], "rationale": c["rationale"], "removed_workers": c["remove_worker_events"]}
            for c in fixtures
        ],
    }
    key = digest(payload)
    path = task / "verifier-case-targets" / key / "TARGETS.json"
    if path.exists():
        return read(path)["targets"]
    models = _models(root, task / "fixture_target_calls", "task_calibration_targets")
    draft, receipt = models.call(
        "task_calibration_targets",
        """Identify exactly which accepted assessment requirements each negative calibration case breaks. Use the supplied case rationale and assessment, never verifier output. Return one target per case kind. alternative and missing_evidence have empty criteria/workers lists. For every other case identify at least one necessarily violated one-based criterion number or required worker dependency. A removed relevant contributor targets that worker's dependency, not otherwise complete business artifacts. A wrong total targets the criterion requiring accurate comparison/calculations. Do not target requirements the case still satisfies. Explain briefly.\n"""
        + json.dumps(payload),
        FailureTargets,
    )
    targets = [t.model_dump() for t in draft.targets]
    if len(targets) != len(KINDS) or {t["kind"] for t in targets} != KINDS:
        raise ValueError("Failure targets must cover every case exactly once")
    criteria = {c["criterion"] for c in assessment["criteria"]}
    workers = {d["worker_id"] for d in assessment["dependencies"]}
    for target in targets:
        if set(target["criteria"]) - criteria or set(target["workers"]) - workers:
            raise ValueError("A failure target names an unknown requirement")
        positive = target["kind"] in {"alternative", "missing_evidence"}
        if positive == bool(target["criteria"] or target["workers"]):
            raise ValueError("Positive/evidence-gap cases need no failure target; other cases need one")
    write(path, {"input_hash": key, "targets": targets, "receipt": receipt})
    return targets


def apply_case(reference, case):
    value = deepcopy(reference)
    for edit in case["edits"]:
        tokens = edit["path"].split("/")
        if len(tokens) < 2 or tokens[0] or tokens[1] not in {"final", "files", "events"}:
            raise ValueError("Fixture changes must stay in final state, files or events")
        parts = [p.replace("~1", "/").replace("~0", "~") for p in tokens[1:]]
        target = value
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        target[int(parts[-1]) if isinstance(target, list) else parts[-1]] = json.loads(edit["value_json"])
    removed = set(case["remove_worker_events"])
    if removed - {e["worker_id"] for e in reference["events"]}:
        raise ValueError("Fixture removes a worker absent from the reference evidence")
    value["events"] = [e for e in value["events"] if e["worker_id"] not in removed]
    if value == reference:
        raise ValueError("Calibration case must change the reference evidence")
    return value


def reference_replay(root, folder, task_id, work):
    root, folder = Path(root).resolve(), Path(folder).resolve()
    require_task_world(root, folder, task_id)
    task = folder / "tasks" / task_id
    key = digest(
        {
            "golden": read(task / "golden.json"),
            "frozen": read(folder / "world/FROZEN.json"),
            "implementation": {
                f.__name__: ast.dump(ast.parse(inspect.getsource(f)))
                for f in (reference_replay, _snapshot, ObservedWorld)
            },
        }
    )
    directory = folder / "runtime/verifier" / task_id / "references" / key
    output = directory / "REFERENCE.json"
    if output.exists():
        report = read(output)
        data = read(directory / "data.json")
        if report["data_hash"] != digest(data):
            raise ValueError("Reference replay evidence changed")
        return data
    attempts = directory / "attempts"
    number = len(list(attempts.glob("*"))) + 1
    if number > 3:
        raise ValueError(f"Three incomplete reference attempts retained for diagnosis: {attempts}")
    company = attempts / f"{number:02d}" / "company"
    _snapshot(folder, company, task_id)
    config = load_config(root)
    world = ObservedWorld(
        company, Path(config["design"]["hub_root"]), Path(work), root=root, episode=f"reference-{key[:12]}"
    )
    try:
        world.start()
        baseline = initial_states(company)
        for number, step in enumerate(read(task / "golden.json"), 1):
            if step["action"] == "message":
                continue
            url = world.endpoints[step["app_id"]]["workers"][step["worker_id"]]
            parsed = urlsplit(url)
            proxy = HubClient(parsed._replace(path="", query="", fragment="").geturl())
            before = proxy.current(world.sid)["stored_state"]
            if step["app_id"] == "gmail_mock":
                before = canonical_mail(before)
            proposed = merge_patch(before, step["state_patch"])
            if step["app_id"] == "gmail_mock":
                proposed = browser_mail(proposed)
            proxy.update(world.sid, proposed)
            write(directory / "PROGRESS.json", {"last_step": number, "at": now()})
        final = {app: client.inspect(world.sid) for app, client in world.clients.items()}
        if any(snapshot["initial_state"] != baseline[app] for app, snapshot in final.items()):
            raise ValueError("Reference replay changed an initial baseline")
        data = {
            "initial": baseline,
            "final": {app: snapshot["current_state"] for app, snapshot in final.items()},
            "files": {},
            "events": read_events(company, world.sid),
        }
        write(directory / "data.json", data)
        world.reset()
        if any(
            client.current(world.sid)["stored_state"] != baseline[app]
            for app, client in world.clients.items()
        ):
            raise ValueError("Reference reset differs from the original baseline")
        write(
            output,
            {
                "at": now(),
                "status": "replayed_and_reset",
                "data_hash": digest(data),
                "golden_hash": digest(read(task / "golden.json")),
                "scope": "reference replay through worker proxies; not a desktop team solve",
            },
        )
        return data
    finally:
        world.stop()


FIXTURE_PROMPT = """Author calibration counterexamples and one valid ALTERNATIVE business outcome for an
accepted task. This is a separate fixture-author session; do not author or change its verifier.
The supplied reference was executed through worker proxies. Its changed records, native pointers,
input policies and public assessment are supplied. Return exactly one case of each required kind.
Each case is a copy of the entire reference, then edits replace the specified JSON Pointer values.
Paths start /final/<app>/<collection>/..., /files/<worker-relative-path> or /events. Use native list
indices only from the provided reference pointer list. Escape ~ as ~0 and / as ~1 in map keys.
value_json is a JSON-encoded replacement, not code. Do not change initial data.
remove_worker_events can remove the trusted contribution evidence for specified workers.
Every case needs an explicit, grounded rationale and expected pass/fail/unavailable.
The alternative must change a meaningful allowed business decision while preserving every required
obligation and evidence of collaboration. Update dependent prose/settings together; cosmetic
paraphrase alone is insufficient. It must pass. partial, wrong_record, wrong_total,
missing_authority, irrelevant_contribution and forged_claim must fail for their specific defect.
Irrelevant work or self-reported approvals must not substitute for actual required contributions.
For missing_evidence replace /final with {} and expect unavailable, because missing snapshots
are an evidence gap, not an ordinary unsuccessful task. Avoid requiring a policy the task lacks.
Other cases must keep complete app snapshots and should remain native-schema shaped.
"""


def changed_pointers(initial, final, path="/final"):
    rows = {}
    if isinstance(initial, dict) and isinstance(final, dict):
        for key, value in final.items():
            pointer = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
            if key not in initial:
                rows[pointer] = value
            elif initial[key] != value:
                rows.update(changed_pointers(initial[key], value, pointer))
    elif isinstance(initial, list) and isinstance(final, list):
        for i, value in enumerate(final):
            if i >= len(initial):
                rows[f"{path}/{i}"] = value
            elif initial[i] != value:
                rows.update(changed_pointers(initial[i], value, f"{path}/{i}"))
    elif initial != final:
        rows[path] = final
    return rows


def author_cases(root, folder, task_id, reference, *, models=None):
    task = Path(folder) / "tasks" / task_id
    workflow = read(task / "workflow.json")
    key = digest({"reference": reference, "assessment": workflow["assessment"]})
    path = task / "verifier-cases.json"
    if path.exists():
        saved = read(path)
        if saved["input_hash"] == key:
            return saved["cases"]
        write(task / "verifier-case-history" / saved["input_hash"] / "verifier-cases.json", saved)
    payload = {
        "public_brief": read(task / "assignment.json"),
        "assessment": workflow["assessment"],
        "sources": assessment_sources(workflow["assessment"], reference["initial"]),
        "reference_changes": changed_pointers(reference["initial"], reference["final"]),
        "reference_events": reference["events"],
        "required_kinds": sorted(KINDS),
    }
    models = models or _models(root, task / "fixture_calls", "task_calibration_fixtures")
    feedback = None
    for attempt in range(3):
        draft, receipt = models.call(
            "task_calibration_fixtures",
            FIXTURE_PROMPT + "\n" + json.dumps({**payload, "feedback": feedback}),
            FixtureDraft,
        )
        try:
            cases = [case.model_dump() for case in draft.cases]
            if len(cases) != len(KINDS) or {c["kind"] for c in cases} != KINDS:
                raise ValueError("Return every required case kind once")
            for case in cases:
                expected = (
                    "pass"
                    if case["kind"] == "alternative"
                    else "unavailable"
                    if case["kind"] == "missing_evidence"
                    else "fail"
                )
                if case["expected"] != expected:
                    raise ValueError(f"Incorrect expectation for {case['kind']}")
                apply_case(reference, case)
            break
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            feedback = str(exc)
            write(
                task / f"verifier-cases-invalid-{attempt}.json",
                {"draft": draft.model_dump(), "error": feedback, "receipt": receipt},
            )
    else:
        raise ValueError(f"Fixture author could not meet the contract: {feedback}")
    write(path, {"input_hash": key, "cases": cases, "receipt": receipt})
    return cases


def classify_result(result, name, target=None):
    observed = "pass" if result["passed"] else "fail" if result["complete"] else "unavailable"
    if name == "noop" and any(r["status"] != "fail" for r in result["semantic_checks"]):
        return (
            "semantic_false_positive"
            if any(r["status"] == "pass" for r in result["semantic_checks"])
            else "unavailable"
        )
    if target:
        intended = [r["status"] for r in result["semantic_checks"] if r["criterion"] in target["criteria"]]
        intended += [r["status"] for r in result["dependencies"] if r["worker_id"] in target["workers"]]
        if len(intended) != len(target["criteria"]) + len(target["workers"]):
            return "unavailable"
        if any(status == "pass" for status in intended):
            return "semantic_false_positive"
        if any(status != "fail" for status in intended):
            return "unavailable"
    return observed


def calibrate_verifier(root, folder, task_id, work, *, reference_checkpoint=None):
    root, folder = Path(root).resolve(), Path(folder).resolve()
    task = folder / "tasks" / task_id
    key = proof_key(root, folder, task_id)
    if reference_checkpoint is None:
        reference = reference_replay(root, folder, task_id, work)
    else:
        checkpoint = Path(reference_checkpoint).resolve()
        if not checkpoint.is_relative_to(folder / "runtime/verifier" / task_id / "references"):
            raise ValueError("Reference checkpoint must belong to this task's retained replays")
        receipt = read(checkpoint / "REFERENCE.json")
        reference = read(checkpoint / "data.json")
        if (
            receipt.get("status") != "replayed_and_reset"
            or receipt.get("data_hash") != digest(reference)
            or receipt.get("golden_hash") != digest(read(task / "golden.json"))
            or reference["initial"] != initial_states(folder)
        ):
            raise ValueError("Reference checkpoint is stale, incomplete or changed")
    workflow = read(task / "workflow.json")
    expected = [i + 1 for i, c in enumerate(workflow["success_criteria"]) if c["method"] == "state"]
    basic = run_verifier(task / "verifier.py", reference, expected)
    write(task / "verifier-reference-mechanics.json", basic)
    if not all(c["passed"] for c in basic["checks"].values()):
        raise ValueError(f"Reference mechanics need diagnosis: {basic['checks']}")
    fixtures = []
    targets = {}
    cases = [
        ("noop", "fail", {**reference, "final": reference["initial"], "files": {}, "events": []}),
        ("reference", "pass", reference),
    ]
    directory = folder / "runtime/verifier" / task_id / "calibration"
    models = _models(root, directory / "judge_calls", "task_judgment")
    rows = []
    for name, expected_result, data in cases:
        mode_rows = []
        for mode in ("expanded", "encoded"):
            case_key = digest(
                {
                    "proof": key,
                    "evidence_mode": mode,
                    "data": data,
                    "name": name,
                    "expected": expected_result,
                    "failure_target": targets.get(name),
                }
            )
            path = directory / case_key / "RESULT.json"
            if path.exists():
                row = read(path)
                if "report_hash" in row and row["report_hash"] != digest(read(path.parent / "report.json")):
                    raise ValueError("Calibration evidence changed after it was recorded")
            else:
                try:
                    result = evaluate(
                        root,
                        folder,
                        task_id,
                        data,
                        models=models,
                        votes=3 if expected_result == "pass" else 1,
                        judge_unchanged=name == "noop",
                        force_encoding=mode == "encoded",
                    )
                    observed = classify_result(result, name, targets.get(name))
                    write(path.parent / "report.json", result)
                    row = {
                        "case": name,
                        "evidence_mode": mode,
                        "expected": expected_result,
                        "observed": observed,
                        "ok": observed == expected_result,
                        "report_hash": digest(result),
                    }
                except VerifierUnavailable as exc:
                    row = {
                        "case": name,
                        "evidence_mode": mode,
                        "expected": expected_result,
                        "observed": "unavailable",
                        "ok": expected_result == "unavailable",
                        "error": str(exc),
                    }
                write(path, row)
            rows.append(row)
            print(
                f"Calibration {task_id}/{name}/{mode}: {row['observed']} (expected {expected_result})",
                flush=True,
            )
            mode_rows.append(row)
        if name in {"noop", "reference"} and not all(r["ok"] for r in mode_rows):
            break
        if name == "reference":
            fixtures = author_cases(root, folder, task_id, reference)
            targets = {t["kind"]: t for t in failure_targets(root, task, fixtures)}
            cases.extend((c["kind"], c["expected"], apply_case(reference, c)) for c in fixtures)
    report = {
        "at": now(),
        "status": "accepted"
        if len(rows) == (len(KINDS) + 2) * 2 and all(r["ok"] for r in rows)
        else "needs_diagnosis",
        "proof_key": key,
        "cases": rows,
        "reference_hash": digest(reference),
        "golden_hash": digest(read(task / "golden.json")),
        "fixtures_hash": digest(fixtures),
        "failure_targets": targets,
        "scope": "isolated mechanics and semantic calibration in expanded and losslessly encoded evidence modes; no real-team necessity proof",
    }
    write(task / "verifier-calibration.json", report)
    return report
