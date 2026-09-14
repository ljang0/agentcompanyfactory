"""Mechanical coherence checks catch duplicates, role drift, fact drift, future timestamps and dangling ids."""

import pytest

from company_envs.world import world_check


@pytest.mark.parametrize(
    "total_key", ["total", "amount", "balance", "subtotal", "grand_total", "grandTotal", "totalAmount"]
)
@pytest.mark.parametrize("list_key", ["items", "lines", "lineItems", "entries", "transactions", "charges"])
def test_arithmetic_finds_bad_totals_in_lists_and_mappings(total_key, list_key):
    record = {"id": "bill-1", total_key: 31, list_key: [{"amount": 10}, {"total": 20}]}
    for collection in ([record], {"bill-1": record}):
        findings = world_check.check_arithmetic("books", {"invoices": collection})
        assert len(findings) == 1
        assert findings[0]["severity"] == "error"
        assert findings[0]["source"] == "books"
        assert "bill-1" in findings[0]["message"]
        assert f"{total_key} 31 differs from {list_key} sum 30" in findings[0]["message"]
        assert "correct the total or its parts" in findings[0]["message"]


@pytest.mark.parametrize("total", [0.29, 0.3, 0.31])
def test_arithmetic_accepts_a_cent_of_rounding(total):
    assert (
        world_check.check_arithmetic("books", {"total": total, "items": [{"amount": 0.1}, {"amount": 0.2}]})
        == []
    )


@pytest.mark.parametrize("total", [30, 32, 32.01])
def test_arithmetic_accepts_price_times_quantity_and_combined_adjustments(total):
    record = {
        "total": total,
        "lines": [{"price": 5, "quantity": 2}, {"unitPrice": 10, "quantity": 2}],
        "tax": 3,
        "fee": 1,
        "discount": 2,
    }
    assert world_check.check_arithmetic("books", record) == []
    record["total"] = 33
    findings = world_check.check_arithmetic("books", record)
    assert len(findings) == 1
    assert "lines sum 30 + tax 3 + fee 1 - discount 2 = 32" in findings[0]["message"]


@pytest.mark.parametrize(
    "adjustment", [{"taxAmount": 2}, {"fee_amount": 2}, {"fees": 2}, {"discountAmount": -2}]
)
def test_arithmetic_accepts_named_adjustment_amounts(adjustment):
    total = 8 if "discountAmount" in adjustment else 12
    assert (
        world_check.check_arithmetic("books", {"total": total, "items": [{"amount": 10}], **adjustment}) == []
    )


def test_arithmetic_subtotal_excludes_tax_and_explicit_line_totals_take_precedence():
    record = {"subtotal": 15, "tax": 5, "lines": [{"amount": 10, "price": 20, "quantity": 1}]}
    assert len(world_check.check_arithmetic("books", record)) == 1
    record["subtotal"] = 10
    assert world_check.check_arithmetic("books", record) == []


@pytest.mark.parametrize(
    "subtotal, total, bad_field",
    [(10, 12, None), (11, 12, "subtotal"), (10, 14, "total"), (11, 14, "subtotal")],
)
def test_arithmetic_checks_subtotal_and_total_once_per_invoice(subtotal, total, bad_field):
    record = {"subtotal": subtotal, "total": total, "items": [{"amount": 10}], "tax": 2}
    findings = world_check.check_arithmetic("books", record)
    if bad_field is None:
        assert findings == []
    else:
        assert len(findings) == 1
        assert f"{bad_field} {record[bad_field]} differs" in findings[0]["message"]


@pytest.mark.parametrize(
    "record",
    [
        {"total": 9, "items": []},
        {"total": 9, "items": [{"amount": 1}, {"description": "price pending"}]},
        {"total": 9, "items": [{"amount": "1"}]},
        {"total": 9, "items": [{"price": 1}]},
        {"total": 9, "items": [{"price": 1, "quantity": True}]},
        {"total": 9, "items": [{"amount": float("nan")}]},
        {"total": float("inf"), "items": [{"amount": 1}]},
        {"total": True, "items": [{"amount": 9}]},
        {"total": "$9", "items": [{"amount": 1}]},
        {"total": 9, "items": [1]},
        {"total": 9, "items": [{"amount": 1, "total": 2}]},
        {"total": 9, "amount": 8, "items": [{"amount": 1}]},
        {"total": 9, "items": [{"amount": 1}], "charges": [{"amount": 2}]},
        {"total": 9, "items": [{"amount": 1}], "tax": "included"},
        {"total": 9, "items": [{"amount": 1}], "taxRate": 0.1},
        {"total": 9, "items": [{"amount": 1, "currency": "EUR"}], "currency": "USD"},
        {"total": 9, "transactions": [{"amount": 1, "type": "debit"}]},
    ],
)
def test_arithmetic_skips_ambiguous_shapes(record):
    assert world_check.check_arithmetic("books", record) == []


@pytest.mark.parametrize(
    "opening_key, closing_key", [("openingBalance", "closingBalance"), ("opening_balance", "closing_balance")]
)
def test_arithmetic_checks_statement_movements(opening_key, closing_key):
    record = {
        "id": "statement",
        opening_key: 100,
        closing_key: 125,
        "transactions": [{"amount": 30}, {"amount": -10}],
    }
    findings = world_check.check_arithmetic("books", {"statements": [record]})
    assert len(findings) == 1
    assert "125 differs from opening balance 100 + transactions sum 20 = 120" in findings[0]["message"]
    record[closing_key] = 120
    assert world_check.check_arithmetic("books", record) == []


def test_arithmetic_caps_each_collection_and_reports_records_without_ids():
    bad = {"total": 5, "items": [{"amount": 1}]}
    state = {"nested": {"invoices": [bad] * 12, "bills": {str(i): bad for i in range(12)}}}
    findings = world_check.check_arithmetic("books", state)
    assert len(findings) == 16
    assert sum(f["path"].startswith("/nested/invoices/") for f in findings) == 8
    assert sum(f["path"].startswith("/nested/bills/") for f in findings) == 8
    assert "record '/nested/invoices/0'" in findings[0]["message"]


@pytest.mark.parametrize("prose_key", ["body", "content", "description", "notes"])
@pytest.mark.parametrize(
    "text",
    ["Completed 2026-09-04.", "Completed September 4, 2026.", "Completed Sept 4, 2026.", "Completed Sep. 4."],
)
def test_chronology_rejects_prose_after_record_date(prose_key, text):
    record = {"id": "doc-1", "timestamp": "2026-09-03T23:00:00Z", prose_key: text}
    findings = world_check.check_chronology("docs", {"documents": {"doc-1": record}}, "2026-09-08")
    assert len(findings) == 1
    assert findings[0]["path"] == f"/documents/doc-1/{prose_key}"
    assert (
        "doc-1" in findings[0]["message"]
        and "2026-09-04 after timestamp 2026-09-03T23:00:00Z" in findings[0]["message"]
    )
    assert findings[0]["severity"] == "warning"  # a plan may name a later date; the reviewer decides


@pytest.mark.parametrize(
    "key", ["updatedAt", "modifiedAt", "createdAt", "updated_at", "modified_at", "created_at"]
)
def test_chronology_uses_latest_timestamp(key):
    record = {"timestamp": "2026-08-01", key: "2026-09-04", "body": "Completed September 4, 2026."}
    assert world_check.check_chronology("docs", {"documents": [record]}, "2026-09-08") == []


def test_chronology_uses_prose_year_and_ignores_ambiguous_years():
    record = {"timestamp": "2026-01-02", "notes": "Closed December 1, 2025. Last call Dec 4."}
    assert world_check.check_chronology("docs", record, "2026-09-08") == []
    record["notes"] = "2025 notes: last call Dec 4."
    assert world_check.check_chronology("docs", record, "2026-09-08") == []
    record["notes"] = "Closed December 1, 2024 and January 1, 2026. Last call Dec 4."
    assert world_check.check_chronology("docs", record, "2026-09-08") == []


@pytest.mark.parametrize(
    "record",
    [
        {"timestamp": "bad", "body": "Completed 2026-09-04."},
        {"body": "Completed 2026-09-04."},
        {"timestamp": "2026-09-03", "body": "Completed 2026-02-30 and February 30, 2026."},
        {"timestamp": "2026-09-03", "body": "See <a href='/2026-09-04'>the report</a>."},
        {"timestamp": "2026-09-03", "body": ["Completed 2026-09-04."]},
        {"timestamp": "2026-09-03", "title": "Completed 2026-09-04."},
        {"timestamp": "2026-09-04T00:30:00+02:00", "body": "Completed 2026-09-04."},
        {"timestamp": "2026-09-04", "body": "Completed 2026-09-04."},
    ],
)
def test_chronology_skips_missing_times_invalid_dates_and_same_day_prose(record):
    assert world_check.check_chronology("docs", record, "2026-09-08") == []


@pytest.mark.parametrize("link", ["inReplyTo", "parentId", "threadId", "in_reply_to", "parent_id"])
def test_chronology_rejects_replies_before_explicit_parent(link):
    state = {
        "emails": [
            {"id": "reply", link: "original", "timestamp": "2026-09-02T09:00:00Z"},
            {"id": "original", "timestamp": "2026-09-02T10:00:00Z"},
        ]
    }
    findings = world_check.check_chronology("mail", state, "2026-09-08")
    assert len(findings) == 1
    assert (
        findings[0]["message"]
        == "reply 'reply' at 2026-09-02T09:00:00Z precedes 'original' at 2026-09-02T10:00:00Z; correct the reply date or parent link"
    )


@pytest.mark.parametrize("prefix", ["Re:", "fwd:", " RE: Fwd:"])
def test_chronology_matches_reply_subject_to_one_original(prefix):
    state = {
        "emails": [
            {"subject": "Visit", "createdAt": "2026-09-04"},
            {"subject": f"{prefix} visit", "createdAt": "2026-09-03"},
        ]
    }
    findings = world_check.check_chronology("mail", state, "2026-09-08")
    assert len(findings) == 1 and "reply '/emails/1'" in findings[0]["message"]


def test_chronology_can_match_subjects_with_a_shared_thread_id():
    state = {
        "emails": [
            {"subject": "Visit", "threadId": "shared", "timestamp": "2026-09-04"},
            {"subject": "Re: Visit", "threadId": "shared", "timestamp": "2026-09-03"},
        ]
    }
    assert len(world_check.check_chronology("mail", state, "2026-09-08")) == 1
    state["emails"][0]["threadId"] = "other"
    assert world_check.check_chronology("mail", state, "2026-09-08") == []


@pytest.mark.parametrize(
    "extra",
    [
        {"inReplyTo": "missing"},
        {"inReplyTo": "reply"},
        {"threadId": "shared"},
    ],
)
def test_chronology_skips_unresolved_self_and_shared_thread_links(extra):
    state = {
        "emails": [
            {"id": "parent", "timestamp": "2026-09-04", "threadId": "shared"},
            {"id": "reply", "timestamp": "2026-09-03", **extra},
        ]
    }
    assert world_check.check_chronology("mail", state, "2026-09-08") == []


def test_chronology_skips_ambiguous_subjects_and_parent_ids():
    original = {"id": "parent", "subject": "Visit", "timestamp": "2026-09-04"}
    for link in ({"subject": "Re: Visit"}, {"inReplyTo": "parent"}):
        state = {"emails": [original, original, {"id": "reply", "timestamp": "2026-09-03", **link}]}
        assert world_check.check_chronology("mail", state, "2026-09-08") == []


def test_chronology_skips_conflicting_parent_links_and_other_collections():
    original = {"id": "original", "subject": "Visit", "timestamp": "2026-09-04"}
    reply = {"id": "reply", "subject": "Re: Visit", "timestamp": "2026-09-03"}
    assert world_check.check_chronology("mail", {"emails": [original], "drafts": [reply]}, "2026-09-08") == []
    reply.update(inReplyTo="original", parentId="other")
    state = {"emails": [original, {**original, "id": "other"}, reply]}
    assert world_check.check_chronology("mail", state, "2026-09-08") == []


def test_chronology_uses_offsets_to_choose_latest_update_and_date_replies():
    doc = {
        "createdAt": "2026-09-03T23:00:00-02:00",
        "updatedAt": "2026-09-04T00:00:00+02:00",
        "body": "Completed September 4, 2026.",
    }
    findings = world_check.check_chronology("docs", doc, "2026-09-08")
    assert len(findings) == 1
    assert "after createdAt 2026-09-03T23:00:00-02:00" in findings[0]["message"]
    state = {
        "emails": [
            {"id": "parent", "sentAt": "2026-09-03T09:00:00Z"},
            {"id": "reply", "inReplyTo": "parent", "sentAt": "2026-09-03T10:00:00+02:00"},
        ]
    }
    assert len(world_check.check_chronology("mail", state, "2026-09-08")) == 1


@pytest.mark.parametrize(
    "reply_time, parent_time",
    [
        ("2026-09-03T10:00:00+02:00", "2026-09-03T08:00:00Z"),
        ("2026-09-03", "2026-09-03T10:00:00Z"),
        ("2026-09-03T10:00:00", "2026-09-03T09:00:00Z"),
    ],
)
def test_chronology_compares_reply_instants_and_respects_date_precision(reply_time, parent_time):
    state = {
        "emails": [
            {"id": "parent", "timestamp": parent_time, "modifiedAt": "2026-09-05"},
            {"id": "reply", "inReplyTo": "parent", "timestamp": reply_time},
        ]
    }
    assert world_check.check_chronology("mail", state, "2026-09-08") == []


def test_world_runs_both_gates_for_world_and_every_app_without_cutoff_duplicates():
    bad = {
        "invoices": [{"id": "bill", "total": 99, "items": [{"amount": 1}]}],
        "documents": [{"id": "doc", "body": "Completed 2026-09-04.", "timestamp": "2026-09-03"}],
    }
    report = world_check.check_world(bad, {"a": bad, "b": bad}, {}, [], "2026-09-08")
    errors = [f for f in report["findings"] if f["severity"] == "error"]
    leads = [f for f in report["findings"] if f["severity"] == "warning" and "cites" in f["message"]]
    assert len(errors) == 3 and len(leads) == 3  # arithmetic blocks; a later prose date is a lead
    assert {f["source"] for f in errors} == {"world", "a", "b"}
    future = {"documents": [{"timestamp": "2026-09-10", "body": "Completed 2026-09-09."}]}
    report = world_check.check_world({}, {"a": future}, {}, [], "2026-09-08")
    assert report["errors"] == 1
    assert "after the reference date" in report["findings"][0]["message"]


WORKERS = [{"id": "w1", "title": "Dispatcher"}, {"id": "w2", "title": "Accountant"}]
WORLD = {
    "leases": [
        {"id": "L1", "monthly_rate": 60, "status": "active"},
        {"id": "L2", "monthly_rate": 90, "status": "active"},
    ],
    "history": [{"id": "h1", "at": "2026-08-01T10:00:00Z"}],
}


@pytest.mark.parametrize("key", ["users", "userDirectory", "teamMembers", "directory"])
@pytest.mark.parametrize("mapping", [False, True])
def test_alternate_user_directory_requires_every_worker(key, mapping):
    ann = {"id": "u1", "name": "Ann", "email": "ann@example.test"}
    directory = {"u1": ann} if mapping else [ann]
    state = {"user": ann, key: directory}
    identities = {"w1": ann, "w2": {"id": "u2", "name": "Bob", "email": "bob@example.test"}}
    assert world_check.has_user_collection(state)
    findings = world_check.check_state_identities("app", state, identities)
    assert any("w2" in row["message"] for row in findings)
    assert not any("w1" in row["message"] for row in findings)


@pytest.mark.parametrize("directory", [[], {}])
def test_empty_user_directory_is_not_a_single_account_app(directory):
    ann = {"id": "u1", "name": "Ann"}
    assert world_check.check_state_identities("app", {"user": ann, "users": directory}, {"w1": ann})


def test_clean_world_passes():
    states = {
        "tickets_app": {
            "tickets": [{"id": 1, "requester_id": 7, "created_at": "2026-09-01T00:00:00Z"}],
            "users": [{"id": 7, "name": "Ann Reyes", "email": "ann.reyes@acme.test"}],
            "leases": [{"id": "L1", "monthly_rate": "$60", "status": "Active"}],
        }
    }
    identities = {
        "w1": {
            "tickets_app": {
                "id": 7,
                "name": "Ann Reyes",
                "email": "ann.reyes@acme.test",
                "title": "Dispatcher",
            }
        }
    }
    result = world_check.check_world(WORLD, states, identities, WORKERS, "2026-09-08")
    assert result == {"ok": True, "errors": 0, "warnings": 0, "findings": []}


def test_fact_drift_between_world_and_app_is_an_error():
    states = {"a": {"leases": [{"id": "L1", "monthly_rate": 75, "status": "active"}]}}
    result = world_check.check_world(WORLD, states, {}, WORKERS, "2026-09-08")
    assert not result["ok"] and any("L1.monthly_rate: 60 vs 75" in f["message"] for f in result["findings"])


def test_fact_drift_between_two_apps_is_an_error_but_same_app_copies_are_not():
    states = {
        "a": {"leases": [{"id": "L2", "status": "active"}]},
        "b": {"rows": [{"id": "L2", "status": "ended"}]},
    }
    result = world_check.check_world(WORLD, states, {}, WORKERS, "2026-09-08")
    assert any(f["source"] == "a~b" and "L2.status" in f["message"] for f in result["findings"])


def test_duplicates_role_drift_future_dates_and_dangling_ids():
    states = {
        "a": {
            "users": [{"id": 1, "name": "Ann", "email": "a@x"}, {"id": 1, "name": "Ann B", "email": "a@x"}],
            "tickets": [
                {
                    "id": 5,
                    "requester_id": 99,
                    "created_at": "2026-09-09T00:00:00Z",
                    "due_at": "2026-10-01T00:00:00Z",
                }
            ],
        },
        "b": {"people": [{"id": 1, "name": "Anne", "email": "a@x"}]},
    }
    identities = {"w1": {"a": {"id": 1, "name": "Ann", "title": "Clerk"}, "b": {"id": 1, "name": "Anne"}}}
    result = world_check.check_world(WORLD, states, identities, WORKERS, "2026-09-08")
    messages = " | ".join(f["message"] for f in result["findings"])
    assert "id '1' appears 2 times" in messages
    assert "email 'a@x' repeated 2 times" in messages
    assert "different names across apps" in messages
    assert "differs from dossier 'Dispatcher'" in messages
    assert "2026-09-09T00:00:00Z is after the reference date" in messages
    assert "due_at" not in messages
    assert "1 of 1 requester_id values name no record in this app" in messages and "99" in messages
    assert not result["ok"]


def test_system_log_prose_is_flagged_but_human_prose_is_not():
    loggy = " ".join(
        ["ACC-F311-1 confirms TX-L310-2 and AP312-18 for REQ-H310-0907 per RC-1 and RV-1 rules."] * 6
    )
    human = " ".join(
        [
            "Hey Nora, can you fit a second stop on the 24th? The customer says the elevator is booked until noon."
        ]
        * 4
    )
    states = {"a": {"comments": [{"id": 1, "body": loggy}, {"id": 2, "body": human}]}}
    result = world_check.check_world(
        WORLD, states, {}, WORKERS, "2026-09-08", materials={"notes/x.md": human}
    )
    messages = [f["message"] for f in result["findings"]]
    assert any("reads like a system log" in m for m in messages) and result["ok"]
    only_human = world_check.check_plain_language("p", {"m": human})
    assert only_human == []


@pytest.mark.parametrize("rid", [1, "1", "shared-1"])
def test_facts_do_not_compare_unrelated_collections(rid):
    world = {"tickets": [{"id": rid, "status": "open"}]}
    states = {"a": {"users": [{"id": rid, "status": "active"}]}}
    assert world_check.check_facts(world, states) == []


def test_facts_compare_integer_and_string_ids_in_same_collection():
    findings = world_check.check_facts(
        {"tickets": [{"id": 1, "status": "open"}]},
        {"a": {"tickets": [{"id": "1", "status": "closed"}]}},
    )
    assert len(findings) == 1 and "1.status" in findings[0]["message"]


def test_facts_use_kind_for_generic_canonical_entities():
    world = {"entities": [{"id": "1", "kind": "ticket", "status": "open"}]}
    states = {"a": {"users": [{"id": 1, "status": "active"}], "tickets": [{"id": 1, "status": "closed"}]}}
    findings = world_check.check_facts(world, states)
    assert len(findings) == 1 and "/tickets/" in findings[0]["path"]


@pytest.mark.parametrize(
    "value, reference, future",
    [
        ("2026-09-09T00:30:00+02:00", "2026-09-08", False),
        ("2026-09-08T23:30:00-02:00", "2026-09-08", True),
        ("20260909T003000+0200", "2026-09-08", False),
        ("20260909T003000-0200", "2026-09-08", True),
        ("2026-09-08T15:01:00+02:00", "2026-09-08T13:00:00Z", True),
        ("2026-09-08T15:00:00+02:00", "2026-09-08T13:00:00Z", False),
    ],
)
def test_timestamps_compare_iso_instants_with_offsets(value, reference, future):
    findings = world_check.check_timestamps("a", {"created_at": value, "due_at": value}, reference)
    assert bool(findings) is future
    assert all(f["path"] == "/created_at" for f in findings)


@pytest.mark.parametrize("field", ["at", "timestamp", "createdAt", "lastLoginAt", "posted_at"])
def test_history_timestamp_fields_are_checked(field):
    assert world_check.check_timestamps(
        "world", {"history": [{field: "2026-09-09T12:00:00+00:00"}]}, "2026-09-08"
    )


def test_known_proper_names_are_not_system_log_codes():
    body = " ".join(["CORT works with IKEA and NASA to arrange a comfortable office for everyone."] * 5)
    states = {
        "a": {
            "organizations": [{"id": i, "name": name} for i, name in enumerate(["CORT", "IKEA", "NASA"])],
            "messages": [{"id": "m1", "body": body}],
        }
    }
    report = world_check.check_world({}, states, {}, [], "2026-09-08")
    assert report["findings"] == []


def test_world_check_reports_identity_drift_against_users_collection():
    identities = {"w1": {"a": {"id": "1", "name": "Ann", "email": "ann@example.test"}}}
    state = {"users": [{"id": 1, "name": "Anne", "email": "other@example.test"}]}
    report = world_check.check_world({}, {"a": state}, identities, [], "2026-09-08")
    assert not report["ok"]
    assert any("identity" in f["message"] for f in report["findings"])


def test_single_account_apps_do_not_require_a_users_collection():
    from company_envs.world.world_check import check_state_identities

    gmail_like = {
        "user": {"id": "u1", "name": "Ann"},
        "emails": [{"id": "m1", "subject": "hi"}],
        "labels": [],
    }
    identities = {"w1": {"id": "u1", "name": "Ann"}, "w2": {"id": "u2", "name": "Bob"}}
    assert check_state_identities("gmail_mock", gmail_like, identities) == []
    with_directory = {**gmail_like, "users": [{"id": "u1", "name": "Ann"}]}
    findings = check_state_identities("gmail_mock", with_directory, identities)
    assert any("w2" in f["message"] for f in findings)


def test_cross_company_identity_reuse_is_flagged(tmp_path):
    import json

    from company_envs.world.world_check import check_cross_company_identities

    for company, name, email in (
        ("a", "Dana Whitfield", "dana@a.example"),
        ("b", "Dana Whitfield", "dana@b.example"),
        ("c", "Eli Brooks", "eli@c.example"),
    ):
        (tmp_path / company / "world").mkdir(parents=True)
        (tmp_path / company / "world" / "identities.json").write_text(
            json.dumps({"w1": {"app": {"id": 1, "name": name, "email": email}}})
        )
    result = check_cross_company_identities(tmp_path)
    assert result["companies"] == 3 and not result["ok"]
    assert any(
        "Dana Whitfield" in f["message"] and "belongs to a" in f["message"] for f in result["findings"]
    )


def test_generator_rules_are_rejected_as_data():
    world = {
        "population": {
            "children": {
                "range": "i=1..600",
                "id": "C+pad(i,4)",
                "name": "firstNames[floor((i-1)/20)+1] + space + surnames[mod(i-1,20)+1]",
            }
        }
    }
    findings = world_check.check_literal_records("world", world)
    assert findings and all(f["severity"] == "error" for f in findings)
    assert "pad(i,4)" in " ".join(f["message"] for f in findings)
    assert (
        world_check.check_literal_records(
            "world", {"note": "Padded envelopes, range of dates, floor plan v2"}
        )
        == []
    )


def test_stub_emails_without_recipients_or_subjects_are_errors():
    emails = [{"id": f"e{i}", "from": {"name": "A"}, "subject": "x", "body": "Done."} for i in range(20)]
    findings = world_check.check_communications("gmail_mock", {"emails": emails})
    messages = " ".join(f["message"] for f in findings)
    assert "no to" in messages and "median body" in messages
    full = [
        {
            "id": f"e{i}",
            "from": {"name": "A", "email": "a@x.test"},
            "to": ["b@x.test"],
            "subject": f"Invoice {i}",
            "body": f"Hi Beth, invoice {i} came in above the quote we approved in March. Can you check with the vendor before Friday? Thanks, Ana",
        }
        for i in range(20)
    ]
    assert world_check.check_communications("gmail_mock", {"emails": full}) == []


def test_templated_message_openings_and_recurring_phrases_are_errors():
    emails = [
        {
            "id": f"e{i}",
            "to": ["x"],
            "subject": f"s{i}",
            "body": f"Just checking this file {i} again. The diary entry is still present for matter {i}.",
        }
        for i in range(30)
    ]
    findings = world_check.check_texture("gmail_mock", {"emails": emails})
    kinds = " ".join(f["message"] for f in findings)
    assert "open with" in kinds and "recurs" in kinds
    varied = [
        {
            "id": f"e{i}",
            "body": " ".join(chr(97 + (i * 7 + j * 3) % 26) * (2 + (i + j) % 5) for j in range(12)),
        }
        for i in range(30)
    ]
    assert world_check.check_texture("gmail_mock", {"emails": varied}) == []


def test_brief_register_gate_rejects_grader_language_and_accepts_plain_speech():
    from company_envs.workflows import brief_register, check_plain_english

    class W:
        id = "x_o1"
        title = "Answer a client's expense dispute"
        brief = (
            "Birch Instruments is disputing last year's expense charges. Work out which charges they can "
            "actually challenge, whether they can take the overcharge off October's rent, and what their "
            "2027 forecast should say. Get them a written answer by September 15."
        )

    assert check_plain_english(W())["register_terms"] == []
    W.brief = (
        "Using records available September 8, prepare an internally approved response, reconcile the "
        "governing lease and statements, and integrate consequential specialist work within the "
        "approved authority limits, retaining all evidence."
    )
    with pytest.raises(ValueError, match="grader's register"):
        check_plain_english(W())
    assert brief_register(W.brief)["register_terms"]


def test_seed_schema_keeps_state_tables_and_drops_api_and_examples():
    from company_envs.world.state_seed import seed_schema

    doc = (
        "# App\n\n## State Schema\n| key | type |\n| users | array |\n### Default User IDs\nuser_1\n"
        "## Routes\n/inbox\n## Minimal Inject Example\ncurl -X POST /post john@company.com\n"
        "## Observable State Changes (for LLM evaluation)\nwatch diff\n"
        "## Default IDs\nsee https://cua-gym-google-drive.xlang.ai?itemId=1 and c1\n"
    )
    kept = seed_schema(doc)
    assert "## State Schema" in kept and "## Default IDs" in kept
    assert "Default User IDs" not in kept and "user_1" not in kept  # sample values, not shape
    assert "Routes" not in kept and "curl" not in kept and "evaluation" not in kept
    assert "xlang" not in kept and "<link>" in kept


def test_leaks_from_the_prompt_are_errors():
    state = {
        "emails": [
            {
                "body": "See https://cua-gym-google-drive.xlang.ai?itemId=doc-1 for the synthetic rate card",
                "from": {"email": "ops@company.com"},
                "subject": "Seeded placeholder",
            }
        ]
    }
    kinds = {f["message"].split(":")[0] for f in world_check.check_leaks("gmail_mock", state)}
    assert {
        "benchmark platform reference",
        "placeholder domain",
        "pipeline vocabulary in company text",
    } <= kinds
    clean = {
        "emails": [
            {"body": "Hi Jo, the July statement is attached.", "from": {"email": "jo@harborview-rentals.com"}}
        ]
    }
    assert world_check.check_leaks("gmail_mock", clean) == []


@pytest.mark.parametrize(
    "body",
    [
        "We still don't have vest sizes. The packer used Black XL as the placeholder for everyone.",
        "Replace that placeholder with the approved decorated image before offering it for purchase.",
        "I have an undecorated Natural tote left for evaluation, but haven't received the contents.",
    ],
)
def test_business_design_language_is_not_pipeline_leakage(body):
    assert not world_check.check_leaks("gmail_mock", {"body": body})
    # A benign use must never short-circuit a later actual leak.
    assert world_check.check_leaks(
        "gmail_mock", {"body": body + " These are seeded records for RL evaluation."}
    )


@pytest.mark.parametrize(
    "body", ["Fill in placeholder data.", "Task for evaluation.", "This is a mock company."]
)
def test_actual_pipeline_language_still_fails(body):
    assert world_check.check_leaks("gmail_mock", {"body": body})


def test_a_grouping_key_is_one_finding_not_one_per_record():
    """The reference check was the largest output of the mechanical pass and almost none was real.

    170,408 of 185,716 warnings across 60 worlds came from here; about 396 were genuinely dangling.
    threadId alone was 133,605, because Gmail groups by a thread key and has no threads collection,
    exactly as the real API does. The noise filled the reviewer's packet in list order, so 18 worlds
    had a genuine error pushed out and 8 saw none of their cross-app fact conflicts.
    """
    grouped = {
        "emails": [{"id": f"m{i}", "threadId": f"t{i // 3}", "subject": "x"} for i in range(30)],
    }
    found = world_check.check_references("gmail_mock", grouped)
    assert len(found) == 1, "one finding for the convention, not one per record"
    assert "no threadId resolves" in found[0]["message"] and "30 values" in found[0]["message"]

    # A record's own primary key resolves whatever it is called: Salesforce names it accountId.
    salesforce = {"accounts": [{"accountId": "a1", "ownerId": "a1"}]}
    assert world_check.check_references("salesforce_mock", salesforce) == []

    # A handful of broken references is not a convention: reported with its count and a citation
    # the repair can be narrowed to.
    broken = {
        "users": [{"id": "u1"}],
        "tickets": [{"id": "t1", "userId": "u1"}, {"id": "t2", "userId": "gone"}],
    }
    dangling = world_check.check_references("zendesk_mock", broken)
    assert len(dangling) == 1
    assert "1 of 2 userId values name no record" in dangling[0]["message"]
    assert "'gone' at /tickets/1/userId" in dangling[0]["message"]


def test_the_company_never_explains_the_harness_to_the_person_using_it():
    """44 desktop files across 15 companies described the instrumentation to the worker.

    They defined computer-use automation, virtual machines and bash, printed the worker's internal
    id, and one documented the applications' HTTP state route outright. A company does not explain
    its own instrumentation, and this is how agents learned to read the apps over HTTP instead of
    opening them: 85% of the shell commands in the delivered run went to that endpoint.
    """
    leaked = {
        "instructions": (
            "Computer-use automation (CUA) means operating the desktop through its visible "
            "controls. A virtual machine (VM) is your separate personal computer environment."
        ),
        "readme": (
            "The documented read-only state route is GET /state?sid=<the session identifier>. "
            "Sheets returns stored_state.sheets and Drive returns stored_state.items."
        ),
        "desk": "Worker ID: investigator. Reference clock: September 7, 2026, 09:00.",
    }
    findings = world_check.check_leaks("materials", leaked)
    assert findings, "harness vocabulary must be reported"
    assert all(f["severity"] == "error" for f in findings)
    assert {f["message"].split(":")[0] for f in findings} == {"harness vocabulary in company text"}

    # The words themselves are fine in their ordinary senses; only the harness meanings are not.
    ordinary = {
        "note": "The delivery van is a virtual machine only in the sense Rosalind jokes about it.",
        "shift": "State the reason on the form before you post it, and keep the receipt.",
    }
    assert world_check.check_leaks("materials", ordinary) == []


def test_company_view_drops_research_provenance():
    from company_envs.world.state_seed import company_view

    company = {
        "name": "Beacon",
        "evidence": [{"kind": "synthetic"}],
        "workers": [
            {"id": "w1", "title": "Manager", "rationale": "synthetic scale assumption", "basis": "inferred"}
        ],
        "facts": [{"text": "Two offices", "evidence_ids": ["e1"]}],
    }
    view = company_view(company)
    assert (
        "evidence" not in view
        and view["workers"] == [{"id": "w1", "title": "Manager"}]
        and view["facts"] == [{"text": "Two offices"}]
    )


def test_fact_drift_ignores_prose_wording_but_not_scalars():
    world = {
        "documents": [
            {
                "id": "doc1",
                "title": "Rate card",
                "owner": "Ana",
                "content": "Rates effective July 1. Long prose here.",
            }
        ]
    }
    state = {
        "documents": {
            "doc1": {
                "id": "doc1",
                "title": "Rate card",
                "owner": "Bo",
                "content": "Rates effective July 1, reworded a little in the app copy.",
            }
        }
    }
    messages = [f["message"] for f in world_check.check_facts(world, {"google_docs_mock": state})]
    assert any("doc1.owner" in m for m in messages) and not any("content" in m for m in messages)


def test_blank_email_addresses_on_message_parties_are_errors():
    emails = [
        {
            "id": f"e{i}",
            "from": {"name": "Lucia", "email": ""},
            "to": [{"name": "Omar", "email": "omar@hh.test"}],
            "subject": f"Visit {i}",
            "body": "Hi Omar, can you cover the Tuesday visit? The family asked for the afternoon. Thanks, Lucia",
        }
        for i in range(12)
    ]
    messages = " ".join(
        f["message"] for f in world_check.check_communications("gmail_mock", {"emails": emails})
    )
    assert "no email address" in messages


def test_placeholder_documents_are_errors():
    docs = {
        f"d{i}": {"id": f"d{i}", "title": f"Policy {i}", "content": "<h1>Policy</h1><p>Effective July 1.</p>"}
        for i in range(8)
    }
    assert any(
        "documents have fewer" in f["message"]
        for f in world_check.check_documents("google_docs_mock", {"documents": docs})
    )
    full = {
        f"d{i}": {
            "id": f"d{i}",
            "title": f"Policy {i}",
            "content": "<p>" + "Real policy text about rates, approvals and exceptions. " * 8 + "</p>",
        }
        for i in range(8)
    }
    assert world_check.check_documents("google_docs_mock", {"documents": full}) == []


def test_relative_time_in_a_brief_is_rejected():
    from company_envs.workflows import check_plain_english

    class W:
        id = "x_o2"
        title = "Confirm the venue booking"
        deliverables = ()
        brief = "Harbor Events wants the June 12, 2026 booking confirmed. Check the room and write back by Friday."

    with pytest.raises(ValueError, match="relative time"):
        check_plain_english(W())
    W.brief = "Harbor Events wants the June 12, 2026 booking confirmed. Check the room and write back by June 5, 2026."
    assert check_plain_english(W())["register_terms"] == []


def test_directory_entries_need_full_names_and_emails():
    state = {
        "users": [
            {"id": "u1", "name": "Damon", "email": ""},
            {"id": "u2", "fullName": "Helen Reed", "email": "helen.reed@acme.test"},
        ]
    }
    findings = world_check.check_directory("google_docs_mock", state)
    assert len(findings) == 1 and "1 of 2" in findings[0]["message"] and "u1" in findings[0]["message"]
    assert (
        world_check.check_directory(
            "x", {"users": [{"id": "u2", "fullName": "Helen Reed", "email": "helen.reed@acme.test"}]}
        )
        == []
    )


def test_gates_accept_real_app_shapes():
    # ServiceNow-style directory: first and last name, no fullName field.
    servicenow = {
        "users": [
            {
                "sys_id": f"u{i}",
                "user_name": f"u{i}",
                "first_name": "Ana",
                "last_name": f"Reyes{i}",
                "email": f"ana{i}@acme.test",
            }
            for i in range(5)
        ]
    }
    assert world_check.check_directory("ServiceNow_mock", servicenow) == []
    # A clinical in-basket addresses people by id and name, never by email.
    epic = {
        "messages": [
            {
                "id": f"m{i}",
                "from": {"id": "p1", "name": "Dr. Lee", "type": "provider"},
                "to": [{"id": "n1", "name": "Nurse Kim", "type": "provider"}],
                "subject": f"Patient {i}",
                "body": "Please review the medication list before the visit. The family asked about the dose change.",
            }
            for i in range(6)
        ]
    }
    assert world_check.check_communications("epic-health_mock", epic) == []
    # A chart of accounts is not a directory.
    assert (
        world_check.check_directory("quickbooks_mock", {"accounts": [{"id": "1000", "name": "Checking"}] * 5})
        == []
    )
    # Spreadsheet formulas are not generator rules.
    assert (
        world_check.check_literal_records(
            "google_sheets_mock", {"cells": {"A1": "=MOD(ROW(),2)=0", "B2": "=FLOOR(B2/7,1)"}}
        )
        == []
    )
    assert world_check.check_literal_records("world", {"note": "firstNames[floor((i-1)/20)+1]"})
    # Bulk records are excluded by whatever identifier they carry.
    msgs = {
        "messages": {
            "general": [
                {"messageId": f"b{i}", "content": "Reminder: weekly cost review at 10."} for i in range(30)
            ]
        }
    }
    assert (
        world_check.check_texture("slack_mock", msgs, exclude_ids=frozenset(f"b{i}" for i in range(30))) == []
    )
    # A shared signature is not templating.
    bodies = [
        "Jo, quick one: the audit line for the March invoice differs from the ledger by a few dollars.",
        "Following up on the Harbor Supply credit; their statement shows it applied, ours does not.",
        "Could you look at the export from Friday? Two rows carry the wrong cost center.",
        "Small thing about the rate card: the July version is still pinned in the shared folder.",
        "Regarding the vendor onboarding form, legal asked for the signed copy, not the draft.",
        "Heads up on the parking invoice; the total includes a month we already paid.",
        "When you have a minute, the Q2 forecast tab has a broken reference in the total row.",
        "Not urgent, but the scanner service contract renews next week and nobody owns it.",
        "Flagging that the Birch statement went out with last year's address block.",
        "One more: the reimbursement for the site visit is missing the mileage line.",
    ]
    emails = {
        "emails": [
            {
                "id": f"e{i}",
                "body": f"{bodies[i % 10]} (item {i})\n\nBest,\nAna Ruiz\nMercy Housing Management Group",
            }
            for i in range(30)
        ]
    }
    assert world_check.check_texture("gmail_mock", emails) == []


def test_child_record_ids_are_scoped_to_their_parent_record():
    from company_envs.world.world_check import check_facts

    world = {
        "space_schedules": [
            {"id": "inventory-placement", "spaces": [{"id": "K1", "occupant": "pl-mesquite"}]},
            {"id": "inventory-renewal", "spaces": [{"id": "K1", "occupant": "rn-mesquite"}]},
        ],
        "tenants": [{"id": "t-1", "status": "active"}],
    }
    assert check_facts(world, {}) == []
    # Top-level records sharing an id must still agree.
    states = {"crm_mock": {"tenants": [{"id": "t-1", "status": "closed"}]}}
    assert [f["severity"] for f in check_facts(world, states)] == ["error"]


def test_one_senders_signature_line_is_not_templated_text():
    from company_envs.world.world_check import check_texture

    def mail(i, sender, body):
        return {"id": f"m{i}", "from": sender, "subject": f"Update {i}", "body": body}

    bodies = [
        "Roof drainage on the east wing needs a contractor visit before the rains.",
        "Lease renewal terms for the bakery are with legal until Thursday.",
        "Signage permits came back approved; installation is scheduled next week.",
        "Parking lot restriping starts Monday night and ends by six in the morning.",
        "The vendor invoice for landscaping is thirty days overdue and disputed.",
        "Lighting in the north garage flickers; electricians quoted two options.",
        "A tenant reported a leak above the food court seating area yesterday.",
        "Budget variance this month comes from snow removal and one elevator repair.",
        "The audit team wants the security logs exported by Friday afternoon.",
        "Holiday hours were posted, and the kiosk operators acknowledged them.",
    ]
    varied = [bodies[i % 10] for i in range(40)]
    signature = [
        mail(i, "dana@mesa.example", f"{varied[i]} Dana Ortiz Manager Mesa Crossing Center")
        for i in range(30)
    ]
    others = [mail(30 + i, f"p{i}@mesa.example", varied[30 + i]) for i in range(10)]
    assert [
        f for f in check_texture("gmail_mock", {"emails": signature + others}) if f["severity"] == "error"
    ] == []
    templated = [
        mail(i, f"p{i}@mesa.example", f"{varied[i]} Please check your worklist before Friday")
        for i in range(40)
    ]
    assert any("worklist" in f["message"] for f in check_texture("gmail_mock", {"emails": templated}))


def test_marketing_profiles_are_not_the_workers_directory():
    """lkq-corporation: Klaviyo's account login plus customer profiles (ids and emails) was read as a
    directory every worker was missing from; a table no worker is in proves no directory."""

    def account(worker, name):
        return {
            "id": f"account-{worker}",
            "companyName": "Crossridge Parts",
            "user": {"name": name, "email": f"{name.split()[0].lower()}@crossridge.example"},
        }

    identities = {"w1": account("w1", "Avery Haldane"), "w2": account("w2", "Beatriz Montalvo")}
    state = {
        "account": identities["w2"],
        "profiles": [{"id": "A001", "email": "a001@shopmail.example", "firstName": "", "lastName": ""}],
        "campaigns": [{"id": "c1", "name": "Spring offers"}],
    }
    assert not world_check.has_user_collection(state, identities.values())
    assert world_check.check_state_identities("klaviyo_mock", state, identities) == []


def test_cloudflare_workers_are_scripts_not_people():
    """morning-brew: a ``workers`` collection of edge scripts (empty or not) is not a staff directory
    and its entries owe nobody a full name or an email."""
    ann = {"name": "Ann Reyes", "email": "ann@daybreak.example", "avatar": None}
    bob = {"name": "Bob Lin", "email": "bob@daybreak.example", "avatar": None}
    identities = {"w1": ann, "w2": bob}
    scripts = [{"id": "wk-router", "name": "edge-router", "routes": ["ledger.example/*"]}]
    for workers in ([], scripts):
        state = {
            "account": {"id": "cf-1", "email": "domains@daybreak.example"},
            "workers": workers,
            "currentUser": ann,
        }
        assert not world_check.has_user_collection(state, identities.values())
        assert world_check.check_state_identities("cloudflare_mock", state, identities) == []
        assert world_check.check_directory("cloudflare_mock", state) == []


def test_custom_named_directory_is_recognized_by_the_worker_in_it():
    ann = {"id": "u1", "name": "Ann Reyes", "email": "ann@example.test"}
    bob = {"id": "u2", "name": "Bob Lin", "email": "bob@example.test"}
    identities = {"w1": ann, "w2": bob}
    with_ann = {"user": ann, "collaborators": [ann, {"id": "u9", "name": "Guest", "email": "g@example.test"}]}
    findings = world_check.check_state_identities("airtable_mock", with_ann, identities)
    assert [f for f in findings if "w2" in f["message"] and "not in any state collection" in f["message"]]
    assert not [f for f in findings if "w1" in f["message"]]
    contacts_only = {
        "user": ann,
        "contacts": [{"id": "c1", "name": "Customer One", "email": "c1@buyer.test"}],
    }
    assert world_check.check_state_identities("paypal_mock", contacts_only, identities) == []


def test_an_em_dash_stamp_is_a_generator_but_a_hand_written_one_is_not():
    """77.9% of Gmail subjects and 52.5% of Drive filenames across 60 worlds carried an em dash.

    The dominant shape was one skeleton per template: 2,080 subjects read "Time-entry reminder —
    2026-05-14" and 2,280 Drive files "Contract directory — AC-118 — 2026-03-02 — 0044.txt".
    Banning the character outright would be wrong -- one calendar in the fleet holds 322
    hand-written em-dash titles, all different -- so the rule is the repeat, not the dash. The
    largest hand-written repeat on disk is 11 and the smallest stamp 50.
    """
    stamped = {
        "emails": [{"id": f"e{i}", "subject": f"Time-entry reminder — 2026-05-{i:02d}"} for i in range(1, 26)]
    }
    findings = world_check.check_machine_tells("gmail_mock", stamped)
    assert len(findings) == 1 and findings[0]["path"] == "/emails"
    assert "25 records are titled 'time-entry reminder — #-#-#'" in findings[0]["message"]

    # The same 25 subjects a person would have typed, em dashes and all.
    typed = [
        "Invoice 4478 — August caption work",
        "Museum ramp caption — which entrance?",
        "Weekly trading review — holiday weekend",
        "Labor Day — management offices closed",
        "Annual packet appointment — H327",
    ]
    written = {"emails": [{"id": f"e{i}", "subject": f"{typed[i % 5]} ({i})"} for i in range(25)]}
    assert world_check.check_machine_tells("gmail_mock", written) == []


def test_explicit_automation_can_repeat_subjects_but_humans_cannot():
    rows = [
        {
            "id": f"e{i}",
            "subject": f"Invoice copy — INV-{i:04d}",
            "from": {"name": "Order services notifications"},
        }
        for i in range(25)
    ]
    assert not world_check.check_machine_tells("gmail_mock", {"emails": rows})
    for row in rows:
        row["from"]["name"] = "Imani Brooks"
    assert world_check.check_machine_tells("gmail_mock", {"emails": rows})


def test_a_record_never_denies_what_it_means_and_ordinary_prose_still_negates():
    """60,025 records across 60 companies carried a sentence hedging about themselves.

    "This reminder carries no new case assignments", "A readable file does not establish a signed
    return", "This scheduled message does not confirm printing" -- 283 of 1,842 bulk specs stamp
    one on every record they expand, and one message alone reached 2,950 copies. Legal, clinical
    and editorial writing genuinely negates, so a single sentence is never the finding: 313 such
    sentences on disk occur exactly once and are ordinary prose. The repeat is the finding.
    """
    hedged = {
        "emails": [
            {
                "id": f"e{i}",
                "body": f"Time entries for week {i} are open. This reminder carries no new case assignments.",
            }
            for i in range(25)
        ]
    }
    findings = world_check.check_machine_tells("clio_mock", hedged)
    assert len(findings) == 1
    assert "25 records repeat 'this reminder carries no new case assignments.'" in findings[0]["message"]

    # Negation about the world, in the registers that really use it, repeated as often.
    lawyerly = [
        "Neither proposal is accepted, and the March invoice was never delivered.",
        "The tenant has not paid August rent and the notice period has not expired.",
        "We do not accept the vendor's position that the charge is contractual.",
        "The patient does not report chest pain and denies shortness of breath.",
        "Under section 4 the landlord shall not withhold consent unreasonably.",
    ]
    plain = {"emails": [{"id": f"e{i}", "body": f"{lawyerly[i % 5]} Item {i}."} for i in range(25)]}
    assert world_check.check_machine_tells("clio_mock", plain) == []

    # A disclaimer under a sign-off is a footer, which repeats by nature; ``check_texture``
    # skips those for the same reason.
    footer = {
        "emails": [
            {
                "id": f"e{i}",
                "body": f"The {i}th survey is booked.\n\nRegards,\nAna\nThis message does not "
                "constitute legal advice.",
            }
            for i in range(25)
        ]
    }
    assert world_check.check_machine_tells("clio_mock", footer) == []


def test_a_field_the_app_renders_is_not_restated_in_the_record_s_own_prose():
    """14,966 of 17,463 calendar descriptions on disk (85.7%) ended "Organizer: <name>".

    The app draws the organizer, the guests and the location from the record, so the description
    was writing the form's own labels back into the form. A quoted mail header inside a document
    ("From: ... Subject: ...") is a real thing people paste, and reaches 2.5% of a collection at
    most; the stamps start at 33%, so the rule is a share.
    """
    restated = {
        "events": [
            {
                "id": f"ev{i}",
                "title": f"Site walk {i}",
                "location": "North yard",
                "description": "Walk the north yard with the contractor. Organizer: Nerys Quill.",
            }
            for i in range(25)
        ]
    }
    findings = world_check.check_machine_tells("google_calendar_mock", restated)
    assert len(findings) == 1 and "25 of 25 records restate in prose" in findings[0]["message"]

    # The same events with the description saying what happens and the fields holding the rest.
    clean = {
        "events": [
            {
                "id": f"ev{i}",
                "title": f"Site walk {i}",
                "location": "North yard",
                "description": "Bring the punch list; the contractor wants the gate open by eight.",
            }
            for i in range(25)
        ]
    }
    assert world_check.check_machine_tells("google_calendar_mock", clean) == []

    # One pasted mail header among twenty-five documents is a person, not a template.
    quoted = {
        "documents": [
            {
                "id": f"d{i}",
                "content": "Intake check for the sensor bracket claim."
                + (" From: Celia Wexham  To: Arun Vaidyan" if i == 0 else f" Note {i}."),
            }
            for i in range(25)
        ]
    }
    assert world_check.check_machine_tells("clio_mock", quoted) == []


def test_mail_that_names_nobody_who_works_here_reaches_no_mailbox():
    """104,186 of 122,010 inbox emails (85.4%) named the mailbox owner in neither ``to`` nor ``cc``.

    Every worker opens the same store filtered to the records naming them
    (``hub_identity.visible_to``), so 27,042 records across the fleet -- 16.5% of everything with
    a party field, 19,140 of them expanded from bulk templates addressed to one fixed outside
    address -- are bytes no worker can open. Records with no party field at all (a public
    holiday) stay shared and are not judged.
    """
    me = {"id": "u1", "name": "Jo Park", "email": "jo@harborview.test"}
    unseen = {
        "user": me,
        "emails": [
            {
                "id": f"e{i}",
                "from": {"email": "billing@birch.test"},
                "to": [{"email": "accounts@birch.test"}],
                "subject": f"Statement {i}",
            }
            for i in range(25)
        ],
    }
    findings = world_check.check_addressing("gmail_mock", unseen, [me])
    assert len(findings) == 1 and findings[0]["path"] == "/emails"
    assert "25 of 25 records name nobody who works here" in findings[0]["message"]

    delivered = {
        "user": me,
        "emails": [
            {
                "id": f"e{i}",
                "from": {"email": "billing@birch.test"},
                "to": [{"email": "jo@harborview.test"}],
                "subject": f"Statement {i}",
            }
            for i in range(25)
        ],
    }
    assert world_check.check_addressing("gmail_mock", delivered, [me]) == []

    # A holiday nobody is invited to has no party field and is shared with everyone.
    shared = {"user": me, "events": [{"id": f"ev{i}", "title": "Office closed"} for i in range(25)]}
    assert world_check.check_addressing("google_calendar_mock", shared, [me]) == []


def test_the_tells_block_a_world_being_written_and_only_warn_once_the_bulk_is_laid():
    """``world_repair`` leaves "errors that appear only with the bulk records" unrepaired.

    An error there would stop a world nothing can fix: the repair path goes to the app author,
    who did not write the bulk. While the world is being authored the same collection can be
    rewritten, so the rules are errors under ``authoring`` and warnings after.
    """
    me = {"id": "u1", "name": "Jo Park", "email": "jo@harborview.test"}
    state = {
        "user": me,
        "emails": [
            {
                "id": f"e{i}",
                "from": {"email": "billing@birch.test"},
                "to": [{"email": "accounts@birch.test"}],
                "subject": f"Statement reminder — 2026-05-{i:02d}",
                "body": "Balance carried forward. This reminder carries no new case assignments.",
            }
            for i in range(1, 26)
        ],
    }
    args = ({"gmail_mock": state}, {"w1": {"gmail_mock": me}}, [{"id": "w1", "title": "Manager"}])
    laid = world_check.check_world({}, *args, "2026-09-08")
    authored = world_check.check_world({}, *args, "2026-09-08", authoring=True)
    tells = {"name nobody who works here", "records are titled", "records repeat"}

    def kinds(report, severity):
        return {
            t for f in report["findings"] if f["severity"] == severity for t in tells if t in f["message"]
        }

    assert kinds(laid, "warning") == tells and kinds(laid, "error") == set()
    assert kinds(authored, "error") == tells and kinds(authored, "warning") == set()


def test_a_seed_that_renames_the_fields_the_app_reads_is_reported():
    """validate_state checks top-level keys only, so a renamed field inside a collection passes.

    Measured across 132 seeded worlds: meta_ads' creatives carried `cta`/`mediaUrl`/`adId` where
    the app reads `callToAction`/`mediaItems`/`name`; salesforce's accounts carried no billing
    fields at all; gmail's emails never carried `snippet`, in 59 worlds.
    """
    shapes = {"creatives": {"record_fields": ["id", "name", "callToAction", "mediaItems"]}}
    state = {
        "creatives": [
            {"id": "c1", "adId": "a1", "cta": "Shop now", "mediaUrl": "x"},
            {"id": "c2", "adId": "a2", "cta": "Learn more", "mediaUrl": "y"},
            {"id": "c3", "adId": "a3", "cta": "Sign up", "mediaUrl": "z"},
        ]
    }
    messages = [f["message"] for f in world_check.check_record_fields("meta_ads_mock", state, shapes)]
    assert any("adId" in m and "app's own records do not have" in m for m in messages)
    assert any("callToAction" in m and "every record" in m for m in messages)
    assert all(
        f["severity"] == "warning" for f in world_check.check_record_fields("meta_ads_mock", state, shapes)
    )


def test_a_field_the_schema_never_names_is_reported_against_the_schema_not_the_seed():
    """Two different defects wore one sentence, and only one of them is the author's.

    ``record_fields`` is captured from each app's own demo records, so it holds fields no schema
    document mentions -- and the author is asked for the fields the schema documents. Measured over
    the 406 seeded states on disk: 139 absent-field findings, 90 of them naming a field no schema
    document mentions. ``google_calendar_mock.calendars.textColor`` alone is 56 of the 60 worlds:
    the pinned Calendar object documents id, name, color, visible, userId and isDefault and never
    textColor, and a calendar served without it does not get it back from the app either, so the
    seed was reported for obeying its own instructions. Both halves are still reported, because
    meta_ads is the case that says why -- its schema names neither ``callToAction`` nor
    ``mediaItems`` and its app reads both -- but they no longer read as the same finding.
    """
    state = {"rows": [{"id": f"r{i}", "kept": i} for i in range(4)]}
    shapes = {"rows": {"record_fields": ["id", "kept", "documented", "undocumented"]}}
    original = world_check.schema_field_notes
    world_check.schema_field_notes = lambda app_id: (frozenset({"id", "kept", "documented"}), frozenset())
    try:
        messages = [f["message"] for f in world_check.check_record_fields("app_mock", state, shapes)]
    finally:
        world_check.schema_field_notes = original
    seed_side = [m for m in messages if "documented" in m and "schema-versus-app" not in m]
    schema_side = [m for m in messages if "schema-versus-app" in m]
    assert seed_side and "'documented'" in seed_side[0], messages
    assert schema_side and "'undocumented'" in schema_side[0], messages
    assert "'undocumented'" not in seed_side[0]


def test_an_unreadable_schema_document_does_not_silence_the_absent_field_rule():
    """With no document to read there is no list of documented fields, and the old rule stands.

    Otherwise an app whose schema went missing would quietly stop being checked at all, which is the
    failure mode where a gate reports nothing and reads as a pass.
    """
    state = {"rows": [{"id": f"r{i}"} for i in range(4)]}
    shapes = {"rows": {"record_fields": ["id", "missing"]}}
    original = world_check.schema_field_notes
    world_check.schema_field_notes = lambda app_id: (frozenset(), frozenset())
    try:
        messages = [f["message"] for f in world_check.check_record_fields("nosuch_mock", state, shapes)]
    finally:
        world_check.schema_field_notes = original
    assert any("missing" in m and "every record" in m for m in messages)
    assert not any("schema-versus-app" in m for m in messages)


def test_a_collection_with_nothing_to_compare_against_is_not_judged():
    """No contract exists where the app itself held no records, or the seed holds almost none."""
    assert (
        world_check.check_record_fields("app", {"items": [{"a": 1}] * 3}, {"items": {"record_fields": []}})
        == []
    )
    assert world_check.check_record_fields("app", {}, {"items": {"record_fields": ["a"]}}) == []
    # Two records is too few to tell a contract from a coincidence.
    thin = {"items": [{"invented": 1}, {"invented": 2}]}
    assert world_check.check_record_fields("app", thin, {"items": {"record_fields": ["real"]}}) == []


def test_a_collection_keyed_by_id_is_judged_like_a_list():
    shapes = {"users": {"record_fields": ["userId", "displayName"]}}
    state = {"users": {f"u{n}": {"id": f"u{n}", "name": f"Person {n}"} for n in range(4)}}
    messages = [f["message"] for f in world_check.check_record_fields("microsoft_teams_mock", state, shapes)]
    assert any("displayName" in m for m in messages)


def test_a_field_only_one_record_happens_to_carry_is_not_a_rename():
    shapes = {"rows": {"record_fields": ["id", "name"]}}
    state = {
        "rows": [{"id": "1", "name": "a"}, {"id": "2", "name": "b"}, {"id": "3", "name": "c", "note": "x"}]
    }
    assert world_check.check_record_fields("app", state, shapes) == []


def test_a_field_the_schema_told_the_author_to_write_is_not_invented():
    """The field catalogue was captured from each app's demo records, not from its code, so a field
    the pinned schema documents can be absent from it. Reporting those told 58 worlds they had
    invented google_calendar's isDefault, recurring, reminders, meetLink and status -- all five
    documented -- and told 59 worlds gmail's emails were missing `snippet`, which that schema calls
    auto-derived from the body. Both were the seed obeying its own instructions.
    """
    named, derived = world_check.schema_field_notes("gmail_mock")
    assert "snippet" in named
    assert "snippet" in derived, "the schema calls it auto-derived from body if omitted"
    calendar_named, _ = world_check.schema_field_notes("google_calendar_mock")
    assert {"isDefault", "recurring", "reminders", "meetLink", "status"} <= calendar_named
    # An app with no pinned schema contributes nothing either way.
    assert world_check.schema_field_notes("not_an_app_mock") == (frozenset(), frozenset())

    shapes = {"emails": {"record_fields": ["id", "subject", "snippet"]}}
    state = {"emails": [{"id": f"m{n}", "subject": "Quarter close"} for n in range(4)]}
    assert world_check.check_record_fields("gmail_mock", state, shapes) == []
    # A field neither the app nor its schema has is still reported.
    invented = {"emails": [{"id": f"m{n}", "subject": "x", "madeUp": 1} for n in range(4)]}
    assert any(
        "madeUp" in f["message"] for f in world_check.check_record_fields("gmail_mock", invented, shapes)
    )


def test_a_worker_missing_from_the_directory_is_spliced_in_not_raised_over():
    """The record is known in full, and raising threw away every other app the seed had authored.

    Measured: around ten companies are terminal on this with nothing on disk for hundreds of model
    calls each, and six of the sixty seeded worlds could not be served for the same reason. All six
    load after the splice.
    """
    identities = {"w1": {"id": "u1", "name": "Ada Iyer", "email": "ada@x.test"}}
    listed = {"users": [{"id": "u9", "name": "Someone Else"}], "tickets": []}
    assert world_check.people_directory(listed) == "users"
    assert world_check.splice_identities(listed, identities) == 1
    assert {"id": "u1", "name": "Ada Iyer", "email": "ada@x.test"} in listed["users"]
    assert world_check.check_state_identities("demo_mock", listed, identities) == []
    # Splicing twice adds nothing: the record is already there.
    assert world_check.splice_identities(listed, identities) == 0

    keyed = {"users": {"u9": {"id": "u9", "name": "Someone Else"}}}
    assert world_check.splice_identities(keyed, identities) == 1
    assert keyed["users"]["u1"]["name"] == "Ada Iyer"


def test_an_app_with_no_directory_and_one_keyed_on_something_else_are_left_alone():
    """A single-account app has nowhere to put a worker, and a directory keyed on a field the app
    reads instead of `id` must not be given a key the app cannot read -- the finding stands."""
    single = {"user": {"id": "u9", "name": "Someone Else"}, "emails": []}
    assert world_check.people_directory(single) is None
    assert world_check.splice_identities(single, {"w1": {"id": "u1", "name": "Ada Iyer"}}) == 0
    other_key = {"users": {"usr-9": {"userId": "usr-9", "name": "Someone Else"}}}
    assert world_check.splice_identities(other_key, {"w1": {"userId": "usr-1", "name": "Ada"}}) == 0


def test_a_field_the_user_record_does_not_have_is_not_a_mismatch():
    """zoom_web's contacts carry no username, so comparing against a missing one reported a
    mismatch no model could satisfy and retired the company after three identical deaths."""
    identities = {"w1": {"id": "u1", "name": "Ada Iyer", "username": "ada-iyer"}}
    state = {"users": [{"id": "u1", "name": "Ada Iyer"}]}  # no username field at all
    assert world_check.check_state_identities("zoom_web_mock", state, identities) == []
    # A field both sides carry is still compared.
    disagrees = {"users": [{"id": "u1", "name": "Ada Iyer", "username": "someone-else"}]}
    found = world_check.check_state_identities("zoom_web_mock", disagrees, identities)
    assert len(found) == 1 and "username" in found[0]["message"]


SLACK_SHAPE = {
    "users": [
        {"userId": "u-celia", "fullName": "Celia Verran", "email": "celia@commonline.test"},
        {"userId": "u-ivo", "fullName": "Ivo Serrat", "email": "ivo@commonline.test"},
    ],
    "channels": [{"channelId": "ch-desk", "name": "national-desk", "members": ["u-celia", "u-ivo"]}],
    "messages": {
        "ch-desk": [
            {"messageId": "msg-real", "senderId": "u-celia", "content": "Handover is on the doc."},
            {"messageId": "msg-reply", "senderId": "u-ivo", "threadId": "th-real", "content": "Seen."},
        ]
    },
    "threads": {
        "th-real": {"threadId": "th-real", "parentMessageId": "msg-real", "replies": ["msg-reply"]},
        "th-ghost": {"threadId": "th-ghost", "parentMessageId": "msg-gone", "replies": []},
    },
}


def test_a_records_own_primary_key_enters_the_id_universe_whatever_the_app_calls_it():
    """The universe was records carrying a literal ``id``, so the reference check was blind to slack.

    slack keys messages on ``messageId`` and channels on ``channelId``, so not one message entered
    the universe, every ``parentMessageId`` missed, and the "not one value resolves" branch
    collapsed the whole class into a single benign warning. Measured over the 60 seeded worlds:
    3,350 of 4,960 threads name a root message that does not exist, hiding 7,354 replies, and not
    one of them appeared in any CHECKS.json. In a live guest 61 of 85 threads render as a row with
    no sender and no body; ThreadPanel.jsx returns null when the parent message is missing.
    """
    ids = world_check.state_ids(SLACK_SHAPE)
    assert {"msg-real", "msg-reply", "ch-desk", "u-celia", "th-real"} <= ids
    assert world_check.primary_ids("messages", {"messageId": "m1", "senderId": "u-2"}) == {"m1"}

    found = world_check.check_references("slack_mock", SLACK_SHAPE)
    assert len(found) == 1, [f["message"] for f in found]
    assert "1 of 2 parentMessageId values name no record" in found[0]["message"]
    assert "'msg-gone'" in found[0]["message"] and "unreachable" in found[0]["message"]


def test_a_parent_that_does_not_exist_blocks_only_while_the_author_can_still_act():
    """A structural rule applied to a world already on disk is a revision nobody can make.

    19,768 of the 20,155 unreachable drive items are bulk records, which no author owns and
    world_repair leaves alone, and a world's repair budget is spent by the time the check stage
    runs. So the same finding is an error while seeding (``authoring=True``, where the author is
    still in the repair loop and the bulk is not laid yet) and a warning afterwards.
    """
    severities = {
        authoring: [
            f["severity"]
            for f in world_check.check_references(
                "slack_mock", SLACK_SHAPE, severity="error" if authoring else "warning"
            )
        ]
        for authoring in (True, False)
    }
    assert severities == {True: ["error"], False: ["warning"]}


def test_unreachable_children_are_reported_as_a_count_not_six_rows():
    """Structural unreachability was capped at six rows a field, so it could never weigh anything.

    Measured: 20,155 of 78,462 drive items sit under a ``parentId`` naming no item -- and it is not
    a deep-tree problem, it is 128 invented folder ids across 15 of 60 worlds (murdock-industrial
    2,529 of 2,573 items under 8 of them). Six rows said the same thing as a single typo, so the
    finding now carries the count and how many distinct targets are missing.
    """
    state = {
        "items": {
            "fld-real": {"id": "fld-real", "name": "Desk"},
            "doc-filed": {"id": "doc-filed", "name": "Handover", "parentId": "fld-real"},
            **{
                f"doc-{i}": {"id": f"doc-{i}", "name": f"Note {i}", "parentId": f"fld-invented-{i % 2}"}
                for i in range(20)
            },
        }
    }
    found = world_check.check_references("google_drive_mock", state)
    assert len(found) == 1, [f["message"] for f in found]
    assert "20 of 21 parentId values name no record" in found[0]["message"]
    assert "2 missing targets" in found[0]["message"]


def test_a_field_holding_a_list_of_bare_ids_is_resolved():
    """``check_references`` resolved only dict fields matching ``(_id|Id)$``, so a list was skipped.

    ``replies``, ``cardIds``, ``members`` and ``participants`` hold bare ids, and never resolving
    them is the other half of why 20,155 unreachable drive items and 6,240 of 6,794 containerless
    trello cards stayed invisible.
    """
    state = {
        "lists": [{"id": "ch-open", "title": "Open", "cardIds": ["card-1", "card-gone"]}],
        "cards": [{"id": "card-1", "title": "Ship the sample", "listId": "ch-open"}],
        "users": [{"id": "u1", "name": "Jo Park"}, {"id": "u2", "name": "Ada Vane"}],
        "channels": [{"id": "c1", "members": ["u1", "u2"]}],
    }
    found = world_check.check_references("trello_mock", state)
    assert [f["message"] for f in found if "cardIds" in f["message"]], "a list of ids is a reference each"
    assert "1 of 2 cardIds values name no record" in " | ".join(f["message"] for f in found)
    assert not [f for f in found if "members" in f["message"]], "members that resolve are not a finding"


def test_a_child_its_own_parent_does_not_hold_is_on_no_screen():
    """Trello draws a board from ``list.cardIds`` (List.jsx:59), not from the card's ``listId``.

    So a card that names its list while the list does not name it back is on no screen: 145 of 168
    cards at blastx-consulting, 152 of 218 at median-technologies, and monday the same for 282 of
    362 items at eastman-chemical-company. Only a two-sided link is judged -- a hubspot contact in
    no ``contactIds`` list carries no ``listId`` either, and is not reported.
    """
    state = {
        "lists": [{"id": "ch-open", "title": "Open", "cardIds": ["card-1"]}],
        "cards": [
            {"id": "card-1", "title": "Ship the sample", "listId": "ch-open"},
            {"id": "card-2", "title": "Chase the invoice", "listId": "ch-open"},
        ],
    }
    found = world_check.check_containment("trello_mock", state)
    assert len(found) == 1 and found[0]["severity"] == "warning"
    assert "1 of 2 cards name a list whose cardIds does not name them back" in found[0]["message"]
    assert "card-2" in found[0]["message"]
    assert world_check.check_containment("trello_mock", state, severity="error")[0]["severity"] == "error"

    # One side missing on both counts is a dangling reference, which check_references reports.
    contacts = {
        "lists": [{"id": "l1", "name": "Prospects", "contactIds": ["CT-1"]}],
        "contacts": [{"id": "CT-1", "email": "a@b.test"}, {"id": "CT-2", "email": "c@d.test"}],
    }
    assert world_check.check_containment("hubspot_mock", contacts) == []


def test_inline_records_belong_in_the_collection_the_app_renders():
    """Slack's thread replies were written as objects inside ``threads[].replies``.

    The app renders ``state.messages`` filtered by ``threadId`` (ThreadPanel.jsx:26) and its own
    threads hold reply *ids*; the seeded worlds declare 9,963 replies fleet-wide of which 11 exist
    as messages, so 4,082 threads in 59 of 60 worlds show a reply count in the list and nothing
    when opened. Judged only where the inline records carry an app-named key that is some
    collection's own primary key, so a card's checklists or a ticket's comments are untouched.
    """
    state = {
        "messages": {"ch-desk": [{"messageId": "msg-real", "content": "Handover is on the doc."}]},
        "threads": {
            "th-1": {
                "threadId": "th-1",
                "parentMessageId": "msg-real",
                "replies": [{"messageId": "msg-ghost", "senderId": "u-ivo", "content": "Seen."}],
            }
        },
    }
    found = [
        f for f in world_check.check_containment("slack_mock", state) if "not in messages" in f["message"]
    ]
    assert len(found) == 1
    assert "1 of 1 records in threads.replies are not in messages" in found[0]["message"]
    assert "msg-ghost" in found[0]["message"]

    # An inline list keyed the generic way names no collection and is ordinary authoring.
    inline = {"cards": [{"id": "c1", "checklists": [{"id": "k1", "title": "Pack"}]}]}
    assert world_check.check_containment("trello_mock", inline) == []


def test_a_stored_total_is_the_sum_of_the_records_it_totals():
    """``google_drive.storageUsed`` disagreed with its own files in 56 of 60 seeded worlds.

    29 declare less than the sum of the sizes they hold, which cannot happen -- one declares 0
    against 3.0 MB of files -- and 10 declare more than ten times it, the worst 184 MB of quota
    against 85 KB of files. A real quota may cover what this state does not hold (another product,
    the trash), so only a total under the sum or more than tenfold it is reported.
    """
    state = {
        "storageUsed": 0,
        "items": {"a": {"id": "a", "size": 2_000_000}, "b": {"id": "b", "size": 1_000_000}},
    }
    found = world_check.check_totals("google_drive_mock", state)
    assert len(found) == 1 and "storageUsed is 0" in found[0]["message"]
    assert "3,000,000" in found[0]["message"]
    assert world_check.check_totals("google_drive_mock", {**state, "storageUsed": 3_000_000}) == []
    # A quota a few times the files it holds is ordinary; a thousandfold is not the same drive.
    assert world_check.check_totals("google_drive_mock", {**state, "storageUsed": 9_000_000}) == []
    assert world_check.check_totals("google_drive_mock", {**state, "storageUsed": 600_000_000})


def test_a_record_may_only_name_a_worker_who_can_open_the_app():
    """Nothing checked that a worker acting inside an app holds a login for it.

    worker_apps.json is the grant the VM forwards on (hub_vm._plan), so a jira issue assigned to
    someone without jira is work its owner can never see. Measured over the seeded worlds: 2,054
    records across 18 company/app pairs, among them 218 jira records at greenbrier-companies
    (including the task's own manager), 216 salesforce records at federal-home-loan-bank-des-moines
    and 153 jira at seattle-police-department -- several in the app their company's own task keys
    on. It is the commonest blocking finding the reviewers raise by hand, one repair round each.
    """
    identities = {
        "desk-editor": {"jira_mock": {"id": "u-celia", "name": "Celia Verran", "email": "celia@x.test"}},
        "reporter": {"jira_mock": {"id": "u-ivo", "name": "Ivo Serrat", "email": "ivo@x.test"}},
    }
    state = {
        "currentUser": {"id": "u-celia"},
        "users": [
            {"id": "u-celia", "name": "Celia Verran", "email": "celia@x.test"},
            {"id": "u-ivo", "name": "Ivo Serrat", "email": "ivo@x.test"},
        ],
        "issues": [
            {"id": "DESK-1", "reporterId": "u-celia", "assigneeId": "u-celia"},
            {"id": "DESK-2", "reporterId": "u-celia", "assigneeId": "u-ivo"},
        ],
    }
    granted = {"desk-editor": ["jira_mock", "gmail_mock"], "reporter": ["gmail_mock"]}
    found = world_check.check_actor_logins("jira_mock", state, identities, granted)
    assert len(found) == 1 and found[0]["severity"] == "warning"
    assert "1 records name reporter (1)" in found[0]["message"]
    assert "does not give that worker jira_mock" in found[0]["message"]
    assert "one of desk-editor" in found[0]["message"]
    # Error while the author can still act: the seed prompt already names workers_using_this_app.
    assert (
        world_check.check_actor_logins("jira_mock", state, identities, granted, severity="error")[0][
            "severity"
        ]
        == "error"
    )
    # A worker's identity record exists in every identity_key app whether they hold it or not
    # (validate_core requires one), so the directory proves nothing: only the attribution is judged.
    both = {"desk-editor": ["jira_mock"], "reporter": ["jira_mock"]}
    assert world_check.check_actor_logins("jira_mock", state, identities, both) == []
    assert world_check.check_actor_logins("jira_mock", state, identities, {}) == []


def test_a_collection_stored_as_a_map_of_lists_is_still_a_collection():
    """``records_of`` returned [] for ``{channelId: [record...]}``, the biggest shape in the fleet.

    slack's messages are stored by channel and Zendesk's comments by ticket -- 37,711 records
    across the seeded worlds, the largest collection in every world that has one -- so every check
    reading a collection through this helper skipped them.
    """
    assert len(world_check.records_of(SLACK_SHAPE["messages"])) == 2
    assert len(world_check.records_of(SLACK_SHAPE["threads"])) == 2
    assert len(world_check.records_of(SLACK_SHAPE["users"])) == 2
    assert world_check.records_of("not a collection") == []


def test_how_an_app_shows_a_person_is_not_a_fact_about_the_world():
    """``check_facts`` compared presentation fields, and the error it raised was unsatisfiable.

    ``avatar`` was all 25 errors blocking sungrow-power-supply and ``username`` the single error
    blocking schweitzer-engineering-laboratories, where github's ``hainsworth`` is a correct handle
    and the calendar's is a display name; the repair then "fixed" it into the opposite violation and
    aborted, spending that company's one repair round on a pair nothing could satisfy. Skipping the
    eight per-app presentation fields removed 28 of 309 check errors over the 60 seeded worlds and
    unblocked both companies outright.
    """
    world = {"people": [{"id": "u1", "name": "Hal Ainsworth", "title": "Counsel"}]}
    states = {
        "github_mock": {"users": [{"id": "u1", "name": "Hal Ainsworth", "username": "hainsworth"}]},
        "google_calendar_mock": {
            "users": [{"id": "u1", "name": "Hal Ainsworth", "username": "Hal Ainsworth", "avatar": "HA"}]
        },
        "slack_mock": {"users": [{"id": "u1", "name": "Hal Ainsworth", "avatar": "", "title": "Partner"}]},
    }
    messages = [f["message"] for f in world_check.check_facts(world, states)]
    assert not [m for m in messages if "username" in m or "avatar" in m], messages
    assert [m for m in messages if "title" in m], "a real fact about the person still differs"


def test_the_grant_is_read_from_the_task_contract_before_the_file_exists(tmp_path):
    """Seeding writes worker_apps.json only after its own check runs, so the file is not there yet.

    A rule that can only fire at the check stage reaches nobody: by then the repair budget is spent
    and the finding is a revision no author can make. The grant is therefore taken from the task
    contract seeding builds worker_apps.json from, which is on disk before the seed call -- derived
    the same way state_seed derives it (the contract's apps plus STANDARD_APPS, intersected with
    this company's apps). Replayed over associated-press with the file removed, the contract alone
    finds its 225 jira records attributed to the three workers the contract does not give jira.
    """
    from company_envs.storage import write

    folder = tmp_path / "companies" / "acme"
    write(folder / "apps.json", {"apps": [{"app_id": "jira_mock"}, {"app_id": "gmail_mock"}]})
    write(folder / "company.json", {"workers": [{"id": "w-lead"}, {"id": "w-clerk"}]})
    write(folder / "world" / "world.json", {})
    write(folder / "world" / "gmail_mock.state.json", {})
    write(
        folder / "world" / "jira_mock.state.json",
        {
            "users": [
                {"id": "u-lead", "name": "Ada Vane", "email": "ada@acme.test"},
                {"id": "u-clerk", "name": "Jo Park", "email": "jo@acme.test"},
            ],
            "issues": [{"id": "ACME-1", "assigneeId": "u-clerk", "reporterId": "u-lead"}],
        },
    )
    write(
        folder / "world" / "identities.json",
        {
            "w-lead": {"jira_mock": {"id": "u-lead", "name": "Ada Vane", "email": "ada@acme.test"}},
            "w-clerk": {"jira_mock": {"id": "u-clerk", "name": "Jo Park", "email": "jo@acme.test"}},
        },
    )
    write(
        folder / "tasks" / "t1" / "workflow.json",
        {
            "contributions": [
                {"worker_id": "w-lead", "apps": ["jira_mock"]},
                {"worker_id": "w-clerk", "apps": []},
            ]
        },
    )
    assert not (folder / "world" / "worker_apps.json").is_file()
    found = [
        f
        for f in world_check.check_folder(tmp_path, folder)["findings"]
        if "worker_apps.json does not give" in f["message"]
    ]
    assert len(found) == 1 and "w-clerk (1)" in found[0]["message"]
    assert "one of w-lead" in found[0]["message"]

    # With no contract and no file there is no grant to read, and the check stays silent.
    (folder / "tasks" / "t1" / "workflow.json").unlink()
    assert not [
        f
        for f in world_check.check_folder(tmp_path, folder)["findings"]
        if "worker_apps.json does not give" in f["message"]
    ]


def test_a_desktop_file_that_names_the_harness_is_caught_where_it_can_be_repaired():
    """The leak rule was written for the documents a worker reads and never ran over them: 19
    companies' materials carry harness vocabulary and all 19 pass the gate today. It is an error only
    while the author is still in the loop, because the check stage's repair rewrites app records and
    not materials -- only the review stage's does -- so an error there would strand all 19.
    """
    world = {"entities": []}
    leaking = {"notes/onboarding.md": "Read Gmail stored_state.drafts to corroborate the recipient."}
    args = ({}, {}, [], "2026-01-01")

    seeding = world_check.check_world(world, *args, materials=leaking, authoring=True)
    errors = [f for f in seeding["findings"] if f["severity"] == "error" and "materials:" in f["source"]]
    assert errors, "the author writes the desktops and can be told"

    later = world_check.check_world(world, *args, materials=leaking, authoring=False)
    found = [f for f in later["findings"] if "materials:" in f["source"]]
    assert found and all(f["severity"] == "warning" for f in found)
    assert not any("materials:" in f["source"] for f in later["findings"] if f["severity"] == "error")


def test_a_record_no_container_holds_is_a_record_the_app_never_draws():
    """Trello renders a list from ``list.cardIds`` (``List.jsx:59``), so a card in no list is nowhere.

    Served and counted: blastx-consulting's trello world holds 3,288 cards and renders **23** --
    every board route the state declares, scrolled to the bottom. 3,120 cards name lists that were
    never written and 145 sit in a list that exists without being in its ``cardIds``; the navbar
    search skips both (``if (!board || !list) continue``). Fleet-wide 3,417 of 3,506 trello cards
    and 503 of 727 monday items. The split is in the message because the repairs differ: one
    writes the missing board, the other adds an id to a list.
    """
    state = {
        "boards": [{"id": "b1", "title": "Cove Home", "listIds": ["l1"]}],
        "lists": [{"id": "l1", "title": "Open", "boardId": "b1", "cardIds": ["card-1"]}],
        "cards": [
            {"id": "card-1", "title": "Ship the sample", "listId": "l1"},
            {"id": "card-2", "title": "Chase the invoice", "listId": "l1"},
            {"id": "card-3", "title": "Write the brief", "listId": "l-never-written"},
        ],
    }
    found = world_check.check_reachability("trello_mock", state)
    assert len(found) == 1 and found[0]["severity"] == "warning" and found[0]["path"] == "/cards"
    assert "2 of 3 cards cannot be reached" in found[0]["message"]
    assert "1 naming a container that does not exist" in found[0]["message"]
    assert "1 in no container" in found[0]["message"]
    assert "lists.cardIds" in found[0]["message"]
    # The caller owns the severity: an error only while the author can still link the record.
    assert world_check.check_reachability("trello_mock", state, "error")[0]["severity"] == "error"
    held = {**state, "lists": [{**state["lists"][0], "cardIds": ["card-1", "card-2", "card-3"]}]}
    assert world_check.check_reachability("trello_mock", held) == []


def test_a_container_that_is_itself_unreachable_hides_everything_under_it():
    """20,192 of 78,462 drive items cannot be reached from a root, 37 of them only transitively.

    The one-hop checks cannot see those 37: the item's ``parentId`` resolves, and its parent's does
    not. Served and looked at: los-angeles-county declares 3,004 items under seven folders that
    were never written, and the app draws an empty My Drive -- a full seed spent on a collection
    the app cannot show one row of.
    """
    state = {
        "items": {
            "root": {"id": "root", "parentId": None, "name": "My Drive", "type": "folder"},
            "team": {"id": "team", "parentId": "root", "name": "Delivery", "type": "folder"},
            "plan": {"id": "plan", "parentId": "team", "name": "Plan.doc", "type": "doc"},
            "orphan": {"id": "orphan", "parentId": "never-written", "name": "Q3.doc", "type": "doc"},
            "below": {"id": "below", "parentId": "orphan", "name": "Appendix.doc", "type": "doc"},
        }
    }
    found = world_check.check_reachability("google_drive_mock", state)
    assert len(found) == 1 and "2 of 5 items cannot be reached" in found[0]["message"]
    assert "1 naming a container that does not exist" in found[0]["message"]
    assert "1 under a container that is itself unreachable" in found[0]["message"]
    # A cycle reaches no root either, and a one-hop check sees two resolving pointers.
    cycle = {"items": {"a": {"id": "a", "parentId": "b"}, "b": {"id": "b", "parentId": "a"}}}
    assert "2 in a cycle of containers" in world_check.check_reachability("drive", cycle)[0]["message"]


def test_a_record_with_two_containers_is_judged_by_the_one_that_works():
    """Reachability grows from the roots, so no answer depends on which record is looked at first.

    miro holds board items both by board and by a ``parentId`` under another item. One sticky here
    is on a board and the other only hangs under it, so both are drawn -- but a walk *up* from each
    record has to cut the chain when it returns to a record already on its own stack, and caching
    that cut as a verdict condemns whichever of the two it happened to start from. A forward
    fixpoint from the roots cannot have the bug, and costs one pass over the links.
    """
    state = {
        "boards": [{"id": "b1", "name": "Roadmap"}],
        "boardItems": {
            "b1": [{"id": "sticky-x", "parentId": "sticky-y", "text": "Pricing page"}],
            "b-never-written": [{"id": "sticky-y", "parentId": "sticky-x", "text": "Checkout"}],
        },
    }
    assert world_check.check_reachability("miro_mock", state) == []


def test_a_collection_stored_under_a_container_id_is_reachable_only_if_the_key_names_one():
    """slack keys messages by channel, and ``state_ids`` puts those keys into the id universe.

    So a key that names no channel is the one dangling reference no check could report: one world
    holds 193 messages under ``CH-OPS`` while its channels are ``CH-BOROUGH``, ``CH-PROGRAM`` and
    ``CH-REPORT``, and serving it shows 195 reachable messages on screen and none of the 193.
    """
    state = {
        "channels": [{"channelId": "ch-desk", "name": "delivery-pod"}],
        "messages": {
            "ch-desk": [{"messageId": "m1", "content": "Handover is on the doc."}],
            "CH-OPS": [{"messageId": "m2", "content": "Nobody will read this."}],
        },
    }
    found = world_check.check_reachability("slack_mock", state)
    assert len(found) == 1 and "1 of 2 messages cannot be reached" in found[0]["message"]
    assert "m2" in found[0]["message"] and "a key naming channels" in found[0]["message"]

    # A map whose keys name nothing groups its records by something the state does not hold, which
    # is how gmail's threadId works and not a fault. Every map on disk resolves none of its keys or
    # at least 83% of them, so the line is clear of the data on both sides.
    grouped = {
        "emails": [{"id": "e1", "threadId": "t-990", "subject": "Renewal"}],
        "byThread": {"t-990": [{"messageId": "m9", "content": "Grouped, not contained."}]},
    }
    assert world_check.check_reachability("gmail_mock", grouped) == []


def test_a_reference_is_not_a_container_and_is_never_called_unreachable():
    """The hazard this check is most likely to fail at, and the one ``check_record_fields`` hit.

    A vocabulary that calls every link a container invents defects: gmail's labels are named by
    every email's ``labels`` and carry no ``emailId``, and without the two-sided test 165 of 244
    labels across 32 worlds are condemned for a sidebar the app renders itself. The same for an
    actor field -- a ServiceNow incident is in a queue whatever its ``assigned_to`` says -- and for
    the collections the app simply lists, which have no container convention at all.
    """
    gmail = {
        "labels": [{"id": "lab-a", "name": "Tenancy"}, {"id": "lab-b", "name": "Subscriber desk"}],
        "emails": [
            {"id": "e1", "subject": "Renewal", "labels": ["lab-a"]},
            {"id": "e2", "subject": "Desk", "labels": ["lab-a"]},
        ],
    }
    assert world_check.check_reachability("gmail_mock", gmail) == []
    incidents = {
        "users": [{"sys_id": "u1", "name": "Jo Park"}],
        "incidents": [
            {"sys_id": "i1", "assigned_to": "u1", "short_description": "Badge reader down"},
            {"sys_id": "i2", "assigned_to": "left-the-company", "short_description": "Printer"},
        ],
    }
    assert world_check.check_reachability("ServiceNow_mock", incidents) == []
    # A membership list repeats its ids across holders, so it is not a container either.
    boards = {
        "users": [{"id": "u1", "name": "Jo Park"}, {"id": "u2", "name": "Ada Vane"}],
        "boards": [{"id": "b1", "memberIds": ["u1", "u2"]}, {"id": "b2", "memberIds": ["u1", "u2"]}],
    }
    assert world_check.check_reachability("miro_mock", boards) == []


def test_a_count_is_derived_from_the_records_it_counts():
    """685 records on disk carry a child count their own state contradicts.

    Zendesk tickets print "5 comments" over twelve (110 records), 158 Expensify reports declare
    expenses and hold none, 191 weibo posts declare comments and hold none, 16 booking properties
    declare 7 reviews over 21. Only one direction is always wrong: more children than the count
    says is impossible, while a count *above* the children is how a public total over a stored
    sample reads -- amazon's products declaring 86 reviews over 4 are left alone -- so that is
    reported only for a record holding none, in a collection where some record counts its children
    exactly and so says what the field means.
    """
    state = {
        "tickets": [
            {"id": "t1", "subject": "Badge", "comment_count": 2},
            {"id": "t2", "subject": "Printer", "comment_count": 5},
            {"id": "t3", "subject": "Laptop", "comment_count": 3},
        ],
        "comments": {
            "t1": [
                {"id": "c1", "ticket_id": "t1", "body": "Looking"},
                {"id": "c2", "ticket_id": "t1", "body": "Fixed"},
            ],
            "t2": [{"id": f"c{n}", "ticket_id": "t2", "body": "Ack"} for n in range(3, 9)],
        },
    }
    found = world_check.check_counts("Zendesk_mock", state)
    assert {f["severity"] for f in found} == {"warning"}
    messages = " | ".join(f["message"] for f in found)
    assert "1 of 3 tickets hold more comments than their comment_count says" in messages
    assert "t2: comment_count 5, 6 comments" in messages
    assert "1 of 3 tickets declare comment_count above zero and hold no comments" in messages
    assert world_check.check_counts("Zendesk_mock", state, "error")[0]["severity"] == "error"
    # A public total over a stored sample is ordinary: no record counts its own reviews exactly.
    market = {
        "products": [{"id": "p1", "title": "Lamp", "reviewCount": 86}],
        "reviews": [{"id": "r1", "productId": "p1", "text": "Bright"}],
    }
    assert world_check.check_counts("amazon_mock", market) == []
    # An inline list is counted the same way.
    surveys = {
        "surveys": [
            {"id": "s1", "name": "Shuttle", "responsesCount": 34, "responses": []},
            {"id": "s2", "name": "Luggage", "responsesCount": 0, "responses": []},
        ]
    }
    assert (
        "1 of 2 surveys declare responsesCount"
        in world_check.check_counts("hotjar_mock", surveys)[0]["message"]
    )
    # ``numReplies`` is the same assertion under the other common name for a count.
    threads = {
        "threads": [
            {"id": "th-1", "title": "Rollout", "numReplies": 1, "replies": [{"id": "r1"}]},
            {"id": "th-2", "title": "Cutover", "numReplies": 4, "replies": []},
        ]
    }
    found = world_check.check_counts("forum", threads)
    assert len(found) == 1
    assert "1 of 2 threads declare numReplies above zero and hold no replies" in found[0]["message"]


def test_reachability_and_counts_are_errors_only_while_the_author_can_act():
    """22,952 of the 27,658 unreachable records are bulk, which no author owns.

    So the same rule as every structural check: an error under ``authoring=True``, where the app
    author is still in the repair loop and every one of the 4,706 human-layer records is one it can
    still link, and a warning afterwards, because a world whose repair budget is spent cannot
    answer a new error.
    """
    state = {
        "lists": [{"id": "l1", "title": "Open", "cardIds": []}],
        "cards": [{"id": "card-1", "title": "Ship the sample", "listId": "l1"}],
        "tickets": [
            {"id": "t1", "subject": "Badge", "comment_count": 4},
            {"id": "t2", "subject": "X", "comment_count": 1},
        ],
        "comments": [{"id": "c1", "ticket_id": "t2", "body": "Ack"}],
    }
    for authoring, severity in ((False, "warning"), (True, "error")):
        result = world_check.check_world(
            {"company": "Clearpath"},
            {"trello_mock": state},
            {},
            [],
            "2026-09-10",
            authoring=authoring,
        )
        mine = [
            f for f in result["findings"] if "cannot be reached" in f["message"] or "hold no" in f["message"]
        ]
        assert mine, "the reachability and count rules run inside check_world"
        assert {f["severity"] for f in mine} == {severity}
