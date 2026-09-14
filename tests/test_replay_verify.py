"""Replay uses real cache lookup with a fake provider, never a model or VM."""

import json
import tomllib

import pytest
from test_state_seed import accept, app_state, core, revise
from test_state_seed import exported as exported  # noqa: PLC0414 -- register the shared pytest fixture

from company_envs import models as model_module
from company_envs.models import ModelOutputInvalid, Models, ModelUnavailable
from company_envs.storage import read, write
from company_envs.world import state_seed
from company_envs.world.bulk_layer import add_bulk
from company_envs.world.replay_verify import replay_verify


def files(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


@pytest.fixture
def cached_company(exported, monkeypatch, request):
    root, folder = exported
    with (root / "config.toml").open("a") as stream:
        stream.write('world_review = ["codex/gpt-test"]\n[design]\nseed_shards = 1\nreview_votes = 3\n')
    write(
        folder.parent / "neighbor/world/world.json",
        {"people": [{"name": "Ana Moss", "email": "ana@harbor.test"}]},
    )

    def execute(cmd, prompt, directory, timeout):
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        value = (
            core()
            if payload["call"] == "world_core"
            else accept()
            if payload["call"] == "world_review"
            else app_state()
        )
        if (
            payload["call"] == "world_review"
            and payload["round"] == 0
            and getattr(request, "param", None) == "repair"
        ):
            value = revise()
        write(directory / "answer.json", value.model_dump())
        (directory / "stdout.jsonl").write_text('{"type":"thread.started","thread_id":"fake-session"}\n')
        return 0, 0.01

    monkeypatch.setattr(model_module, "execute", execute)
    state_seed.seed_world(root, folder, review_rounds=1)

    def forbidden(*args, **kwargs):
        pytest.fail("a cache replay tried to execute a provider")

    monkeypatch.setattr(model_module, "execute", forbidden)
    return root, folder


def test_cached_replay_is_identical_and_leaves_original_untouched(cached_company):
    root, folder = cached_company
    # The replay must retain the old cohort inputs when later companies appear.
    write(folder.parent / "neighbor/world/world.json", {"people": [{"name": "Bela Kent"}]})
    original = files(folder)
    out = root / "replay"
    result = replay_verify(root, folder, out)
    assert result == {"identical": True, "differing": []}
    assert read(out / "REPLAY.json") == result
    assert files(folder) == original
    assert not (out / "world/calls").exists()
    assert (out / "world/materials/w1/notes/desk.md").read_text() == "Desk policy v3."
    assert read(out / "world/REVIEW.json")["rounds"][0]["receipt"]["votes"] == 3


def test_cached_replay_reports_one_changed_file(cached_company):
    root, folder = cached_company
    path = folder / "world/demo_mock.state.json"
    state = read(path)
    state["tickets"][0]["title"] = "A change after seeding"
    write(path, state)
    result = replay_verify(root, folder, root / "replay")
    assert result == {"identical": False, "differing": ["world/demo_mock.state.json"]}


@pytest.mark.parametrize("cached_company", ["repair"], indirect=True)
def test_replay_repeats_recorded_repairs_and_review_rounds(cached_company):
    root, folder = cached_company
    assert len(read(folder / "world/REVIEW.json")["rounds"]) == 2
    assert replay_verify(root, folder, root / "replay")["identical"]


def test_replay_detects_added_missing_and_material_receipt_files(cached_company):
    root, folder = cached_company
    (folder / "world/materials/w1/notes/desk.md").unlink()
    write(folder / "world/unexpected.json", {"extra": True})
    write(folder / "world/materials/w1/receipt.json", {"business_receipt": True})
    write(folder / "world/receipts/run.json", {"seconds": 9})
    write(folder / "world/receipt.json", {"seconds": 8})
    write(folder / "world/check.receipt.json", {"seconds": 7})
    result = replay_verify(root, folder, root / "replay")
    assert result["differing"] == [
        "world/materials/w1/notes/desk.md",
        "world/materials/w1/receipt.json",
        "world/unexpected.json",
    ]


def test_changed_company_input_is_a_cache_miss(cached_company):
    root, folder = cached_company
    company = read(folder / "company.json")
    company["name"] = "Another company"
    write(folder / "company.json", company)
    before = files(folder)
    with pytest.raises(ModelUnavailable, match="cache miss for world_states"):
        replay_verify(root, folder, root / "replay")
    assert files(folder) == before
    assert not (root / "replay/REPLAY.json").exists()


@pytest.mark.parametrize("destination", ["same", "child", "parent", "nonempty"])
def test_replay_refuses_output_that_could_overwrite_evidence(cached_company, destination):
    root, folder = cached_company
    out = {
        "same": folder,
        "child": folder / "replay",
        "parent": folder.parent,
        "nonempty": root / "occupied",
    }[destination]
    if destination == "nonempty":
        write(out / "keep.json", {"keep": True})
    before = files(folder)
    with pytest.raises(ValueError, match="separate folders|new or empty"):
        replay_verify(root, folder, out)
    assert files(folder) == before


def test_replay_without_review_and_with_empty_output(cached_company):
    root, folder = cached_company
    (folder / "world/REVIEW.json").unlink()
    config = tomllib.loads((root / "config.toml").read_text())
    config["models"]["world_states"] = config["models"]["expand"]
    state_seed.seed_world(
        root,
        folder,
        models=Models(config, folder / "world", cache_only=True),
        review_rounds=0,
    )
    out = root / "replay"
    out.mkdir()
    assert replay_verify(root, folder, out)["identical"]
    assert not (out / "world/REVIEW.json").exists()


def test_missing_review_is_regenerated_when_seed_records_review(cached_company):
    root, folder = cached_company
    (folder / "world/REVIEW.json").unlink()
    assert replay_verify(root, folder, root / "replay") == {
        "identical": False,
        "differing": ["world/REVIEW.json"],
    }


def test_bulk_replay_uses_its_own_read_only_cache(cached_company, monkeypatch):
    root, folder = cached_company
    config = tomllib.loads((root / "config.toml").read_text())
    config["models"]["world_states"] = config["models"]["expand"]
    data = {
        "rationale": "ordinary tickets",
        "specs": [
            {
                "app_id": "demo_mock",
                "collection": "tickets",
                "what": "Earlier tickets",
                "count": 2,
                "start": "2026-01-01",
                "end": "2026-08-01",
                "id_prefix": "ticket-",
                "template_json": '{"id": "{{int:100-99999}}"}',
            }
        ],
    }

    def execute(cmd, prompt, directory, timeout):
        assert json.loads(prompt.rsplit("\n", 1)[1])["call"] == "bulk_specs"
        write(directory / "answer.json", data)
        (directory / "stdout.jsonl").write_text("")
        return 0, 0

    monkeypatch.setattr(model_module, "execute", execute)
    result = add_bulk(root, folder, models=Models(config, folder / "world/bulk"))
    assert result["added"] == {"demo_mock": {"tickets": 2}}

    def forbidden(*args, **kwargs):
        pytest.fail("bulk replay tried to execute a provider")

    monkeypatch.setattr(model_module, "execute", forbidden)
    before = files(folder)
    assert replay_verify(root, folder, root / "replay")["identical"]
    assert len(read(root / "replay/world/demo_mock.state.json")["tickets"]) == 3
    assert files(folder) == before


@pytest.fixture
def model_cache(tmp_path, monkeypatch):
    config = {
        "generation": {"completion_timeout_seconds": 1},
        "models": {"expand": ["codex/gpt-test"], "reasoning": "high"},
    }

    def execute(cmd, prompt, directory, timeout):
        write(directory / "answer.json", core().model_dump())
        (directory / "stdout.jsonl").write_text("")
        return 0, 0

    monkeypatch.setattr(model_module, "execute", execute)
    models = Models(config, tmp_path)
    models.call("expand", "cached", state_seed.WorldCore)

    def forbidden(*args, **kwargs):
        pytest.fail("cache-only mode tried to execute a provider")

    monkeypatch.setattr(model_module, "execute", forbidden)
    return config, tmp_path


def test_cache_only_hit_validates_and_uses_cached_fallback(model_cache):
    config, directory = model_cache
    config["models"]["expand"].insert(0, "codex/gpt-uncached")
    before = files(directory)
    seen = []
    parsed, receipt = Models(config, directory, cache_only=True).call(
        "expand",
        "cached",
        state_seed.WorldCore,
        validate=lambda value: seen.append(value),
    )
    assert parsed == core() and seen == [core()]
    assert receipt["model"] == "codex/gpt-test"
    assert files(directory) == before


@pytest.mark.parametrize("change", ["prompt", "reasoning", "schema", "model"])
def test_cache_miss_raises_without_writing_or_executing(model_cache, change):
    config, directory = model_cache
    kwargs = {}
    if change == "reasoning":
        config["models"]["reasoning"] = "low"
    elif change == "model":
        config["models"]["expand"] = ["codex/gpt-other"]
    elif change == "schema":
        kwargs["schema"] = {"type": "object", "description": "changed contract"}
    before = files(directory)
    with pytest.raises(ModelUnavailable, match="cache miss for expand") as error:
        Models(config, directory, cache_only=True).call(
            "expand",
            "new" if change == "prompt" else "cached",
            state_seed.WorldCore,
            **kwargs,
        )
    assert not error.value.retryable
    assert files(directory) == before


def test_cache_only_preserves_invalid_drafts(model_cache):
    config, directory = model_cache
    saved = next(directory.glob("calls/*/result.json"))
    result = read(saved)
    saved.unlink()
    write(
        saved.with_name("invalid.json"),
        {"error": "bad draft", "data": result["data"], "receipt": result["receipt"]},
    )
    before = files(directory)
    with pytest.raises(ModelOutputInvalid, match="bad draft"):
        Models(config, directory, cache_only=True).call("expand", "cached", state_seed.WorldCore)
    assert files(directory) == before
