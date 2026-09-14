"""Project connected human episodes and explicit automated order events into the pilot apps."""

import html
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from company_envs.config import load_config
from company_envs.storage import digest, now, read, write
from company_envs.world.population import additive_world
from company_envs.world.population_activity import refresh_activity
from company_envs.world.population_amendments import corrected_world
from company_envs.world.population_contract import app_fingerprint
from company_envs.world.population_quality import normalize_slack
from company_envs.world.state_seed import app_contract


def main():
    root = Path(__file__).resolve().parents[3]
    folder = root / "experiments/pilot-sanmar/company"
    work = folder / "world/population"
    prepared = work / "episode-repair"
    ready = read(prepared / "RESULT.json")
    states = {
        a: read(folder / f"world/{a}.state.json")
        for a in ["hubspot_mock", "gmail_mock", "slack_mock", "google_calendar_mock"]
    }
    if any(digest(states[a]) != h for a, h in ready["source_states"].items()):
        raise ValueError("Population changed during episode authorship")
    world = read(work / "EFFECTIVE-WORLD.json")
    assert digest(world) == ready["source_world_hash"]
    archive = prepared / "before"
    archive.mkdir(exist_ok=True)
    for a, s in states.items():
        write(archive / f"{a}.state.json", s)
    for f in ["EXTENSION.json", "AMENDMENTS.json", "EFFECTIVE-WORLD.json", "transactions/BINDINGS.json"]:
        write(archive / Path(f).name, read(work / f))
    entries = {a: read(work / f"{a}.json") for a in states}
    for a, e in entries.items():
        write(archive / f"{a}.json", e)
    zone = ZoneInfo("America/Los_Angeles")
    crm = states["hubspot_mock"]
    mail = states["gmail_mock"]
    slack = states["slack_mock"]
    calendar = states["google_calendar_mock"]
    accounts = {a["id"]: a for a in world["accounts"]}
    contacts = {c["account_id"]: c for c in world["contacts"]}
    products = {p["id"]: p for p in world["products"]}
    ledger = read(work / "ledger.json")
    rows = {r[0]: r for r in ledger["rows"]}
    bindings = read(work / "transactions/BINDINGS.json")
    {b["order_id"]: b for b in bindings}
    resources = read(work / "transactions/RESOURCES.json")
    by_invoice = {r["id"].removeprefix("FILE-"): r for r in resources}
    episodes = []
    for key in ready["batches"]:
        batch = read(prepared / f"{key}.json")
        assert digest(batch["episodes"]) == batch["output_hash"]
        cases = {c["id"]: c for c in batch["cases"]}
        episodes += [{"case": cases[e["id"]], "writing": e} for e in batch["episodes"]]

    def local(value):
        dt = datetime.fromisoformat(value)
        return dt.replace(tzinfo=zone) if not dt.tzinfo else dt.astimezone(zone)

    def previous_workday(dt):
        dt -= timedelta(days=1)
        while dt.weekday() > 4:
            dt -= timedelta(days=1)
        return dt

    def next_workday(dt):
        dt += timedelta(days=1)
        while dt.weekday() > 4:
            dt += timedelta(days=1)
        return dt

    bulk = set(entries["hubspot_mock"]["bulk_ids"])
    human = {
        k: [r for r in crm[k] if r["id"] not in bulk]
        for k in ["deals", "tickets", "tasks", "notes", "meetings"]
    }
    generated = {k: [r for r in crm[k] if r["id"] in bulk] for k in human}
    # Routine deals are actual imported accepted orders, not fabricated sales conversations.
    order_sources = sorted(bindings, key=lambda b: digest("deal:" + b["order_id"]))
    for target, b in zip(generated["deals"], order_sources, strict=False):
        row = rows[b["order_id"]]
        account = accounts[b["account_id"]]
        target.update(
            name=f"{account['name']} — {row[7]} {row[4]} — {row[0]}",
            stage="closed_won",
            amount=row[9],
            closeDate=row[2],
            dealType="existing_business",
            priority="low",
            owner="Imani Brooks",
            companyId=b["account_id"],
            contactIds=[b["contact_id"]],
            probability=100,
            closedLostReason="",
            createDate=b["accepted_at"],
            lastActivityDate=b["shipment_at"],
            description=f"Imported from central order services: {row[0]}, accepted {row[2]}. {row[7]} {row[5]} {products[row[4]]['name']}, size {row[6]}; unit price ${row[8]:.2f}, merchandise ${row[9]:,.2f}. Shipped {b['shipment_date']}; invoice {b['invoice_id']}. This entry records accepted business, not a separate quote.",
        )
    # A real invoice download supports each automated self-service case.
    available = []
    for b in sorted(bindings, key=lambda b: digest("copy:" + b["order_id"])):
        if b["invoice_id"] not in by_invoice:
            continue
        at = next_workday(local(b["shipment_at"])).replace(
            hour=10, minute=int(digest(b["order_id"])[:3], 16) % 55
        )
        if at.date().isoformat() > "2026-08-31":
            continue
        available.append((b, at))
    assert len(available) >= len(generated["tickets"])
    service = []
    for i, (ticket, (b, at)) in enumerate(zip(generated["tickets"], available, strict=False)):
        rid = f"COPY-{i + 1:04}"
        resource = by_invoice[b["invoice_id"]]
        finished = at + timedelta(seconds=18 + i % 30)
        event = {
            "id": rid,
            "account_id": b["account_id"],
            "contact_id": b["contact_id"],
            "order_id": b["order_id"],
            "invoice_id": b["invoice_id"],
            "requested_at": at.isoformat(),
            "completed_at": finished.isoformat(),
            "kind": "self_service_invoice_copy",
            "status": "completed",
            "resource_url": resource["url"],
            "resource_hash": resource["sha256"],
            "ticket_id": ticket["id"],
            "apps": ["hubspot_mock", "gmail_mock", "slack_mock"],
        }
        service.append(event)
        ticket.update(
            subject=f"Invoice copy — {b['invoice_id']}",
            description=f"Automated customer self-service request {rid} for a copy of {b['invoice_id']} ({b['order_id']}). The download was supplied successfully. No manual case handling was required. File: {resource['url']}",
            status="closed",
            pipeline="support",
            priority="low",
            category="billing",
            source="web",
            owner="Imani Brooks",
            contactId=b["contact_id"],
            companyId=b["account_id"],
            createDate=at.isoformat(),
            closeDate=finished.isoformat(),
            lastActivityDate=finished.isoformat(),
        )
    episode_mail = []
    canonical_episodes = []
    new_messages = {}
    new_threads = {}
    new_slack_ids = []
    # Preserve original human Slack anchors, replacing both failed bulk attempts.
    old_ids = set(entries["slack_mock"]["bulk_ids"])
    slack["messages"] = {
        k: [r for r in v if r["messageId"] not in old_ids] for k, v in slack["messages"].items()
    }
    anchor_ids = {r["messageId"] for v in slack["messages"].values() for r in v}
    slack["threads"] = {k: v for k, v in slack["threads"].items() if v["parentMessageId"] in anchor_ids}
    note_targets = generated["notes"]
    task_targets = generated["tasks"]
    meeting_by_id = {m["id"]: m for m in generated["meetings"]}
    event_by_id = {e["id"]: e for e in calendar["events"]}
    imani = {"name": "Imani Brooks", "email": "imani.brooks@cedarlineapparel.com"}

    def email(rid, thread, from_, to, subject, body, at, attachments=()):
        return dict(
            id=rid,
            threadId=thread,
            **{"from": from_},
            to=[to],
            cc=[],
            bcc=[],
            subject=subject,
            body="<p>" + html.escape(body).replace("\n", "</p><p>") + "</p>",
            timestamp=at.isoformat(),
            read=True,
            starred=False,
            important=False,
            labels=[],
            category="primary",
            folder="sent" if from_ == imani else "inbox",
            attachments=list(attachments),
        )

    for i, record in enumerate(episodes):
        case, e = record["case"], record["writing"]
        at = local(case["date"])
        account = case["account"]["id"]
        contact = case["contact"]
        eid = e["id"]
        tid = "TH-" + eid
        start = at.replace(hour=11)
        request_at = previous_workday(at).replace(hour=14)
        response_at = at.replace(hour=15, minute=10)
        note_at = at.replace(hour=15, minute=30)
        external = {"name": contact["name"], "email": contact["email"]}
        thread = "MAIL-" + eid
        episode_mail += [
            email(thread + "-REQUEST", thread, external, imani, e["title"], e["request"], request_at),
            email(
                thread + "-REPLY", thread, imani, external, "Re: " + e["title"], e["response"], response_at
            ),
        ]
        ids = []
        for j, m in enumerate(e["messages"]):
            rid = f"MSG-{eid}-{j + 1}"
            ids.append(rid)
            new_slack_ids.append(rid)
            new_messages.setdefault(case["channel"], []).append(
                {
                    "messageId": rid,
                    "senderId": m["sender"],
                    "content": m["text"],
                    "timestamp": (start + timedelta(minutes=j * (7 + i % 7))).isoformat(),
                    "threadId": tid if j else None,
                    "reactions": [],
                    "attachments": [],
                    "isEdited": False,
                }
            )
        new_threads[tid] = {
            "threadId": tid,
            "parentMessageId": ids[0],
            "channelId": case["channel"],
            "dmId": None,
            "replies": ids[1:],
            "followers": list(dict.fromkeys(m["sender"] for m in e["messages"])),
        }
        note_targets[i].update(
            body=e["note"],
            associatedType="company",
            associatedId=account,
            createdBy="Imani Brooks",
            createDate=note_at.isoformat(),
        )
        task_targets[i].update(
            title="Reply: " + e["title"],
            type="email",
            status="completed",
            priority="medium",
            dueDate=response_at.isoformat(),
            notes=e["response"],
            owner="Imani Brooks",
            contactId=contact["id"],
            companyId=account,
            dealId=None,
            createDate=request_at.isoformat(),
            completedDate=response_at.isoformat(),
        )
        mid = case.get("existing_meeting_id")
        if mid:
            m = meeting_by_id[mid]
            meeting_at = at.replace(hour=9, minute=30)
            m.update(
                title=e["title"],
                date=meeting_at.isoformat(),
                notes=e["note"],
                status="completed",
                createDate=request_at.isoformat(),
            )
            event_by_id[mid].update(
                title=e["title"],
                description="Customer request for discussion: " + e["request"],
                start=meeting_at.isoformat(),
                end=(meeting_at + timedelta(minutes=m["duration"])).isoformat(),
            )
        canonical_episodes.append(
            {
                "id": eid,
                "date": at.date().isoformat(),
                "account_id": account,
                "contact_id": contact["id"],
                "title": e["title"],
                "context": e["context"],
                "outcome": e["outcome"],
                "requested_at": request_at.isoformat(),
                "replied_at": response_at.isoformat(),
                "slack_thread_id": tid,
                "email_thread_id": thread,
                "note_id": note_targets[i]["id"],
                "task_id": task_targets[i]["id"],
                "meeting_id": mid,
                "source_order_id": (case.get("historical_order") or {}).get("Order"),
                "apps": ["hubspot_mock", "gmail_mock", "slack_mock", "google_calendar_mock"],
            }
        )
    for i, target in enumerate(note_targets[len(episodes) :]):
        event = service[i % len(service)]
        target.update(
            body=f"Automatic activity entry: self-service invoice copy request {event['id']} was fulfilled for {event['invoice_id']}. The file is available at {event['resource_url']}.",
            associatedType="company",
            associatedId=event["account_id"],
            createdBy="Order services notifications",
            createDate=event["completed_at"],
        )
    for i, target in enumerate(task_targets[len(episodes) :]):
        e = canonical_episodes[i]
        writing = episodes[i]["writing"]
        at = local(e["replied_at"]) + timedelta(minutes=25)
        target.update(
            title="Record customer follow-up: " + e["title"],
            type="to_do",
            status="completed",
            priority="low",
            dueDate=at.isoformat(),
            notes=writing["note"],
            owner="Imani Brooks",
            contactId=e["contact_id"],
            companyId=e["account_id"],
            dealId=None,
            createDate=e["requested_at"],
            completedDate=at.isoformat(),
        )
    # Each background automatic thread has an actual request and fulfillment event.
    needed_threads = 520 - len(slack["threads"]) - len(new_threads)
    for event in service[:needed_threads]:
        tid = "TH-" + event["id"]
        parent = "MSG-" + event["id"] + "-REQUEST"
        reply = "MSG-" + event["id"] + "-DONE"
        ids = [parent, reply]
        for rid, text, at in [
            (
                parent,
                f"Customer self-service: {accounts[event['account_id']]['name']} requested a copy of {event['invoice_id']}.",
                event["requested_at"],
            ),
            (reply, f"Invoice copy delivered automatically: {event['resource_url']}", event["completed_at"]),
        ]:
            new_messages.setdefault("CH-SERVICE", []).append(
                {
                    "messageId": rid,
                    "senderId": "integration-orders",
                    "content": text,
                    "timestamp": at,
                    "threadId": tid if rid == reply else None,
                    "reactions": [],
                    "attachments": [],
                    "isEdited": False,
                }
            )
            new_slack_ids.append(rid)
        new_threads[tid] = {
            "threadId": tid,
            "parentMessageId": parent,
            "channelId": "CH-SERVICE",
            "dmId": None,
            "replies": [reply],
            "followers": ["person-imani", "person-owen"],
        }
    for group, values in new_messages.items():
        slack["messages"].setdefault(group, []).extend(values)
    slack["threads"].update(new_threads)
    # Keep the remaining volume as clearly labelled notifications of known order events.
    target_messages = sum(len(v) for v in read(archive / "slack_mock.state.json")["messages"].values())
    needed = target_messages - sum(len(v) for v in slack["messages"].values())
    notices = []
    for b in sorted(bindings, key=lambda b: digest("notice:" + b["order_id"])):
        row = rows[b["order_id"]]
        for phase in ["accepted", "shipment"]:
            at = b[phase + "_at"]
            rid = f"NOTICE-{b['order_id']}-{phase}"
            notices.append(
                {
                    "messageId": rid,
                    "senderId": "integration-orders",
                    "content": f"Order services: {b['order_id']} {'accepted' if phase == 'accepted' else 'shipped'} — {accounts[b['account_id']]['name']}. {row[7]} {row[5]} {row[4]}, {row[6]}; merchandise ${row[9]:,.2f}. Invoice {b['invoice_id']}.",
                    "timestamp": at,
                    "threadId": None,
                    "reactions": [],
                    "attachments": [],
                    "isEdited": False,
                }
            )
    assert len(notices) >= needed
    slack["messages"]["CH-SERVICE"].extend(notices[:needed])
    new_slack_ids.extend(m["messageId"] for m in notices[:needed])
    for values in slack["messages"].values():
        values.sort(key=lambda m: m["timestamp"])
    # Gmail retains all anchored and transaction messages; only fabricated bulk prose is replaced.
    old_mail_ids = set(entries["gmail_mock"]["bulk_ids"])
    kept = [m for m in mail["emails"] if m["id"] not in old_mail_ids or m["id"].startswith("mail-ORD-")]
    remaining = len(mail["emails"]) - len(kept) - len(episode_mail)
    automatic = []
    for event in service:
        contact = contacts[event["account_id"]]
        external = {"name": contact["name"], "email": contact["email"]}
        robot = {"name": "Cedarline order services notifications", "email": "orders@cedarlineapparel.com"}
        resource = by_invoice[event["invoice_id"]]
        for phase, text, at in [
            (
                "REQUEST",
                f"We received your self-service request {event['id']} for invoice {event['invoice_id']} on order {event['order_id']}. An automated copy will follow.",
                event["requested_at"],
            ),
            (
                "DONE",
                f"Your requested copy of invoice {event['invoice_id']} is attached. Self-service request {event['id']} is complete. This is an automatic service receipt.",
                event["completed_at"],
            ),
        ]:
            attachments = (
                [
                    {
                        "id": "ATT-" + event["id"],
                        "name": event["invoice_id"] + ".pdf",
                        "size": resource["bytes"],
                        "type": "application/pdf",
                        "url": resource["url"],
                    }
                ]
                if phase == "DONE"
                else []
            )
            message = email(
                "MAIL-" + event["id"] + "-" + phase,
                "MAIL-" + event["id"],
                robot,
                external,
                f"Invoice copy {event['invoice_id']} — {'request received' if phase == 'REQUEST' else 'delivered'}",
                text,
                local(at),
                attachments,
            )
            message["cc"] = [imani]
            automatic.append(message)
    assert len(automatic) >= remaining
    mail["emails"] = sorted(kept + episode_mail + automatic[:remaining], key=lambda m: m["timestamp"])
    # Precise reviewer corrections to anchored role attribution and chronology.
    for r in crm["notes"]:
        if r["id"] == "H260826":
            r["body"] = (
                "Tom's web form repeats the cap cancellation email. Owen identified the duplicate; Imani linked it to the original conversation and closed SVC-106 in HubSpot. The cancellation request continues under SVC-105."
            )
    for r in crm["tickets"]:
        if r["id"] == "SVC-106":
            r["description"] = (
                "Tom also submitted the cap cancellation through the contact form. Owen identified the duplicate. Imani linked it to the email conversation under SVC-105 and closed SVC-106 on August 26. Lucia is still checking the cancellation request in the original conversation."
            )
    for r in crm["tasks"]:
        if "Owen linked" in r.get("notes", "") and "SVC-106" in r["notes"]:
            r["notes"] = r["notes"].replace("Owen linked", "Owen identified the duplicate; Imani linked")
    for values in slack["messages"].values():
        for m in values:
            if "SVC-106" in m["content"] and m["senderId"] == "person-owen":
                m["content"] = (
                    m["content"]
                    .replace("I've linked and closed", "Imani has linked and closed")
                    .replace("I linked and closed", "Imani linked and closed")
                )
            if m["messageId"] == "MSG-MC-260828-01":
                m["content"] = m["content"].replace(
                    "I've put a note in the office channel as well",
                    "I'll put a note in the office channel as well",
                )
            if m["messageId"] == "MSG-H260430-R1":
                m["content"] = (
                    "Thanks, Priya. Let's use those two filed reports when we discuss what to carry forward from the spring campaign."
                )
            if m["messageId"] == "MSG-H260814-R1":
                m["content"] = (
                    "Thanks for covering those calls. I'll check the customer notes before I pick up the afternoon follow-ups."
                )
    for r in crm["companies"]:
        r["description"] = r.get("description", "").replace("Embro embroiders", "The company embroiders")
    refresh_activity(crm)
    extension = read(work / "EXTENSION.json")
    extension["collections"]["work_episodes"] = canonical_episodes
    extension["collections"]["self_service_events"] = service
    write(work / "EXTENSION.json", extension)
    amendment = read(work / "AMENDMENTS.json")
    base = additive_world(read(folder / "world/world.json"), extension)
    amendment["parent_world_hash"] = digest(base)
    for collection, rid, field, new in [
        ("history", "H260826", "actor_id", "person-imani"),
        (
            "history",
            "H260826",
            "text",
            "The web form repeated Tom’s email request. Owen identified the duplicate; Imani linked and closed it in HubSpot, retaining the original service conversation.",
        ),
        (
            "service_items",
            "SVC-106",
            "text",
            "Tom also submitted the request through the contact form. Owen identified the duplicate; Imani linked the form to the email conversation in HubSpot.",
        ),
    ]:
        old = next(r[field] for r in base[collection] if r["id"] == rid)
        amendment["world"].append(
            {
                "collection": collection,
                "id": rid,
                "field": field,
                "before": old,
                "after": new,
                "reason": "Preserve service coordination while attributing CRM execution to its authorized operator.",
            }
        )
    for collection in ["accounts", "contacts"]:
        for r in base[collection]:
            if "Embro embroiders" in r.get("business_model", ""):
                amendment["world"].append(
                    {
                        "collection": collection,
                        "id": r["id"],
                        "field": "business_model",
                        "before": r["business_model"],
                        "after": r["business_model"].replace("Embro embroiders", "The company embroiders"),
                        "reason": "Remove duplicated business-description fragment.",
                    }
                )
    write(work / "AMENDMENTS.json", amendment)
    world = corrected_world(folder, base)
    write(work / "EFFECTIVE-WORLD.json", world)
    normalize_slack(slack)
    for a, s in states.items():
        write(folder / f"world/{a}.state.json", s)
    entries["slack_mock"]["bulk_ids"] = new_slack_ids
    entries["gmail_mock"]["bulk_ids"] = [
        m["id"]
        for m in mail["emails"]
        if m["id"] not in {m["id"] for m in kept if not m["id"].startswith("mail-ORD-")}
    ]
    config = load_config(root)
    _, contract = app_contract(root, folder)
    for app in contract:
        aid = app["app_id"]
        state = read(folder / f"world/{aid}.state.json")
        saved = entries.get(aid) or read(work / f"{aid}.json")
        saved.setdefault("generation_inputs", saved["inputs"])
        saved.update(
            inputs=app_fingerprint(
                read(folder / "world/CORE.json")["hashes"],
                world,
                read(work / "targets.json"),
                app,
                read(folder / "world/identities.json"),
                read(folder / "world/worker_apps.json"),
                config,
            ),
            state_hash=digest(state),
            bytes=len(json.dumps(state, ensure_ascii=False).encode()),
            episode_repair="world/population/episode-repair/APPLIED.json",
        )
        write(work / f"{aid}.json", saved)
    write(
        prepared / "APPLIED.json",
        {
            "at": now(),
            "world_hash": digest(world),
            "before": ready["source_states"],
            "after": {a: digest(s) for a, s in states.items()},
            "human_episodes": len(episodes),
            "automated_self_service_events": len(service),
            "human_emails": len(episode_mail),
            "automated_replacement_emails": remaining,
            "imported_order_deals": len(generated["deals"]),
            "preserved_canonical_core": True,
            "review": "pending",
            "source_batches": ready["batches"],
        },
    )
    print("Projected connected episodes and real automated event history; counts preserved.", flush=True)


if __name__ == "__main__":
    main()
