"""Golden state feasibility with a real HTTP protocol stub; no models/VMs/npm."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from test_hub_app import StubHub

from company_envs.models import strict_schema
from company_envs.storage import read, write
from company_envs.world.golden import GoldenDraft, author_golden, merge_patch, replay_golden
from company_envs.world.grader import CalibrationError, TaskGrader, author_grader, calibrate, grade
from company_envs.world.hub_app import HubClient

ROOT = Path(__file__).resolve().parents[1]
APP = "Zendesk_mock"
TASK = "resolve"


class Model:
    def __init__(self, result):
        self.result, self.calls = result, []

    def call(self, job, prompt, response_type):
        self.calls.append((job, prompt, response_type))
        assert isinstance(self.result, response_type)
        return self.result, {"job": job, "session_id": f"fresh-{job}"}


def step(worker, patch):
    return {"worker_id": worker, "app_id": APP, "action": "set_current", "state_patch": patch}


@pytest.fixture
def company(tmp_path):
    initial = read(ROOT / "companies/cort/world.v1-compact" / f"{APP}.state.json")
    folder = tmp_path / "company"
    write(folder / "world" / f"{APP}.state.json", initial)
    write(
        folder / "apps.json",
        {
            "apps": [
                {
                    "app_id": APP,
                    "top_level_keys": list(initial),
                    "schema": "schema.md",
                    "state_file": f"world/{APP}.state.json",
                }
            ]
        },
    )
    (tmp_path / "schema.md").write_text("Zendesk tickets: id, status; comments keyed by ticket id.")
    task = folder / "tasks" / TASK
    write(task / "assignment.json", {"brief": "Resolve the three target cases."})
    write(
        task / "workflow.json",
        {
            "worker_ids": ["w1", "w2", "w3"],
            "success_criteria": [{"method": "state", "requirement": f"Resolve case {i}"} for i in range(3)],
            "completion": {"feasible_path": {"secret": "PRIVATE-PATH", "steps": ["Resolve each target"]}},
        },
    )
    checks = [
        {
            "id": f"resolved_{ticket['id']}",
            "criterion_ref": f"/success_criteria/{i}",
            "kind": "state",
            "description": f"Resolve case {ticket['id']}",
            "predicate": {
                "app_id": APP,
                "selector": f"$.tickets[?(@.id=={ticket['id']})].status",
                "operator": "equals",
                "value_json": '"solved"',
            },
        }
        for i, ticket in enumerate(initial["tickets"][:3])
    ]
    author_grader(tmp_path, folder, TASK, models=Model(TaskGrader.model_validate({"checks": checks})))
    trajectory = [
        step(f"w{i + 1}", {"tickets": [{**ticket, "status": "solved"}]})
        for i, ticket in enumerate(initial["tickets"][:3])
    ]
    write(task / "golden.json", trajectory)
    hub = StubHub()
    client = HubClient(hub.url)
    client.seed("live", initial)
    write(folder / "runtime/sessions.json", {"sid": "live"})
    try:
        yield tmp_path, folder, {APP: client}, initial, trajectory
    finally:
        hub.close()


def logs(folder):
    return [
        json.loads(line)
        for line in (folder / "runtime/golden" / TASK / "attribution" / f"{APP}.jsonl")
        .read_text()
        .splitlines()
    ]


def test_replay_record_replacement_keyed_updates_messages_and_attribution(company):
    _, folder, clients, initial, trajectory = company
    comments = deepcopy(initial["comments"]["1310"])
    comments.append({"id": "golden-comment", "body": "Resolution confirmed"})
    trajectory.insert(1, {"worker_id": "w2", "app_id": APP, "action": "message", "text": "Review complete"})
    trajectory.append(
        step("w3", {"comments": {"1310": comments}, "tickets": [{"id": 9999, "status": "new"}]})
    )
    write(folder / "tasks" / TASK / "golden.json", trajectory)
    result = replay_golden(folder, TASK, clients, "reference")
    assert result[APP]["tickets"][:3] == [{**row, "status": "solved"} for row in initial["tickets"][:3]]
    assert result[APP]["tickets"][3:-1] == initial["tickets"][3:]
    assert result[APP]["tickets"][-1] == {"id": 9999, "status": "new"}
    assert result[APP]["comments"] == {**initial["comments"], "1310": comments}
    assert result[APP]["users"] == initial["users"]
    snapshot = clients[APP].inspect("reference")
    assert snapshot["initial_state"] == initial and snapshot["current_state"] == result[APP]
    assert set(snapshot["state_diff"]) == {"tickets", "comments"}
    entries = logs(folder)
    assert [row["worker_id"] for row in entries] == ["w1", "w2", "w2", "w3", "w3"]
    assert [row["step"] for row in entries] == list(range(1, 6))
    assert all(row["sid"] == "reference" and row["at"] for row in entries)
    assert entries[1]["changed_keys"] == [] and entries[1]["text"] == "Review complete"
    assert entries[-1]["changed_keys"] == ["comments", "tickets"]
    assert not (folder / "runtime/attribution").exists()
    assert clients[APP].inspect("live")["current_state"] == initial
    assert read(folder / "world" / f"{APP}.state.json") == initial
    with pytest.raises(FileExistsError):
        replay_golden(folder, TASK, clients, "reference")


def test_golden_calibration_and_normal_grading(company):
    _, folder, clients, initial, _ = company
    report = calibrate(folder, TASK, clients, golden=True)
    assert report["accepted"] and report["initial_score"] == 0 and report["reference_score"] == 1
    assert all(row["status"] == "fail" for row in report["initial_checks"])
    assert all(row["status"] == "pass" for row in report["reference_checks"])
    assert report["contributions"]["observed_workers"] == 3
    assert all(row["passing_checks"] for row in report["contributions"]["workers"])
    assert read(folder / "runtime/sessions.json") == {"sid": "live"}
    assert grade(folder, TASK, clients)["score"] == 0
    assert clients[APP].inspect("live")["current_state"] == initial
    # Normal episode grading consumes only its own session and attribution.
    sid = report["golden_sid"]
    clients[APP].update("live", clients[APP].inspect(sid)["current_state"])
    assert grade(folder, TASK, clients)["score"] == 0
    path = folder / "runtime/attribution" / f"{APP}.jsonl"
    path.parent.mkdir()
    path.write_text("".join(json.dumps({**row, "sid": "live"}) + "\n" for row in logs(folder)))
    assert grade(folder, TASK, clients)["score"] == 1


@pytest.mark.parametrize("third_action", ["missing", "message", "noop", "irrelevant"])
def test_third_worker_must_contribute_not_just_appear(company, third_action):
    _, folder, clients, initial, trajectory = company
    # All checks still pass, but w2 now does w3's consequential work.
    trajectory[2]["worker_id"] = "w2"
    if third_action == "message":
        trajectory.append({"worker_id": "w3", "app_id": APP, "action": "message", "text": "Done"})
    elif third_action == "noop":
        trajectory.append(step("w3", trajectory[2]["state_patch"]))
    elif third_action == "irrelevant":
        trajectory.append(step("w3", {"ui": {"golden_test": True}}))
    write(folder / "tasks" / TASK / "golden.json", trajectory)
    with pytest.raises(CalibrationError, match="contributions:.*3 distinct workers; observed 2"):
        calibrate(folder, TASK, clients, golden=True)
    report = read(folder / "runtime/grades" / TASK / "calibration.json")
    assert not report["accepted"] and report["reference_score"] == 1
    assert clients[APP].inspect("live")["current_state"] == initial


def test_repeated_calibration_ignores_prior_replay_attribution(company):
    _, folder, clients, _, trajectory = company
    first = calibrate(folder, TASK, clients, golden=True)
    trajectory[2]["worker_id"] = "w2"
    write(folder / "tasks" / TASK / "golden.json", trajectory)
    with pytest.raises(CalibrationError, match="contributions"):
        calibrate(folder, TASK, clients, golden=True)
    second = read(folder / "runtime/grades" / TASK / "calibration.json")
    assert first["golden_sid"] != second["golden_sid"]
    assert second["contributions"]["observed_workers"] == 2
    assert len(logs(folder)) == 6


@pytest.mark.parametrize("failure", ["initial", "reference"])
def test_calibration_names_failing_check(company, failure):
    root, folder, clients, _, trajectory = company
    if failure == "reference":
        trajectory[1]["state_patch"]["tickets"][0]["status"] = "pending"
        write(folder / "tasks" / TASK / "golden.json", trajectory)
        check_id = "resolved_1311"
    else:
        draft = read(folder / "tasks" / TASK / "grader.json")
        draft["grader"]["checks"][0]["predicate"]["value_json"] = '"open"'
        author_grader(root, folder, TASK, models=Model(TaskGrader.model_validate(draft["grader"])))
        check_id = "resolved_1310"
    with pytest.raises(CalibrationError, match=f"{check_id}: {failure} must"):
        calibrate(folder, TASK, clients, golden=True)


def test_author_call_barrier_and_strict_output_schema(company):
    root, folder, _, initial, trajectory = company
    model = Model(GoldenDraft(golden_json=json.dumps(trajectory)))
    assert author_golden(root, folder, TASK, models=model) == trajectory
    assert read(folder / "tasks" / TASK / "golden.json") == trajectory
    assert len(model.calls) == 1
    job, prompt, response_type = model.calls[0]
    assert job == "task_golden" and response_type is GoldenDraft
    payload = json.loads(prompt.split("\n")[-1])
    assert payload["workflow"]["completion"]["feasible_path"]["secret"] == "PRIVATE-PATH"
    assert payload["apps"][0]["initial_state"] == initial
    assert "grader" not in payload
    # Reauthor AFTER golden exists: the grader call still receives no private solution.
    task = folder / "tasks" / TASK
    grader_model = Model(TaskGrader.model_validate(read(task / "grader.json")["grader"]))
    author_grader(root, folder, TASK, models=grader_model)
    grader_prompt = grader_model.calls[0][1]
    data = json.loads(grader_prompt.split("\n")[-1])
    assert set(data) == {"public_brief", "apps", "success_criteria", "contract"}
    assert "PRIVATE-PATH" not in grader_prompt and "state_patch" not in grader_prompt
    assert read(task / "golden.author.json")["receipt"]["session_id"] == "fresh-task_golden"
    assert read(task / "grader.json")["receipt"]["session_id"] == "fresh-task_grader"
    assert strict_schema(GoldenDraft)["additionalProperties"] is False


def test_validate_all_steps_before_seeding_and_do_not_overwrite_draft(company):
    root, folder, clients, _, trajectory = company
    invalid = deepcopy(trajectory)
    invalid[-1]["worker_id"] = "outsider"
    task = folder / "tasks" / TASK
    with pytest.raises(ValueError, match="unknown worker"):
        author_golden(root, folder, TASK, Model(GoldenDraft(golden_json=json.dumps(invalid))))
    assert read(task / "golden.json") == trajectory
    invalid[-1] = step("w3", {"unknown_collection": []})
    write(task / "golden.json", invalid)
    with pytest.raises(ValueError, match="golden step 3: unknown collection"):
        replay_golden(folder, TASK, clients, "invalid")
    assert not clients[APP].current("invalid")["has_custom_state"]


def test_merge_contract():
    initial = {
        "records": [{"id": 1, "old": True}, {"id": 2}],
        "keyed": {"x": {"keep": 1, "replace": [1, 2]}, "other": True},
    }
    patch = {"records": [{"id": 1, "new": True}, {"id": 3}], "keyed": {"x": {"replace": [3]}}}
    assert merge_patch(initial, patch) == {
        "records": [{"id": 1, "new": True}, {"id": 2}, {"id": 3}],
        "keyed": {"x": {"keep": 1, "replace": [3]}, "other": True},
    }
    assert initial["records"][0] == {"id": 1, "old": True}
    for bad in (
        {"records": [{"id": 1}, {"id": 1}]},
        {"records": ["text"]},
        {"records": {"k": "text"}},
        {"keyed": []},
    ):
        with pytest.raises((ValueError, TypeError)):
            merge_patch(initial, bad)


def test_multiple_apps_share_ordered_trajectory_and_fresh_baselines(company):
    _, folder, clients, initial, trajectory = company
    second = StubHub()
    try:
        clients["other_mock"] = HubClient(second.url)
        write(folder / "world/other_mock.state.json", {"records": []})
        apps = read(folder / "apps.json")
        apps["apps"].append({"app_id": "other_mock"})
        write(folder / "apps.json", apps)
        trajectory.insert(
            1,
            {
                "worker_id": "w2",
                "app_id": "other_mock",
                "action": "set_current",
                "state_patch": {"records": [{"id": "linked", "ticket_id": 1310}]},
            },
        )
        write(folder / "tasks" / TASK / "golden.json", trajectory)
        result = replay_golden(folder, TASK, clients, "multi")
        assert result["other_mock"] == {"records": [{"id": "linked", "ticket_id": 1310}]}
        assert clients["other_mock"].inspect("multi")["initial_state"] == {"records": []}
        assert clients[APP].inspect("multi")["initial_state"] == initial
        other_log = read_jsonl(folder / "runtime/golden" / TASK / "attribution/other_mock.jsonl")
        assert [row["step"] for row in logs(folder)] == [1, 3, 4]
        assert other_log[0]["step"] == 2
    finally:
        second.close()


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_golden_rejects_mixed_references_and_existing_sessions(company):
    _, folder, clients, initial, _ = company
    with pytest.raises(CalibrationError, match="cannot be combined"):
        calibrate(folder, TASK, clients, golden=True, reference_states={APP: initial})
    with pytest.raises(ValueError, match="session already exists"):
        replay_golden(folder, TASK, clients, "live")
    assert clients[APP].inspect("live")["current_state"] == initial


def test_calibration_reads_world_without_live_session_and_small_rosters(company):
    root, folder, clients, _, trajectory = company
    task = folder / "tasks" / TASK
    workflow = read(task / "workflow.json")
    workflow["worker_ids"] = ["w1", "w2"]
    write(task / "workflow.json", workflow)
    trajectory[2]["worker_id"] = "w2"
    write(task / "golden.json", trajectory)
    author_grader(
        root, folder, TASK, models=Model(TaskGrader.model_validate(read(task / "grader.json")["grader"]))
    )
    (folder / "runtime/sessions.json").unlink()
    report = calibrate(folder, TASK, clients, golden=True)
    assert report["accepted"] and report["contributions"]["required_workers"] == 0
    assert report["contributions"]["observed_workers"] == 2
    assert not (folder / "runtime/sessions.json").exists()


def test_failed_write_cannot_become_a_successful_reference(company, monkeypatch):
    _, folder, clients, initial, _ = company
    update = clients[APP].update
    calls = []

    def drop_second_write(sid, state):
        calls.append(state)
        if len(calls) == 2:
            return {"success": False}
        return update(sid, state)

    monkeypatch.setattr(clients[APP], "update", drop_second_write)
    with pytest.raises(CalibrationError, match="golden step 2: Zendesk_mock update readback differs"):
        calibrate(folder, TASK, clients, golden=True)
    assert len(calls) == 2
    assert [row["worker_id"] for row in logs(folder)] == ["w1"]
    assert clients[APP].inspect("live")["current_state"] == initial
    assert not (folder / "runtime/grades" / TASK / "calibration.json").exists()


def test_default_author_job_configuration_and_separate_call_directory(company, monkeypatch):
    from company_envs.world import grader_author

    root, folder, _, _, trajectory = company
    (root / "config.toml").write_text('[models]\nexpand = ["codex/gpt-test"]\n')
    made = []
    model = Model(GoldenDraft(golden_json=json.dumps(trajectory)))

    def factory(config, directory):
        made.append((config, directory))
        return model

    monkeypatch.setattr(grader_author, "Models", factory)
    author_golden(root, folder, TASK)
    assert len(model.calls) == 1 and model.calls[0][0] == "task_golden"
    assert made[0][0]["models"]["task_golden"] == ["codex/gpt-test"]
    assert made[0][1] == folder / "tasks" / TASK / "golden_calls"


def test_replay_rejects_recorded_live_sid_even_if_state_was_lost(company):
    _, folder, clients, _, _ = company
    clients[APP].reset("live")
    with pytest.raises(ValueError, match="live session"):
        replay_golden(folder, TASK, clients, "live")
    assert not clients[APP].current("live")["has_custom_state"]


def test_replay_uses_manifest_state_file(company):
    _, folder, clients, initial, _ = company
    apps = read(folder / "apps.json")
    apps["apps"][0]["state_file"] = "world/baseline.json"
    write(folder / "apps.json", apps)
    (folder / "world" / f"{APP}.state.json").rename(folder / "world/baseline.json")
    replay_golden(folder, TASK, clients, "manifest-baseline")
    assert clients[APP].inspect("manifest-baseline")["initial_state"] == initial


def test_merge_patch_accepts_records_keyed_by_id():
    from company_envs.world.golden import merge_patch

    state = {"documents": [{"id": "doc-a", "title": "A"}]}
    merged = merge_patch(
        state, {"documents": {"doc-b": {"title": "B"}, "doc-a": {"id": "doc-a", "title": "A2"}}}
    )
    assert sorted((d["id"], d["title"]) for d in merged["documents"]) == [("doc-a", "A2"), ("doc-b", "B")]


def test_truncated_golden_draft_gets_size_feedback(tmp_path, monkeypatch):
    import json

    from company_envs.world import golden

    prompts = []

    class Draft:
        golden_json = '[{"worker_id": "a", "app_id": "x", "action": "message", "text": "unterminated'

    class Models:
        def call(self, job, prompt, schema):
            prompts.append(prompt)
            return Draft(), {"receipt": True}

    monkeypatch.setattr(golden, "_task", lambda folder, task_id: tmp_path)
    monkeypatch.setattr(golden, "initial_states", lambda folder: {})
    monkeypatch.setattr(golden, "authoring_payload", lambda root, folder, task_id: {})
    (tmp_path / "workflow.json").write_text(json.dumps({"completion": {"feasible_path": "yes"}}))
    with pytest.raises(ValueError, match="cut off at character"):
        golden.author_golden(tmp_path, tmp_path, "t", models=Models())
    assert len(prompts) == 3 and "Write a shorter golden" in prompts[1]


def test_merge_patch_keys_idless_records_by_name_or_content():
    from company_envs.world.golden import merge_patch

    state = {"namedRanges": [{"name": "Total", "range": "A1"}], "notes": [{"text": "a"}]}
    merged = merge_patch(
        state,
        {
            "namedRanges": [{"name": "Total", "range": "B2"}, {"name": "Other", "range": "C3"}],
            "notes": [{"text": "b"}],
        },
    )
    assert sorted((r["name"], r["range"]) for r in merged["namedRanges"]) == [
        ("Other", "C3"),
        ("Total", "B2"),
    ]
    assert sorted(n["text"] for n in merged["notes"]) == ["a", "b"]


def test_golden_may_not_cite_records_it_never_writes():
    from company_envs.world.golden import GoldenStep, dangling_references

    initial = {
        "slack_mock": {"messages": [{"id": "msg-1", "text": "hi"}]},
        "docs": {"documents": [{"id": "doc-1", "content": "x"}]},
    }
    steps = [
        GoldenStep(
            worker_id="a",
            app_id="docs",
            action="set_current",
            state_patch={
                "documents": [
                    {
                        "id": "doc-2",
                        "content": "see msg-plastic-withdrawal and doc-1",
                        "pinned": "msg-plastic-withdrawal",
                    }
                ]
            },
        )
    ]
    final = {
        "slack_mock": initial["slack_mock"],
        "docs": {"documents": initial["docs"]["documents"] + steps[0].state_patch["documents"]},
    }
    problems = dangling_references(steps, initial, final)
    assert problems == [
        "step 1 refers to msg-plastic-withdrawal, which the world never had and the golden never writes"
    ]
    steps[0].state_patch["documents"][0]["pinned"] = "msg-1"
    assert dangling_references(steps, initial, final) == []
    # A thread the golden creates by posting into it is known through the message's threadId.
    post = GoldenStep(
        worker_id="a",
        app_id="slack_mock",
        action="set_current",
        state_patch={"messages": [{"id": "msg-2", "threadId": "th-plan-0908", "text": "see th-plan-0908"}]},
    )
    final2 = {
        **final,
        "slack_mock": {"messages": initial["slack_mock"]["messages"] + post.state_patch["messages"]},
    }
    assert dangling_references([post], initial, final2) == []


def test_a_refused_golden_leaves_a_receipt_and_a_written_one_clears_it(tmp_path, monkeypatch):
    """author_golden raised and wrote nothing, so the driver found no golden.json, called the step
    again, and called it again: a stage with no failure receipt is an infinite retry. Measured on
    the cohort: 2 of 18 attempted goldens left no receipt, and one of those two companies already
    held an accepted, all-checks calibration on its other task and was stuck anyway."""
    from company_envs.world import golden

    task = tmp_path / "tasks" / "t"
    task.mkdir(parents=True)
    write(task / "workflow.json", {"completion": {"feasible_path": "yes"}})
    monkeypatch.setattr(golden, "initial_states", lambda folder: {})
    monkeypatch.setattr(golden, "authoring_payload", lambda root, folder, task_id: {})

    class Refuse:
        def call(self, job, prompt, schema):
            return GoldenDraft(golden_json="[]"), {"receipt": True}

    with pytest.raises(ValueError, match="could not meet the task contract"):
        golden.author_golden(tmp_path, tmp_path, "t", models=Refuse())
    receipt = read(task / golden.GOLDEN_FAILED)
    assert receipt["task_id"] == "t" and "golden trajectory must contain steps" in receipt["reason"]

    monkeypatch.setattr(golden, "_trajectory", lambda *a, **k: [])
    monkeypatch.setattr(golden, "_models", lambda *a, **k: Refuse())
    golden.author_golden(tmp_path, tmp_path, "t")
    # A marker left by the earlier attempt would retire a task this one just authored.
    assert not (task / golden.GOLDEN_FAILED).exists() and (task / "golden.json").is_file()


def test_an_exhausted_account_is_not_a_verdict_about_the_task(tmp_path, monkeypatch):
    """An empty run must not write the receipt a real failure writes: a provider outage or an
    exhausted account is retried, and retiring the task on one would throw the world away."""
    from company_envs.models import CallBudgetExhausted
    from company_envs.world import golden

    task = tmp_path / "tasks" / "t"
    task.mkdir(parents=True)
    write(task / "workflow.json", {"completion": {"feasible_path": "yes"}})
    monkeypatch.setattr(golden, "initial_states", lambda folder: {})
    monkeypatch.setattr(golden, "authoring_payload", lambda root, folder, task_id: {})

    class Exhausted:
        def call(self, job, prompt, schema):
            raise CallBudgetExhausted(12, 12)

    with pytest.raises(CallBudgetExhausted):
        golden.author_golden(tmp_path, tmp_path, "t", models=Exhausted())
    assert not (task / golden.GOLDEN_FAILED).exists()


def test_merge_patch_writes_a_collection_of_bare_ids():
    """amazon_mock.wishlist, slack_mock.bookmarkedMessages and instagram_mock.savedPostIds hold ids,
    not records, and four feature cells in three seeded worlds name one. Every golden attempt for
    the task whose decisive cell was amazon_mock.wishlist died on "record patches require objects;
    got 'p31'", and with no receipt it held an accepted calibration's company out of the VM stage."""
    state = {"wishlist": ["p31", "p33", "p37"], "cart": []}
    assert merge_patch(state, {"wishlist": ["p33", "p35"]})["wishlist"] == ["p31", "p33", "p37", "p35"]
    assert merge_patch(state, {"cart": ["c1"]})["cart"] == ["c1"]
    assert merge_patch(state, {"cart": [{"id": "c1"}]})["cart"] == [{"id": "c1"}]
    with pytest.raises(TypeError, match="wishlist holds bare ids"):
        merge_patch(state, {"wishlist": [{"id": "p32", "productId": "p32"}]})


def test_a_page_slug_is_a_location_not_a_dangling_reference():
    """dangling_references flagged any id-shaped value under any key, so a Canvas page's
    url="learning-checks" read as a citation of a record that never existed. Measured: 3 of 33
    recorded golden drafts -- all three attempts for one task, which then left no receipt at all."""
    from company_envs.world.golden import GoldenStep, dangling_references

    initial = {
        "lms": {
            "modules": [{"id": "learning-unit-1", "title": "Unit 1"}],
            "pages": [{"id": "page-1", "url": "learning-outcomes", "title": "Outcomes"}],
        }
    }
    step = GoldenStep(
        worker_id="a",
        app_id="lms",
        action="set_current",
        state_patch={"pages": [{"id": "page-2", "url": "learning-checks", "title": "Checks"}]},
    )
    final = {
        "lms": {
            "modules": initial["lms"]["modules"],
            "pages": initial["lms"]["pages"] + step.state_patch["pages"],
        }
    }
    assert dangling_references([step], initial, final) == []
    # A reference in a field that is not a location is still caught.
    step.state_patch["pages"][0]["parentPage"] = "learning-missing"
    assert dangling_references([step], initial, final) == [
        "step 1 refers to learning-missing, which the world never had and the golden never writes"
    ]
