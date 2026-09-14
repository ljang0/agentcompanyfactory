"""Apply source-backed pilot population corrections, preserving original outputs."""

import csv
import io
import json
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from company_envs.storage import digest, now, read, write
from company_envs.world.blueprint import load_core
from company_envs.world.history_orders import historical_orders, history_coverage_errors, ledger_row
from company_envs.world.population import additive_world, missing_targets
from company_envs.world.seed_calls import load_skill
from company_envs.world.state_seed import app_contract


def main():
    root = Path(__file__).resolve().parents[3]
    folder = root / "experiments/pilot-sanmar/company"
    work = folder / "world/population"
    report_path = work / "COHERENCE-REPAIRS.json"
    if report_path.exists():
        raise SystemExit("Repairs already applied; inspect their saved checkpoint before changing it.")
    load_core(root, folder)
    manifest, contract = app_contract(root, folder)
    app_ids = [a["app_id"] for a in manifest["apps"]]
    # Do not race a still-running writer or hide a missing app.
    entries = {a: read(work / f"{a}.json") for a in app_ids}
    states = {a: read(folder / "world" / f"{a}.state.json") for a in app_ids}
    for app_id in app_ids:
        assert digest(states[app_id]) == entries[app_id]["state_hash"], app_id
    base = read(folder / "world/world.json")
    extension = read(work / "EXTENSION.json")
    plan = read(work / "targets.json")
    ledger = read(work / "ledger.json")
    assets = read(work / "ASSETS.json")
    archive = work / "before-coherence-fixes"
    for name in ("EXTENSION.json", "targets.json", "ledger.json", "ASSETS.json", "ROW-TARGETS.json"):
        write(archive / name, read(work / name))
    for app_id in app_ids:
        write(archive / f"{app_id}.state.json", states[app_id])
        write(archive / f"{app_id}.json", entries[app_id])

    orders = historical_orders(base)
    existing = {r[0]: r for r in ledger["rows"]}
    added = []
    for order in orders:
        row = ledger_row(order)
        if order["id"] in existing:
            assert existing[order["id"]] == row
        else:
            ledger["rows"].append(row)
            added.append(order["id"])
    ledger["rows"].sort(key=lambda r: (r[2], r[0]))
    assert not history_coverage_errors(base, ledger["rows"])
    extension["collections"]["orders"] = orders
    hash_replacements = {}
    drive = states["google_drive_mock"]["items"]
    for export in assets["exports"]:
        period = export["id"].removeprefix("EXPORT-ORDERS-")
        rows = [r for r in ledger["rows"] if r[2].startswith(period)]
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(ledger["columns"])
        writer.writerows(rows)
        content = stream.getvalue()
        data = content.encode()
        old_hash = export["sha256"]
        export.update(record_count=len(rows), sha256=digest(data))
        hash_replacements[old_hash] = export["sha256"]
        path = folder / export["local_path"]
        original = archive / "exports" / path.name
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(path.read_bytes())
        path.write_bytes(data)
        for directory in ("materials", "desktop"):
            (folder / "world" / directory / "order-coordinator/order-history" / path.name).write_bytes(data)
        item = drive[export["id"]]
        item.update(
            name=path.name,
            content=content,
            mimeType="text/csv",
            size=len(data),
            description=f"Central orders export for {period}: {len(rows)} order lines. "
            "Desk staff can reconcile this copy and request central operations corrections.",
        )
    extension["collections"]["historical_exports"] = deepcopy(assets["exports"])
    states["google_drive_mock"]["storageUsed"] = sum(item.get("size", 0) for item in drive.values())
    # Saved app prose may carry export hashes. Replace only exact old hashes.
    for app_id, state in states.items():
        encoded = json.dumps(state, ensure_ascii=False)
        for old, new in hash_replacements.items():
            encoded = encoded.replace(old, new)
        states[app_id] = json.loads(encoded)

    slack = states["slack_mock"]
    messages = {r["messageId"]: r for group in slack["messages"].values() for r in group}
    copied, roots = [], []
    for thread in slack["threads"].values():
        parent = messages[thread["parentMessageId"]]
        if parent.get("threadId"):
            parent["threadId"] = None
            roots.append(parent["messageId"])
        group = slack["messages"][thread.get("channelId") or thread["dmId"]]
        for reply in thread["replies"]:
            if reply["messageId"] not in messages:
                group.append(deepcopy(reply))
                messages[reply["messageId"]] = group[-1]
                copied.append(reply["messageId"])
    for group in slack["messages"].values():
        group.sort(key=lambda r: (r["timestamp"], r["messageId"]))
    unknowns = 0
    for company in states["hubspot_mock"]["companies"]:
        for key in ("numberOfEmployees", "annualRevenue"):
            if company.get(key) == 0:
                company[key] = None
                unknowns += 1
    # The relationship-system meeting is the source for its calendar projection.
    calendar = states["google_calendar_mock"]
    event_ids = {e["id"] for e in calendar["events"]}
    contacts = {c["id"]: c for c in states["hubspot_mock"]["contacts"]}
    projections = []
    for meeting in states["hubspot_mock"]["meetings"]:
        if meeting["id"] in event_ids:
            continue
        start = datetime.fromisoformat(meeting["date"])
        if start.tzinfo is None:
            start = start.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
        projections.append(
            {
                "id": meeting["id"],
                "calendarId": "CAL-IMANI",
                "title": meeting["title"],
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=meeting["duration"])).isoformat(),
                "allDay": False,
                "location": meeting.get("location", ""),
                "description": meeting.get("notes", ""),
                "guests": ["imani.brooks@cedarlineapparel.com", contacts[meeting["contactId"]]["email"]],
                "color": "#0B8043",
                "recurring": "none",
            }
        )
    bulk_events = set(entries["google_calendar_mock"]["bulk_ids"])
    removable = [
        e["id"] for e in calendar["events"] if e["id"] in bulk_events and e["calendarId"] == "CAL-IMANI"
    ]
    assert len(removable) >= len(projections), "Do not replace authored appointments"
    removed = set(removable[: len(projections)])
    calendar["events"] = [e for e in calendar["events"] if e["id"] not in removed] + projections
    entries["google_calendar_mock"]["bulk_ids"] = sorted(
        (bulk_events - removed) | {e["id"] for e in projections}
    )
    # Explain the approved contact expansion in the operative target, too.
    for target in plan:
        if (target["app_id"], target["collection"]) == ("hubspot_mock", "contacts"):
            target["purpose"] = (
                "All 220 named customer/prospect contacts from the additive profile pass; "
                "preserve the 28 original identities and all canonical company IDs."
            )
    additive_world(base, extension)  # validate the additive contract before writing
    _, skill = load_skill(root)
    fingerprint = digest(
        {
            "core": read(folder / "world/CORE.json")["hashes"],
            "extension": extension,
            "plan": plan,
            "skill": skill,
            "schemas": contract,
        }
    )
    for app_id, state in states.items():
        entry = entries[app_id]
        entry.setdefault("generation_inputs", entry["inputs"])
        entry.update(
            inputs=fingerprint,
            state_hash=digest(state),
            initial_state_hash=digest(read(work / f"{app_id}.initial.json"))
            if (work / f"{app_id}.initial.json").exists()
            else None,
            bytes=len(json.dumps(state, ensure_ascii=False).encode()),
            shortfalls=missing_targets(state, plan, app_id),
            coherence_repair={"at": now(), "report": "population/COHERENCE-REPAIRS.json", "model_calls": 0},
        )
        write(folder / "world" / f"{app_id}.state.json", state)
        write(work / f"{app_id}.json", entry)
    for name, data in (
        ("EXTENSION.json", extension),
        ("targets.json", plan),
        ("ledger.json", ledger),
        ("ASSETS.json", assets),
    ):
        write(work / name, data)
    counts = Counter(r[2][:7] for r in ledger["rows"])
    write(
        work / "ROW-TARGETS.json",
        {
            "total_historical_orders": len(ledger["rows"]),
            "months": 18,
            "orders_by_month": dict(sorted(counts.items())),
            "native_recent_order_rows": 2400,
            "historical_csv_exports": 18,
            "older_rows_in_exports": 4812,
            "canonical_order_anchors_preserved": 24,
            "policy": "Count unique orders across the six-month workbook and 18 monthly exports.",
            "generation": "Seed 0 rows retained; 12 explicit history-only anchors appended without regeneration.",
        },
    )
    write(
        work / "COHERENCE-REPAIRS.json",
        {
            "at": now(),
            "model_calls": 0,
            "inputs": fingerprint,
            "history_orders_added": added,
            "native_csv_exports": len(assets["exports"]),
            "slack_replies_copied_to_messages": copied,
            "slack_roots_made_visible": roots,
            "crm_unknown_numeric_fields_restored_to_null": unknowns,
            "crm_meetings_projected_to_calendar": [e["id"] for e in projections],
            "routine_calendar_events_replaced": sorted(removed),
            "meeting_timezone": "Naive CRM timestamps interpreted in the canonical America/Los_Angeles zone.",
            "originals": "population/before-coherence-fixes/",
            "accepted_core_unchanged": True,
        },
    )
    load_core(root, folder)
    print(f"Reconciled {len(added)} older orders, {len(copied)} Slack replies, {len(roots)} Slack roots.")


if __name__ == "__main__":
    main()
