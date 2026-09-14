"""Prepare one connected source of human history instead of independent generic app prose."""

import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from company_envs.config import load_config
from company_envs.models import Models
from company_envs.storage import digest, read, write
from company_envs.world.population_episodes import author_episodes

root = Path(__file__).resolve().parents[3]
folder = root / "experiments/pilot-sanmar/company"
work = folder / "world/population"
world = read(work / "EFFECTIVE-WORLD.json")
crm = read(folder / "world/hubspot_mock.state.json")
ledger = read(work / "ledger.json")
accounts = {a["id"]: a for a in world["accounts"]}
contacts = {c["account_id"]: c for c in world["contacts"]}
products = {p["id"]: p for p in world["products"]}
zone = ZoneInfo("America/Los_Angeles")
bulk = set(read(work / "hubspot_mock.json")["bulk_ids"])
meetings = [m for m in crm["meetings"] if m["id"] in bulk]
rows = random.Random(67).sample(
    [r for r in ledger["rows"] if r[2] < "2026-08-01" and int(r[1][1:]) >= 29], 240 - len(meetings)
)
cases = []
for i in range(240):
    if i < len(meetings):
        meeting = meetings[i]
        account = meeting["companyId"]
        day = datetime.fromisoformat(meeting["date"])
        day = day.replace(tzinfo=zone) if not day.tzinfo else day.astimezone(zone)
        prior = [r for r in ledger["rows"] if r[1] == account and r[2] < day.date().isoformat()]
        order = prior[-1] if prior else None
    else:
        order = rows[i - len(meetings)]
        account = order[1]
        day = datetime.fromisoformat(order[2]).replace(tzinfo=zone) + timedelta(days=12 + i % 10)
        meeting = None
    while day.weekday() > 4:
        day += timedelta(days=1)
    day = day.replace(hour=11, minute=i % 35, second=0)
    channel = "CH-SERVICE" if i % 3 == 0 else "CH-DESK"
    participants = (
        ["person-imani", "person-owen", "person-lucia", "person-gareth"]
        if channel == "CH-SERVICE"
        else ["person-mara", "person-imani", "person-owen", "person-priya"]
    )
    cases.append(
        {
            "id": f"EP-{i + 1:04}",
            "date": day.isoformat(),
            "channel": channel,
            "participants": participants,
            "account": accounts[account],
            "contact": contacts[account],
            "historical_order": dict(zip(ledger["columns"], order)) if order else None,
            "product": products[order[4]] if order else None,
            "existing_meeting_id": meeting["id"] if meeting else None,
            "staff_roles": {
                "person-mara": "desk supervisor, priorities and coverage",
                "person-imani": "customer contact and CRM operator",
                "person-owen": "order/services liaison, reconciles recorded orders",
                "person-priya": "product resources and marketing",
                "person-lucia": "central order operations",
                "person-gareth": "central credit/documentary decisions",
            },
        }
    )
models = Models(load_config(root), folder / "world", max_calls=500, cumulative=True)
output = work / "episode-repair"
output.mkdir(exist_ok=True)
batches = author_episodes(cases, models, output)
write(
    output / "RESULT.json",
    {
        "source_world_hash": digest(world),
        "source_states": {
            a: digest(read(folder / f"world/{a}.state.json"))
            for a in ["hubspot_mock", "gmail_mock", "slack_mock", "google_calendar_mock"]
        },
        "batches": [b["inputs"] for b in batches],
        "cases": cases,
    },
)
print("Prepared 240 connected episodes for explicit native projection.", flush=True)
