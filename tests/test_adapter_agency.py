import json

import pytest

from company_envs import models
from company_envs.models import Models, ModelUnavailable, command
from company_envs.schemas import WorkflowBatch
from company_envs.storage import read, write


def test_batch_disables_duplicate_skill_discovery_and_delegation(tmp_path):
    cmd = command("codex/gpt-test", tmp_path, {}, False, "high")
    assert "agents.enabled=false" in cmd and "features.plugins=false" in cmd
    assert "features.multi_agent=false" in cmd
    assert any(arg.startswith("skills.config=[") and "enabled=false" in arg for arg in cmd)


@pytest.mark.parametrize(
    "server,tool,allowed",
    [
        ("codex", "list_mcp_resources", True),
        ("codex", "list_mcp_resource_templates", True),
        ("stage1", "calculate", True),
        ("other", "calculate", False),
        ("stage1", "shell", False),
        ("codex", "read_mcp_resource", False),
    ],
)
def test_inventory_is_not_external_access_and_failures_keep_usage(
    tmp_path, workflow, monkeypatch, server, tool, allowed
):
    def execute(cmd, prompt, directory, timeout):
        write(directory / "answer.json", {"workflows": [workflow.model_dump()]})
        events = [
            {"type": "thread.started", "thread_id": "fresh-session"},
            {
                "type": "item.completed",
                "item": {"type": "mcp_tool_call", "server": server, "tool": tool, "status": "completed"},
            },
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
        ]
        (directory / "stdout.jsonl").write_text("\n".join(json.dumps(e) for e in events))
        return 0, 1.0

    monkeypatch.setattr(models, "execute", execute)
    config = {
        "models": {"expand": ["codex/gpt-test"], "reasoning": "high"},
        "generation": {"completion_timeout_seconds": 10},
    }
    model = Models(config, tmp_path)
    if allowed:
        _, receipt = model.call("expand", "test", WorkflowBatch, context={})
    else:
        with pytest.raises(ModelUnavailable, match="external tool"):
            model.call("expand", "test", WorkflowBatch, context={})
        receipt = read(next(tmp_path.glob("calls/*/attempt-001/receipt.json")))
    assert receipt["usage"] == {"input_tokens": 10, "output_tokens": 2}
    assert receipt["session_id"] == "fresh-session"
    assert receipt["tool_calls"][0]["tool"] == tool
