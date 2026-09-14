"""Concurrent, model-free worker harness; see docs/AGENT_HARNESS.md.

Backends are bound to one worker's VM. A real MyPCBench adapter belongs behind
Backend, with cancellable screenshot/CUA transport and bash over that VM's SSH.
Policies are trusted, quick synchronous doubles or cancellation-cooperative async
callables. Blocking model/VM calls must not run on this event loop.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import math
import re
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

ActionName = Literal["click", "type", "key", "open_url", "screenshot", "bash", "send_message", "wait", "done"]
MessageKind = Literal["delegate", "ask", "spawn", "reply", "message"]


def _seconds(value: float) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError("Seconds must be positive and finite")


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        raise ValueError("Worker IDs must be safe path components")


def _append(path: Path, value: Any) -> None:
    line = json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line)


@dataclass(frozen=True)
class Action:
    name: ActionName
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        required = {
            "click": {"x", "y"},
            "type": {"text"},
            "key": {"key"},
            "open_url": {"url"},
            "screenshot": set(),
            "bash": {"command"},
            "send_message": {"recipient", "text"},
            "wait": {"seconds"},
            "done": set(),
        }
        optional = {"send_message": {"kind"}, "done": {"summary"}}
        if self.name not in required:
            raise ValueError(f"Unknown action: {self.name}")
        keys = set(self.arguments)
        if not required[self.name] <= keys or keys - required[self.name] - optional.get(self.name, set()):
            raise ValueError(f"Invalid arguments for {self.name}")
        for key, value in self.arguments.items():
            if key in {"x", "y"}:
                if type(value) is not int or value < 0:
                    raise ValueError("Click coordinates must be nonnegative integers")
            elif key == "seconds":
                _seconds(value)
            elif not isinstance(value, str):
                raise ValueError(f"{key} must be text")
            elif key == "url" and not value.startswith(("http://", "https://", "file://")):
                raise ValueError("open_url requires an http, https or file URL")


@dataclass(frozen=True)
class Budget:
    actions: int = 100
    seconds: float = 300

    def __post_init__(self) -> None:
        if type(self.actions) is not int or self.actions < 0:
            raise ValueError("Action budget must be a nonnegative integer")
        _seconds(self.seconds)


@dataclass(frozen=True)
class Screen:
    png: bytes
    text: str = ""


@dataclass(frozen=True)
class Message:
    sequence: int
    elapsed: float
    sender: str
    sender_role: str
    recipient: str
    recipient_role: str
    text: str
    kind: MessageKind = "message"


@dataclass(frozen=True)
class Observation:
    worker_id: str
    role: str
    roster: tuple[tuple[str, str], ...]
    screen: Screen
    messages: tuple[Message, ...]
    brief: str | None
    elapsed: float
    remaining_seconds: float
    remaining_actions: int
    last_result: Mapping[str, Any] | None


class Backend(Protocol):
    """One instance per VM; operations must honor deadline and cancellation.

    deadline is absolute asyncio loop.time(). execute accepts only click/type/key/
    bash/open_url; coordination stays in the harness. Returned dictionaries must be JSON.
    Cancellation must stop/drain remote effects before returning. The launcher
    owns VM creation/teardown; this protocol owns only admitted operations.
    """

    async def observe(self, *, deadline: float) -> Screen: ...

    async def execute(self, action: Action, *, deadline: float) -> dict[str, Any]: ...


class FakeBackend:
    """Records actions, returns a valid 1px PNG, and NEVER executes shell text."""

    PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
    )

    def __init__(self, *, bash_results: Mapping[str, Mapping[str, Any]] | None = None):
        self.actions: list[Action] = []
        self.bash_results = dict(bash_results or {})

    async def observe(self, *, deadline: float) -> Screen:
        await asyncio.sleep(0)
        return Screen(self.PNG, f"Fake desktop; {len(self.actions)} actions")

    async def execute(self, action: Action, *, deadline: float) -> dict[str, Any]:
        await asyncio.sleep(0)
        self.actions.append(action)
        if action.name == "bash":
            return dict(
                self.bash_results.get(
                    action.arguments["command"],
                    {
                        "stdout": "fake: command recorded",
                        "stderr": "",
                        "exit_code": 0,
                    },
                )
            )
        return {"ok": True}


class BudgetExhausted(RuntimeError):
    """A worker's model-call budget is spent: that worker is done, the episode goes on."""


def episode_reason(reasons):
    """How the episode ended, from the set of worker stop reasons.

    ``error`` means every worker ended on a fault, so nothing was learned. ``worker_error`` means
    some did and some worked: the episode is not a clean run, and what the others did still happened
    and is still worth grading. Keeping those two apart is the difference between discarding a
    company and reading a real grade off four workers' work.
    """
    if not reasons:
        return "all_done"
    if reasons == {"error"}:
        return "error"
    if "error" in reasons:
        return "worker_error"
    return "budget_exhausted" if any("budget" in r for r in reasons) else "all_done"


class MessageBus:
    """Single-event-loop FIFO bus. Sender identity/roles come from the roster.

    Journal send before delivery; receive/undelivered events reference sequence.
    No replay/resume or cross-process transport is implied by this scaffold.
    """

    def __init__(
        self, path: Path, roles: Mapping[str, str], *, elapsed: Callable[[], float], enabled: bool = True
    ):
        self.path, self.roles, self.elapsed = Path(path), dict(roles), elapsed
        self.enabled = enabled
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=False)
        self._queues: dict[str, deque[Message]] = {who: deque() for who in roles}
        self._events = {who: asyncio.Event() for who in roles}
        self._active = set(roles)
        self._sequence = 0

    def send(self, sender: str, recipient: str, text: str, kind: MessageKind = "message") -> Message:
        if not self.enabled:
            raise ValueError("Direct messaging is disabled; communicate through company apps")
        if sender not in self._active or recipient not in self._active or sender == recipient:
            raise ValueError("Messages require distinct active roster members")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Message must be nonempty text")
        if kind not in {"delegate", "ask", "spawn", "reply", "message"}:
            raise ValueError("Unknown message kind")
        message = Message(
            self._sequence + 1,
            self.elapsed(),
            sender,
            self.roles[sender],
            recipient,
            self.roles[recipient],
            text,
            kind,
        )
        _append(self.path, {"event": "send", **asdict(message)})
        self._sequence += 1
        self._queues[recipient].append(message)
        self._events[recipient].set()
        return message

    def drain(self, recipient: str) -> tuple[Message, ...]:
        messages = tuple(self._queues[recipient])
        for message in messages:
            _append(
                self.path,
                {
                    "event": "receive",
                    "sequence": message.sequence,
                    "recipient": recipient,
                    "elapsed": self.elapsed(),
                },
            )
        self._queues[recipient].clear()
        self._events[recipient].clear()
        return messages

    async def wait(self, recipient: str, seconds: float) -> str:
        _seconds(seconds)
        try:
            async with asyncio.timeout(seconds):
                await self._events[recipient].wait()
            return "message" if self._queues[recipient] else "closed"
        except TimeoutError:
            return "timeout"

    def finish(self, recipient: str) -> None:
        self._active.discard(recipient)
        while self._queues[recipient]:
            message = self._queues[recipient].popleft()
            _append(
                self.path,
                {
                    "event": "undelivered",
                    "sequence": message.sequence,
                    "recipient": recipient,
                    "elapsed": self.elapsed(),
                },
            )
        self._events[recipient].set()


Policy = Callable[[Observation], Action | Awaitable[Action]]


@dataclass
class WorkerAgent:
    worker_id: str
    role: str
    policy: Policy
    backend: Backend
    budget: Budget = field(default_factory=Budget)
    start_on_message: bool = False

    async def run(self, episode: Episode) -> dict[str, Any]:
        """Observe, decide and admit one action at a time on this worker's VM."""
        who = self.worker_id
        trace = episode.runtime / "vms" / who / "trace"
        deadline = episode.started + min(self.budget.seconds, episode.seconds)
        count, screen_count = 0, 0
        reason, error = "done", None
        last_result = None
        timer = asyncio.timeout_at(deadline)

        def log(event: str, **data: Any) -> None:
            _append(trace / "events.jsonl", {"event": event, "elapsed": episode.elapsed(), **data})

        def fault(exc: BaseException) -> str:
            """Record this worker's own fault, and end the episode only if nobody can go on.

            A worker's failure used to cancel every peer, and the measurement says that was the
            harness's costliest habit: of the 27 episode results on disk on 2026-09-10, 10 ended on
            ``error``, in every one of them exactly one worker of five carried the error, and
            cancelling the other four discarded 2,416 seconds of guest time and 830 completed worker
            actions -- once at 930 s with 293 actions already done, on a malformed ``open_url`` from a
            single policy. Each worker has its own VM and its own model process, so a peer's fault
            says nothing about this one, and a fleet that is really gone fails every worker on its own
            first call anyway (the episode reason is then still ``error``).

            The one exception is the boss before its first action. The public brief reaches peers only
            through the boss, so a boss that fails having delegated nothing leaves no episode to go on
            with -- which is also the shape of every instant failure on disk: four episodes that ended
            in 3-4 seconds with zero actions.
            """
            if who == episode.boss_id and count == 0:
                episode.stop("error")
            return f"{type(exc).__name__}: {exc}"

        async def capture() -> Screen:
            nonlocal screen_count
            screen = await self.backend.observe(deadline=deadline)
            filename = f"screen-{screen_count:06d}.png"
            (trace / filename).write_bytes(screen.png)
            screen_count += 1
            log("screen", file=filename, text=screen.text)
            return screen

        try:
            async with timer:
                await asyncio.sleep(0)  # Enter every worker's cleanup scope before any can fail.
                if who != episode.boss_id:
                    await episode._boss_ready.wait()
                if self.start_on_message:
                    log("awaiting_assignment")
                    await episode.bus.wait(who, max(0.000001, deadline - episode.now()))
                while count < self.budget.actions:
                    if episode.now() >= deadline:
                        reason = episode.time_reason(self)
                        break
                    screen = await capture()
                    messages = episode.bus.drain(who)
                    for message in messages:
                        log("message_received", message=asdict(message))
                    brief = episode.brief if who == episode.boss_id and count == 0 else None
                    observation = Observation(
                        who,
                        self.role,
                        tuple(episode.roles.items()),
                        screen,
                        messages,
                        brief,
                        episode.elapsed(),
                        max(0, deadline - episode.now()),
                        self.budget.actions - count,
                        last_result,
                    )
                    log(
                        "observation",
                        brief=brief,
                        remaining_actions=observation.remaining_actions,
                        remaining_seconds=observation.remaining_seconds,
                    )
                    if who == episode.boss_id:
                        episode._boss_ready.set()
                    action = self.policy(observation)
                    if inspect.isawaitable(action):
                        action = await action
                    # Even a synchronous double must not admit effects after its deadline.
                    if episode.now() >= deadline:
                        reason = episode.time_reason(self)
                        break
                    if not isinstance(action, Action):
                        raise TypeError("Policy must return Action")
                    action.validate()
                    # Snapshot mutable policy arguments before dispatch and serialization.
                    action = Action(action.name, json.loads(json.dumps(dict(action.arguments))))
                    count += 1
                    log("action", number=count, name=action.name, arguments=dict(action.arguments))
                    args = action.arguments
                    if action.name == "send_message":
                        try:
                            message = episode.bus.send(
                                who, args["recipient"], args["text"], args.get("kind", "message")
                            )
                        except ValueError as exc:
                            # A bad recipient or kind is the worker's mistake to correct, not the
                            # episode's failure: it costs the action and comes back as the result.
                            last_result = {"error": str(exc)}
                            log("result", number=count, result=last_result)
                            await asyncio.sleep(0)
                            continue
                        log("message_sent", message=asdict(message))
                        last_result = {"sequence": message.sequence}
                    elif action.name == "wait":
                        last_result = {"wake_reason": await episode.bus.wait(who, args["seconds"])}
                    elif action.name == "done":
                        last_result = {"summary": args.get("summary", "")}
                    elif action.name == "screenshot":
                        await capture()
                        last_result = {"screen": f"screen-{screen_count - 1:06d}.png"}
                    else:
                        last_result = await self.backend.execute(action, deadline=deadline)
                        log("backend_result", number=count, result=last_result)
                        await capture()  # Preserve the final visible effect even at the action cap.
                    log("result", number=count, result=last_result)
                    if action.name == "done":
                        break
                    await asyncio.sleep(0)
                else:
                    reason = "action_budget"
        except TimeoutError as exc:
            if timer.expired():
                reason = episode.time_reason(self)
            else:
                reason, error = "error", fault(exc)
        except asyncio.CancelledError:
            reason = episode.stop_reason or "cancelled"
            raise
        except BudgetExhausted:
            reason = "model_budget"
        except Exception as exc:  # noqa: BLE001 -- isolate arbitrary policy/backend failures.
            reason, error = "error", fault(exc)
        finally:
            if who == episode.boss_id:
                episode._boss_ready.set()  # Zero budget/failure cannot strand peers.
            episode.bus.finish(who)
            receipt = {
                "worker_id": who,
                "role": self.role,
                "reason": reason,
                "actions": count,
                "screens": screen_count,
                "elapsed": episode.elapsed(),
                "error": error,
            }
            log("stop", **receipt)
            (trace / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
            episode.results[who] = receipt
        return receipt


class Episode:
    """One-shot concurrent episode in an existing company runtime directory.

    Only public brief/roster enter this interface. It never reads workflow.json,
    seeds apps, launches VMs, calls models, or assigns a business grade.
    """

    def __init__(
        self,
        workers: Sequence[WorkerAgent],
        *,
        boss_id: str,
        brief: str,
        runtime: Path,
        seconds: float = 300,
        direct_messages: bool = True,
    ):
        _seconds(seconds)
        self.workers = tuple(workers)
        self.roles = {worker.worker_id: worker.role for worker in self.workers}
        if not self.workers or len(self.roles) != len(self.workers):
            raise ValueError("Provide a nonempty, unique worker roster")
        for worker in self.workers:
            _identifier(worker.worker_id)
            if not isinstance(worker.role, str) or not worker.role.strip():
                raise ValueError("Every worker needs a role")
            if worker.start_on_message and (not direct_messages or worker.worker_id == boss_id):
                raise ValueError("Only non-boss workers with direct messaging may start on message")
        if boss_id not in self.roles or not isinstance(brief, str) or not brief.strip():
            raise ValueError("Provide a roster boss and nonempty public brief")
        if len({id(worker.backend) for worker in self.workers}) != len(self.workers):
            raise ValueError("Each worker needs its own backend/VM")
        self.boss_id, self.brief, self.runtime = boss_id, brief, Path(runtime)
        self.seconds, self.direct_messages = seconds, direct_messages
        self.results: dict[str, dict[str, Any]] = {}
        self.stop_reason: str | None = None
        self._ran = False
        self._tasks: list[asyncio.Task] = []
        self._boss_ready = asyncio.Event()

    @staticmethod
    def now() -> float:
        return asyncio.get_running_loop().time()

    def elapsed(self) -> float:
        return self.now() - self.started

    def time_reason(self, worker: WorkerAgent) -> str:
        return "episode_time_budget" if self.seconds <= worker.budget.seconds else "worker_time_budget"

    def stop(self, reason: str = "stopped") -> None:
        """Stop admission and cancel cooperative operations; run() drains tasks."""
        self.stop_reason = self.stop_reason or reason
        for task in self._tasks:
            if task is not asyncio.current_task() and not task.done():
                task.cancel()

    async def run(self) -> dict[str, Any]:
        if self._ran:
            raise RuntimeError("Episode instances are single-use")
        self._ran = True
        self.runtime.mkdir(parents=True, exist_ok=True)
        # Claim before touching traces; refuse stale attempts without deleting them.
        with (self.runtime / "episode.json").open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "schema_version": 1,
                    "started_at": datetime.now(UTC).isoformat(),
                    "boss_id": self.boss_id,
                    "direct_messages": self.direct_messages,
                    "seconds": self.seconds,
                    "workers": [
                        {
                            "id": w.worker_id,
                            "role": w.role,
                            "budget": asdict(w.budget),
                            "start_on_message": w.start_on_message,
                        }
                        for w in self.workers
                    ],
                },
                stream,
                indent=2,
            )
        for worker in self.workers:
            trace = self.runtime / "vms" / worker.worker_id / "trace"
            trace.mkdir(parents=True, exist_ok=False)
            (trace / "events.jsonl").touch()
        self.started = self.now()
        self.bus = MessageBus(
            self.runtime / "messages.jsonl", self.roles, elapsed=self.elapsed, enabled=self.direct_messages
        )
        # All tasks exist before execution; peers wait until boss observation delivery.
        self._tasks = [
            asyncio.create_task(worker.run(self), name=worker.worker_id) for worker in self.workers
        ]
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            self.stop(self.stop_reason or "cancelled")
            await asyncio.gather(*self._tasks, return_exceptions=True)
            if self.stop_reason == "cancelled":
                raise
        except Exception:  # Drain peers even when trace persistence fails.
            self.stop("error")
            await asyncio.gather(*self._tasks, return_exceptions=True)
            raise
        finally:
            reasons = {result["reason"] for result in self.results.values()}
            reason = self.stop_reason or episode_reason(reasons)
            report = {
                "schema_version": 1,
                "reason": reason,
                "elapsed": self.elapsed(),
                "workers": self.results,
                "graded": False,
            }
            (self.runtime / "episode-result.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
