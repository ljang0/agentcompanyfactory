import json

import pytest

from company_envs.world.evidence_encoding import apply_edits, decode_actors, edits, encode_actors


@pytest.mark.parametrize(
    "before,after",
    [
        ({"a": 1}, {"b": None}),
        ({"typed": True}, {"typed": 1}),
        ([{"x": "old"}, 7], [{"x": "new"}]),
        ([], [{"body": "A\n日本語 🐈"}, None]),
        ("prefix old suffix", "prefix NEW suffix"),
        ("same", "same"),
        (None, {"id": "new", "body": "complete text"}),
        ({"id": "deleted"}, None),
    ],
)
def test_json_edits_preserve_exact_values_and_types(before, after):
    assert json.dumps(apply_edits(before, edits(before, after)), sort_keys=True) == json.dumps(
        after, sort_keys=True
    )


def test_actor_encoding_keeps_reverts_and_uncredited_projection_changes():
    baseline = {"id": "memo", "content": "A" * 20000}
    first = {**baseline, "content": baseline["content"] + " Approved"}
    projected = {**first, "mirror": "updated by app projection"}
    final = {**projected, "content": first["content"] + " Consumed"}
    events = [
        {"sequence": 1, "worker_id": "boss", "changes": {"docs#memo": {"before": baseline, "after": first}}},
        {"sequence": 2, "worker_id": "peer", "changes": {"docs#memo": {"before": projected, "after": final}}},
        {"sequence": 3, "worker_id": "boss", "changes": {"docs#memo": {"before": final, "after": baseline}}},
    ]
    # A reverted record is absent from final changed_records; the anchor still preserves it.
    encoded = encode_actors(events, {})
    assert decode_actors(encoded, {}) == events
    assert "before_sync" in encoded["events"][1]["changes"]["docs#memo"]
    assert len(json.dumps(encoded)) < len(json.dumps(events)) / 3


def test_a_text_splice_cannot_silently_apply_to_the_wrong_record_version():
    with pytest.raises(ValueError, match="prior evidence"):
        apply_edits("different", edits("prefix old suffix", "prefix new suffix"))
