import io
import json

import pytest
from test_pipeline import setup_job

from company_envs import pipeline, workflows
from company_envs.catalogs import Catalogs
from company_envs.models import ModelOutputInvalid, command
from company_envs.stage_tools import calculate, invoke, serve
from company_envs.storage import read


@pytest.mark.parametrize(
    "expression", ["__import__('os')", "1 ** 99999", "True", "1e999", "1/0", "'x'", "1" * 1001]
)
def test_calculator_rejects_code_and_unbounded_inputs(expression):
    with pytest.raises((ValueError, ArithmeticError)):
        calculate(expression)


def test_frozen_tools():
    context = {
        "sources": [{"url": "https://example.com", "excerpt": "abcdef"}],
        "catalog": [{"id": "calendar", "description": "Scheduling"}],
        "occupations": {"17-2112": "Industrial Engineer"},
    }
    assert calculate("(37*19)-12")["value"] == 691
    assert invoke(context, "read_source", {"url": "https://example.com", "length": 3})["next_offset"] == 3
    assert invoke(context, "read_source", {"url": "https://example.com", "offset": 3})["text"] == "def"
    for url in ("/etc/passwd", "https://unprovided.example", "http://127.0.0.1"):
        with pytest.raises(ValueError):
            invoke(context, "read_source", {"url": url})
    with pytest.raises(ValueError):
        invoke(context, "read_source", {"url": "https://example.com", "offset": -1})
    assert invoke(context, "search_catalog", {"query": "engineer"})[0]["soc"] == "17-2112"
    assert invoke(context, "search_catalog", {"query": "scheduling"})[0]["id"] == "calendar"


def test_mcp_protocol():
    requests = [
        {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"method": "notifications/initialized"},
        {"id": 2, "method": "tools/list"},
        {
            "id": 3,
            "method": "tools/call",
            "params": {"name": "calculate", "arguments": {"expression": "2+2"}},
        },
        {"id": 4, "method": "tools/call", "params": {"name": "shell", "arguments": {}}},
    ]
    output = io.StringIO()
    serve({}, io.StringIO("\n".join(json.dumps(r) for r in requests)), output)
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3, 4]
    assert {tool["name"] for tool in replies[1]["result"]["tools"]} == {
        "read_source",
        "read_app_schema",
        "search_catalog",
        "calculate",
    }
    assert json.loads(replies[2]["result"]["content"][0]["text"])["value"] == 4
    assert replies[3]["result"]["isError"]


def test_adapter_scopes_tools(tmp_path):
    cmd = command("codex/gpt-6-astra", tmp_path, {}, False, "high", tmp_path / "context.json")
    assert "features.shell_tool=false" in cmd and 'web_search="disabled"' in cmd
    assert any("mcp_servers.stage1.enabled_tools" in arg for arg in cmd)
    assert not any("mcp_servers" in arg for arg in command("codex/gpt-6-astra", tmp_path, {}, False, "high"))


class Author:
    def __init__(self, batch):
        self.batch = batch
        self.calls = []

    def call(self, job, prompt, response_type, **kwargs):
        self.calls.append((json.loads(prompt.split("\n", 1)[1]), kwargs))
        return self.batch, {"model": "codex/gpt-test", "job": "expand", "call_id": "test"}


def test_joint_selection_can_choose_nonfirst_outline(root, company, workflow):
    company.outlines.append(company.outlines[0].model_copy(update={"id": "second"}))
    second = workflow.model_copy(update={"id": "example_second", "outline_id": "second"})
    model = Author(
        workflows.DesignBatch(workflows=[second], amendment=None, selection_reason="Uncovered work")
    )
    amended, result, _, _ = workflows.design(
        model,
        "skill",
        company,
        [],
        Catalogs(root / "catalogs"),
        [],
        1,
        coverage={"existing_work": [{"title": "First outline"}]},
    )
    assert amended == company and result == [second] and len(model.calls) == 1
    assert not model.calls[0][0]["fixed_selection"]
    assert model.calls[0][0]["portfolio_coverage"]["existing_work"]
    with pytest.raises(ModelOutputInvalid, match="eligible outlines"):
        workflows.design(
            model, "skill", company, [], Catalogs(root / "catalogs"), [], 1, selected=["release"]
        )


def test_amendments_are_typed_bounded_and_optional(root, company, workflow):
    amendment = workflows.DossierAmendment(
        reason="Clarify inferred process release authority",
        workers=company.workers,
        teams=company.teams,
        software=company.software,
    )
    amendment = amendment.model_copy(deep=True)
    amendment.workers[1].authority = "Inferred: approve manufacturing process release"
    batch = workflows.DesignBatch(workflows=[workflow], amendment=amendment, selection_reason="Fixed")
    model = Author(batch)
    revised, _, _, decision = workflows.design(
        model, "skill", company, [], Catalogs(root / "catalogs"), [], 1
    )
    assert revised.workers[1].authority != company.workers[1].authority
    assert revised.evidence == company.evidence and revised.outlines == company.outlines
    assert decision["amendment"]["reason"]
    with pytest.raises(ModelOutputInvalid, match="published"):
        workflows.design(
            model, "skill", company, [], Catalogs(root / "catalogs"), [], 1, allow_amendment=False
        )
    batch.amendment.workers.pop()
    with pytest.raises(ModelOutputInvalid, match="cannot remove"):
        workflows.design(model, "skill", company, [], Catalogs(root / "catalogs"), [], 1)


def test_pipeline_connects_agency_and_records_decision(root, company, workflow, monkeypatch):
    directory, state, path = setup_job(root, company, workflow)
    state["config"]["design"]["agency"] = True
    (path / "workflows.json").unlink()
    called = []

    def design(*args, **kwargs):
        assert args[5][0]["excerpt"] == "We manufacture instruments."
        assert kwargs["selected"] is None and kwargs["allow_amendment"]
        called.append(True)
        return company, [workflow], {"model": "codex/gpt-test", "job": "expand"}, {"amendment": None}

    monkeypatch.setattr(pipeline.workflow_job, "design", design)
    pipeline.prepare(root, directory, state, state["jobs"][company.id])
    assert called == [True]
    assert read(path / "design-decision-0-0.json") == {"amendment": None}
    assert read(path / "workflows.json") == [workflow.model_dump()]


def test_partial_missing_source_demotes_without_web_repair(root, company, workflow, monkeypatch):
    directory, state, path = setup_job(root, company, workflow)
    (path / "company.json").unlink()
    incomplete = company.model_copy(deep=True)
    incomplete.evidence.append(
        company.evidence[0].model_copy(update={"id": "missing", "source_url": "https://missing.example"})
    )
    calls = []

    def research(*args, **kwargs):
        calls.append(kwargs["needs_sources"])
        return (incomplete if len(calls) == 1 else company), {"model": "codex/gpt-test", "job": "research"}

    original = pipeline.Sources.capture
    monkeypatch.setattr(
        pipeline.Sources,
        "capture",
        lambda self, url: (
            {"status": "error", "text": ""} if url == "https://missing.example" else original(self, url)
        ),
    )
    monkeypatch.setattr(pipeline.research_job, "research", research)
    pipeline.prepare(root, directory, state, state["jobs"][company.id])
    assert calls == [False]
    saved = read(path / "company.json")
    assert saved["evidence"][1]["kind"] == "inferred"
    evidence = read(path / "evidence.json")[1]
    assert evidence["attempted_source_url"] == "https://missing.example"
    assert evidence["capture_status"] == "error"
