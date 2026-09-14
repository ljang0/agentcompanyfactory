"""Project actual seeded order rows into invoice files and transactional mail."""

import base64
import html
import json
import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fpdf import FPDF

from company_envs.storage import digest, now, read, write
from company_envs.world.population import additive_world


def next_weekday(day):
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def main():
    root = Path(__file__).resolve().parents[1]
    folder = root / "experiments/pilot-sanmar/company"
    work = folder / "world/population"
    world = additive_world(read(folder / "world/world.json"), read(work / "EXTENSION.json"))
    accounts = {a["id"]: a for a in world["accounts"]}
    contacts = {c["account_id"]: c for c in world["contacts"]}
    products = {p["id"]: p for p in world["products"]}
    anchors = {o["id"] for o in world["orders"]}
    ledger = read(work / "ledger.json")["rows"]
    sampled = random.Random(731).sample([r for r in ledger if r[0] not in anchors], 2200)
    drive = read(work / "google_drive_mock.human.json")
    gmail = read(folder / "world/gmail_mock.state.json")
    mail_entry = read(work / "gmail_mock.json")
    invoices_needed = 1120 - len(drive["items"])
    resource_rows, messages, bindings = [], [], []
    output = work / "transactions"
    output.mkdir(exist_ok=True)
    for index, row in enumerate(sampled):
        oid, account_id, accepted, invoice, pid, color, size, quantity, unit, gross, _status = row
        account, contact, product = accounts[account_id], contacts[account_id], products[pid]
        invoice_date = next_weekday(date.fromisoformat(accepted))
        shipped_date = next_weekday(invoice_date)
        attachments = []
        if index < invoices_needed:
            content = (
                f"Cedarline Apparel Supply\nInvoice line export {invoice}\n\n"
                f"Invoice date: {invoice_date}\nOrder: {oid}\nAccepted: {accepted}\n"
                f"Account: {account_id} - {account['name']}\n{account['city']}, {account['state']}\n\n"
                f"{product['name']} ({pid})\nColor: {color}    Size: {size}\n"
                f"Quantity: {quantity}\nUnit price: USD {unit:.2f}\nLine amount: USD {gross:.2f}\n\n"
                "Source: central order services. Merchandise line amount; freight and tax excluded from this extract.\n"
            )
            pdf = FPDF()
            pdf.set_creation_date(datetime(2026, 9, 1, tzinfo=UTC))
            pdf.add_page()
            pdf.set_font("Helvetica", size=11)
            pdf.multi_cell(0, 6, content.encode("latin-1", errors="replace").decode("latin-1"))
            binary = bytes(pdf.output())
            file = output / f"{invoice}.pdf"
            file.write_bytes(binary)
            url = f"https://resources.cedarlineapparel.com/invoices/{invoice}.pdf"
            timestamp = int(datetime.combine(invoice_date, datetime.min.time(), UTC).timestamp() * 1000)
            rid = f"FILE-{invoice}"
            drive["items"][rid] = {
                "id": rid,
                "parentId": "F-LEDGER",
                "name": f"{invoice}.pdf",
                "type": "pdf",
                "mimeType": "application/pdf",
                "size": len(binary),
                "ownerId": "person-owen",
                "sharedWith": [
                    {"userId": "person-priya", "role": "editor", "addedAt": "2026-08-31T16:30:00-07:00"}
                ],
                "starred": False,
                "trashed": False,
                "color": None,
                "createdAt": timestamp,
                "modifiedAt": timestamp,
                "accessedAt": timestamp,
                "description": f"{account['name']} - {quantity} {color} {product['name']}; order {oid}.",
                "content": content,
                "thumbnailUrl": "data:application/pdf;base64," + base64.b64encode(binary).decode(),
            }
            resource_rows.append(
                {
                    "id": rid,
                    "url": url,
                    "path": str(file.relative_to(folder)),
                    "bytes": len(binary),
                    "sha256": digest(binary),
                }
            )
            attachments = [
                {
                    "id": f"ATT-{invoice}",
                    "name": file.name,
                    "size": len(binary),
                    "type": "application/pdf",
                    "url": url,
                }
            ]
        staff = (
            {"name": "Owen Delgado", "email": "owen.delgado@cedarlineapparel.com"}
            if index % 3
            else {"name": "Imani Brooks", "email": "imani.brooks@cedarlineapparel.com"}
        )
        description = f"{quantity} {color.lower()} {product['name']} in size {size}"
        for event, day, body in [
            (
                "confirmation",
                date.fromisoformat(accepted),
                f"We accepted order {oid} for {html.escape(description)}. The unit price is ${unit:.2f}; the merchandise line amount is ${gross:,.2f}. Your account is {html.escape(account['name'])}.",
            ),
            (
                "shipment",
                shipped_date,
                f"Order {oid} has shipped: {html.escape(description)}. Your invoice reference is {invoice}."
                + (" The invoice line export is attached." if attachments else ""),
            ),
        ]:
            mid = f"mail-{oid}-{event}"
            messages.append(
                {
                    "id": mid,
                    "threadId": f"thread-{oid}",
                    "from": {
                        "name": "Cedarline order services",
                        "email": "orders@cedarlineapparel.com",
                        "avatar": "",
                    },
                    "to": [{"name": contact["name"], "email": contact["email"]}],
                    "cc": [staff],
                    "bcc": [],
                    "subject": f"Order {oid} {'accepted' if event == 'confirmation' else 'shipped'}",
                    "body": f"<p>{body}</p><p>Central order services</p>",
                    "timestamp": f"{day}T15:00:00Z",
                    "read": True,
                    "starred": False,
                    "important": False,
                    "labels": [],
                    "category": "primary",
                    "folder": "inbox",
                    "attachments": attachments if event == "shipment" else [],
                }
            )
        bindings.append(
            {
                "order_id": oid,
                "account_id": account_id,
                "contact_id": contact["id"],
                "invoice_id": invoice,
                "accepted": accepted,
                "invoice_date": str(invoice_date),
                "shipment_date": str(shipped_date),
                "quantity": quantity,
                "unit_price": unit,
                "line_amount": gross,
            }
        )
    original_bulk = set(mail_entry["bulk_ids"])
    human = [m for m in gmail["emails"] if m["id"] not in original_bulk]
    routine = [m for m in gmail["emails"] if m["id"] in original_bulk]
    retained = random.Random(74).sample(routine, 6200 - len(human) - len(messages))
    gmail["emails"] = sorted(human + retained + messages, key=lambda m: m["timestamp"])
    drive["storageUsed"] = sum(
        item.get("size", 0) for item in drive["items"].values() if item.get("type") != "folder"
    )
    write(output / "RESOURCES.json", resource_rows)
    write(output / "BINDINGS.json", bindings)
    write(output / "gmail_mock.state.json", gmail)
    write(output / "google_drive_mock.state.json", drive)
    result = {
        "status": "prepared",
        "model_calls": 0,
        "orders_linked": len(bindings),
        "transaction_emails": len(messages),
        "invoices": len(resource_rows),
        "human_emails_preserved": len(human),
        "routine_emails_retained": len(retained),
        "drive_items": len(drive["items"]),
        "gmail_emails": len(gmail["emails"]),
        "bytes": {
            "gmail_mock": len(json.dumps(gmail, ensure_ascii=False).encode()),
            "google_drive_mock": len(json.dumps(drive, ensure_ascii=False).encode()),
        },
        "source_ledger_hash": digest(read(work / "ledger.json")),
        "constructed_at": now(),
        "date_rule": "For routine orders only: invoice on the next weekday after acceptance; shipment on the next weekday after invoice. Canonical anchor orders are excluded.",
        "runtime_limitation": "The pinned Drive downloader supports data URLs; its PDF preview uses an image element and needs Stage 4 correction/browser proof.",
    }
    write(output / "PREPARED.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
