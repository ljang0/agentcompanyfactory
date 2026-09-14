"""Uniformity rules judge a world as it is authored: templated bulk, flat amount columns, copied
series, conversation shells. They run only with ``authoring=True`` and never re-judge a world on disk."""

import itertools

from company_envs.storage import write
from company_envs.world.world_check import check_folder, check_uniformity, check_world

WORDS = ("quarterly", "vendor", "safety", "payroll", "fleet")
TOPICS = ("audit", "handover", "forecast", "inspection", "rota")


def templated(n, prefix="e"):
    return [
        {
            "id": f"{prefix}{i}",
            "body": f"Dear {'Maria' if i % 2 else 'Tom'}, your invoice {i:04d} for USD {900 + i} is due on "
            f"2026-0{1 + i % 9}-1{i % 10}. Thanks, Ops",
        }
        for i in range(n)
    ]


def varied(n, prefix="e"):
    pairs = list(itertools.product(WORDS, TOPICS))
    return [
        {
            "id": f"{prefix}{i}",
            "body": f"the {a} {b} still needs a second reader before {('friday', 'monday')[i % 2]} lunch",
        }
        for i, (a, b) in enumerate(pairs[:n])
    ]


def uniformity_messages(state, **kwargs):
    return [f["message"] for f in check_uniformity("app", state, **kwargs)]


def test_shared_text_skeletons_are_an_error_but_varied_prose_is_not():
    messages = uniformity_messages({"emails": templated(24)})
    assert len(messages) == 1
    assert "copies of a few templates" in messages[0] and "e0" in messages[0]
    assert "vary the bulk parameters per record" in messages[0]
    assert uniformity_messages({"emails": varied(24)}) == []
    assert uniformity_messages({"emails": templated(19)}) == [], "small collections are not judged"


def test_flat_amount_columns_are_errors_but_enums_and_unused_fields_are_not():
    rows = [{"id": f"inv{i}", "amount": 12000, "priority": i % 3, "tax": 0, "hours": 8} for i in range(60)]
    messages = uniformity_messages({"invoices": rows})
    assert len(messages) == 1 and messages[0].startswith(
        "amount takes only 1 distinct values [12000.0] over 60"
    )
    assert "inv0" in messages[0]
    rows = [{"id": f"inv{i}", "amount": 100 + 37 * i} for i in range(60)]
    assert uniformity_messages({"invoices": rows}) == []
    rows = [{"id": f"inv{i}", "amount": 12000} for i in range(49)]
    assert uniformity_messages({"invoices": rows}) == [], "fewer than fifty rows are not judged"


def test_sheet_columns_and_csv_packed_cells_are_judged():
    data = {"A1": {"value": "Account"}, "B1": {"value": "encounter,charges,cash,payer"}}
    for i in range(2, 62):
        data[f"A{i}"] = {"value": f"M{i:03d}"}
        data[f"B{i}"] = {"value": f"E{i:03d},12000,{2000 if i % 11 else 12000},P{i % 4}"}
    state = {"sheets": [{"id": "source", "name": "Source", "data": data}]}
    messages = uniformity_messages(state)
    assert any(m.startswith("column B[charges] takes only 1 distinct values") for m in messages)
    assert any(m.startswith("column B[cash] takes only 2 distinct values") for m in messages)
    assert not any("payer" in m or "encounter" in m for m in messages)
    plain = {"A1": {"value": "Month"}, "B1": {"value": "Principal"}, "C1": {"value": "Recovery"}}
    for i in range(2, 62):
        plain[f"A{i}"] = {"value": f"2026-{i % 12 + 1:02d}"}
        plain[f"B{i}"] = {"value": "10000"}
        plain[f"C{i}"] = {"value": str(500 * (i % 23))}
    messages = uniformity_messages({"sheets": [{"id": "hist", "data": plain}]})
    assert len(messages) == 1 and messages[0].startswith("column B[Principal] takes only 1 distinct")


def test_several_constant_columns_in_one_sheet_are_a_constant_series_error():
    data = {
        "A1": {"value": "Week"},
        "B1": {"value": "Deliveries"},
        "C1": {"value": "Returns"},
        "D1": {"value": "Flag"},
    }
    for i in range(2, 16):
        data.update(
            {
                f"A{i}": {"value": f"W{i}"},
                f"B{i}": {"value": 20},
                f"C{i}": {"value": 3},
                f"D{i}": {"value": 1},
            }
        )
    messages = uniformity_messages({"sheets": [{"id": "weekly", "data": data}]})
    assert len(messages) == 1 and messages[0].startswith(
        "constant series: column B[Deliveries] repeats 20 over 14"
    )
    assert "Flag" not in messages[0], "0/1 columns are flags, not series"
    for i in range(2, 16):
        data[f"C{i}"] = {"value": i}
    assert uniformity_messages({"sheets": [{"id": "weekly", "data": data}]}) == [], (
        "one constant column is ordinary"
    )


def test_identical_long_series_across_rows_or_records_is_an_error():
    same = ",".join(str(16 + i % 3) for i in range(24))
    data = {"A1": {"value": "Member"}, "B1": {"value": "24-month history"}}
    for i in range(2, 8):
        data[f"A{i}"] = {"value": f"M{i:03d}"}
        data[f"B{i}"] = {
            "value": same if i in (3, 5, 7) else ",".join(str(10 * i + j % 5) for j in range(24))
        }
    messages = uniformity_messages({"sheets": [{"id": "SH-COLL", "data": data}]})
    assert len(messages) == 1 and "24-period series" in messages[0] and "B3, B5, B7" in messages[0]
    members = [{"id": f"m{i}", "history": [10 * i + j % 5 for j in range(24)]} for i in range(6)]
    assert uniformity_messages({"members": members}) == []
    members[4]["history"] = members[1]["history"]
    messages = uniformity_messages({"members": members})
    assert len(messages) == 1 and "m1.history, m4.history" in messages[0]


def test_conversation_shells_and_understated_counts_are_errors():
    conversations = {f"c{i}": {"id": f"c{i}", "lastMessagePreview": "see you"} for i in range(30)}
    messages = [
        {"id": f"m{i}", "conversationId": f"c{i}", "content": "are we still on for later"} for i in range(5)
    ]
    found = uniformity_messages({"conversations": conversations, "messages": messages})
    assert len(found) == 1 and found[0].startswith("25 of 30 conversations have no messages (e.g. c10, c11")
    # Messages that point at none of the conversations mean the join is not this one: no verdict.
    stray = [{**m, "conversationId": f"other{i}"} for i, m in enumerate(messages)]
    assert uniformity_messages({"conversations": conversations, "messages": stray}) == []
    tickets = [{"id": 100 + i, "comment_count": 1} for i in range(30)]
    comments = {
        str(100 + i): [{"id": f"{100 + i}{k}", "ticket_id": 100 + i} for k in range(2)] for i in range(30)
    }
    found = uniformity_messages({"tickets": tickets, "comments": comments})
    assert len(found) == 1 and found[0].startswith(
        "30 of 30 tickets hold more comments than their comment_count says"
    )
    # A marketplace total above the stored sample is ordinary; only extra children are copies.
    products = [{"id": f"p{i}", "reviewCount": 86} for i in range(30)]
    reviews = [{"id": f"r{i}", "productId": f"p{i}"} for i in range(30)]
    assert uniformity_messages({"products": products, "reviews": reviews}) == []


def test_bulk_layer_records_are_not_judged():
    ids = frozenset(f"e{i}" for i in range(24))
    assert uniformity_messages({"emails": templated(24)}, exclude_ids=ids) == []
    assert uniformity_messages({"emails": templated(24) + varied(24, "v")}, exclude_ids=ids) == []


def test_uniformity_runs_only_at_authoring_time(tmp_path):
    world = {"company": {"name": "Acme"}}
    states = {"gmail_mock": {"emails": templated(24)}}

    def uniform(result):
        return [f for f in result["findings"] if "copies of a few templates" in f["message"]]

    assert not uniform(check_world(world, states, {}, [], "2026-09-08"))
    flagged = uniform(check_world(world, states, {}, [], "2026-09-08", authoring=True))
    assert flagged and flagged[0]["source"] == "gmail_mock" and flagged[0]["path"] == "/emails"
    folder = tmp_path / "companies" / "acme"
    write(
        folder / "apps.json",
        {"apps": [{"app_id": "gmail_mock", "state_file": "world/gmail_mock.state.json"}]},
    )
    write(folder / "company.json", {"id": "acme", "workers": []})
    write(folder / "world" / "world.json", world)
    write(folder / "world" / "SEED.json", {"reference_date": "2026-09-08"})
    write(folder / "world" / "gmail_mock.state.json", states["gmail_mock"])
    (tmp_path / "config.toml").write_text("[generation]\nseed = 0\n")
    assert not uniform(check_folder(tmp_path, folder)), "a world already on disk is not re-judged"
    assert uniform(check_folder(tmp_path, folder, authoring=True))
