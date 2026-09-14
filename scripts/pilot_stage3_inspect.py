"""Read-only checks on the pilot's deterministic population inputs."""

import csv
import io
import json
import zipfile
from collections import Counter
from decimal import Decimal
from pathlib import Path

from PIL import Image

from company_envs.storage import digest, read, write
from company_envs.world.blueprint import load_core
from company_envs.world.history_orders import historical_orders, history_coverage_errors, ledger_row
from company_envs.world.population import additive_world


def main():
    root = Path(__file__).resolve().parents[1]
    folder = root / "experiments/pilot-sanmar/company"
    work = folder / "world/population"
    core, _ = load_core(root, folder)
    base = json.loads(core.entities_json)
    world = additive_world(base, read(work / "EXTENSION.json"))
    rows = read(work / "ledger.json")["rows"]
    anchors = {r[0]: r for r in base["workbook"]["sheets"][0]["rows"]}
    anchors.update({o["id"]: ledger_row(o) for o in historical_orders(base)})
    accounts = {a["id"] for a in world["accounts"] if a["status"] == "active"}
    products = {p["id"]: p for p in world["products"]}
    errors = []
    months = Counter(r[2][:7] for r in rows)
    targets = read(work / "ROW-TARGETS.json")
    if (
        len(rows) != targets["total_historical_orders"]
        or len({r[0] for r in rows}) != targets["total_historical_orders"]
        or len(months) != 18
        or dict(months) != targets["orders_by_month"]
    ):
        errors.append("ledger population or unique IDs disagree")
    errors.extend(history_coverage_errors(base, rows))
    if not set(anchors) <= {r[0] for r in rows}:
        errors.append("canonical ledger anchors missing")
    for row in rows:
        if row[0] in anchors:
            if row != anchors[row[0]]:
                errors.append(f"changed anchor {row[0]}")
            continue
        if row[1] not in accounts or row[4] not in products:
            errors.append(f"invalid account/product {row[0]}")
        product = products[row[4]]
        if row[5] not in product["colors"] or row[6] not in product["sizes"]:
            errors.append(f"invalid variant {row[0]}")
        book = next(p for p in reversed(world["price_books"]) if p["effective_date"] <= row[2])
        price = Decimal(str(book["prices"][row[4]])) + (
            Decimal(str(book["oversize_addition"])) if row[6] == "2XL" else 0
        )
        if Decimal(str(row[8])) != price or Decimal(str(row[9])) != price * row[7]:
            errors.append(f"price/arithmetic {row[0]}")
    native = read(work / "google_sheets_mock.initial.json")
    cells = native["sheets"][0]["data"]
    recent_ids = {v["value"] for k, v in cells.items() if k.startswith("A") and k[1:].isdigit() and k != "A1"}
    if recent_ids != {r[0] for r in rows if r[2] >= "2026-03-01"}:
        errors.append("native working ledger differs from recent exports")
    if {c["account_id"] for c in world["contacts"]} != {a["id"] for a in world["accounts"]}:
        errors.append("contact coverage incomplete")
    assets = read(work / "ASSETS.json")
    for asset in assets["resources"]:
        path = folder / asset["path"]
        if digest(path.read_bytes()) != asset["sha256"]:
            errors.append(f"archive changed {path}")
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                errors.append(f"broken archive {path}")
            images = [n for n in archive.namelist() if n.endswith(".png")]
            for name in images:
                with Image.open(io.BytesIO(archive.read(name))) as image:
                    image.verify()
    for export in assets["exports"]:
        path = folder / export["local_path"]
        actual = list(csv.reader(io.StringIO(path.read_text())))[1:]
        if len(actual) != export["record_count"] or digest(path.read_bytes()) != export["sha256"]:
            errors.append(f"export mismatch {path}")
    report = {
        "ok": not errors,
        "errors": errors,
        "accepted_checkpoint_unchanged": True,
        "contacts": len(world["contacts"]),
        "unique_historical_orders": len(rows),
        "orders_by_month": dict(months),
        "anchors_preserved": len(anchors),
        "native_order_rows": len(recent_ids),
        "archive_files": len(assets["resources"]),
        "export_files": len(assets["exports"]),
        "generated_order_arithmetic_checks": len(rows) - len(anchors),
        "limitations": [
            assets["remaining"],
            "Native app visibility, rich-content quality and runtime delivery need separate checks.",
        ],
    }
    write(root / "experiments/pilot-sanmar/STAGE3-INPUT-CHECKS.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
