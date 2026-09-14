import asyncio
import json

import pytest

from company_envs.world.harness import Action, Budget, Episode, FakeBackend, MessageBus, WorkerAgent


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def worker(who, policy, **kwargs):
    return WorkerAgent(who, f"{who} role", policy, FakeBackend(), **kwargs)


def episode(tmp_path, workers, **kwargs):
    return Episode(workers, boss_id="boss", brief="Public boss-only assignment", runtime=tmp_path, **kwargs)


def test_three_workers_delegate_act_reply_and_exhaust_budgets(tmp_path):
    seen = {who: [] for who in ("boss", "sales", "dispatch")}
    calls = []
    replies = set()

    def boss(obs):
        calls.append(obs.worker_id)
        seen["boss"].append(obs)
        replies.update(message.sender for message in obs.messages)
        if len(seen["boss"]) <= 2:
            target = ("sales", "dispatch")[len(seen["boss"]) - 1]
            return Action(
                "send_message", {"recipient": target, "text": f"Inspect {target}", "kind": "delegate"}
            )
        if len(replies) < 2:
            return Action("wait", {"seconds": 1})
        return Action("key", {"key": "ENTER"})

    def peer(who, gui_action):
        step = 0

        def policy(obs):
            nonlocal step
            calls.append(obs.worker_id)
            seen[who].append(obs)
            step += 1
            if step == 1:
                assert obs.messages[0].sender == "boss"
                return gui_action
            if step == 2:
                return Action("bash", {"command": "inspect-local-files"})
            if step == 3:
                assert obs.last_result["exit_code"] == 0
                return Action(
                    "send_message", {"recipient": "boss", "text": f"{who} checked", "kind": "reply"}
                )
            return Action("screenshot")

        return policy

    workers = [
        worker(
            "sales",
            peer("sales", Action("click", {"x": 12, "y": 34})),
            budget=Budget(4, 5),
            start_on_message=True,
        ),
        worker("boss", boss, budget=Budget(8, 5)),
        worker(
            "dispatch",
            peer("dispatch", Action("type", {"text": "prepared"})),
            budget=Budget(4, 5),
            start_on_message=True,
        ),
    ]
    report = asyncio.run(episode(tmp_path, workers, seconds=5).run())
    assert report["reason"] == "budget_exhausted"
    assert report["graded"] is False
    assert calls[0] == "boss"  # Boss need not be first in the roster.
    assert replies == {"sales", "dispatch"}
    assert seen["boss"][0].brief == "Public boss-only assignment"
    assert all(obs.brief is None for obs in seen["boss"][1:] + seen["sales"] + seen["dispatch"])
    messages = rows(tmp_path / "messages.jsonl")
    sent = [row for row in messages if row["event"] == "send"]
    assert [row["sequence"] for row in sent] == [1, 2, 3, 4]
    assert sum(row["kind"] == "delegate" for row in sent) == 2
    assert sum(row["kind"] == "reply" for row in sent) == 2
    assert all(row["sender_role"] == f"{row['sender']} role" for row in sent)
    assert len([row for row in messages if row["event"] == "receive"]) == 4
    for agent in workers:
        who = agent.worker_id
        result = report["workers"][who]
        assert result["reason"] == "action_budget"
        assert result["actions"] == agent.budget.actions
        trace = tmp_path / "vms" / who / "trace"
        events = rows(trace / "events.jsonl")
        assert events[-1]["event"] == "stop"
        assert len([row for row in events if row["event"] == "action"]) == agent.budget.actions
        assert any(row["event"] == "message_sent" for row in events)
        assert any(row["event"] == "message_received" for row in events)
        assert json.loads((trace / "receipt.json").read_text()) == result
        assert all(path.read_bytes().startswith(b"\x89PNG") for path in trace.glob("*.png"))
        assert len(list(trace.glob("*.png"))) == result["screens"] > 0
        assert [obs.elapsed for obs in seen[who]] == sorted(obs.elapsed for obs in seen[who])
    assert json.loads((tmp_path / "episode-result.json").read_text()) == report


def test_workers_really_overlap(tmp_path):
    async def scenario():
        entered = set()
        together = asyncio.Event()

        async def policy(obs):
            entered.add(obs.worker_id)
            if len(entered) == 3:
                together.set()
            await together.wait()  # Serial execution could never finish.
            return Action("done")

        report = await episode(tmp_path, [worker(who, policy) for who in ("boss", "a", "b")], seconds=2).run()
        assert report["reason"] == "all_done"
        assert entered == {"boss", "a", "b"}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "worker_seconds,episode_seconds,reason",
    [
        (0.04, 1, "worker_time_budget"),
        (1, 0.04, "episode_time_budget"),
    ],
)
def test_time_budget_cancels_policy(tmp_path, worker_seconds, episode_seconds, reason):
    cancelled = []

    async def slow_policy(obs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(obs.worker_id)

    agent = worker("boss", slow_policy, budget=Budget(10, worker_seconds))
    report = asyncio.run(episode(tmp_path, [agent], seconds=episode_seconds).run())
    assert report["workers"]["boss"]["reason"] == reason
    assert report["workers"]["boss"]["actions"] == 0
    assert cancelled == ["boss"]


def test_time_budget_cancels_backend_before_freezing_trace(tmp_path):
    class SlowBackend(FakeBackend):
        drained = False

        async def execute(self, action, *, deadline):
            try:
                await asyncio.Event().wait()
            finally:
                self.drained = True

    backend = SlowBackend()
    agent = WorkerAgent("boss", "manager", lambda obs: Action("bash", {"command": "slow"}), backend)
    report = asyncio.run(episode(tmp_path, [agent], seconds=0.04).run())
    assert backend.drained
    assert report["workers"]["boss"]["reason"] == "episode_time_budget"
    events = rows(tmp_path / "vms/boss/trace/events.jsonl")
    assert sum(row["event"] == "action" for row in events) == 1
    assert not any(row["event"] == "result" for row in events)


def test_late_activation_does_not_reset_worker_clock(tmp_path):
    calls = []
    workers = [
        worker("boss", lambda obs: Action("wait", {"seconds": 10})),
        worker("peer", lambda obs: calls.append(obs), budget=Budget(10, 0.02), start_on_message=True),
    ]
    report = asyncio.run(episode(tmp_path, workers, seconds=0.05).run())
    assert not calls
    assert report["workers"]["peer"]["reason"] == "worker_time_budget"
    assert report["workers"]["peer"]["actions"] == 0
    assert report["workers"]["boss"]["reason"] == "episode_time_budget"


def test_peer_can_activate_another_reserved_worker(tmp_path):
    def boss(obs):
        if obs.remaining_actions == 2:
            return Action("send_message", {"recipient": "peer", "text": "Coordinate the work"})
        return Action("done")

    def peer(obs):
        assert obs.messages[0].sender == "boss"
        return Action("send_message", {"recipient": "sub", "text": "Inspect records", "kind": "spawn"})

    def sub(obs):
        assert obs.brief is None
        assert obs.messages[0].sender == "peer"
        assert obs.messages[0].kind == "spawn"
        return Action("done")

    agents = [
        worker("boss", boss, budget=Budget(2, 2)),
        worker("peer", peer, budget=Budget(1, 2), start_on_message=True),
        worker("sub", sub, start_on_message=True),
    ]
    report = asyncio.run(episode(tmp_path, agents, seconds=2).run())
    assert report["workers"]["sub"]["reason"] == "done"


def test_bus_fifo_roles_wait_and_undelivered(tmp_path):
    async def scenario():
        bus = MessageBus(tmp_path / "messages.jsonl", {"a": "sales", "b": "ops"}, elapsed=lambda: 0.5)
        bus.send("a", "b", "first", "ask")
        bus.send("a", "b", "second")
        assert await bus.wait("b", 1) == "message"
        assert [message.text for message in bus.drain("b")] == ["first", "second"]
        assert bus.drain("b") == ()
        assert await bus.wait("b", 0.001) == "timeout"
        bus.send("a", "b", "unread")
        bus.finish("b")
        with pytest.raises(ValueError, match="active"):
            bus.send("a", "b", "too late")
        with pytest.raises(ValueError, match="active"):
            bus.send("unknown", "a", "forged")
        assert rows(bus.path)[-1]["event"] == "undelivered"

    asyncio.run(scenario())


def test_disabled_direct_channel_and_fake_shell(tmp_path):
    shell_target = tmp_path / "must-not-exist"
    actions = iter(
        [
            Action("bash", {"command": f"touch {shell_target}"}),
            Action("send_message", {"recipient": "peer", "text": "hello"}),
            Action("done", {"summary": "stopped"}),
        ]
    )
    seen = []
    agents = [
        worker("boss", lambda obs: seen.append(obs.last_result) or next(actions)),
        worker("peer", lambda obs: Action("done")),
    ]
    report = asyncio.run(episode(tmp_path, agents, direct_messages=False).run())
    assert not shell_target.exists()
    # The refused send is the boss's mistake to see and correct, not the episode's failure.
    assert report["workers"]["boss"]["reason"] == "done"
    assert any(r and "disabled" in r.get("error", "") for r in seen)
    assert (tmp_path / "messages.jsonl").read_text() == ""
    assert len(report["workers"]) == 2


def test_unknown_message_kind_is_an_error_result_not_an_episode_failure(tmp_path):
    actions = iter(
        [
            Action("send_message", {"recipient": "peer", "text": "hello", "kind": "update"}),
            Action("send_message", {"recipient": "peer", "text": "hello", "kind": "message"}),
            Action("done", {"summary": "stopped"}),
        ]
    )
    seen = []
    peer_actions = iter([Action("wait", {"seconds": 10}), Action("wait", {"seconds": 10}), Action("done")])
    agents = [
        worker("boss", lambda obs: seen.append(obs.last_result) or next(actions)),
        worker("peer", lambda obs: next(peer_actions)),
    ]
    report = asyncio.run(episode(tmp_path, agents).run())
    assert report["workers"]["boss"]["reason"] == "done"
    assert "Unknown message kind" in seen[1]["error"]
    assert seen[2] == {"sequence": 1}


@pytest.mark.parametrize("failure", [ValueError("bad policy"), TimeoutError("transport timeout")])
def test_one_workers_fault_ends_that_worker_and_leaves_its_peers_working(tmp_path, failure):
    """A worker's own failure used to cancel every peer, and that was the harness's costliest habit.

    Of the 27 episode results on disk on 2026-09-10, 10 ended on ``error``; in every one of them
    exactly one worker of five carried the error and the other four were cancelled, discarding 2,416
    seconds of guest time and 830 completed actions -- once at 930 s with 293 actions done, on a
    malformed ``open_url`` from a single policy. Each worker has its own VM and its own model
    process, so a peer's fault says nothing about this one.
    """

    class BrokenBackend(FakeBackend):
        async def observe(self, *, deadline):
            raise failure

    done = iter([Action("key", {"key": "ENTER"}), Action("done", {"summary": "boss finished"})])
    agents = [
        WorkerAgent("boss", "manager", lambda obs: next(done), FakeBackend()),
        WorkerAgent("peer", "peer role", lambda obs: Action("done"), BrokenBackend()),
    ]
    report = asyncio.run(episode(tmp_path, agents).run())
    assert report["workers"]["peer"]["reason"] == "error"
    assert str(failure) in report["workers"]["peer"]["error"]
    # The boss ran to its own end rather than being cancelled at the peer's first observation.
    assert report["workers"]["boss"]["reason"] == "done"
    assert report["workers"]["boss"]["actions"] == 2
    # ... and the episode still says a worker faulted, which is not the same as a clean run.
    assert report["reason"] == "worker_error"
    assert len(report["workers"]) == 2


def test_a_boss_that_fails_before_its_first_action_ends_the_episode(tmp_path):
    """The public brief reaches peers only through the boss, so a boss that has delegated nothing
    leaves no episode to go on with. That is also the shape of every instant failure on disk: four
    episodes that ended in 3-4 seconds with zero actions when the first model call failed.
    """

    class BrokenBackend(FakeBackend):
        async def observe(self, *, deadline):
            raise RuntimeError("ModelUnavailable: provider process failed")

    agents = [
        WorkerAgent("boss", "manager", lambda obs: Action("done"), BrokenBackend()),
        worker("peer", lambda obs: Action("wait", {"seconds": 30})),
    ]
    report = asyncio.run(episode(tmp_path, agents).run())
    assert report["reason"] == "error"
    assert report["workers"]["peer"]["actions"] == 0


def test_an_episode_where_every_worker_faulted_is_still_an_error(tmp_path):
    """``error`` has to keep meaning "nothing was learned": a dead fleet fails every worker."""

    class BrokenBackend(FakeBackend):
        async def observe(self, *, deadline):
            raise RuntimeError("VM SSH failed (255): Connection refused")

    agents = [
        WorkerAgent("boss", "manager", lambda obs: Action("done"), BrokenBackend()),
        WorkerAgent("peer", "peer role", lambda obs: Action("done"), BrokenBackend()),
    ]
    report = asyncio.run(episode(tmp_path, agents).run())
    assert report["reason"] == "error"
    assert {row["reason"] for row in report["workers"].values()} == {"error"}


def test_episode_reason_ranks_faults_above_budgets_above_a_clean_finish():
    from company_envs.world.harness import episode_reason

    assert episode_reason({"error"}) == "error"
    assert episode_reason({"error", "done"}) == "worker_error"
    assert episode_reason({"error", "episode_time_budget"}) == "worker_error"
    assert episode_reason({"done", "model_budget"}) == "budget_exhausted"
    assert episode_reason({"done"}) == "all_done"
    assert episode_reason(set()) == "all_done"


def test_external_cancel_drains_workers_and_records_stop(tmp_path):
    async def scenario():
        entered = asyncio.Event()

        async def policy(obs):
            entered.set()
            await asyncio.Event().wait()

        run = episode(tmp_path, [worker("boss", policy)])
        task = asyncio.create_task(run.run())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(task.done() for task in run._tasks)
        assert json.loads((tmp_path / "episode-result.json").read_text())["reason"] == "cancelled"

    asyncio.run(scenario())


def test_zero_budget_boss_releases_peers_and_refuses_stale_runs(tmp_path):
    agents = [
        worker("boss", lambda obs: pytest.fail("zero budget sampled"), budget=Budget(0, 2)),
        worker("peer", lambda obs: Action("done")),
    ]
    run = episode(tmp_path, agents)
    report = asyncio.run(run.run())
    assert report["workers"]["boss"]["reason"] == "action_budget"
    assert report["workers"]["peer"]["reason"] == "done"
    before = (tmp_path / "messages.jsonl").read_bytes()
    with pytest.raises(RuntimeError, match="single-use"):
        asyncio.run(run.run())
    with pytest.raises(FileExistsError):
        asyncio.run(episode(tmp_path, agents).run())
    assert (tmp_path / "messages.jsonl").read_bytes() == before


@pytest.mark.parametrize(
    "action",
    [
        Action("click", {"x": -1, "y": 1}),
        Action("unknown"),
        Action("bash", {"command": "x", "worker_id": "peer"}),
        Action("send_message", {"recipient": "peer", "text": "x", "sender_role": "boss"}),
        Action("wait", {"seconds": float("nan")}),
    ],
)
def test_action_validation(action):
    with pytest.raises(ValueError):
        action.validate()


def test_roster_and_budget_validation(tmp_path):
    with pytest.raises(ValueError, match="path"):
        episode(tmp_path, [worker("boss", None), worker("../escape", None)])
    with pytest.raises(ValueError, match="unique"):
        episode(tmp_path, [worker("boss", None), worker("boss", None)])
    a, b = worker("boss", None), worker("peer", None)
    b.backend = a.backend
    with pytest.raises(ValueError, match="own backend"):
        episode(tmp_path, [a, b])
    with pytest.raises(ValueError):
        Budget(-1, 2)
    with pytest.raises(ValueError):
        Budget(1, float("inf"))


def test_model_budget_exhaustion_ends_the_worker_not_the_episode(tmp_path):
    from company_envs.world.harness import BudgetExhausted

    def broke(obs):
        raise BudgetExhausted("model call budget exhausted")

    peer_actions = iter([Action("wait", {"seconds": 10}), Action("done")])
    agents = [worker("boss", broke), worker("peer", lambda obs: next(peer_actions))]
    report = asyncio.run(episode(tmp_path, agents).run())
    assert report["workers"]["boss"]["reason"] == "model_budget"
    assert report["workers"]["peer"]["reason"] == "done"
    assert report["reason"] != "error"
