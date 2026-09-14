import json

import pytest

from company_envs import workflows
from company_envs.catalogs import Catalogs
from company_envs.stage_tools import definitions


class Author:
    def __init__(self, workflow):
        self.workflow = workflow
        self.payload = None
        self.options = None

    def call(self, stage, prompt, response_type, **options):
        self.payload = json.loads(prompt.split("\n", 1)[1])
        self.options = options
        return workflows.DesignBatch(
            workflows=[self.workflow], amendment=None, selection_reason="Complementary decision problem"
        ), {"model": "codex/author", "job": stage}


@pytest.mark.parametrize("read_only_tools", [False, True])
@pytest.mark.parametrize("fixed_selection", [False, True])
def test_selection_and_tools_do_not_authorize_amendments(
    root, company, workflow, read_only_tools, fixed_selection
):
    author = Author(workflow)
    excerpts = [{"url": company.website, "excerpt": "x" * 5000}]
    coverage = {"existing_work": [{"decision_problem": "Other operational work"}]}
    original = company.model_dump()
    amended, generated, _, _ = workflows.design(
        author,
        "skill",
        company,
        [],
        Catalogs(root / "catalogs"),
        excerpts,
        1,
        selected=[workflow.outline_id] if fixed_selection else None,
        coverage=coverage,
        allow_amendment=False,
        read_only_tools=read_only_tools,
    )
    assert amended.model_dump() == original and generated == [workflow]
    assert author.payload["fixed_selection"] is fixed_selection
    assert author.payload["portfolio_coverage"] == coverage
    assert author.payload["allow_amendment"] is False
    assert author.options["schema"]["properties"]["amendment"] == {"type": "null"}
    if read_only_tools:
        assert "read_app_schema" in author.payload["available_tools"]
        assert author.options["context"]["sources"] == excerpts
        assert "app_schemas" in author.options["context"]
        assert len(author.payload["source_previews"][0]["excerpt"]) == 4000
    else:
        assert "context" not in author.options
        assert author.payload["available_tools"] == []
        assert author.payload["source_excerpts"] == excerpts
        assert "source_previews" not in author.payload


def test_legacy_expand_remains_tool_free(company, workflow):
    class LegacyAuthor(Author):
        def call(self, stage, prompt, response_type, **options):
            _, receipt = super().call(stage, prompt, response_type, **options)
            return workflows.WorkflowBatch(workflows=[workflow]), receipt

    author = LegacyAuthor(workflow)
    workflows.expand(author, "skill", company, [], [workflow.outline_id])
    assert "context" not in author.options
    assert author.payload["fixed_selection"]
    assert not author.payload["allow_amendment"]


def test_the_payload_never_names_a_tool_the_server_will_not_register(root, company, workflow):
    """55 of 64 rejected drafts were empty batches; their selection_reason said no read_seed was
    enabled. available_tools listed all six TOOL_NAMES while the context held no seed, so
    stage_tools.definitions() registered four and the designer declined rather than guess."""
    author = Author(workflow)
    workflows.design(
        author,
        "skill",
        company,
        [],
        Catalogs(root / "catalogs"),
        [{"url": company.website, "excerpt": "x"}],
        1,
        selected=[workflow.outline_id],
        allow_amendment=False,
    )
    offered = {tool["name"] for tool in definitions(author.options["context"])}
    assert set(author.payload["available_tools"]) == offered
    assert "read_seed" not in author.payload["available_tools"]
