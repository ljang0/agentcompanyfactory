"""Read current staged evidence without running authors, models or worker trials."""

from pathlib import Path

from .storage import digest, now, read, write


def inspect_pilot(root, folder, output=None):
    from .collaborator import require_team_evidence
    from .doctor import inspect_prerequisites
    from .world.blueprint import load_core
    from .world.population_snapshot import population_snapshot
    from .world.python_verifier import VerifierUnavailable
    from .world.staged_grading import proof_key
    from .world.task_assessment import require_task_world
    from .world.world_acceptance import accepted_world

    root, folder = Path(root).resolve(), Path(folder).resolve()
    tasks = read(folder / "MANIFEST.json").get("tasks", [])
    stages = []

    def inspect(number, name, function):
        try:
            details = function()
            row = {"stage": number, "name": name, "status": "accepted", "details": details}
        except FileNotFoundError as exc:
            row = {"stage": number, "name": name, "status": "unmeasured", "reason": str(exc)}
        except (ValueError, KeyError, OSError, VerifierUnavailable) as exc:
            row = {"stage": number, "name": name, "status": "not_accepted", "reason": str(exc)}
        stages.append(row)

    def foundations():
        report = inspect_prerequisites(root, folder=folder)
        if not report["ok"]:
            raise ValueError(str([r for r in report["checks"] if r["status"] != "pass"]))
        return {
            "scope": "Current dossier, captured research, catalog and export contract checks",
            "checks": report["checks"],
        }

    def task_designs():
        if not tasks:
            raise ValueError("No accepted tasks are indexed")
        for task in tasks:
            require_task_world(root, folder, task)
            review = read(folder / "tasks" / task / "review.json")["review"]
            if not any(r["workflow_id"] == task and r["verdict"] == "accept" for r in review["tasks"]):
                raise ValueError(f"{task}: no accepted independent task review")
        return {"tasks": tasks}

    def verifiers():
        if not tasks:
            raise ValueError("No accepted tasks are indexed")
        for task in tasks:
            calibration = read(folder / "tasks" / task / "verifier-calibration.json")
            if calibration["status"] != "accepted" or calibration["proof_key"] != proof_key(
                root, folder, task
            ):
                raise ValueError(f"{task}: missing current verifier calibration")
            if calibration.get("golden_hash") != digest(read(folder / "tasks" / task / "golden.json")):
                raise ValueError(f"{task}: reference changed since calibration")
        return {"tasks": tasks, "scope": "Isolated mechanics and retained independent semantic calibration"}

    def team_trials():
        trials = require_team_evidence(root, folder)
        return {
            task: {
                kind: {"class": row["receipt"]["class"], "score": row["grade"]["score"]}
                for kind, row in kinds.items()
            }
            for task, kinds in trials.items()
        }

    def package():
        proof = read(root / "COLLABORATOR-VERIFIED.json")
        payload = read(root / "COLLABORATOR.json")
        if proof.get("status") == "inspection_verified_not_published":
            raise ValueError(
                "Inspection replay/reset is verified separately; full team acceptance remains required"
            )
        if proof["status"] != "verified_not_published" or proof["payload_hash"] != digest(payload["files"]):
            raise ValueError("Extracted collaborator verification is missing or stale")
        for name, expected in {**payload["files"], **proof.get("proof_files", {})}.items():
            if digest((root / name).read_bytes()) != expected:
                raise ValueError(f"Packaged file changed: {name}")
        return {"scope": proof["scope"], "published": False}

    inspect(1, "Foundations", foundations)
    inspect(2, "Canonical world", lambda: {"reference_date": load_core(root, folder)[0].reference_date})
    inspect(3, "Native population", lambda: population_snapshot(root, folder))
    inspect(4, "World acceptance", lambda: {"frozen_hash": digest(accepted_world(root, folder))})
    inspect(5, "Task and assessment review", task_designs)
    inspect(6, "Verifier calibration", verifiers)
    inspect(7, "Real teams and ablations", team_trials)
    inspect(8, "Verified collaborator extraction", package)
    boundary = 0
    for row in stages:
        if row["status"] != "accepted":
            break
        boundary = row["stage"]
    result = {"at": now(), "last_accepted_stage": boundary, "model_calls": 0, "stages": stages}
    if output is not None:
        write(Path(output), result)
    return result
