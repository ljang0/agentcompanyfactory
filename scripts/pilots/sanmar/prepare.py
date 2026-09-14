"""Build reproducible historical exports and additive Stage 3 inputs for the pilot."""

import calendar
import csv
import io
import json
import random
import zipfile
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path

from openpyxl.utils import get_column_letter

from company_envs.storage import digest, read, write
from company_envs.world.population import additive_world


def main():
    root = Path(__file__).resolve().parents[3]
    folder = root / "experiments/pilot-sanmar/company"
    work = folder / "world/population"
    base = read(folder / "world/world.json")
    profiles = read(work / "PROFILES.json")
    if profiles["parent_world_hash"] != digest(base):
        raise ValueError("stale profiles")
    contacts = {c["account_id"]: c for c in base["contacts"]}
    accounts, additions = [], []
    for p in profiles["profiles"]:
        accounts.append(
            {"id": p["account_id"], **{k: p[k] for k in ("domain", "city", "state", "business_model")}}
        )
        existing = contacts.get(p["account_id"])
        additions.append(
            {
                **(
                    existing
                    or {
                        "id": f"CT-{p['account_id']}",
                        "account_id": p["account_id"],
                        "apps": ["hubspot_mock", "gmail_mock", "google_drive_mock"],
                    }
                ),
                "name": p["contact_name"],
                "email": p["contact_email"],
                "job_title": p["contact_job_title"],
                "business_model": p["business_model"],
            }
        )
    extension = {
        "parent_world_hash": digest(base),
        "collections": {"accounts": accounts, "contacts": additions},
    }
    world = additive_world(base, extension)
    # New routine orders never reuse customers with explicit onboarding history.
    excluded = {a["account_id"] for a in base["applications"]}
    cohort = [
        a
        for a in world["accounts"]
        if a["status"] == "active" and a["id"] not in excluded and int(a["id"][1:]) >= 29
    ]
    rng = random.Random(0)
    rows = []
    exports = []
    columns = world["workbook"]["sheets"][0]["columns"]
    for month_index in range(18):
        year, month0 = divmod(2025 * 12 + 2 + month_index, 12)
        month = month0 + 1
        period = f"{year}-{month:02d}"
        days = [
            date(year, month, d)
            for d in range(1, calendar.monthrange(year, month)[1] + 1)
            if date(year, month, d).weekday() < 5 and d <= 25
        ]
        anchors = [r for r in world["workbook"]["sheets"][0]["rows"] if r[2].startswith(period)]
        current = deepcopy(anchors)
        for n in range(400 - len(anchors)):
            accepted = rng.choice(days)
            price_book = next(
                p for p in reversed(world["price_books"]) if p["effective_date"] <= accepted.isoformat()
            )
            product = rng.choice(world["products"])
            size = rng.choice(product["sizes"])
            price = Decimal(str(price_book["prices"][product["id"]])) + (
                Decimal(str(price_book["oversize_addition"])) if size == "2XL" else 0
            )
            qty = rng.choice([24, 36, 48, 72, 96, 120, 144, 240, 288, 360, 480, 600])
            number = f"{year % 100:02d}{month:02d}-{n + 1001}"
            current.append(
                [
                    f"ORD-{number}",
                    rng.choice(cohort)["id"],
                    accepted.isoformat(),
                    f"INV-{number}",
                    product["id"],
                    rng.choice(product["colors"]),
                    size,
                    qty,
                    float(price),
                    float(price * qty),
                    "Shipped",
                ]
            )
        current.sort(key=lambda r: (r[2], r[0]))
        rows.extend(current)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(current)
        relative = f"exports/orders-{period}.csv"
        target = work / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(stream.getvalue())
        for directory in ("materials", "desktop"):
            destination = folder / "world" / directory / "order-coordinator" / "order-history" / target.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(target.read_bytes())
        exports.append(
            {
                "id": f"EXPORT-ORDERS-{period}",
                "title": f"Central orders export {period}",
                "owner_id": "person-owen",
                "created": f"{period}-{calendar.monthrange(year, month)[1]:02d}T17:00:00-07:00",
                "folder_id": "F-LEDGER",
                "record_count": len(current),
                "sha256": digest(target.read_bytes()),
                "url": f"https://resources.cedarlineapparel.com/exports/orders-{period}.csv",
                "local_path": f"world/population/{relative}",
                "apps": ["google_drive_mock", "google_sheets_mock"],
                "description": "Read-only central-order export; desk staff can reconcile it and request central operations corrections.",
            }
        )
    extension["collections"]["historical_exports"] = exports
    asset_files = []
    asset_additions = []
    for asset in base["assets"]:
        product = next(p for p in base["products"] if p["id"] == asset["product_id"])
        image = work / "assets" / f"{product['id']}.png"
        target = work / "assets" / Path(asset["url"]).name.replace(".zip", f"-{product['id']}.zip")
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in [
                (f"{product['id']}-views.png", image.read_bytes()),
                ("product.json", json.dumps(product, indent=2).encode()),
                (
                    "usage.txt",
                    (
                        "Product resource: "
                        + product["name"]
                        + "\nPermitted channels: "
                        + ", ".join(asset["permitted_channels"])
                        + "\n"
                    ).encode(),
                ),
            ]:
                entry = zipfile.ZipInfo(name, (2026, 9, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, content)
        asset_files.append(
            {
                "id": asset["id"],
                "url": asset["url"],
                "path": str(target.relative_to(folder)),
                "bytes": target.stat().st_size,
                "sha256": digest(target.read_bytes()),
                "catalog_advertised_bytes": asset["bytes"],
                "image_url": f"https://resources.cedarlineapparel.com/products/{product['id']}/views.png",
                "image_path": str(image.relative_to(folder)),
                "image_sha256": digest(image.read_bytes()),
            }
        )
        asset_additions.append(
            {
                "id": asset["id"],
                "local_download_bytes": target.stat().st_size,
                "image_url": asset_files[-1]["image_url"],
                "local_path": str(target.relative_to(folder)),
            }
        )
    extension["collections"]["assets"] = asset_additions
    write(work / "EXTENSION.json", extension)
    write(
        work / "ASSETS.json",
        {
            "resources": asset_files,
            "exports": exports,
            "routing": "Stage 4 must serve these exact local files at the canonical resource URLs.",
            "remaining": "Stage 2 catalog advertised archive sizes differ from actual generated packages; preserve the original claim and expose measured sizes, then resolve during world acceptance.",
        },
    )
    write(work / "ledger.json", {"columns": columns, "rows": rows})
    workbook = deepcopy(world["workbook"])
    workbook["sheets"][0]["rows"] = [r for r in rows if r[2] >= "2026-03-01"]
    state = {
        "id": workbook["id"],
        "title": workbook["title"],
        "activeSheetId": workbook["sheets"][0]["id"],
        "selectedCell": "A1",
        "selectionRange": None,
        "clipboard": None,
        "isDragging": False,
        "undoStack": [],
        "redoStack": [],
        "namedRanges": [],
        "conditionalFormats": [],
        "charts": [],
        "showGridlines": True,
        "showFormulas": False,
        "zoom": 100,
        "sheets": [],
    }
    for tab in workbook["sheets"]:
        cells = {}
        for row_index, row in enumerate([tab["columns"], *tab["rows"]], 1):
            for column, value in enumerate(row, 1):
                cells[f"{get_column_letter(column)}{row_index}"] = {
                    "value": str(value),
                    "formula": str(value),
                }
        state["sheets"].append(
            {
                "id": tab["id"],
                "name": tab["name"],
                "data": cells,
                "rowCount": max(100, len(tab["rows"]) + 20),
                "colCount": 26,
                "frozenRows": 1,
            }
        )
    write(work / "google_sheets_mock.initial.json", state)
    plan = read(folder / "world/population.json")
    for target in plan:
        if (target["app_id"], target["collection"]) == ("hubspot_mock", "contacts"):
            target["target_records"] = len(additions)
    write(work / "targets.json", plan)
    write(
        work / "ROW-TARGETS.json",
        {
            "total_historical_orders": 7200,
            "months": 18,
            "orders_per_month": 400,
            "native_recent_order_rows": 2400,
            "historical_csv_exports": 18,
            "older_rows_in_exports": 4800,
            "canonical_order_anchors_preserved": 12,
            "policy": "The six-month working workbook and 18 monthly native CSV exports together expose every row. Do not double-count recent rows repeated in exports.",
            "generation": "Deterministic seed 0; product variant and date-effective pricebook; routine closed historical orders from established account cohort, plus immutable anchors.",
        },
    )
    print(
        json.dumps(
            {
                "contacts": len(additions),
                "orders": len(rows),
                "exports": len(exports),
                "native_order_rows": len(workbook["sheets"][0]["rows"]),
                "sheets_bytes": len(json.dumps(state).encode()),
            }
        )
    )


if __name__ == "__main__":
    main()
