"""Recover order facts held in dated canonical history, without inventing them."""

from decimal import Decimal


def historical_orders(world):
    known = {row["id"] for row in world.get("orders", [])}
    completed = {
        h["order_id"]: h
        for h in world.get("history", [])
        if h.get("kind") == "completed" and h.get("order_id")
    }
    result = []
    for event in world.get("history", []):
        rid = event.get("record_id") or event.get("order_id")
        if event.get("kind") != "order_accepted" or not rid or rid in known:
            continue
        required = {"account_id", "product_id", "color", "size", "quantity", "unit_price", "amount", "date"}
        if not required <= event.keys():
            raise ValueError(f"historical order lacks explicit fields: {rid}")
        done = completed.get(rid)
        if not done or not {"invoice_id", "invoice_date", "shipment_date", "paid_date"} <= done.keys():
            raise ValueError(f"historical order needs its completed record: {rid}")
        amount = Decimal(str(event["quantity"])) * Decimal(str(event["unit_price"]))
        if amount != Decimal(str(event["amount"])) or Decimal(str(done["amount"])) != amount:
            raise ValueError(f"historical order amounts disagree: {rid}")
        result.append(
            {
                "id": rid,
                "account_id": event["account_id"],
                "accepted_date": event["date"],
                **{k: event[k] for k in ("product_id", "color", "size", "quantity", "unit_price")},
                **{k: done[k] for k in ("invoice_id", "invoice_date", "shipment_date", "paid_date")},
                "gross": event["amount"],
                "status": "shipped",
                "payment_status": "paid",
                "apps": ["google_sheets_mock", "google_drive_mock", "gmail_mock", "hubspot_mock"],
            }
        )
        known.add(rid)
    return result


def ledger_row(order):
    return [
        order["id"],
        order["account_id"],
        order["accepted_date"],
        order.get("invoice_id", ""),
        order["product_id"],
        order["color"],
        order["size"],
        order["quantity"],
        order["unit_price"],
        order["gross"],
        "Shipped" if order["status"] == "shipped" else order["status"],
    ]


def history_coverage_errors(world, rows):
    indexed = {row[0]: row for row in rows}
    errors = []
    for order in historical_orders(world):
        if indexed.get(order["id"]) != ledger_row(order):
            errors.append(f"historical order missing or changed in ledger: {order['id']}")
    return errors
