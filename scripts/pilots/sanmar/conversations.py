"""Repair the pilot's generic Slack chatter from actual business records, without reseeding."""

import json
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field

from company_envs.config import load_config
from company_envs.models import Models
from company_envs.storage import digest, read, write
from company_envs.world.population_quality import plain
from company_envs.world.seed_calls import Contract


class Message(Contract):
    sender: str
    text: str = Field(min_length=3, max_length=750)


class Conversation(Contract):
    id: str
    messages: list[Message]


class Conversations(Contract):
    conversations: list[Conversation]


PROMPT = """Write brief, natural historical workplace Slack conversations from the supplied sources.
These are ordinary records before task discovery. Preserve source facts, names, quantities, dates,
prices, rights and authority. People ask a specific question, answer it, and sometimes clarify or
acknowledge. Every reply must answer the preceding exchange. Vary openings, length and resolution;
never repeat generic instructions about recording evidence, handoffs, retaining notes or passing
context. No benchmark language. Do not invent a customer promise, approval, discount, shipment
status, performance number or policy. Questions, proposals and small administrative actions are
fine. A past order can be discussed retrospectively but its status/amount cannot change.
Use only the supplied participants; central operations alone executes order acceptance and credit.
Only Imani operates CRM, Owen handles order records, Priya operates advertising, Mara sets priorities.
For document discussions use specific content in that document. Its source is visible to the
supplied participants for this conversation. Exactly the requested message count in each case,
usually one or two sentences per message. Return IDs, sender IDs and text only. Code supplies native
IDs, timestamps, thread indexes and access. Each case is unrelated unless its source says otherwise."""


def main():
    root = Path(__file__).resolve().parents[3]
    folder = root / "experiments/pilot-sanmar/company"
    work = folder / "world/population"
    slack = read(folder / "world/slack_mock.state.json")
    world = read(work / "EFFECTIVE-WORLD.json")
    accounts = {r["id"]: r for r in world["accounts"]}
    products = {r["id"]: r for r in world["products"]}
    ledger = read(work / "ledger.json")
    rows = ledger["rows"]
    rng = random.Random(417)
    source_rows = rng.sample([r for r in rows if r[2] < "2026-08-01"], 360)
    docs = read(folder / "world/google_docs_mock.state.json")["documents"]
    requests = []
    zone = ZoneInfo("America/Los_Angeles")
    for i in range(515):
        n = [2, 3, 4, 5, 3, 4, 6, 3][i % 8]
        if i < 360:
            row = source_rows[i]
            channel = "CH-SERVICE" if i % 2 else "CH-DESK"
            people = (
                ["person-imani", "person-owen", "person-lucia", "person-gareth"]
                if i % 2
                else ["person-mara", "person-imani", "person-owen", "person-priya"]
            )
            day = datetime.fromisoformat(row[2]).replace(tzinfo=zone) + timedelta(days=10 + i % 13)
            source = {
                "order": dict(zip(ledger["columns"], row)),
                "account": accounts[row[1]],
                "product": products[row[4]],
                "discussion": "Retrospective order question, assortment wording, invoice-line explanation or internal coverage for customer questions. Do not imply a new transaction.",
            }
        else:
            doc = list(docs.values())[(i - 360) % len(docs)]
            people = list(dict.fromkeys([doc["ownerId"]] + [r["userId"] for r in doc.get("sharedWith", [])]))
            # Public office channel for office coordination; resource/desk documents remain in their channel.
            people = [p for p in people if any(u["userId"] == p for u in slack["users"])]
            if len(people) < 2:
                doc = docs["DOC-WELCOME"]
                people = ["person-mara", "person-imani", "person-owen", "person-priya"]
            channel = "CH-RESOURCES" if i % 3 else "CH-DESK"
            members = next(c["members"] for c in slack["channels"] if c["channelId"] == channel)
            people = [p for p in people if p in members]
            if len(people) < 2:
                doc = docs["DOC-WELCOME"]
                people = ["person-mara", "person-imani", "person-owen", "person-priya"]
                channel = "CH-DESK"
            day = datetime.fromisoformat(doc["updated"])
            day = day.replace(tzinfo=zone) if not day.tzinfo else day
            day = min(day + timedelta(days=i % 4), datetime(2026, 8, 31, tzinfo=zone))
            source = {
                "document_id": doc["id"],
                "title": doc["title"],
                "content": plain(doc["content"]),
                "discussion": "A substantive question about this document, suggested clarification or review comment. Do not invent changed company policy or additional approvals.",
            }
        while day.weekday() > 4:
            day += timedelta(days=1)
        day = day.replace(hour=9 + (i % 7), minute=(i * 17) % 45, second=0)
        requests.append(
            {
                "id": f"CHAT-{i + 1:04}",
                "message_count": n,
                "date": day.isoformat(),
                "channel": channel,
                "participants": people,
                "source": source,
            }
        )
    models = Models(load_config(root), folder / "world", max_calls=500, cumulative=True)
    output = work / "conversation-repair"
    output.mkdir(exist_ok=True)

    def batch(offset):
        selected = requests[offset : offset + 20]
        body = {"cases": selected}
        key = digest({"prompt": PROMPT, "body": body})
        path = output / f"{key}.json"
        if path.exists():
            saved = read(path)
            if saved["output_hash"] != digest(saved["conversations"]):
                raise ValueError("conversation checkpoint drift")
            return saved
        receipts = []
        for attempt in range(3):
            value, receipt = models.call("world_states", PROMPT + "\n" + json.dumps(body), Conversations)
            receipts.append(receipt)
            mapping = {c.id: c for c in value.conversations}
            errors = []
            if set(mapping) != {c["id"] for c in selected} or len(mapping) != len(value.conversations):
                errors.append("Return every requested case ID exactly once.")
            for case in selected:
                c = mapping.get(case["id"])
                if not c:
                    continue
                if len(c.messages) != case["message_count"]:
                    errors.append(case["id"] + ": wrong message count")
                if any(m.sender not in case["participants"] for m in c.messages):
                    errors.append(case["id"] + ": use supplied participants only")
                if len({m.sender for m in c.messages}) < 2:
                    errors.append(case["id"] + ": at least two people must participate")
            if not errors:
                break
            body["repair"] = errors
        if errors:
            raise ValueError(errors)
        payload = [c.model_dump() for c in value.conversations]
        saved = {
            "inputs": key,
            "cases": selected,
            "conversations": payload,
            "output_hash": digest(payload),
            "receipts": receipts,
        }
        write(path, saved)
        print(f"conversations {offset + 1}-{offset + len(selected)} saved", flush=True)
        return saved

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(batch, range(0, len(requests), 20)))
    write(
        output / "RESULT.json",
        {
            "source_state_hash": digest(slack),
            "batches": [r["inputs"] for r in results],
            "conversations": sum(len(r["conversations"]) for r in results),
            "requests": requests,
        },
    )
    print("Prepared 515 grounded conversations; not applied.", flush=True)


if __name__ == "__main__":
    main()
