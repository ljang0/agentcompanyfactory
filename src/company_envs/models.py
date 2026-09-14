"""Replaceable CLI backends, structured responses, cached calls, and bounded subprocesses."""

import copy
import fcntl
import itertools
import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from pydantic import ValidationError

from .preflight import EnvironmentBlocked, require_model_environment
from .storage import digest, now, read, write

# Bump only when the request/transport contract changes; shared by cache keys and invocation metadata.
MODEL_ADAPTER_VERSION = 5

_processes = set()
_process_lock = threading.Lock()
_cancelled = threading.Event()


def reset_cancellation():
    _cancelled.clear()


def cancel_calls():
    """Terminate only subprocess groups created by this coordinator process."""
    _cancelled.set()
    with _process_lock:
        for proc in _processes:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


class ModelUnavailable(RuntimeError):
    def __init__(self, message, *, retryable=False, global_failure=False):
        super().__init__(message)
        self.retryable = retryable
        self.global_failure = global_failure


def failure_policy(exc):
    """Only explicit transport failures are retried; setup failures stop shared work."""
    if isinstance(exc, ModelUnavailable):
        return exc.retryable, exc.global_failure
    if isinstance(exc, subprocess.TimeoutExpired):
        return True, False
    if isinstance(exc, FileNotFoundError) and exc.filename == "codex":
        return False, True
    return False, False


def provider_failure(details):
    message = details.casefold()
    shared = any(
        term in message
        for term in (
            "unauthorized",
            "authentication",
            "invalid api key",
            "insufficient_quota",
            "quota exhausted",
            "usage limit",
            "model_not_found",
            "unknown model",
            "read-only file system",
            "failed to initialize in-process app-server client",
        )
    )
    # A busy provider answers again in a minute; a timeout or a reset does not.
    busy = (
        not shared
        and "input_too_large" not in message
        and any(
            term in message
            for term in ("at capacity", "turn/start failed", "rate limit", "429", "502", "503", "504")
        )
    )
    # A prompt over the provider's hard input ceiling is refused identically every time. It arrives
    # wrapped in transport words ("turn/start failed"), which read as transient, so it was retried
    # until something else stopped it: one macerich authoring call made 162 attempts, each failing
    # in under half a second. The size is the caller's to fix; retrying it is never the answer.
    oversized = any(
        term in message
        for term in ("input_too_large", "exceeds the maximum length", "input exceeds the maximum")
    )
    transient = (
        not shared
        and not oversized
        and any(
            term in message
            for term in (
                "rate limit",
                "turn/start failed",
                "429",
                "502",
                "503",
                "504",
                "connection reset",
                "timed out",
                "temporarily unavailable",
                "at capacity",
                "stream disconnected",
            )
        )
    )
    failure = ModelUnavailable(
        f"provider process failed: {details}", retryable=transient, global_failure=shared
    )
    failure.busy = busy
    return failure


# Jobs whose invalid drafts are kept and raised as ModelOutputInvalid so the caller can send the
# reason back for a revision instead of treating the answer as a transport failure.
# Jobs that write a whole world or app and need the longer authoring budget, not the completion one.
AUTHORING_JOBS = frozenset({"world_states", "world_seed", "world_review"})


def timeout_key(job):
    """Which generation budget a job is given.

    Authoring a golden path, grader or rubric reads a whole bounded world first, and writing an
    app's records is the longest call the pipeline makes: measured live, the slowest tenth ran past
    ten minutes, so the ordinary completion budget timed them out and threw the work away.
    """
    if job in ("discover", "research"):
        return "research_timeout_seconds"
    if job.startswith("task_") or job in AUTHORING_JOBS:
        return "authoring_timeout_seconds"
    return "completion_timeout_seconds"


REVISABLE_JOBS = frozenset(
    {
        "research",
        "research_repair",
        "expand",
        "world_seed",
        "app_bindings",
        "runtime_program",
        "task_golden",
        "task_grader",
    }
)


PROMPT_CEILING = 1_048_576  # the provider refuses a longer prompt; the same number world_repair uses


class PromptTooLarge(ValueError):
    """The prompt is longer than the provider will accept, so sending it cannot succeed.

    Raised before dispatch. The provider's own refusal arrives wrapped in transport wording and
    reads as transient, which is how one call reached 162 attempts that each failed in under half
    a second. Refusing here names the real problem and hands the caller the size to shrink.
    """

    def __init__(self, job, size, ceiling=PROMPT_CEILING):
        super().__init__(
            f"{job}: prompt is {size:,} characters, over the {ceiling:,} the provider accepts; "
            "shrink what is sent rather than retrying"
        )
        self.job, self.size, self.ceiling = job, size, ceiling


class ModelOutputInvalid(ValueError):
    """A provider responded, but its draft needs the bounded author repair."""

    def __init__(self, message, raw, receipt=None):
        super().__init__(message)
        self.raw = raw
        self.receipt = receipt


def strict_schema(model):
    schema = copy.deepcopy(model.model_json_schema())

    def visit(node):
        if isinstance(node, dict):
            node.pop("default", None)
            # Pydantic emits annotated references, but the provider rejects
            # descriptive siblings of $ref. These annotations aren't constraints.
            if "$ref" in node:
                node.pop("description", None)
                node.pop("title", None)
            if "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return schema


def command(spec, directory, schema, research, reasoning, tool_context=None, service_tier="fast", images=()):
    if not spec.startswith("codex/gpt-"):
        raise ValueError(f"GPT-only policy disables model: {spec}")
    backend, model = spec.split("/", 1)
    if backend == "codex":
        cmd = [
            "codex",
            "-a",
            "never",
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-C",
            str(directory),
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning}"',
            "-c",
            f'service_tier="{service_tier}"',
            "-c",
            "features.shell_tool=false",
            "-c",
            "features.apps=false",
            "-c",
            "features.remote_plugin=false",
            "-c",
            "features.plugins=false",
            "-c",
            "agents.enabled=false",
            "-c",
            "features.multi_agent=false",
            "-c",
            "features.skill_search=false",
            "-c",
            "features.skip_host_skill_discovery=true",
            "-c",
            "features.hooks=false",
            "-c",
            'web_search="live"' if research else 'web_search="disabled"',
            "--json",
            "--output-schema",
            str(directory / "schema.json"),
            "--output-last-message",
            str(directory / "answer.json"),
            "-",
        ]
        # Project skills are separate from host discovery. Complete frozen skill
        # bodies are already inlined; don't advertise a second live copy.
        skill_files = sorted(Path(__file__).resolve().parents[2].glob(".agents/skills/*/SKILL.md"))
        if skill_files:
            disabled = ",".join(f"{{path={json.dumps(str(p))},enabled=false}}" for p in skill_files)
            cmd[-1:-1] = ["-c", f"skills.config=[{disabled}]"]
        if tool_context is not None:
            from .stage_tools import TOOL_NAMES

            options = {
                "command": sys.executable,
                "args": ["-m", "company_envs.stage_tools", str(tool_context)],
                "enabled_tools": TOOL_NAMES,
                "default_tools_approval_mode": "approve",
                "startup_timeout_sec": 15,
                "tool_timeout_sec": 15,
            }
            for key, value in options.items():
                cmd[-1:-1] = ["-c", f"mcp_servers.stage1.{key}={json.dumps(value)}"]
        for image in images:
            # Codex attaches one image per --image flag; the prompt still arrives on stdin.
            cmd[-1:-1] = ["--image", str(image)]
        return cmd
    raise ValueError(f"unknown backend: {backend}")


_home_lock = threading.Lock()
_home_counter = itertools.count(os.getpid())  # fresh processes start on different accounts


def codex_homes(config):
    """Configured Codex account directories that actually hold a login; [] means the default."""
    homes = []
    for raw in config.get("models", {}).get("codex_homes", []) or []:
        path = Path(os.path.expanduser(str(raw)))
        if (path / "auth.json").is_file():
            homes.append(str(path))
    return homes


_exhausted = {}  # codex home -> monotonic time when it may be tried again
EXHAUSTED_COOLDOWN_SECONDS = 600
TRANSIENT_RETRIES = 2
TRANSIENT_WAIT_SECONDS = 45


EXHAUSTED_MARKER = ".company-envs-exhausted-until"  # inside the codex home; holds a wall-clock time


def _exhausted_until(home):
    """The shared marker's wall-clock time, so fresh processes skip an account that just hit its limit."""
    try:
        return float(Path(home).joinpath(EXHAUSTED_MARKER).read_text().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def available_homes(config):
    """Logged-in accounts that have not hit their usage limit in the last cooldown window."""
    now_, wall = time.monotonic(), time.time()
    with _home_lock:
        return [
            h for h in codex_homes(config) if _exhausted.get(h, 0) <= now_ and _exhausted_until(h) <= wall
        ]


def mark_exhausted(home):
    with _home_lock:
        _exhausted[home] = time.monotonic() + EXHAUSTED_COOLDOWN_SECONDS
    try:
        Path(home).joinpath(EXHAUSTED_MARKER).write_text(str(time.time() + EXHAUSTED_COOLDOWN_SECONDS))
    except OSError:
        pass  # the in-process marker still holds


def next_codex_home(config):
    """Round-robin over accounts with quota left, so one exhausted account does not stop the run."""
    homes = available_homes(config) or codex_homes(config)
    if not homes:
        return None
    with _home_lock:
        return homes[next(_home_counter) % len(homes)]


def call_environment(env_overrides=None):
    """Process environment for a model call: no Claude variables, plus the selected account."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env.update(env_overrides or {})
    return env


def execute(cmd, prompt, directory, timeout, env_overrides=None):
    env = call_environment(env_overrides)
    start = time.monotonic()
    with (directory / "stdout.jsonl").open("w") as stdout, (directory / "stderr.txt").open("w") as stderr:
        proc = subprocess.Popen(
            cmd,
            cwd=directory,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            text=True,
            env=env,
            start_new_session=True,
        )
        with _process_lock:
            _processes.add(proc)
        try:
            proc.communicate(prompt, timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            raise
        finally:
            with _process_lock:
                _processes.discard(proc)
    return proc.returncode, time.monotonic() - start


def read_codex_telemetry(directory, receipt, *, best_effort=False):
    """Retain observed events even when no final answer was returned.

    Reading is idempotent: completed tool records replace, rather than append
    to, the previous summary. Partial JSON lines do not constitute events.
    Unknown usage/cost stays unknown; telemetry never promotes a failed call.
    """
    try:
        lines = (Path(directory) / "stdout.jsonl").read_text().splitlines()
    except (OSError, UnicodeError) as exc:
        if not best_effort:
            raise
        receipt["telemetry_error"] = f"{type(exc).__name__}: {exc}"
        return []
    events, calls = [], []
    for line in lines:
        try:
            event = json.loads(line)
        except RecursionError as exc:
            if not best_effort:
                raise ValueError("completion contained an excessively nested transcript event") from exc
            receipt["telemetry_error"] = "RecursionError: transcript event exceeded the JSON nesting limit"
            continue
        except ValueError:
            continue
        events.append(event)
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started":
            receipt["session_id"] = event.get("thread_id")
        if event.get("type") == "turn.completed":
            receipt["usage"] = event.get("usage", {})
        item = event.get("item", {})
        if (
            isinstance(item, dict)
            and item.get("type") == "mcp_tool_call"
            and event.get("type") == "item.completed"
        ):
            calls.append(
                {"server": item.get("server"), "tool": item.get("tool"), "status": item.get("status")}
            )
    receipt["tool_calls"] = calls
    return events


def dispatched_calls(run_dir):
    """Calls already dispatched from this directory, counted by the attempts they recorded."""
    return sum(1 for _ in Path(run_dir).glob("calls/*/attempt-*"))


class CallBudgetExhausted(RuntimeError):
    """This unit of work has spent its allowance of model calls.

    A world is not otherwise capped: the most expensive one authored so far spent 653 calls over
    43 hours, and nothing would have stopped it. A budget turns a runaway into a stop with a
    reason the caller can record, the way a wall-clock timeout already does.
    """

    def __init__(self, spent, budget):
        super().__init__(f"model call budget exhausted: {spent} of {budget} spent")
        self.spent, self.budget = spent, budget


@contextmanager
def _file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def _model_slot(run_dir, limit):
    """One shared process/thread limit for every author using this run directory."""
    directory = run_dir / "calls/.coordination/slots"
    directory.mkdir(parents=True, exist_ok=True)
    while True:
        if _cancelled.is_set():
            raise ModelUnavailable("model calls cancelled while waiting for a slot", global_failure=True)
        for index in range(limit):
            handle = (directory / str(index)).open("a")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()
            return
        time.sleep(0.05)


class Models:
    def __init__(self, config, run_dir, *, cache_only=False, max_calls=None, cumulative=False):
        self.config = config
        # Child processes run inside the attempt directory. Relative -C, schema,
        # output and MCP-context paths would otherwise resolve beneath it again.
        self.run_dir = Path(run_dir).resolve()
        self.cache_only = cache_only
        # None means uncapped, which is what every caller that has no budget of its own gets.
        self.max_calls = int(max_calls) if max_calls else None
        self.cumulative = cumulative
        # A budget is per unit of work, and a retry is the same unit. Counting only this process's
        # calls gave each of the driver's three attempts a fresh allowance: one company reached 900
        # calls against a budget of 500, and the 22 companies that passed it hold a third of all
        # compute with 20 of them producing nothing. Counting what the directory already records
        # spends the configured number once.
        self.calls_spent = dispatched_calls(self.run_dir) if (cumulative and self.max_calls) else 0

    def call(self, job, prompt, response_type, *args, **kwargs):
        model_job = "research" if job == "research_repair" else job
        permitted = any(spec.startswith("codex/gpt-") for spec in self.config["models"][model_job])
        if self.cache_only or not permitted:
            return self._call(job, prompt, response_type, *args, **kwargs)
        # Serialize identical requests before reading their cache, while unrelated calls
        # share four slots even when separate helper processes each own a Models instance.
        request_key = digest({"job": job, "prompt": prompt, "response": response_type.__qualname__})
        limit = min(4, max(1, int(self.config.get("design", {}).get("model_calls_in_flight", 4))))
        with (
            _file_lock(self.run_dir / "calls/.coordination/requests" / request_key),
            _model_slot(self.run_dir, limit),
        ):
            return self._call(job, prompt, response_type, *args, **kwargs)

    def _call(
        self,
        job,
        prompt,
        response_type,
        avoid=(),
        schema=None,
        validate=None,
        context=None,
        timeout_seconds=None,
        images=(),
    ):
        schema = schema or strict_schema(response_type)
        research = job in ("discover", "research")
        generation = self.config["generation"]
        key = timeout_key(job)
        timeout = timeout_seconds or generation.get(key) or generation["completion_timeout_seconds"]
        failures, policies = [], []
        model_job = "research" if job == "research_repair" else job
        queue = list(self.config["models"][model_job])
        account_retries = 0
        transient_retries = 0
        timeout_retries = 0
        while queue:
            spec = queue.pop(0)
            if _cancelled.is_set():
                raise ModelUnavailable(
                    "model calls cancelled by coordinator interruption", global_failure=True
                )
            if spec in avoid:
                continue
            # Also applies to frozen configs on resume, before cache lookup.
            # Historical artifacts remain readable; new work must be GPT-only.
            if not spec.startswith("codex/gpt-"):
                failures.append(f"{spec}: disabled by GPT-only policy")
                policies.append((False, True))
                continue
            fingerprint = {
                "job": job,
                "prompt": prompt,
                "schema": schema,
                "model": spec,
                "reasoning": self.config["models"]["reasoning"],
                "adapter": MODEL_ADAPTER_VERSION,
                "tool_context": context,
                "tool_implementation": digest(Path(__file__).with_name("stage_tools.py").read_bytes())
                if context is not None
                else None,
            }
            if images:
                # A picture is part of the question. Without it, a screen whose prompt text matches
                # an earlier one is answered from that screen's verdict, and a re-render after a
                # repair returns the verdict for the picture the repair replaced. Added only when
                # there are images, so every cached answer for a text-only call stays valid.
                fingerprint["images"] = [digest(Path(image).read_bytes()) for image in images]
            key = digest(fingerprint)
            base = self.run_dir / "calls" / key
            saved = base / "result.json"
            if saved.exists():
                result = read(saved)
                parsed = response_type.model_validate(result["data"])
                if validate:
                    validate(parsed)
                return parsed, result["receipt"]
            invalid = base / "invalid.json"
            if invalid.exists() and job in REVISABLE_JOBS:
                draft = read(invalid)
                raise ModelOutputInvalid(draft["error"], draft["data"], draft.get("receipt"))
            if self.cache_only:
                failures.append(f"cache miss for {job}: {key} ({spec})")
                policies.append((False, False))
                continue
            if len(prompt) > PROMPT_CEILING:
                raise PromptTooLarge(job, len(prompt))
            home = next_codex_home(self.config)
            try:
                require_model_environment(home)
            except EnvironmentBlocked as exc:
                # No attempt or budget reservation: no provider process has been dispatched.
                # Preserve cached outputs and record the blocker separately from bad model output.
                write(base / "preflight" / f"{time.time_ns()}.json", exc.report)
                raise ModelUnavailable(str(exc), global_failure=True) from exc
            with _file_lock(self.run_dir / "calls/.coordination/budget"):
                if self.cumulative:
                    self.calls_spent = dispatched_calls(self.run_dir)
                if self.max_calls is not None and self.calls_spent >= self.max_calls:
                    raise CallBudgetExhausted(self.calls_spent, self.max_calls)
                self.calls_spent += 1
                attempt = len(list(base.glob("attempt-*"))) + 1
                directory = base / f"attempt-{attempt:03d}"
                # Creating the directory reserves the attempt under the same budget lock.
                directory.mkdir(parents=True)
            write(directory / "schema.json", schema)
            if context is not None:
                write(directory / "context.json", context)
            (directory / "prompt.txt").write_text(prompt)
            receipt = {
                "job": job,
                "model": spec,
                "started_at": now(),
                "prompt_hash": digest(prompt),
                "call_id": key,
                "attempt": attempt,
                "status": "running",
                "seconds": 0,
                "usage": {},
                "cost_usd": None,
                "tool_context_hash": digest(context) if context is not None else None,
                "tool_calls": [],
            }
            started = time.monotonic()
            if home:
                receipt["codex_home"] = home
            write(directory / "receipt.json", receipt)
            try:
                rc, elapsed = execute(
                    command(
                        spec,
                        directory,
                        schema,
                        research,
                        self.config["models"]["reasoning"],
                        directory / "context.json" if context is not None else None,
                        service_tier=self.config["models"].get("service_tier", "fast"),
                        images=images,
                    ),
                    prompt,
                    directory,
                    timeout,
                    # Keyword only when an account is selected: test doubles of execute()
                    # and frozen runs without codex_homes keep the original signature.
                    **({"env_overrides": {"CODEX_HOME": home}} if home else {}),
                )
                receipt["seconds"] = elapsed
                if rc:
                    details = (directory / "stderr.txt").read_text()[-500:]
                    trace = directory / "stdout.jsonl"
                    # Codex also writes a harmless stdin banner to stderr. The structured
                    # terminal error carries capacity/auth details and must take precedence.
                    if trace.exists():
                        for line in reversed(trace.read_text().splitlines()):
                            try:
                                event = json.loads(line)
                            except ValueError:
                                continue
                            if not isinstance(event, dict):
                                continue
                            error = event.get("error")
                            message = event.get("message") or (
                                error.get("message") if isinstance(error, dict) else error
                            )
                            if event.get("type") in ("error", "turn.failed") and message:
                                details = str(message)[-1500:]
                                break
                    failure = provider_failure(f"process exited {rc}: {details}")
                    if home and "usage limit" in details.casefold():
                        # This account is out of quota for now; the other account carries on.
                        mark_exhausted(home)
                        failure.account_exhausted = True
                    raise failure
                events = read_codex_telemetry(directory, receipt)
                raw = read(directory / "answer.json")
                unexpected_tools = []
                for event in events:
                    if not isinstance(event, dict):
                        raise ModelUnavailable("completion contained a malformed transcript event")
                    item = event.get("item", {})
                    if not isinstance(item, dict):
                        raise ModelUnavailable("completion contained a malformed tool event")
                    if not research and item.get("type") in (
                        "command_execution",
                        "web_search",
                        "mcp_tool_call",
                        "collab_tool_call",
                    ):
                        from .stage_tools import TOOL_NAMES

                        permitted = (
                            context is not None
                            and item.get("type") == "mcp_tool_call"
                            and (
                                (item.get("server") == "stage1" and item.get("tool") in TOOL_NAMES)
                                or (
                                    item.get("server") == "codex"
                                    and item.get("tool")
                                    in ("list_mcp_resources", "list_mcp_resource_templates")
                                )
                            )
                        )
                        if not permitted:
                            unexpected_tools.append(item.get("tool", item["type"]))
                # Read all usage/session events even if a tool violated policy.
                if unexpected_tools:
                    raise ModelUnavailable(f"completion attempted an external tool: {unexpected_tools}")
                try:
                    parsed = response_type.model_validate(raw)
                    if validate:
                        validate(parsed)
                except (ValidationError, ValueError) as exc:
                    receipt.update(status="invalid_output", error=str(exc))
                    write(directory / "receipt.json", receipt)
                    if job in REVISABLE_JOBS:
                        write(invalid, {"error": str(exc), "data": raw, "receipt": receipt})
                        raise ModelOutputInvalid(str(exc), raw, receipt) from exc
                    raise ValueError(str(exc)) from exc
                receipt["status"] = "complete"
                write(saved, {"data": parsed.model_dump(), "receipt": receipt})
                write(directory / "receipt.json", receipt)
                return parsed, receipt
            except ModelOutputInvalid:
                raise
            except KeyboardInterrupt:
                if spec.startswith("codex/"):
                    read_codex_telemetry(directory, receipt, best_effort=True)
                receipt.update(
                    status="interrupted",
                    seconds=time.monotonic() - started,
                    error="coordinator interrupted this call",
                )
                write(directory / "receipt.json", receipt)
                raise
            except (OSError, ValueError, KeyError, subprocess.TimeoutExpired, ModelUnavailable) as exc:
                if spec.startswith("codex/"):
                    read_codex_telemetry(directory, receipt, best_effort=True)
                receipt["status"] = "error"
                receipt["seconds"] = time.monotonic() - started
                receipt["error"] = str(exc)[-2000:]
                retryable, global_failure = failure_policy(exc)
                if (
                    getattr(exc, "account_exhausted", False)
                    and available_homes(self.config)
                    and account_retries < len(codex_homes(self.config))
                ):
                    # Same model, the next account with quota; not a failure of the call.
                    account_retries += 1
                    retryable, global_failure = True, False
                    receipt.update(
                        retryable=True, global_failure=False, error=f"{exc}; retrying on another account"
                    )
                    write(directory / "receipt.json", receipt)
                    queue.insert(0, spec)
                    continue
                if isinstance(exc, subprocess.TimeoutExpired) and timeout_retries < 1:
                    # One hung provider process must not throw away hours of seeding: the same
                    # call is tried once more before the timeout counts as the answer.
                    timeout_retries += 1
                    receipt.update(retryable=True, global_failure=False, error=f"{exc}; retrying once")
                    write(directory / "receipt.json", receipt)
                    queue.insert(0, spec)
                    continue
                if getattr(exc, "busy", False) and transient_retries < TRANSIENT_RETRIES:
                    # A busy provider is not a failed call: wait briefly and try the same model again.
                    transient_retries += 1
                    receipt.update(
                        retryable=True,
                        global_failure=False,
                        error=f"{exc}; retrying in {TRANSIENT_WAIT_SECONDS}s",
                    )
                    write(directory / "receipt.json", receipt)
                    if _cancelled.wait(TRANSIENT_WAIT_SECONDS):
                        raise ModelUnavailable(
                            "model calls cancelled by coordinator interruption", global_failure=True
                        )
                    queue.insert(0, spec)
                    continue
                receipt.update(retryable=retryable, global_failure=global_failure)
                write(directory / "receipt.json", receipt)
                failures.append(f"{spec}: {exc}")
                policies.append((retryable, global_failure))
        raise ModelUnavailable(
            "; ".join(failures) or f"no independent backend available for {job}",
            retryable=any(p[0] for p in policies),
            global_failure=not policies or all(p[1] for p in policies),
        )
