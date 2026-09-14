import shutil
from pathlib import Path

import pytest

from company_envs.schemas import Company, Workflow

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_live_model_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unit tests must mock model execution")

    monkeypatch.setattr("company_envs.models.execute", forbidden)
    # Provider doubles need no real account writes or networking. Capability probes
    # have their own tests, including restoring this guard to test dispatch ordering.
    monkeypatch.setattr("company_envs.models.require_model_environment", lambda home=None: None)


@pytest.fixture
def root(tmp_path):
    shutil.copytree(REPO / "catalogs", tmp_path / "catalogs")
    shutil.copytree(REPO / ".agents", tmp_path / ".agents")
    shutil.copyfile(REPO / "config.toml", tmp_path / "config.toml")
    # Tests count one review call; voting tests set generation.review_votes themselves.
    text = (tmp_path / "config.toml").read_text()
    (tmp_path / "config.toml").write_text(text.replace("review_votes = 3", "review_votes = 1", 1))
    return tmp_path


@pytest.fixture
def company():
    return Company.model_validate(
        {
            "id": "example",
            "name": "Example Analogue",
            "real_firm": "Example Real Firm",
            "website": "https://example.com",
            "sector": "Manufacturing",
            "naics": "334",
            "location": "United States",
            "operations": "Design and manufacture instruments.",
            "evidence": [
                {
                    "id": "operations",
                    "kind": "sourced",
                    "text": "Makes instruments.",
                    "source_url": "https://example.com",
                    "quote": "We manufacture instruments.",
                    "basis": "",
                }
            ],
            "workers": [
                {
                    "id": wid,
                    "title": "Industrial Engineer",
                    "soc": "17-2112",
                    "responsibility": responsibility,
                    "information_access": ["Engineering workspace"],
                    "authority": "Approve own design analysis",
                    "evidence_ids": ["operations"],
                }
                for wid, responsibility in [("design", "Design a product"), ("process", "Plan manufacturing")]
            ],
            "teams": [
                {
                    "id": "engineering",
                    "name": "Engineering",
                    "purpose": "Develop products",
                    "worker_ids": ["design", "process"],
                },
                {
                    "id": "launch",
                    "name": "Launch",
                    "purpose": "Release production",
                    "worker_ids": ["process"],
                },
            ],
            "software": [
                {
                    "id": "workspace",
                    "capability": "Engineering document collaboration",
                    "reference_product": "",
                    "catalog_app_ids": [],
                    "status": "gap",
                    "rationale": "An engineering document adapter is still needed.",
                    "evidence_ids": [],
                }
            ],
            "outlines": [
                {
                    "id": "release",
                    "title": "Release instrument",
                    "objective": "Qualify a new design",
                    "decision_problem": "Balance accuracy and process yield",
                    "team_ids": ["engineering", "launch"],
                    "worker_ids": ["design", "process"],
                    "evidence_ids": ["operations"],
                }
            ],
            "assumptions": ["Team structure is synthetic, not a claim about the real firm."],
        }
    )


@pytest.fixture
def workflow():
    return Workflow.model_validate(
        {
            "id": "example_release",
            "company_id": "example",
            "outline_id": "release",
            "title": "Release instrument",
            "brief": "Develop and qualify the instrument for production.",
            "objective": "Release a viable design",
            "decision_problem": "Balance accuracy and process yield",
            "canonical_description": "Develop an instrument and qualify its production process. Design choices affect manufacturing yield; process trials feed back into the release decision.",
            "team_ids": ["engineering", "launch"],
            "worker_ids": ["design", "process"],
            "phases": [
                {
                    "id": "design",
                    "name": "Design",
                    "worker_ids": ["design"],
                    "depends_on": [],
                    "inputs": ["Customer requirements"],
                    "outputs": ["Design specification"],
                    "decision": "Select architecture",
                    "downstream_effect": "Determines process tolerances",
                },
                {
                    "id": "qualify",
                    "name": "Qualify",
                    "worker_ids": ["process"],
                    "depends_on": ["design"],
                    "inputs": ["Design specification"],
                    "outputs": ["Qualification evidence and release disposition"],
                    "decision": "Release or request redesign",
                    "downstream_effect": "Controls production release",
                },
            ],
            "contributions": [
                {
                    "worker_id": wid,
                    "inputs": ["Requirements"],
                    "work": work,
                    "produces": ["Engineering analysis"],
                    "shares_with": [other],
                }
                for wid, other, work in [
                    ("design", "process", "Design architecture"),
                    ("process", "design", "Qualify process"),
                ]
            ],
            "events": [],
            "initial_materials": ["Customer requirements and instrument platform constraints"],
            "deliverables": ["Design specification", "Qualification evidence", "Release disposition"],
            "success_criteria": [
                {
                    "requirement": "Design meets customer needs",
                    "observable": "Traceable qualification evidence",
                    "method": "judgment",
                }
            ],
            "software_requirement_ids": ["workspace"],
            "evidence_ids": ["operations"],
            "calendar_days": 30,
            "estimated_human_effort": {
                "minimum_hours": 40,
                "maximum_hours": 80,
                "basis": "Synthetic scoping estimate",
            },
            "assumptions": ["Trials are represented by supplied digital measurements."],
        }
    )
