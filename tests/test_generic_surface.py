"""Public entry points and small shared contracts survive native-path archival."""

import json
from importlib.util import find_spec

import pytest
from pydantic import ValidationError

from company_envs.__main__ import main
from company_envs.stage_tools import invoke
from company_envs.world.capabilities import app_mount
from company_envs.world.state_seed import Identity

COMMANDS = (
    "bootstrap",
    "generate",
    "report",
    "audit",
    "hub-smoke",
    "hub-serve",
    "render-check",
    "seed-world",
    "world-check",
    "world-repair",
    "review-repair",
    "launch-vms",
    "stop-vms",
    "reset-vms",
    "run-episode",
    "calibrate",
    "author-golden",
    "readability",
    "grade",
    "export-company",
    "release",
    "verify-release",
    "transform",
    "run-company",
    "amplify",
    "inspect",
)


@pytest.mark.parametrize("command", (None, *COMMANDS))
def test_cli_help_without_runtime_inputs(command, monkeypatch, capsys):
    args = [] if command is None else [command]
    monkeypatch.setattr("sys.argv", ["company-envs", *args, "--help"])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "usage:" in output
    if command is None:
        assert all(name in output for name in COMMANDS)
        assert "prepare-world" not in output and "build-world" not in output


def test_real_vms_refuse_a_zero_model_call_budget(monkeypatch, capsys, tmp_path):
    """Booting five machines with nothing to spend reads exactly like a task that cannot be done.

    With --model-calls left at its default every worker's first step raises "budget exhausted",
    the teacher scores 0.0 and the run is recorded as a teacher failure, which is the same shape
    as a world that genuinely does not work.
    """
    monkeypatch.setattr(
        "sys.argv",
        ["company-envs", "run-company", str(tmp_path), "task", "--backend", "mypcbench"],
    )
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
    assert "--model-calls" in capsys.readouterr().err


@pytest.mark.parametrize(
    "command", ("pilot", "prepare-world", "build-seed", "bind-apps", "build-world", "build-company")
)
def test_archived_commands_are_unavailable(command, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["company-envs", command, "--help"])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("module", ("compare", "campaign", "snapshots"))
def test_experiment_modules_are_not_installed(module):
    assert find_spec(f"company_envs.{module}") is None


def test_seed_tool_preserves_pointer_escapes_and_pagination():
    seed = {"records/active": [{"a~b": "pending"}]}
    assert json.loads(invoke({"seed": seed}, "read_seed", {"pointer": ""})["text"]) == seed
    result = invoke({"seed": seed}, "read_seed", {"pointer": "/records~1active/0/a~0b", "length": 4})
    assert result["text"] == '"pen' and result["next_offset"] == 4
    with pytest.raises(ValueError, match="does not resolve"):
        invoke({"seed": seed}, "read_seed", {"pointer": "/missing"})
    with pytest.raises(ValueError, match="No generated seed"):
        invoke({}, "read_seed", {"pointer": ""})


def test_state_seed_contract_still_forbids_extras_and_mutation():
    identity = Identity(worker_id="worker", app_id="app", user_json='{"id":"worker"}')
    with pytest.raises(ValidationError, match="frozen"):
        identity.worker_id = "other"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Identity(**identity.model_dump(), private_extra="unexpected")


def test_historical_mount_facts_do_not_need_native_builders():
    assert app_mount("sheets") == "/sheets"
    assert app_mount("docs", "/team/docs") == "/team/docs"
    with pytest.raises(ValueError, match="canonical local path"):
        app_mount("docs", "/team/../docs")
    with pytest.raises(ValueError, match="only for Docs"):
        app_mount("zendesk", "/support")
