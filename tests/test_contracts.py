import copy
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from company_envs.catalogs import Catalogs, sector_targets
from company_envs.diversity import nearest, select
from company_envs.models import Models, ModelUnavailable, command, strict_schema
from company_envs.review import evidence_errors, validate_review
from company_envs.schemas import Company, Review, Workflow, validate_workflow
from company_envs.sources import Sources, normalize, public_url
from company_envs.storage import bound_review, digest, read, run_lock, write


@pytest.mark.parametrize("level", [None, "easy", "medium", "hard"])
def test_workflow_difficulty_round_trips_without_changing_old_files(workflow, level):
    raw = workflow.model_dump()
    if level is not None:
        raw["difficulty"] = level
    restored = Workflow.model_validate(raw)
    assert restored.difficulty == level
    assert restored.model_dump() == raw
    assert Workflow.model_validate_json(restored.model_dump_json()).model_dump() == raw
    assert "difficulty" not in restored.assignment()


@pytest.mark.parametrize("level", ["expert", "Easy", "", 2, False])
def test_workflow_rejects_unknown_difficulty(workflow, level):
    with pytest.raises(ValidationError, match="difficulty"):
        Workflow.model_validate({**workflow.model_dump(), "difficulty": level})


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("version", ["1", "2"])
def test_authoring_schema_only_requires_difficulty_for_skills_that_request_it(required, version):
    from company_envs.schemas import WorkflowBatch
    from company_envs.workflows import generation_schema

    fields = generation_schema(WorkflowBatch, version, difficulty_required=required)["$defs"]["Workflow"]
    assert ("difficulty" in fields["required"]) is required
    assert ("difficulty" in fields["properties"]) is required
    if required:
        assert fields["properties"]["difficulty"] == {"type": "string", "enum": ["easy", "medium", "hard"]}


def test_legacy_expansion_enforces_requested_difficulty_with_a_double(company, workflow):
    from company_envs.models import ModelOutputInvalid
    from company_envs.workflows import expand

    class Author:
        def __init__(self):
            self.config = {"generation": {"difficulty_required": True}}

        def call(self, stage, prompt, response_type, **options):
            self.schema = options["schema"]
            return response_type(workflows=[workflow]), {"model": "fake"}

    author = Author()
    with pytest.raises(ModelOutputInvalid, match="requires difficulty"):
        expand(author, "Return difficulty for every task.", company, [], [workflow.outline_id])
    workflow.difficulty = "medium"
    result, _ = expand(author, "Return difficulty for every task.", company, [], [workflow.outline_id])
    assert result[0].difficulty == "medium"
    assert "difficulty" in author.schema["$defs"]["Workflow"]["required"]


def verdict(workflow, **changes):
    task = {
        "workflow_id": workflow.id,
        "verdict": "accept",
        "quality": 4,
        "novelty": "distinct",
        "duplicate_of": "",
        "reasons": [],
    }
    task.update(changes)
    return Review(company_verdict="accept", company_reasons=[], tasks=[task])


@pytest.mark.parametrize("n", [1, 3, 9, 50, 100])
def test_small_and_large_economic_targets(root, n):
    sectors = Catalogs(root / "catalogs").sectors
    counts = sector_targets(sectors, n)
    assert sum(counts.values()) == n
    assert min(counts.values()) >= (1 if n >= len(sectors) else 0)


def test_shared_worker_and_reused_occupation_are_valid(company, workflow, root):
    Catalogs(root / "catalogs").validate(company)
    validate_workflow(company, workflow)
    assert len(company.workers) == 2
    assert company.teams[1].worker_ids == ["process"]
    assert len({w.soc for w in company.workers}) == 1


def test_naics_must_match_sector(company, root):
    company.naics = "52"
    with pytest.raises(ValueError, match="NAICS"):
        Catalogs(root / "catalogs").validate(company)


@pytest.mark.parametrize("mutation", ["unknown_worker", "cycle", "missing_contribution", "duplicate_phase"])
def test_invalid_graphs_fail(workflow, mutation):
    raw = workflow.model_dump()
    if mutation == "unknown_worker":
        raw["phases"][0]["worker_ids"] = ["stranger"]
    elif mutation == "cycle":
        raw["phases"][0]["depends_on"] = ["qualify"]
    elif mutation == "missing_contribution":
        raw["contributions"].pop()
    else:
        raw["phases"][1]["id"] = "design"
    with pytest.raises(ValidationError):
        Workflow.model_validate(raw)


def test_unknown_team_fails(company):
    raw = company.model_dump()
    raw["outlines"][0]["team_ids"] = ["unknown"]
    with pytest.raises(ValidationError):
        Company.model_validate(raw)


def test_valid_component_ids_can_form_a_workflow_id(company, workflow):
    company.id = "c" * 80
    company.outlines[0].id = "o" * 80
    raw = workflow.model_dump()
    raw.update(
        company_id=company.id, outline_id=company.outlines[0].id, id=company.id + "_" + company.outlines[0].id
    )
    validate_workflow(company, Workflow.model_validate(raw))


def test_review_missing_vote_fails(workflow):
    second = workflow.model_copy(update={"id": "second"})
    with pytest.raises(ValueError, match="every workflow"):
        validate_review(verdict(workflow), [workflow, second], {})


@pytest.mark.parametrize(
    "changes",
    [
        {"quality": 2},
        {"novelty": "variant", "duplicate_of": "unknown", "reasons": ["Same"]},
        {"verdict": "revise"},
        {"duplicate_of": "other"},
    ],
)
def test_invalid_review_fails(workflow, changes):
    with pytest.raises(ValueError):
        validate_review(verdict(workflow, **changes), [workflow], {})


def test_cyclic_duplicates_fail(workflow):
    second = workflow.model_copy(update={"id": "second"})
    review = verdict(workflow, novelty="variant", duplicate_of="second", reasons=["Same decision"])
    review.tasks.append(
        verdict(second, novelty="variant", duplicate_of=workflow.id, reasons=["Same decision"]).tasks[0]
    )
    with pytest.raises(ValueError, match="cycle"):
        validate_review(review, [workflow, second], {})


def test_source_quotes_fail_closed(company, root, monkeypatch):
    source = Sources(root)
    monkeypatch.setattr(
        source, "capture", lambda url: {"status": "captured", "text": "Unrelated page", "text_hash": "bad"}
    )
    assert evidence_errors(company, source.evidence(company))
    monkeypatch.setattr(
        source,
        "capture",
        lambda url: {"status": "captured", "text": "We manufacture instruments.", "text_hash": "ok"},
    )
    assert not evidence_errors(company, source.evidence(company))


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:pass@example.com", "http://127.0.0.1"])
def test_private_source_rejected(url):
    with pytest.raises(ValueError):
        public_url(url)


def test_typographic_equivalents_do_not_reject_exact_words():
    assert normalize("department’s pre‑production") == normalize("department's pre-production")
    assert normalize("pre-production") != normalize("post-production")
    assert normalize("−10") != normalize("+10")
    assert normalize("\u00ad\u200b") == ""


def test_typographic_quote_excerpt_keeps_relevant_context(root, company):
    company.evidence[0].quote = "department's pre-production"
    text = "Unrelated opening " * 500 + "The department’s pre‑production work is coordinated here."
    write(root / "data" / "sources" / f"{digest(company.website)}.json", {"text": text})
    from company_envs.sources import normalize

    assert "department's pre-production" in normalize(Sources(root).excerpts(company)[0]["excerpt"])
    assert "department" not in Sources(root).excerpts(company, legacy=True)[0]["excerpt"]


def test_content_hash_only_tracks_review_inputs(company, workflow, root):
    args = [company.model_dump(), [workflow.model_dump()], [], "review prompt", {"author/model"}]
    before = bound_review(*args)
    write(root / "unrelated.json", {"note": "New documentation"})
    assert bound_review(*args) == before
    changed = copy.deepcopy(args)
    changed[1][0]["decision_problem"] = "A different decision"
    assert bound_review(*changed) != before


def test_same_run_is_locked(root):
    with run_lock(root), pytest.raises(RuntimeError, match="coordinator"), run_lock(root):
        pass


def test_selection_counts_and_variants(company, root):
    companies, entries = {}, []
    for i in range(50):
        cid = f"company-{i}"
        companies[cid] = company.model_copy(update={"id": cid})
        for j in range(3):
            wid = f"{cid}-{j}"
            entries.append(
                {
                    "company_id": cid,
                    "workflow_id": wid,
                    "family_id": wid,
                    "quality": 4,
                    "verdict": "accept",
                    "novelty": "distinct" if j < 2 else "variant",
                }
            )
    selected = select(entries, companies, 50, 100, Catalogs(root / "catalogs").sectors)
    assert len(selected) == 100
    assert len({e["company_id"] for e in selected}) == 50
    assert all(e["novelty"] == "distinct" for e in selected)
    assert selected == select(
        list(reversed(entries)), companies, 50, 100, Catalogs(root / "catalogs").sectors
    )


def test_nearest_not_names_decides_retrieval(workflow):
    twin = workflow.model_copy(update={"id": "renamed", "title": "Incidental different company name"})
    other = workflow.model_copy(update={"id": "other", "canonical_description": "Different work"})
    encoder = SimpleNamespace(
        encode=lambda text: np.array([0.0, 1.0]) if text == "Different work" else np.array([1.0, 0.0])
    )
    neighbors = nearest(workflow, [workflow, other, twin], encoder, 1)
    assert neighbors[0]["workflow_id"] == "renamed"


def test_adapter_has_no_tools_for_reviewer(root):
    schema = strict_schema(Review)
    codex = command("codex/gpt-test", root, schema, False, "high")
    assert 'web_search="disabled"' in codex
    assert "--ignore-user-config" in codex
    assert "features.apps=false" in codex
    assert "features.skip_host_skill_discovery=true" in codex
    assert "features.hooks=false" in codex
    assert schema["required"] == list(schema["properties"])


def test_missing_independent_backend_fails(root):
    models = Models(
        {"generation": {"completion_timeout_seconds": 1}, "models": {"review": ["codex/gpt-author"]}}, root
    )
    with pytest.raises(ModelUnavailable, match="independent"):
        models.call("review", "prompt", Review, avoid={"codex/gpt-author"})


def test_dossier_repair_is_tool_free(root, company, monkeypatch):
    import company_envs.models as adapter
    from company_envs.research import research

    def execute(cmd, prompt, directory, timeout):
        assert 'web_search="disabled"' in cmd
        assert "previous_dossier" in prompt and "captured_source_excerpts" in prompt
        write(directory / "answer.json", company.model_dump())
        (directory / "stdout.jsonl").write_text("")
        return 0, 1.0

    monkeypatch.setattr(adapter, "execute", execute)
    config = {
        "generation": {"completion_timeout_seconds": 1},
        "models": {"research": ["codex/gpt-author"], "reasoning": "high"},
    }
    _, receipt = research(
        Models(config, root),
        Catalogs(root / "catalogs"),
        "prompt",
        company,
        {},
        previous=company,
        excerpts=[{"excerpt": "We manufacture instruments."}],
    )
    assert receipt["job"] == "research_repair"


def test_model_resume_uses_cached_call(root, workflow, monkeypatch):
    import company_envs.models as adapter

    calls = []

    def execute(cmd, prompt, directory, timeout):
        calls.append(cmd)
        write(directory / "answer.json", verdict(workflow).model_dump())
        (directory / "stdout.jsonl").write_text("")
        return 0, 1.0

    monkeypatch.setattr(adapter, "execute", execute)
    config = {
        "generation": {"completion_timeout_seconds": 1},
        "models": {"review": ["codex/gpt-critic"], "reasoning": "high"},
    }
    first, receipt = Models(config, root).call("review", "prompt", Review)
    second, repeated = Models(config, root).call("review", "prompt", Review)
    assert first == second and receipt == repeated and len(calls) == 1
    assert digest(read(root / "calls" / receipt["call_id"] / "result.json")["data"]) == digest(
        first.model_dump()
    )


def test_expansion_schema_uses_company_registry(company):
    from company_envs.workflows import expansion_schema

    schema = expansion_schema(company, ["release"])
    props = schema["$defs"]["Workflow"]["properties"]
    assert props["software_requirement_ids"]["items"]["enum"] == ["workspace"]
    assert props["id"]["enum"] == ["example_release"]


def test_research_schema_fits_provider_enum_limit(root, company):
    import re

    from company_envs.research import research

    catalogs = Catalogs(root / "catalogs")

    def capture(job, prompt, response_type, schema):
        def enums(node):
            if isinstance(node, dict):
                return len(node.get("enum", [])) + sum(
                    enums(value) for key, value in node.items() if key != "enum"
                )
            if isinstance(node, list):
                return sum(enums(value) for value in node)
            return 0

        assert enums(schema) <= 1000
        pattern = schema["$defs"]["Worker"]["properties"]["soc"]["pattern"]
        assert all(re.fullmatch(pattern, soc) for soc in catalogs.occupations)
        assert not re.fullmatch(pattern, "00-0000")
        return company, {}

    research(SimpleNamespace(call=capture), catalogs, "prompt", company, {})


def test_invalid_model_output_is_not_provider_outage(root, monkeypatch):
    import company_envs.models as adapter

    def execute(cmd, prompt, directory, timeout):
        write(directory / "answer.json", {"company_verdict": "accept", "company_reasons": [], "tasks": []})
        (directory / "stdout.jsonl").write_text("")
        return 0, 1.0

    monkeypatch.setattr(adapter, "execute", execute)
    config = {
        "generation": {"completion_timeout_seconds": 1},
        "models": {"expand": ["codex/gpt-author"], "reasoning": "high"},
    }
    with pytest.raises(adapter.ModelOutputInvalid):
        Models(config, root).call("expand", "prompt", Review)


@pytest.mark.parametrize("spec", ["claude/claude-opus-5", "claude/claude-sonnet-5", "codex/o3"])
def test_gpt_only_policy_blocks_commands_and_cached_non_gpt_calls(root, monkeypatch, spec):
    import company_envs.models as adapter

    def forbidden(*args, **kwargs):
        pytest.fail("Blocked models must not read cached results or execute")

    monkeypatch.setattr(adapter, "execute", forbidden)
    monkeypatch.setattr(adapter, "read", forbidden)
    config = {
        "generation": {"completion_timeout_seconds": 1},
        "models": {"review": [spec], "reasoning": "high"},
    }
    with pytest.raises(ValueError, match="GPT-only"):
        command(spec, root, {}, False, "high")
    with pytest.raises(ModelUnavailable, match="GPT-only"):
        Models(config, root).call("review", "prompt", Review)
    assert not (root / "calls").exists()


def test_frozen_mixed_config_uses_only_independent_gpt(root, workflow, monkeypatch):
    import company_envs.models as adapter

    calls = []

    def execute(cmd, prompt, directory, timeout):
        assert cmd[0] == "codex" and cmd[cmd.index("-m") + 1] == "gpt-critic"
        calls.append(cmd)
        write(directory / "answer.json", verdict(workflow).model_dump())
        (directory / "stdout.jsonl").write_text("")
        return 0, 1.0

    monkeypatch.setattr(adapter, "execute", execute)
    config = {
        "generation": {"completion_timeout_seconds": 1},
        "models": {
            "review": ["claude/claude-opus-5", "codex/gpt-author", "codex/gpt-critic"],
            "reasoning": "high",
        },
    }
    _, receipt = Models(config, root).call("review", "prompt", Review, avoid={"codex/gpt-author"})
    assert len(calls) == 1 and receipt["model"] == "codex/gpt-critic"


def test_default_config_is_astra_with_explicit_fresh_session_review(root):
    import tomllib

    models = tomllib.loads((root / "config.toml").read_text())["models"]
    authors = set()
    for job in ("discover", "research", "expand", "review"):
        assert models[job] and all(spec.startswith("codex/gpt-") for spec in models[job])
        if job != "review":
            authors.update(models[job])
    assert authors == set(models["review"]) == {"codex/gpt-6-astra"}
    assert models["review_policy"] == "fresh_session"


def test_feature_bundles_carry_a_domain_collection_when_the_company_has_one(monkeypatch):
    from company_envs import workflows

    class Co:
        id = "acme"
        software = ()

    monkeypatch.setattr(
        workflows,
        "company_collections",
        lambda company, catalogs: ["gmail_mock.emails", "google_docs_mock.documents", "jira_mock.issues"],
    )
    rows = workflows.build_feature_matrix(Co(), None, 4, 0)
    assert all("jira_mock.issues" in r["feature_cell"]["collections"] for r in rows)


def test_success_criteria_may_not_describe_interface_state():
    from types import SimpleNamespace

    from company_envs.workflows import check_criteria_are_about_records

    def workflow(requirement, observable):
        criterion = SimpleNamespace(requirement=requirement, observable=observable)
        return SimpleNamespace(id="acme_review-queue", success_criteria=[criterion])

    with pytest.raises(ValueError, match="interface state"):
        check_criteria_are_about_records(
            workflow("Restore incident coverage.", "The incident view uses currentSortDirection='asc'.")
        )
    check_criteria_are_about_records(
        workflow("Record the assessment.", "INC0012101's work notes name the owner.")
    )


def test_interface_state_sentences_are_dropped_and_repairs_are_recorded(tmp_path):
    import json

    from company_envs.workflows import repair_criteria, strip_interface_state

    text, dropped = strip_interface_state(
        "google_sheets_mock.showGridlines is true. Population headings and corrected formulas agree."
    )
    assert text == "Population headings and corrected formulas agree." and len(dropped) == 1
    task = tmp_path / "tasks" / "acme_x"
    task.mkdir(parents=True)
    (task / "workflow.json").write_text(
        json.dumps(
            {
                "id": "acme_x",
                "success_criteria": [
                    {
                        "requirement": "Focus the week.",
                        "observable": "currentDate shows the week of September 14.",
                        "method": "state",
                    },
                    {
                        "requirement": "Record the owner.",
                        "observable": "INC1's work notes name the owner.",
                        "method": "state",
                    },
                ],
            }
        )
    )
    assert repair_criteria(tmp_path) == {"acme_x": [0]}
    fixed = json.loads((task / "workflow.json").read_text())
    assert fixed["success_criteria"][0]["observable"] == "Focus the week."  # falls back to the requirement
    assert fixed["success_criteria"][1]["observable"] == "INC1's work notes name the owner."
    assert fixed["criteria_repair"]["changes"][0]["dropped"] == [
        "currentDate shows the week of September 14."
    ]
