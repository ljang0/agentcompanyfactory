"""Add missing synthetic CRM profile facts without rewriting the reviewed core."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import Field

from company_envs.config import load_config
from company_envs.models import Models
from company_envs.storage import digest, now, write
from company_envs.world.blueprint import load_core
from company_envs.world.seed_calls import Contract


class Profile(Contract):
    account_id: str
    domain: str
    city: str
    state: str
    business_model: str
    contact_name: str
    contact_email: str
    contact_job_title: str


class Profiles(Contract):
    profiles: list[Profile] = Field(min_length=1)


def main():
    root = Path(__file__).resolve().parents[3]
    folder = root / "experiments/pilot-sanmar/company"
    core, _ = load_core(root, folder)
    world = json.loads(core.entities_json)
    config = load_config(root)
    models = Models(config, folder / "world", max_calls=config["design"]["seed_call_budget"], cumulative=True)
    contacts = {c["account_id"]: c for c in world["contacts"]}
    batches = [world["accounts"][i : i + 55] for i in range(0, 220, 55)]

    def author(item):
        index, accounts = item
        payload = {
            "call": "additive_customer_profiles",
            "company": world["company"],
            "accounts": accounts,
            "existing_contacts": [contacts[a["id"]] for a in accounts if a["id"] in contacts],
            "rules": "Supply one concise plausible synthetic profile per supplied account, preserving IDs, existing contact name/email and business model exactly. These are resellers and prospects for a blank-apparel wholesaler, mostly western US decorators, promotional-product agencies and uniform providers. Vary real business models and locations. Retain ineligible prospect business models such as direct consumer use. Use ordinary names and credible fictional business domains; do not research or contact anyone. No task creation, qualification conclusions, invented sales outcomes, approvals, or explanatory prose. Domain must agree with contact email. Existing facts are immutable. Return only requested profile fields.",
        }
        result, receipt = models.call("world_states", json.dumps(payload), Profiles)
        values = [p.model_dump() for p in result.profiles]
        if {v["account_id"] for v in values} != {a["id"] for a in accounts} or len(values) != len(accounts):
            raise ValueError("profile batch changed the account population")
        for v in values:
            existing = contacts.get(v["account_id"])
            if existing and any(
                e in existing and v[k] != existing[e]
                for k, e in (
                    ("contact_name", "name"),
                    ("contact_email", "email"),
                    ("business_model", "business_model"),
                )
            ):
                raise ValueError(f"profile changed existing facts: {v['account_id']}")
            if v["contact_email"].split("@")[-1] != v["domain"]:
                raise ValueError("contact email and company domain disagree")
        write(folder / f"world/population/profiles-{index}.json", {"profiles": values, "receipt": receipt})
        print(f"profiles {index}: {len(values)} accounts", flush=True)
        return values

    with ThreadPoolExecutor(max_workers=4) as pool:
        profiles = [p for group in pool.map(author, enumerate(batches)) for p in group]
    write(
        folder / "world/population/PROFILES.json",
        {"parent_world_hash": digest(world), "created_at": now(), "profiles": profiles},
    )


if __name__ == "__main__":
    main()
