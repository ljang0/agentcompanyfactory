from types import SimpleNamespace

from company_envs.world.population_comments import author_comments, new_comment_requests


def test_comment_threads_use_permitted_colleagues_and_ordered_history(tmp_path):
    from datetime import datetime

    from company_envs.world.population_comments import author_comment_threads

    state = {
        "documents": {
            "d": {
                "id": "d",
                "title": "Receiving",
                "content": "Count the cartons before signing.",
                "ownerId": "owner",
                "created": "2025-01-01T10:00:00Z",
                "updated": "2025-02-01T10:00:00Z",
                "sharedWith": [{"userId": "reader", "permission": "commenter"}],
            }
        },
        "comments": [],
    }
    requests = new_comment_requests(state, 1, "2025-02-01", "2025-03-01")

    def call(job, prompt, response_type):
        return response_type.model_validate(
            {
                "documents": [
                    {
                        "id": "d",
                        "comments": [
                            {
                                "id": requests[0]["id"],
                                "user_id": "reader",
                                "content": "Does this include sealed cartons?",
                                "quoted_text": "Count the cartons before signing.",
                                "resolved": True,
                                "replies": [
                                    {"user_id": "owner", "content": "Yes, count every carton before signing."}
                                ],
                            }
                        ],
                    }
                ]
            }
        ), {"call_id": "thread"}

    comments, _ = author_comment_threads(
        state, requests, SimpleNamespace(call=call), tmp_path, reference_date="2025-03-01T10:00:00Z"
    )
    c = comments[0]
    assert c["userId"] == "reader" and c["replies"][0]["userId"] == "owner"
    assert (
        datetime.fromisoformat(state["documents"]["d"]["updated"])
        < datetime.fromisoformat(c["created"])
        < datetime.fromisoformat(c["replies"][0]["created"])
        < datetime.fromisoformat("2025-03-01T10:00:00Z")
    )
    assert c["resolved"]


def test_comment_writer_sees_actual_target_body_and_compiles_permitted_author(tmp_path):
    state = {
        "users": [{"id": "owner", "name": "Owner"}],
        "documents": {
            "d": {
                "id": "d",
                "title": "Receiving",
                "content": "<p>Count the cartons before signing.</p>",
                "ownerId": "owner",
                "created": "2025-01-01T10:00:00Z",
                "updated": "2025-02-01T10:00:00Z",
                "sharedWith": [{"userId": "reader", "permission": "viewer"}],
            }
        },
        "comments": [],
    }
    requests = new_comment_requests(state, 1, "2025-01-01", "2025-03-01")
    requests[0].update(userId="reader", created="2025-01-02T10:00:00Z")
    prompts = []

    def call(job, prompt, response_type):
        prompts.append(prompt)
        return response_type.model_validate(
            {
                "comments": [
                    {
                        "id": requests[0]["id"],
                        "content": "Where should we record a discrepancy?",
                        "quoted_text": "Count the cartons before signing.",
                    }
                ]
            }
        ), {"call_id": "one"}

    models = SimpleNamespace(call=call)
    result, provenance = author_comments(state, requests, models, tmp_path)
    assert "Count the cartons before signing." in prompts[0]
    assert "reader" not in prompts[0]
    assert result[0]["userId"] == "owner"
    assert result[0]["created"] == state["documents"]["d"]["updated"]
    assert provenance[0]["document_hashes"]["d"]
    assert author_comments(state, requests, models, tmp_path)[0] == result
    assert len(prompts) == 1  # Resume uses the saved batch.


def test_discussion_stays_in_supplied_overnight_window_and_keeps_replies_compact():
    from company_envs.world.population_comments import schedule_comment_threads
    from company_envs.world.population_quality import instant

    documents = {"d": {"updated": "2026-01-01T22:00:00+05:30"}}
    comments = [{"id": "c", "docId": "d", "replies": [{"id": "r1"}, {"id": "r2"}]}]
    first, last = "2026-07-04T23:59:50+05:30", "2026-07-05T00:00:10+05:30"
    result = schedule_comment_threads(
        comments, documents, "2026-09-01", "Asia/Kolkata", first_date=first, last_date=last
    )[0]
    times = [instant(r["created"]) for r in [result, *result["replies"]]]
    assert instant(first) < times[0] < times[1] < times[2] < instant(last)
    assert all(r["created"].endswith("+05:30") for r in [result, *result["replies"]])
    result = schedule_comment_threads(comments, documents, "2026-09-01")[0]
    assert (instant(result["replies"][-1]["created"]) - instant(result["created"])).total_seconds() <= 7200


def test_invalid_comment_window_uses_no_model_calls(tmp_path):
    import pytest

    from company_envs.world.population_comments import author_comment_threads

    with pytest.raises(ValueError, match="comment window"):
        author_comment_threads(
            {"documents": {"d": {"id": "d", "updated": "2026-09-01"}}},
            [{"id": "c", "docId": "d"}],
            SimpleNamespace(call=lambda *a: pytest.fail("invalid dates consumed a call")),
            tmp_path,
            reference_date="2026-08-31",
        )


def test_comment_repair_reuses_valid_document_and_rechecks_access_on_resume(tmp_path):
    import json
    from copy import deepcopy

    from company_envs.world.population_comments import author_comment_threads

    doc = {
        "id": "a",
        "title": "Inspection",
        "content": "Inspect the seal.",
        "ownerId": "author",
        "updated": "2026-01-01",
        "sharedWith": [{"userId": "colleague", "permission": "commenter"}],
    }
    state = {"documents": {"a": doc, "b": {**deepcopy(doc), "id": "b"}}}
    requests = [{"id": f"comment-{d}", "docId": d} for d in state["documents"]]
    calls = []

    def call(job, prompt, response_type):
        cases = json.loads(prompt.split("\n")[-1])["cases"]
        calls.append([c["id"] for c in cases])
        values = [
            {
                "id": c["id"],
                "comments": [
                    {
                        "id": c["comment_ids"][0],
                        "user_id": "outsider" if len(calls) == 1 and c["id"] == "b" else "author",
                        "content": "Does this include the inner seal?",
                        "quoted_text": "Inspect the seal.",
                        "resolved": False,
                        "replies": [],
                    }
                ],
            }
            for c in cases
        ]
        return response_type.model_validate({"documents": values}), {}

    models = SimpleNamespace(call=call)
    result, _ = author_comment_threads(state, requests, models, tmp_path, reference_date="2026-09-01")
    assert calls == [["a", "b"], ["b"]]
    # Dates can be recompiled without paying to regenerate the same discussion.
    author_comment_threads(state, requests, models, tmp_path, reference_date="2026-08-01")
    assert len(calls) == 2
    state["documents"]["a"]["sharedWith"] = []
    author_comment_threads(state, requests, models, tmp_path, reference_date="2026-09-01")
    assert calls[-1] == ["a"]
    assert len(result) == 2
