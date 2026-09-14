"""Apply reviewed-source conversation outputs and compile honest routine event notifications."""

import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from company_envs.config import load_config
from company_envs.storage import digest, now, read, write
from company_envs.world.population_contract import app_fingerprint
from company_envs.world.population_quality import normalize_slack
from company_envs.world.state_seed import app_contract


def main():
    root = Path(__file__).resolve().parents[3]
    folder = root / "experiments/pilot-sanmar/company"
    work = folder / "world/population"
    prepared = work / "conversation-repair"
    result = read(prepared / "RESULT.json")
    state = read(folder / "world/slack_mock.state.json")
    if result["source_state_hash"] != digest(state):
        raise ValueError("Slack source changed during conversation repair")
    original = deepcopy(state)
    entry = read(work / "slack_mock.json")
    old_bulk = set(entry["bulk_ids"])
    target = sum(len(v) for v in state["messages"].values())
    state["messages"] = {
        k: [r for r in v if r["messageId"] not in old_bulk] for k, v in state["messages"].items()
    }
    remaining_ids = {r["messageId"] for v in state["messages"].values() for r in v}
    state["threads"] = {k: v for k, v in state["threads"].items() if v["parentMessageId"] in remaining_ids}
    zone = ZoneInfo("America/Los_Angeles")
    new_ids = []
    sources = []
    for key in result["batches"]:
        batch = read(prepared / f"{key}.json")
        if batch["output_hash"] != digest(batch["conversations"]):
            raise ValueError("Conversation output drift")
        cases = {r["id"]: r for r in batch["cases"]}
        for conversation in batch["conversations"]:
            case = cases[conversation["id"]]
            tid = "TH-" + case["id"]
            ids = []
            for i, message in enumerate(conversation["messages"]):
                rid = f"MSG-{case['id']}-{i + 1}"
                ids.append(rid)
                new_ids.append(rid)
                at = datetime.fromisoformat(case["date"]) + timedelta(
                    minutes=i * (7 + int(case["id"][-2:]) % 13)
                )
                state["messages"].setdefault(case["channel"], []).append(
                    {
                        "messageId": rid,
                        "senderId": message["sender"],
                        "content": message["text"],
                        "timestamp": at.isoformat(),
                        "threadId": tid if i else None,
                        "reactions": [],
                        "attachments": [],
                        "isEdited": False,
                    }
                )
            state["threads"][tid] = {
                "threadId": tid,
                "parentMessageId": ids[0],
                "channelId": case["channel"],
                "dmId": None,
                "replies": ids[1:],
                "followers": list(dict.fromkeys(m["sender"] for m in conversation["messages"])),
            }
            sources.append({"thread_id": tid, "source": case["source"], "batch": key})
    calendar = read(folder / "world/google_calendar_mock.state.json")
    calendar_changes = []
    for event in calendar["events"]:
        if (
            event["id"].startswith("MEET-CLCAL-OFFICEWALK-")
            and datetime.fromisoformat(event["start"]).weekday() > 4
        ):
            calendar_changes.append(deepcopy(event))
            for key in ["start", "end"]:
                value = datetime.fromisoformat(event[key])
                event[key] = (value - timedelta(days=value.weekday() - 4)).isoformat()
    mail = read(folder / "world/gmail_mock.state.json")
    mail_before = digest(mail)
    bindings = read(work / "transactions/BINDINGS.json")
    by_order = {b["order_id"]: b for b in bindings}
    for m in mail["emails"]:
        if not m["id"].startswith("mail-ORD-"):
            continue
        minute = int(digest(m["id"])[:8], 16) % (7 * 60)
        value = datetime.fromisoformat(m["timestamp"]).replace(
            tzinfo=None, hour=9, minute=0, second=0
        ) + timedelta(minutes=minute)
        m["timestamp"] = value.replace(tzinfo=zone).isoformat()
        oid = m["id"][5:].rsplit("-", 1)[0]
        if oid in by_order:
            by_order[oid]["accepted_at" if m["id"].endswith("-confirmation") else "shipment_at"] = m[
                "timestamp"
            ]
    for uid, label in [
        ("integration-orders", "Order services notifications"),
        ("integration-calendar", "Calendar reminders"),
    ]:
        user = deepcopy(state["users"][0])
        user.update(
            userId=uid,
            fullName=label,
            displayName=label,
            email=f"{uid}@cedarlineapparel.com",
            title="Automated integration",
            statusMessage="Automated notices from recorded company events",
            avatar="",
        )
        state["users"].append(user)
    next(c for c in state["channels"] if c["channelId"] == "CH-SERVICE")["members"].append(
        "integration-orders"
    )
    next(c for c in state["channels"] if c["channelId"] == "CH-OFFICE")["members"].append(
        "integration-calendar"
    )
    notices = []
    roster = {
        "mara.ellison@cedarlineapparel.com",
        "imani.brooks@cedarlineapparel.com",
        "owen.delgado@cedarlineapparel.com",
        "priya.nair@cedarlineapparel.com",
    }
    for e in calendar["events"]:
        if roster.issubset(set(e.get("guests", []))) and e["start"] < "2026-09":
            time = datetime.fromisoformat(e["start"])
            time = time.replace(tzinfo=zone) if not time.tzinfo else time.astimezone(zone)
            notices.append(
                (
                    "CH-OFFICE",
                    {
                        "messageId": "NOTICE-" + e["id"],
                        "senderId": "integration-calendar",
                        "content": f"Calendar reminder: {e['title']} at {time:%H:%M} on {time:%B %d, %Y}. {e.get('location', '')}",
                        "timestamp": (time - timedelta(minutes=30)).isoformat(),
                        "threadId": None,
                        "reactions": [],
                        "attachments": [],
                        "isEdited": False,
                    },
                )
            )
    world = read(work / "EFFECTIVE-WORLD.json")
    accounts = {a["id"]: a for a in world["accounts"]}
    for b in sorted(bindings, key=lambda b: digest(b["order_id"])):
        for event in ["accepted", "shipment"]:
            at = b.get(event + "_at")
            if not at or at[:10] > "2026-08-31":
                continue
            status = "accepted" if event == "accepted" else "shipped"
            text = f"Order services: {b['order_id']} {status} · {accounts[b['account_id']]['name']} · {b['quantity']} units · merchandise ${b['line_amount']:,.2f}."
            if event == "shipment":
                text += f" Invoice {b['invoice_id']}."
            notices.append(
                (
                    "CH-SERVICE",
                    {
                        "messageId": f"NOTICE-{b['order_id']}-{event}",
                        "senderId": "integration-orders",
                        "content": text,
                        "timestamp": (datetime.fromisoformat(at) + timedelta(minutes=1)).isoformat(),
                        "threadId": None,
                        "reactions": [],
                        "attachments": [],
                        "isEdited": False,
                    },
                )
            )
    missing = target - sum(len(v) for v in state["messages"].values())
    if len(notices) < missing:
        raise ValueError("Not enough genuine routine events")
    for channel, message in notices[:missing]:
        state["messages"].setdefault(channel, []).append(message)
        new_ids.append(message["messageId"])
    for messages in state["messages"].values():
        messages.sort(key=lambda m: m["timestamp"])
    normalize_slack(state)
    assert sum(len(v) for v in state["messages"].values()) == target and len(state["threads"]) == 520
    archive = work / "conversation-repair/before"
    archive.mkdir(exist_ok=True)
    write(archive / "slack_mock.state.json", original)
    write(archive / "google_calendar_mock.state.json", read(folder / "world/google_calendar_mock.state.json"))
    write(archive / "gmail_mock.state.json", read(folder / "world/gmail_mock.state.json"))
    write(archive / "BINDINGS.json", read(work / "transactions/BINDINGS.json"))
    write(folder / "world/slack_mock.state.json", state)
    write(folder / "world/google_calendar_mock.state.json", calendar)
    write(folder / "world/gmail_mock.state.json", mail)
    write(work / "transactions/BINDINGS.json", bindings)
    config = load_config(root)
    _, contract = app_contract(root, folder)
    for app in contract:
        aid = app["app_id"]
        native = read(folder / f"world/{aid}.state.json")
        saved = read(work / f"{aid}.json")
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
            state_hash=digest(native),
            bytes=len(json.dumps(native, ensure_ascii=False).encode()),
        )
        if aid == "slack_mock":
            saved.update(
                bulk_ids=new_ids, conversation_repair="world/population/conversation-repair/APPLIED.json"
            )
        write(work / f"{aid}.json", saved)
    write(prepared / "SOURCES.json", sources)
    write(
        prepared / "APPLIED.json",
        {
            "at": now(),
            "source_state_hash": digest(original),
            "output_state_hash": digest(state),
            "threads_authored": 515,
            "retained_anchor_messages": len(remaining_ids),
            "messages": target,
            "automated_notices": missing,
            "source_bindings": "SOURCES.json",
            "calendar_weekend_repairs": calendar_changes,
            "mail_timestamps_before": mail_before,
            "mail_timestamps_after": digest(mail),
            "status": "applied_for_independent_review",
        },
    )
    print(
        "Applied grounded conversations, event notifications, transaction timestamps and four workday corrections."
    )


if __name__ == "__main__":
    main()
