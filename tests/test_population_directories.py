from company_envs.world.population_directories import hubspot_directories


def test_known_profiles_compile_without_inventing_unknown_financials():
    world = {
        "accounts": [
            {"id": "a", "name": "Acme", "domain": "acme.test", "status": "active", "city": "Seattle"}
        ],
        "contacts": [{"id": "c", "account_id": "a", "name": "Ada de Silva", "email": "ada@acme.test"}],
    }
    plan = [{"app_id": "hubspot_mock", "collection": "companies", "first_date": "2025-03-01"}]
    result = hubspot_directories(world, plan, [{"name": "Imani"}])
    assert result["companies"][0]["id"] == "a"
    assert result["companies"][0]["numberOfEmployees"] is None
    assert result["companies"][0]["annualRevenue"] is None
    assert result["companies"][0]["industry"] == ""
    assert result["contacts"][0]["lastName"] == "de Silva"
    assert result["contacts"][0]["companyId"] == "a"
    assert result["contacts"][0]["owner"] == "Imani"
    assert result["contacts"][0]["lifecycleStage"] == "customer"
    world["contacts"][0]["account_id"] = "missing"
    assert hubspot_directories(world, plan, []) == {}


def test_app_author_compiles_directories_with_canonical_owner_without_an_identity_key(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    import pytest

    from company_envs.world import state_seed

    world = {
        "staff": [{"worker_id": "rep", "name": "Imani", "email": "imani@example.test"}],
        "accounts": [{"id": "a", "name": "Acme", "domain": "acme.test", "status": "active"}],
        "contacts": [{"id": "c", "name": "Ada", "email": "ada@acme.test", "account_id": "a"}],
    }
    app = {"app_id": "hubspot_mock", "top_level_keys": ["companies", "contacts"]}
    plan = [
        {"app_id": "hubspot_mock", "collection": k, "target_records": 1, "first_date": "2025-03-01"}
        for k in app["top_level_keys"]
    ]
    monkeypatch.setattr(
        state_seed, "record_field_catalogue", lambda root: {"hubspot_mock": {"companies": [], "contacts": []}}
    )
    monkeypatch.setattr(
        state_seed, "validate_app_state", lambda result, *a, **kw: json.loads(result.state_json)
    )
    monkeypatch.setattr(state_seed, "register_people", lambda *a: None)
    author = state_seed.make_author(
        tmp_path,
        tmp_path / "company",
        {"design": {"seed_shards": 1, "seed_compile_directories": True, "seed_small_collection_limit": 12}},
        SimpleNamespace(call=lambda *a, **kw: pytest.fail("known directories were sent to a model")),
        "instructions",
        world=world,
        reference_date="2026-09-01",
        operating_scope="desk",
        identities={},
        worker_apps={"rep": ["hubspot_mock"]},
        company={},
        tasks=[],
        apps={"hubspot_mock": app},
        receipts={},
        population_plan=plan,
    )
    _, state = author(app)
    assert state["companies"][0]["owner"] == "Imani"
    assert state["contacts"][0]["owner"] == "Imani"
