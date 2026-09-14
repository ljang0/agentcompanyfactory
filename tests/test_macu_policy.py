"""MACU-style manager/worker policy with fake models: no network, VMs or provider calls."""

import asyncio
import copy
import json
import subprocess

import pytest

from company_envs import models as model_backend
from company_envs.storage import read, write
from company_envs.world import controller
from company_envs.world.harness import Action, BudgetExhausted, FakeBackend, Message, Observation, Screen
from company_envs.world.policies import macu

CONFIG = {
    "models": {"expand": ["codex/gpt-test", "codex/gpt-fallback"], "reasoning": "low"},
    "generation": {"completion_timeout_seconds": 99},
}
ROSTER = (("boss", "Program manager"), ("nurse", "Nurse"), ("clerk", "Secretary"))
ACCESS = {"boss": ["gmail", "slack"], "nurse": ["gmail", "docs"], "clerk": ["gmail", "sheets"]}


class Models:
    """Scripted responses keyed by schema; records every prompt packet and image."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = []

    def call(self, job, prompt, schema, *, images):
        packet = prompt.rpartition("\n")[2]
        self.calls.append((schema.__name__, json.loads(packet), [p.read_bytes() for p in images]))
        assert job == "worker_policy"
        if not self.queue:
            raise AssertionError(f"unexpected {schema.__name__} call")
        return self.queue.pop(0), {"job": job, "model": "double"}


def observe(worker="boss", role="Program manager", brief=None, messages=(), last_result=None, **kwargs):
    fields = {"remaining_seconds": 300, "remaining_actions": 20, "elapsed": 0} | kwargs
    return Observation(
        worker,
        role,
        ROSTER,
        Screen(FakeBackend.PNG),
        tuple(messages),
        brief,
        fields["elapsed"],
        fields["remaining_seconds"],
        fields["remaining_actions"],
        last_result,
    )


def message(sender, recipient, text, kind="reply", sequence=1):
    roles = dict(ROSTER)
    return Message(sequence, 1.0, sender, roles[sender], recipient, roles[recipient], text, kind)


def subtask(sid, assignee, apps, depends_on=(), instruction=None):
    return {
        "id": sid,
        "assignee": assignee,
        "instruction": instruction or f"do {sid}",
        "outputs": f"{sid} result",
        "apps": list(apps),
        "depends_on": list(depends_on),
    }


def report(sid, status="done", result="ok", evidence="seen"):
    return macu.protocol(type="report", subtask=sid, status=status, result=result, evidence=evidence)


def run(coro):
    return asyncio.run(coro)


def manager(tmp_path, *responses, reservations=None):
    models = Models(*responses)
    reserve = (lambda: reservations.append(len(models.calls))) if reservations is not None else None
    return macu.MacuPolicy(CONFIG, tmp_path / "boss", models=models, reserve=reserve, access=ACCESS), models


def test_manager_plans_once_then_delegates_by_role_and_dependency(tmp_path):
    reservations = []
    plan = {
        "analysis": "two steps",
        "subtasks": [
            subtask("assess", "nurse", ["docs"]),
            subtask("record", "clerk", ["sheets"], ["assess"]),
        ],
    }
    policy, models = manager(tmp_path, plan, reservations=reservations)
    frozen = copy.deepcopy(CONFIG)

    async def scenario():
        first = await policy(observe(brief="Plan the week"))
        assert first.name == "send_message" and first.arguments["kind"] == "delegate"
        assert first.arguments["recipient"] == "nurse"
        assign = macu.payload(first.arguments["text"])
        assert assign["type"] == "assign" and assign["subtask"] == "assess" and assign["apps"] == ["docs"]
        assert assign["inputs"] == []
        # The dependent subtask waits for its input; nothing to do costs no model call.
        idle = await policy(observe(last_result={"sequence": 1}))
        assert idle.name == "wait" and idle.arguments["seconds"] == macu.IDLE_WAIT
        wake = await policy(
            observe(
                last_result={"wake_reason": "message"}, messages=[message("nurse", "boss", report("assess"))]
            )
        )
        assert wake.name == "send_message" and wake.arguments["recipient"] == "clerk"
        assign = macu.payload(wake.arguments["text"])
        assert assign["subtask"] == "record" and assign["inputs"][0]["subtask"] == "assess"
        assert assign["inputs"][0]["result"] == "ok"

    run(scenario())
    assert CONFIG == frozen and policy.config["models"]["worker_policy"] == ["codex/gpt-test"]
    assert reservations == [0]  # reserve was called before the only (planning) call
    name, packet, images = models.calls[0]
    assert name == "Plan" and images == []
    assert packet["objective"] == "Plan the week"
    assert {t["worker"]: [a["id"] for a in t["apps"]] for t in packet["team"]} == {
        "nurse": ["gmail", "docs"],
        "clerk": ["gmail", "sheets"],
    }
    # Each app carries what it is for; the manager assigns by purpose, not by an opaque id.
    assert all(a["app"] and a["for"] for t in packet["team"] for a in t["apps"])
    assert "boss" not in {t["worker"] for t in packet["team"]}
    assert policy.plan["assess"]["status"] == "done" and policy.plan["record"]["status"] == "sent"


def test_company_reaches_the_manager_and_the_worker(tmp_path):
    """The manager never sees a screen, so the business has to be in words or it is not there.

    Its planning calls carry no screenshot. Without company.json it would be assigning work at a
    company it cannot name, in an industry it does not know, to job titles with no duties attached.
    """
    from company_envs.world.policies import macu

    company = {
        "name": "Harborlight Hospice Services",
        "sector": "Health Care and Social Assistance",
        "location": "Greater Jacksonville, Florida",
        "operations": "A community hospice running recurring care-plan review.",
        "workers": [
            {"id": "boss", "title": "Program manager", "responsibility": "Coordinate the review."},
            {"id": "nurse", "title": "Nurse", "responsibility": "Assess nursing need."},
        ],
    }
    write(tmp_path / "company.json", company)
    write(tmp_path / "world" / "worker_apps.json", dict(ACCESS))
    loaded = macu.load_company(tmp_path / "runtime" / "vms" / "boss" / "policy")

    assert loaded["name"] == "Harborlight Hospice Services"
    assert loaded["sector"] == "Health Care and Social Assistance"
    assert loaded["what_it_does"].startswith("A community hospice")
    assert loaded["people"]["nurse"]["responsibility"] == "Assess nursing need."
    policy = macu.MacuPolicy(CONFIG, tmp_path / "runtime" / "vms" / "boss" / "policy")
    policy.worker_id, policy.roster = "boss", dict(ROSTER)
    packet = policy._team_packet(objective="Plan the week")
    assert packet["company"]["name"] == "Harborlight Hospice Services"
    assert "people" not in packet["company"]  # the roster is in "team", not duplicated here
    assert packet["my_role"] == "Program manager" and packet["my_job"] == "Coordinate the review."
    assert {t["worker"]: t["does"] for t in packet["team"]}["nurse"] == "Assess nursing need."


def test_no_company_file_invents_nothing(tmp_path):
    """The loader walks up for company.json; with none above it, the packet simply has no company."""
    from company_envs.world.policies import macu

    assert macu.load_company(tmp_path / "runtime" / "vms" / "boss" / "policy") == {}


def test_manager_aggregates_finishes_peers_and_ends_with_the_handoff(tmp_path):
    reservations = []
    plan = {"analysis": "one step", "subtasks": [subtask("record", "clerk", ["sheets"])]}
    final = {"final_response": "Schedule recorded in the sheet.", "handoff_on_my_desktop": None}
    policy, models = manager(tmp_path, plan, final, reservations=reservations)

    async def scenario():
        await policy(observe(brief="Record it"))
        done = await policy(observe(messages=[message("clerk", "boss", report("record", result="row 7"))]))
        assert done.name == "send_message" and done.arguments["recipient"] == "nurse"
        assert macu.payload(done.arguments["text"]) == {"type": "finish"}
        second = await policy(observe())
        assert second.arguments["recipient"] == "clerk"
        end = await policy(observe())
        assert end.name == "done" and end.arguments["summary"] == "Schedule recorded in the sheet."

    run(scenario())
    assert [name for name, _, _ in models.calls] == ["Plan", "Aggregation"]
    assert reservations == [0, 1]
    packet = models.calls[1][1]
    assert packet["reports"][0]["report"] == {"status": "done", "result": "row 7", "evidence": "seen"}
    assert packet["objective"] == "Record it"


def test_manager_replans_on_failure_and_dispatches_the_retry(tmp_path):
    reservations = []
    plan = {"analysis": "x", "subtasks": [subtask("record", "nurse", ["sheets"])]}
    retry = {
        "reasoning": "nurse lacks sheets; clerk holds it",
        "action": "update",
        "add": [subtask("record2", "clerk", ["sheets"])],
        "remove": [],
        "modify": [],
    }
    final = {"final_response": "done by clerk", "handoff_on_my_desktop": None}
    policy, models = manager(tmp_path, plan, retry, final, reservations=reservations)

    async def scenario():
        await policy(observe(brief="Record it"))
        refused = report("record", status="not_my_access", result="needs sheets")
        action = await policy(observe(messages=[message("nurse", "boss", refused)]))
        assert action.name == "send_message" and action.arguments["recipient"] == "clerk"
        assert macu.payload(action.arguments["text"])["subtask"] == "record2"
        again = await policy(observe(messages=[message("clerk", "boss", report("record2"))]))
        assert again.name == "send_message" and macu.payload(again.arguments["text"]) == {"type": "finish"}

    run(scenario())
    assert [name for name, _, _ in models.calls] == ["Plan", "Replan", "Aggregation"]
    assert reservations == [0, 1, 2]
    replan_packet = models.calls[1][1]
    assert (
        replan_packet["focus"]["id"] == "record"
        and replan_packet["focus"]["report"]["status"] == "not_my_access"
    )
    assert replan_packet["replans_left"] == macu.MAX_REPLANS
    aggregation = models.calls[2][1]
    assert [r["id"] for r in aggregation["reports"]] == ["record", "record2"]
    assert policy.replans == 1


def test_manager_rejects_invalid_replans_and_stops_at_the_replan_budget(tmp_path):
    config = copy.deepcopy(CONFIG) | {"worker_policy": {"macu_replans": 1}}
    plan = {
        "analysis": "x",
        "subtasks": [subtask("a", "nurse", ["docs"]), subtask("b", "clerk", ["sheets"], ["a"])],
    }
    frozen_edit = {"reasoning": "bad", "action": "update", "add": [], "remove": ["a"], "modify": []}
    models = Models(plan, frozen_edit)
    policy = macu.MacuPolicy(config, tmp_path, models=models, access=ACCESS)

    async def scenario():
        await policy(observe(brief="Go"))
        # A failed report triggers one replan; removing the finished subtask is rejected whole.
        action = await policy(observe(messages=[message("nurse", "boss", report("a", status="failed"))]))
        assert action.name == "send_message" and macu.payload(action.arguments["text"])["subtask"] == "b"
        assert policy.replans == 0 and policy.notes[-1]["replan_rejected"] == ["remove a: not pending"]
        assert list(policy.plan) == ["a", "b"] and policy.plan["a"]["status"] == "failed"
        # Once the replan budget is spent a bad report goes straight to aggregation.
        policy.replans = 1
        models.queue.append({"final_response": "partial", "handoff_on_my_desktop": None})
        finish = await policy(observe(messages=[message("clerk", "boss", report("b", status="partial"))]))
        assert macu.payload(finish.arguments["text"]) == {"type": "finish"}

    run(scenario())
    assert [name for name, _, _ in models.calls] == ["Plan", "Replan", "Aggregation"]


def test_manager_drops_structurally_invalid_subtasks_and_fails_closed_on_an_empty_plan(tmp_path):
    plan = {
        "analysis": "x",
        "subtasks": [
            subtask("self", "boss", []),  # the manager never executes
            subtask("ghost", "nobody", []),
            subtask("orphan", "nurse", [], ["ghost"]),
            subtask("loop1", "nurse", [], ["loop2"]),
            subtask("loop2", "clerk", [], ["loop1"]),
            subtask("ok", "clerk", ["sheets"]),
        ],
    }
    policy, _ = manager(tmp_path, plan)
    action = run(policy(observe(brief="Go")))
    assert macu.payload(action.arguments["text"])["subtask"] == "ok" and list(policy.plan) == ["ok"]
    assert sorted(policy.notes[0]["dropped_invalid_subtasks"]) == [
        "ghost",
        "loop1",
        "loop2",
        "orphan",
        "self",
    ]

    empty, _ = manager(tmp_path / "empty", {"analysis": "x", "subtasks": [subtask("self", "boss", [])]})
    with pytest.raises(ValueError, match="no valid subtask"):
        run(empty(observe(brief="Go")))


def test_worker_waits_executes_the_assignment_and_reports_back(tmp_path):
    reservations = []
    models = Models(
        {"name": "bash", "command": "cat ~/Desktop/APPS.html"},
        {"name": "report", "status": "done", "result": "7 rows", "evidence": "sheet row 7"},
    )
    policy = macu.MacuPolicy(
        CONFIG,
        tmp_path / "clerk",
        role_note="Secretary",
        models=models,
        reserve=lambda: reservations.append(len(models.calls)),
        access=ACCESS,
    )
    assign = macu.protocol(
        type="assign", subtask="record", instruction="Record it", outputs="row", apps=["sheets"], inputs=[]
    )

    async def scenario():
        idle = await policy(observe("clerk", "Secretary"))
        assert idle.name == "wait" and idle.arguments["seconds"] == macu.IDLE_WAIT
        step = await policy(
            observe("clerk", "Secretary", messages=[message("boss", "clerk", assign, "delegate")])
        )
        assert step == Action("bash", {"command": "cat ~/Desktop/APPS.html"})
        done = await policy(observe("clerk", "Secretary", last_result={"stdout": "<html>", "exit_code": 0}))
        assert done.name == "send_message" and done.arguments == {
            "recipient": "boss",
            "kind": "reply",
            "text": macu.protocol(
                type="report", subtask="record", status="done", result="7 rows", evidence="sheet row 7"
            ),
        }
        again = await policy(observe("clerk", "Secretary", last_result={"sequence": 3}))
        assert again.name == "wait"
        finish = message("boss", "clerk", macu.protocol(type="finish"), "message")
        end = await policy(observe("clerk", "Secretary", messages=[finish]))
        assert end.name == "done"

    run(scenario())
    assert reservations == [0, 1]
    names = [name for name, _, _ in models.calls]
    assert names == ["WorkerStep", "WorkerStep"]
    first, second = models.calls[0][1], models.calls[1][1]
    assert first["assignment"]["subtask"] == "record"
    assert [a["id"] for a in first["my_apps"]] == ["gmail", "sheets"]
    assert "from" not in first["assignment"] and first["role_note"] == "Secretary"
    assert models.calls[0][2] == [FakeBackend.PNG]  # the screenshot travels with every step
    assert second["last_bash_result"] == {"stdout": "<html>", "exit_code": 0}
    assert second["history"][-2]["action"]["name"] == "bash"
    assert "Plan the week" not in json.dumps([c[1] for c in models.calls])  # workers never see the brief


def test_worker_refuses_out_of_access_subtasks_without_a_model_call(tmp_path):
    models = Models()
    policy = macu.MacuPolicy(
        CONFIG, tmp_path, models=models, reserve=lambda: pytest.fail("no call"), access=ACCESS
    )
    assign = macu.protocol(
        type="assign", subtask="pay", instruction="Pay", outputs="x", apps=["quickbooks"], inputs=[]
    )
    action = run(policy(observe("nurse", "Nurse", messages=[message("boss", "nurse", assign, "delegate")])))
    payload = macu.payload(action.arguments["text"])
    assert action.arguments["recipient"] == "boss" and payload["status"] == "not_my_access"
    assert "quickbooks" in payload["result"] and models.calls == []


def test_worker_model_may_refuse_when_access_is_unknown(tmp_path):
    models = Models(
        {"name": "report", "status": "not_my_access", "result": "no CRM here", "evidence": "APPS.html"}
    )
    policy = macu.MacuPolicy(CONFIG, tmp_path, models=models, access={})
    assign = macu.protocol(
        type="assign", subtask="crm", instruction="Update CRM", outputs="x", apps=["crm"], inputs=[]
    )
    action = run(policy(observe("nurse", "Nurse", messages=[message("boss", "nurse", assign, "delegate")])))
    assert macu.payload(action.arguments["text"])["status"] == "not_my_access"
    assert models.calls[0][1]["my_apps"] is None


def test_manager_runs_its_own_handoff_step_when_aggregation_asks(tmp_path):
    plan = {"analysis": "x", "subtasks": [subtask("assess", "nurse", ["docs"])]}
    final = {"final_response": "Proposal ready.", "handoff_on_my_desktop": "Email the proposal from Gmail"}
    step = {"name": "open_url", "url": "http://gmail.local/"}
    handoff = {"name": "report", "status": "done", "result": "sent to the team", "evidence": "Sent folder"}
    policy, models = manager(tmp_path, plan, final, step, handoff)

    async def scenario():
        await policy(observe(brief="Go"))
        finish = await policy(observe(messages=[message("nurse", "boss", report("assess"))]))
        assert macu.payload(finish.arguments["text"]) == {"type": "finish"}
        await policy(observe())  # second finish
        own = await policy(observe())
        assert own == Action("open_url", {"url": "http://gmail.local/"})
        end = await policy(observe(last_result={"ok": True}))
        assert end.name == "done" and end.arguments["summary"].startswith("Proposal ready.")
        assert "sent to the team" in end.arguments["summary"]

    run(scenario())
    assert [name for name, _, _ in models.calls] == ["Plan", "Aggregation", "WorkerStep", "WorkerStep"]
    assert models.calls[2][1]["assignment"]["instruction"] == "Email the proposal from Gmail"
    assert models.calls[2][2] == [FakeBackend.PNG]


def test_manager_marks_undeliverable_delegations_failed(tmp_path):
    plan = {"analysis": "x", "subtasks": [subtask("assess", "nurse", ["docs"])]}
    no_change = {"reasoning": "peer gone", "action": "no_change", "add": [], "remove": [], "modify": []}
    final = {"final_response": "nothing recorded", "handoff_on_my_desktop": None}
    policy, models = manager(tmp_path, plan, no_change, final)

    async def scenario():
        await policy(observe(brief="Go"))
        replanned = await policy(
            observe(last_result={"error": "Messages require distinct active roster members"})
        )
        assert policy.plan["assess"]["status"] == "failed" and replanned.name == "wait"
        assert replanned.arguments["seconds"] == macu.SHORT_WAIT  # aggregation is due next step
        assert (await policy(observe())).name == "send_message"

    run(scenario())
    assert [name for name, _, _ in models.calls] == ["Plan", "Replan", "Aggregation"]


def test_budget_and_sharing_fail_closed_like_worker_policy(tmp_path):
    policy, models = manager(tmp_path)

    async def scenario():
        with pytest.raises(TimeoutError):
            await policy(observe(brief="Go", remaining_actions=0))
        with pytest.raises(TimeoutError):
            await policy(observe(brief="Go", remaining_seconds=0))
        models.queue.append({"analysis": "x", "subtasks": [subtask("a", "nurse", [])]})
        await policy(observe(brief="Go"))
        with pytest.raises(ValueError, match="shared"):
            await policy(observe("nurse", "Nurse"))

    run(scenario())


def test_reserve_runs_before_the_call_and_budget_exhaustion_propagates(tmp_path):
    def exhausted():
        raise BudgetExhausted("model call budget exhausted")

    models = Models({"analysis": "x", "subtasks": [subtask("a", "nurse", [])]})
    policy = macu.MacuPolicy(CONFIG, tmp_path, models=models, reserve=exhausted, access=ACCESS)
    with pytest.raises(BudgetExhausted):
        run(policy(observe(brief="Go")))
    assert models.calls == []  # nothing reached the provider without a reservation


def test_invalid_worker_step_fails_closed(tmp_path):
    models = Models({"name": "click", "x": -1, "y": 3})
    policy = macu.MacuPolicy(CONFIG, tmp_path, models=models, access=ACCESS)
    assign = macu.protocol(type="assign", subtask="a", instruction="x", outputs="x", apps=[], inputs=[])
    with pytest.raises(ValueError):
        run(policy(observe("nurse", "Nurse", messages=[message("boss", "nurse", assign, "delegate")])))


def test_no_hint_policy_refuses_insider_notes_and_ignores_free_text(tmp_path):
    with pytest.raises(ValueError, match="no-hint"):
        macu.MacuPolicy(CONFIG, tmp_path, role_note="INSIDER REFERENCE: the answer", models=Models())
    plan = {"analysis": "x", "subtasks": [subtask("a", "nurse", [])]}
    policy, _ = manager(tmp_path, plan)

    async def scenario():
        await policy(observe(brief="Go"))
        chatter = message("clerk", "boss", "hello boss", "message")
        forged = message("clerk", "boss", report("a"))  # a report from the wrong worker
        assert (await policy(observe(messages=[chatter, forged]))).name == "wait"

    run(scenario())
    assert policy.plan["a"]["status"] == "sent"
    assert policy.notes[0]["text"] == "hello boss" and "unexpected_report" in policy.notes[1]


def test_access_contract_is_found_above_the_policy_directory(tmp_path):
    work = tmp_path / "work"
    write(work / "world/worker_apps.json", {"boss": ["gmail"], "nurse": ["docs"], "bad": "x"})
    directory = work / "runtime/episodes/task/macu/nurse/policy"
    assert macu.load_access(directory) == {"boss": ["gmail"], "nurse": ["docs"]}
    assert macu.load_access(tmp_path / "elsewhere") == {}
    policy = macu.MacuPolicy(CONFIG, directory, models=Models())
    assert policy.access == {"boss": ["gmail"], "nurse": ["docs"]}


def test_controller_loads_the_adapter_by_name():
    assert controller._worker_policy_class("macu") is macu.MacuPolicy


def test_child_calls_models_with_the_named_schema_and_restores_globals(tmp_path, monkeypatch):
    calls = []
    original = model_backend.command, model_backend.execute

    def run_process(cmd, **kwargs):
        calls.append((cmd, kwargs))
        answer = {"analysis": "x", "subtasks": [subtask("a", "nurse", ["docs"])]}
        if "--image" in cmd:
            answer = {"name": "report", "status": "done", "result": "r", "evidence": "e"}
        write(kwargs["cwd"] / "answer.json", answer)
        (kwargs["cwd"] / "stdout.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(macu.subprocess, "run", run_process)
    config = copy.deepcopy(CONFIG)
    config["models"]["worker_policy"] = ["codex/gpt-test"]
    text_only = tmp_path / "plan/request.json"
    write(text_only, {"config": config, "prompt": "plan please", "schema": "Plan", "image": None})
    macu._call_in_child(text_only)
    result = read(tmp_path / "plan/response.json")
    assert result["data"]["subtasks"][0]["id"] == "a" and result["receipt"]["job"] == "worker_policy"
    assert "--image" not in calls[0][0]
    with_image = tmp_path / "step/request.json"
    write(
        with_image,
        {"config": config, "prompt": "step", "schema": "WorkerStep", "image": str(tmp_path / "s.png")},
    )
    macu._call_in_child(with_image)
    assert read(tmp_path / "step/response.json")["data"]["name"] == "report"
    assert calls[1][0][-3:] == ["--image", str(tmp_path / "s.png"), "-"]
    assert (model_backend.command, model_backend.execute) == original
