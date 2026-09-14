import json

import pytest

from company_envs.catalogs import Catalogs
from company_envs.research import research, research_view
from company_envs.schemas import Candidate
from company_envs.stage_tools import definitions, invoke
from company_envs.storage import digest, read


@pytest.mark.parametrize(
    "tool,identity", [("read_source", {"url": "source"}), ("read_app_schema", {"app_id": "app"})]
)
def test_pagination_advertises_enforced_bounds(tool, identity):
    schema = next(d["inputSchema"] for d in definitions() if d["name"] == tool)
    bounds = schema["properties"]["length"]
    assert bounds["minimum"] == 1
    assert bounds["default"] <= bounds["maximum"]
    assert schema["properties"]["offset"]["minimum"] == 0
    text = "x" * (bounds["maximum"] + 1)
    context = {"sources": [{"url": "source", "excerpt": text}], "app_schemas": {"app": {"text": text}}}
    first = invoke(context, tool, {**identity, "length": bounds["maximum"]})
    assert len(first["text"]) == bounds["maximum"]
    assert invoke(context, tool, {**identity, "offset": first["next_offset"]})["text"] == "x"
    for length in (bounds["minimum"] - 1, bounds["maximum"] + 1):
        with pytest.raises(ValueError, match="length"):
            invoke(context, tool, {**identity, "length": length})


def test_schema_tools_use_frozen_documentation_and_paginate(root):
    catalogs = Catalogs(root / "catalogs")
    packet = catalogs.tool_context([])
    app_id, schema = next(iter(packet["app_schemas"].items()))
    page = invoke(packet, "read_app_schema", {"app_id": app_id, "length": 20})
    assert page["text"] == schema["text"][:20]
    assert page["next_offset"] == 20
    assert page["sha256"] == digest(schema["text"].encode())
    assert page["path"].startswith("app_schemas/")
    # A frozen call does not read the source files again, and model-supplied paths
    # cannot escape the catalog packet.
    (root / "catalogs" / schema["path"]).unlink()
    assert invoke(packet, "read_app_schema", {"app_id": app_id, "offset": 20})["text"]
    with pytest.raises(ValueError, match="no schema"):
        invoke(packet, "read_app_schema", {"app_id": "../../etc/passwd"})
    with pytest.raises(ValueError, match="offset"):
        invoke(packet, "read_app_schema", {"app_id": app_id, "offset": -1})
    with pytest.raises(ValueError, match="missing"):
        catalogs.tool_context([])


def test_schema_load_rejects_changed_bytes(root):
    catalogs = Catalogs(root / "catalogs")
    schema = next(iter(catalogs.app_schemas().values()))
    (root / "catalogs" / schema["path"]).write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        catalogs.app_schemas()


def test_reference_provenance_does_not_relabel_import_as_upstream(root):
    catalogs = Catalogs(root / "catalogs")
    path = root / "catalogs" / "manifest.json"
    original = path.read_bytes()
    manifest = read(path)
    provenance = catalogs.reference_provenance()
    assert provenance["catalog_manifest_hash"] == digest(manifest)
    assert provenance["import_commit"] == manifest["source_commit"]
    assert provenance["upstreams"]
    assert all(row["revision"] == "unrecorded" for row in provenance["upstreams"].values())
    assert path.read_bytes() == original


class Recorder:
    def __init__(self, company, tools):
        self.config = {"design": {"read_only_tools": tools}}
        self.company = company
        self.calls = []

    def call(self, job, prompt, response_type, **kwargs):
        self.calls.append((job, json.loads(prompt.rsplit("\n", 1)[1]), kwargs))
        return self.company, {"job": job}


@pytest.mark.parametrize(
    "repair,needs_sources,job",
    [(False, False, "research"), (True, False, "research_repair"), (True, True, "research")],
)
def test_research_and_repairs_have_bounded_reference_access(root, company, repair, needs_sources, job):
    models = Recorder(company, True)
    candidate = Candidate(
        id=company.id,
        real_firm=company.real_firm,
        website=company.website,
        sector=company.sector,
        reason="Relevant operation",
    )
    sources = [{"url": company.website, "excerpt": "We manufacture instruments."}]
    research(
        models,
        Catalogs(root / "catalogs"),
        "skill",
        candidate,
        {},
        previous=company if repair else None,
        excerpts=sources,
        needs_sources=needs_sources,
    )
    actual_job, payload, kwargs = models.calls[0]
    assert actual_job == job
    assert kwargs["context"]["sources"] == sources
    assert kwargs["context"]["app_schemas"]
    assert "read_app_schema" in payload["reference_access"]["tools"]


def test_read_source_is_offered_only_for_pages_actually_in_the_packet(root, company):
    """94 of 162 first-call read_source turns failed: 84% of jobs start with no packet."""
    candidate = Candidate(
        id=company.id,
        real_firm=company.real_firm,
        website=company.website,
        sector=company.sector,
        reason="Relevant operation",
    )
    empty = Recorder(company, True)
    research(empty, Catalogs(root / "catalogs"), "skill", candidate, {})
    access = empty.calls[0][1]["reference_access"]
    assert "read_source" not in access["tools"]
    assert access["readable_source_urls"] == []
    assert "read_app_schema" in access["tools"]

    supplied = Recorder(company, True)
    pages = [
        {"url": company.website, "excerpt": "We manufacture instruments."},
        {"url": "https://example.com/403", "excerpt": "", "capture_status": "unreadable"},
    ]
    research(supplied, Catalogs(root / "catalogs"), "skill", candidate, {}, excerpts=pages)
    access = supplied.calls[0][1]["reference_access"]
    assert "read_source" in access["tools"]
    # A failed capture is in the packet but has no text to page through.
    assert access["readable_source_urls"] == [company.website]


def test_research_payload_drops_the_coordinator_ledger_it_never_reads(root, company):
    """The ledger the dossier author is never asked to read was 16% of a 230 KB prompt."""
    candidate = Candidate(
        id=company.id,
        real_firm=company.real_firm,
        website=company.website,
        sector=company.sector,
        reason="Relevant operation",
    )
    coverage = {
        "app_usage": {"gmail_mock": 2},
        "unreadable_hosts": ["www.example.com"],
        "assigned_app_history": [{"job_id": "other", "declined_apps": {"sap_mock": "no fit"}}],
        "cell_histogram": {'[["a.b"],"correct-records"]': 3},
        "company_feature_cells": {"other": [{"collections": ["a.b"], "decision_type": "plan-schedule"}]},
        "in_flight_app_usage": {"asana_mock": 1},
        "uncovered_priority_occupations": {"17-2112": "Industrial Engineer"},
    }
    models = Recorder(company, False)
    research(models, Catalogs(root / "catalogs"), "skill", candidate, coverage)
    sent = models.calls[0][1]["coverage"]
    assert set(sent) == {"app_usage", "unreadable_hosts"}
    assert sent["app_usage"] == coverage["app_usage"]
    assert models.calls[0][1]["unreadable_hosts"] == ["www.example.com"]
    # Dropping it from this prompt must not edit the coordinator's own frozen payload,
    # which expansion still reads for its feature cells.
    assert coverage["company_feature_cells"]["other"]
    assert research_view(coverage) is not coverage


def test_legacy_research_does_not_enable_new_tools(root, company):
    models = Recorder(company, False)
    candidate = Candidate(
        id=company.id,
        real_firm=company.real_firm,
        website=company.website,
        sector=company.sector,
        reason="Relevant operation",
    )
    research(models, Catalogs(root / "catalogs"), "skill", candidate, {})
    assert "context" not in models.calls[0][2]
