from copy import deepcopy

from company_envs.storage import digest, write
from company_envs.world.hub_access import access_errors
from company_envs.world.hub_identity import visible_to
from company_envs.world.hub_resources import ResourceStore


def test_private_chat_maps_follow_channel_and_dm_membership():
    state = {
        "channels": [
            {"channelId": "a", "isPrivate": True, "members": ["alice"]},
            {"channelId": "b", "isPrivate": False, "members": []},
        ],
        "dms": [
            {"dmId": "ab", "participants": ["alice", "bob"]},
            {"dmId": "bc", "participants": ["bob", "carol"]},
        ],
        "messages": {"a": [1], "b": [2], "ab": [3], "bc": [4]},
        "threads": {"private": {"channelId": "a"}, "public": {"channelId": "b"}, "dm": {"dmId": "bc"}},
    }
    view = visible_to(state, {"id": "carol"})
    assert set(view["messages"]) == {"b", "bc"}
    assert set(view["threads"]) == {"public", "dm"}
    assert len(state["messages"]) == 4


def test_private_files_comments_and_read_only_writes():
    state = {
        "documents": {
            "secret": {"id": "secret", "ownerId": "bob", "content": "payroll", "sharedWith": []},
            "shared": {
                "id": "shared",
                "ownerId": "bob",
                "content": "policy",
                "sharedWith": [{"userId": "alice", "permission": "viewer"}],
            },
        },
        "comments": [{"id": "c", "docId": "secret", "content": "private"}],
    }
    person = {"id": "alice"}
    base = visible_to(state, person)
    assert set(base["documents"]) == {"shared"} and base["comments"] == []
    edited = deepcopy(base)
    edited["documents"]["shared"]["content"] = "tamper"
    assert access_errors(state, edited, base, person)
    edited = deepcopy(base)
    edited["documents"]["secret"] = deepcopy(state["documents"]["secret"])
    assert access_errors(state, edited, base, person)
    edited = deepcopy(base)
    edited["documents"]["shared"]["starred"] = True
    assert not access_errors(state, edited, base, person)


def test_drafts_are_private_to_author():
    state = {
        "drafts": [{"id": "draft", "from": {"email": "bob@co.test"}, "to": [{"email": "alice@co.test"}]}]
    }
    assert visible_to(state, {"email": "alice@co.test"})["drafts"] == []


def test_mail_and_comment_replies_cannot_impersonate_another_worker():
    person = {"id": "alice", "email": "alice@co.test"}
    state = {"emails": [], "drafts": []}
    forged = {
        "emails": [{"id": "forged", "from": {"email": "bob@co.test"}, "body": "Approved"}],
        "drafts": [],
    }
    assert access_errors(state, forged, state, person)
    forged["emails"][0]["from"]["email"] = "alice@co.test"
    assert not access_errors(state, forged, state, person)
    state = {
        "documents": {"d": {"id": "d", "ownerId": "alice"}},
        "comments": [{"id": "c", "docId": "d", "userId": "alice", "content": "Question", "replies": []}],
    }
    forged = deepcopy(state)
    forged["comments"][0]["replies"] = [{"id": "r", "userId": "bob", "content": "Approved"}]
    assert access_errors(state, forged, state, person)


def test_resources_are_bound_reversible_and_revocable(tmp_path):
    data = b"%PDF-test"
    file = tmp_path / "world/population/test.pdf"
    file.parent.mkdir(parents=True)
    file.write_bytes(data)
    url = "https://resources.example/invoice.pdf"
    binary = "data:application/pdf;base64,JVBERi10ZXN0"
    write(
        tmp_path / "world/population/ASSETS.json",
        {
            "resources": [
                {"id": "invoice", "url": url, "path": str(file.relative_to(tmp_path)), "sha256": digest(data)}
            ]
        },
    )
    state = {
        "items": {
            "invoice": {
                "id": "invoice",
                "ownerId": "bob",
                "thumbnailUrl": binary,
                "content": "Download " + url + ".",
            }
        }
    }
    store = ResourceStore(tmp_path, {"google_drive_mock": state})
    rewritten = store.rewrite(state)
    assert store.rewrite(rewritten, reverse=True) == state
    route = store.aliases[url]
    assert store.response(route, visible_to(state, {"id": "alice"}))[0] == 404
    assert store.response(route, visible_to(state, {"id": "bob"}))[1] == data
    file.write_bytes(b"changed")
    assert store.response(route, state)[0] == 409


def test_document_projection_preserves_rich_text_on_drive_rename():
    from company_envs.world.hub_documents import project_document_edits
    from company_envs.world.population_quality import DOC_MIME

    before = {
        "items": {
            "d": {
                "id": "d",
                "name": "Before",
                "type": "doc",
                "mimeType": DOC_MIME,
                "ownerId": "alice",
                "content": "Rich text",
                "createdAt": 1_700_000_000_000,
                "modifiedAt": 1_700_000_000_000,
                "sharedWith": [],
            }
        }
    }
    after = deepcopy(before)
    after["items"]["d"]["name"] = "After"
    peer = {
        "documents": {
            "d": {
                "id": "d",
                "title": "Before",
                "content": "<p><strong>Rich</strong> text</p>",
                "ownerId": "alice",
                "created": "2023-11-14T22:13:20+00:00",
                "updated": "2023-11-14T22:13:20+00:00",
                "sharedWith": [],
            }
        },
        "comments": [],
    }
    result = project_document_edits("google_drive_mock", before, after, peer)
    assert result["documents"]["d"]["title"] == "After"
    assert result["documents"]["d"]["content"] == peer["documents"]["d"]["content"]
    assert peer["documents"]["d"]["title"] == "Before"
    after["items"]["d"]["linkSharing"] = {"enabled": True, "permission": "editor"}
    shared = project_document_edits("google_drive_mock", before, after, peer)
    assert shared["documents"]["d"]["linkSharing"] == after["items"]["d"]["linkSharing"]
    closed = deepcopy(shared)
    closed["documents"]["d"]["linkSharing"]["enabled"] = False
    assert not project_document_edits("google_docs_mock", shared, closed, after)["items"]["d"]["linkSharing"][
        "enabled"
    ]


def test_amendment_cannot_overwrite_a_different_source(tmp_path):
    import pytest

    from company_envs.world.population_amendments import corrected_world

    original = {"assets": [{"id": "a", "bytes": 10}]}
    write(
        tmp_path / "world/population/AMENDMENTS.json",
        {
            "parent_world_hash": digest(original),
            "world": [
                {
                    "collection": "assets",
                    "id": "a",
                    "field": "bytes",
                    "before": 10,
                    "after": 8,
                    "reason": "measured archive",
                }
            ],
        },
    )
    assert corrected_world(tmp_path, original)["assets"][0]["bytes"] == 8
    assert original["assets"][0]["bytes"] == 10
    with pytest.raises(ValueError, match="different population"):
        corrected_world(tmp_path, {"assets": [{"id": "a", "bytes": 12}]})


def test_review_sheet_rows_do_not_starve_other_apps_and_threads_are_complete():
    from company_envs.world.world_review import bound_states

    messages = [
        {
            "messageId": f"m{i}",
            "content": "A specific business question " + str(i),
            "threadId": "t" if i else None,
        }
        for i in range(4)
    ]
    states = {
        "google_sheets_mock": {
            "sheets": [{"id": "sheet", "data": {f"A{n}": {"value": "x" * 100} for n in range(1, 3000)}}]
        },
        "gmail_mock": {
            "emails": [{"id": f"e{i}", "body": "hello"} for i in range(200)],
            "user": {"id": "u", "name": "Alice", "email": "alice@example.test", "avatar": ""},
        },
        "slack_mock": {
            "threads": {"t": {"threadId": "t", "parentMessageId": "m0", "replies": ["m1", "m2", "m3"]}},
            "messages": {"channel": messages},
        },
    }
    bounded = bound_states(states, limit=30000)
    assert len(bounded["gmail_mock"]["emails"]) > 20
    assert bounded["gmail_mock"]["user"] == states["gmail_mock"]["user"]
    assert {m["messageId"] for m in bounded["slack_mock"]["messages"]["channel"]} == {"m0", "m1", "m2", "m3"}
    assert "_sampled_rows" in bounded["google_sheets_mock"]["sheets"][0]


def test_gmail_native_drafts_render_save_and_delete_without_resurrection():
    from company_envs.world.hub_mail import browser_mail, canonical_mail

    state = {
        "emails": [{"id": "e", "folder": "inbox"}],
        "drafts": [{"id": "d", "folder": "drafts", "body": "unfinished"}],
    }
    browser = browser_mail(state)
    assert len(browser["emails"]) == 2
    assert canonical_mail(browser) == state
    browser["emails"] = [r for r in browser["emails"] if r["id"] != "d"]
    assert canonical_mail(browser)["drafts"] == []


def test_calendar_without_guests_is_private_to_its_calendar_owner():
    state = {
        "calendars": [{"id": "alice-cal", "userId": "alice"}],
        "events": [{"id": "e", "calendarId": "alice-cal", "guests": []}],
    }
    assert visible_to(state, {"id": "alice"})["events"] == state["events"]
    assert visible_to(state, {"id": "bob"})["events"] == []


def test_contact_recency_uses_observed_activity_and_ignores_future_deadlines():
    from company_envs.world.population_activity import activity_errors, refresh_activity

    crm = {
        "contacts": [{"id": "c", "companyId": "co", "lastActivityDate": "2025-01-01"}],
        "tasks": [
            {
                "id": "task",
                "companyId": "co",
                "createDate": "2025-03-04",
                "dueDate": "2026-09-30",
                "status": "not_started",
            }
        ],
        "notes": [
            {
                "id": "note",
                "associatedType": "contact",
                "associatedId": "c",
                "createDate": "2025-06-12T16:00:00Z",
            }
        ],
    }
    assert activity_errors(crm)
    assert refresh_activity(crm) == ["c"]
    assert crm["contacts"][0]["lastActivityDate"].startswith("2025-06-12")
    assert activity_errors(crm) == []


def test_monthly_office_cadence_respects_weekdays_without_looping_at_end():
    from company_envs.world.bulk import BulkSpec, periods

    spec = BulkSpec(
        app_id="google_calendar_mock",
        what="Monthly office appointments",
        id_prefix="office-",
        collection="events",
        count=4,
        template_json='{"id":"x{{seq}}"}',
        start="2026-01-10",
        end="2026-04-12",
        cadence="monthly",
        weekdays_only=True,
    )
    dates = periods(spec)
    assert [d.date().isoformat() for d in dates] == ["2026-01-12", "2026-02-10", "2026-03-10", "2026-04-10"]
