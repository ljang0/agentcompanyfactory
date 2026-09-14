import pytest

from company_envs.world.history_orders import historical_orders, history_coverage_errors, ledger_row


def world():
    return {
        "orders": [],
        "history": [
            {
                "kind": "order_accepted",
                "record_id": "ORD-old",
                "account_id": "A1",
                "date": "2025-03-11",
                "product_id": "P1",
                "color": "White",
                "size": "M",
                "quantity": 120,
                "unit_price": 3.8,
                "amount": 456,
            },
            {
                "kind": "completed",
                "order_id": "ORD-old",
                "invoice_id": "INV-old",
                "invoice_date": "2025-03-12",
                "shipment_date": "2025-03-12",
                "paid_date": "2025-03-20",
                "amount": 456,
            },
        ],
    }


def test_missing_historical_orders_are_recovered_from_both_events():
    source = world()
    recovered = historical_orders(source)
    assert len(recovered) == 1
    assert recovered[0]["paid_date"] == "2025-03-20"
    assert history_coverage_errors(source, [])
    assert not history_coverage_errors(source, [ledger_row(recovered[0])])
    assert source["orders"] == []
    source["orders"] = recovered
    assert historical_orders(source) == []


def test_missing_completion_is_not_filled_with_guessed_dates():
    source = world()
    source["history"].pop()
    with pytest.raises(ValueError, match="completed record"):
        historical_orders(source)


def test_inconsistent_historical_amount_is_rejected():
    source = world()
    source["history"][1]["amount"] = 450
    with pytest.raises(ValueError, match="amounts disagree"):
        historical_orders(source)
