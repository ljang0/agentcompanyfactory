"""A worker id is a login, a desktop directory and an actor field, so it may not be a list index.

Two of the 99 exported companies shipped a numbered roster that nothing noticed:
brixmor-property-group and cort, both w1..w6, 70 model hours between them, and cort's set-aside
world has materials/w1 .. materials/w6 on disk. Of 415 distinct worker ids across every dossier
written so far those six are the only ones containing a digit.
"""

import pytest
from test_research_references import Recorder

from company_envs.catalogs import Catalogs
from company_envs.models import ModelOutputInvalid
from company_envs.research import placeholder_worker_ids, research
from company_envs.schemas import Candidate, Company


def number_the_roster(company, ids):
    raw = company.model_dump()
    rename = dict(zip([w["id"] for w in raw["workers"]], ids))
    for worker, new in zip(raw["workers"], ids):
        worker["id"] = new
    for group in (*raw["teams"], *raw["outlines"]):
        group["worker_ids"] = [rename[w] for w in group["worker_ids"]]
    return Company.model_validate(raw)


def run(root, company):
    candidate = Candidate(
        id=company.id,
        real_firm=company.real_firm,
        website=company.website,
        sector=company.sector,
        reason="Relevant operation",
    )
    models = Recorder(company, False)
    return research(models, Catalogs(root / "catalogs"), "skill", candidate, {})


@pytest.mark.parametrize("ids", [["w1", "w2"], ["worker-1", "worker-2"], ["emp_7", "staff3"]])
def test_a_numbered_roster_is_sent_back_for_repair_with_a_receipt(root, company, ids):
    numbered = number_the_roster(company, ids)
    assert placeholder_worker_ids(numbered) == ids
    with pytest.raises(ModelOutputInvalid, match=f"worker ids name the role.*{ids[0]}") as error:
        run(root, numbered)
    # A failure receipt, not a silent pass: the repair round needs the rejected dossier back.
    assert error.value.raw == numbered.model_dump()
    assert error.value.receipt == {"job": "research"}


def test_role_named_ids_pass_including_the_ones_that_merely_start_with_w(root, company):
    """w-manager, cdi-manager and buyer are real ids on disk; only a bare number is a placeholder."""
    named = number_the_roster(company, ["w-manager", "process2-engineer"])
    assert placeholder_worker_ids(named) == []
    result, _ = run(root, named)
    assert result is named
