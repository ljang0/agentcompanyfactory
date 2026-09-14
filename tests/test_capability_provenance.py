"""Compact invocation identity replaces copied coordinator source trees."""

from test_model_telemetry import Answer, backend, transcript

from company_envs import models, pipeline
from company_envs.storage import digest, read, write


def test_adapter_identity_preserves_cache_until_contract_changes(tmp_path, monkeypatch):
    calls = []

    def execute(cmd, prompt, directory, timeout):
        calls.append(directory)
        transcript(directory, [])
        write(directory / "answer.json", {"value": "accepted"})
        return 0, 0.1

    models.reset_cancellation()
    monkeypatch.setattr(models, "execute", execute)
    client = backend(tmp_path)
    first = client.call("discover", "prompt", Answer)
    assert client.call("discover", "prompt", Answer) == first
    assert len(calls) == 1
    monkeypatch.setattr(models, "MODEL_ADAPTER_VERSION", models.MODEL_ADAPTER_VERSION + 1)
    second = client.call("discover", "prompt", Answer)
    assert second[0] == first[0]
    assert second[1]["call_id"] != first[1]["call_id"]
    assert len(calls) == 2


def test_a_screen_is_part_of_the_question_it_is_asked_about(tmp_path, monkeypatch):
    """Two screens share a prompt whenever the app, company and person are the same, and a
    re-render after a repair reuses the prompt exactly. With the picture outside the cache key
    the second screen was answered from the first one's verdict."""
    calls = []

    def execute(cmd, prompt, directory, timeout):
        calls.append(directory)
        transcript(directory, [])
        write(directory / "answer.json", {"value": "accepted"})
        return 0, 0.1

    models.reset_cancellation()
    monkeypatch.setattr(models, "execute", execute)
    client = backend(tmp_path)
    before, after = tmp_path / "before.jpg", tmp_path / "after.jpg"
    before.write_bytes(b"the screen as seeded")
    after.write_bytes(b"the screen after the repair")

    first = client.call("discover", "prompt", Answer, images=[str(before)])
    assert client.call("discover", "prompt", Answer, images=[str(before)]) == first
    assert len(calls) == 1, "the same screen and prompt is still one call"

    repaired = client.call("discover", "prompt", Answer, images=[str(after)])
    assert repaired[1]["call_id"] != first[1]["call_id"]
    assert len(calls) == 2, "a different screen has to be looked at"

    # A call that carries no image keeps the key it always had, so nothing else is re-run.
    text_only = client.call("discover", "prompt", Answer)
    assert text_only[1]["call_id"] not in {first[1]["call_id"], repaired[1]["call_id"]}
    assert len(calls) == 3
    assert client.call("discover", "prompt", Answer)[1]["call_id"] == text_only[1]["call_id"]
    assert len(calls) == 3


def test_invocations_record_adapter_and_preserve_historical_metadata(root, monkeypatch):
    directory = pipeline.new_run(root, 1, 1)
    original = read(directory / "run.json")
    original["implementation_history"] = [{"hash": "historical", "path": "implementations/historical"}]
    write(directory / "run.json", original)
    monkeypatch.setattr(pipeline, "Embeddings", lambda *args: None)
    monkeypatch.setattr(pipeline, "fill_queue", lambda *args: False)
    for _ in range(2):
        state = pipeline.run(root, directory)
    assert len(state["invocations"]) == 2
    assert all(i["model_adapter_version"] == models.MODEL_ADAPTER_VERSION for i in state["invocations"])
    assert state["implementation_history"] == original["implementation_history"]
    assert not (directory / "implementations").exists()
    assert read(directory / "dataset.json")["invocations"] == state["invocations"]
    assert state["skills"] == original["skills"]
    assert all(digest(skill["source"]) == skill["hash"] for skill in state["skills"].values())
    assert state["catalog_hash"] == original["catalog_hash"]
    assert state["portfolio"] == original["portfolio"]
