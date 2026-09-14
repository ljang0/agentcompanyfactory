"""Stage 3 contract, versioned independently from the accepted Stage 2 skill."""

import ast
from pathlib import Path

from company_envs.storage import digest

from .bulk_layer import BULK_VERSION, max_bytes_for
from .state_seed import project_world

NATIVE_RULES = """Populate native workplace apps from the accepted shared world and explicit additions.
The world is the source of truth. Preserve IDs, names, dates, amounts, policies, relationships,
and worker identities. Do not introduce contradictory copies of the same event or document.
Preserve each worker's apps, access grants, private material, expertise and central-office authority.
Work crosses desks through ordinary requests and handoffs; do not give every worker every fact.
Author connected work episodes before projecting substantive content into multiple apps. An episode
records the customer need, obstacle, action and outcome once; its email, chat, CRM note and meeting
recap must agree. Keep authoring labels out of worker records. Invitations state the discussion purpose;
meeting notes use the participant's actual account of the conversation and dated follow-ups.
When importing won orders, clear incompatible loss reasons. File outgoing customer replies as sent.
Supply missing noncontradictory business context explicitly instead of making staff
quiz one another about known facts. Distinguish human work from labelled automated event imports.
Write complete, readable ordinary work: specific correspondence, documents, unresolved business
matters and history across the requested period. Use supplied source records for business facts.
Different authors and kinds of work should sound different. Do not invent execution authority,
approvals, campaign performance or completed benchmark deliverables. Task discovery happens later.
Return only the requested collections in the exact native state schema. Keep supplied records
unchanged. Use stable native IDs and supplied references. UI defaults should be minimal and valid.
Use concise rationale and complete useful content. Count targets describe volume; byte limits are
ceilings, never writing targets. A compact template may describe genuinely recurring traffic;
substantive documents and discussions require literal content grounded in the supplied material.
Bulk templates must never impersonate substantive human conversations, meeting outcomes or notes.
Use them for explicit automated events and real recurring schedules only. Prefer deterministic
imports from actual orders, requests and deliveries; do not invent separate completed sales.
For ordinary office recurrences set weekdays_only unless weekend work is explicitly supported.
For bulk specifications, correlate fields through the same table row. Keep dates inside the
window and respect cadence. Return only the requested typed output, without restating the input.
"""


def app_fingerprint(core_hashes, world, plan, app, identities, grants, config):
    """An app depends on its world view and compiler, not another app's schema or budget."""
    directory = Path(__file__).parent
    implementation = {
        name: digest(ast.dump(ast.parse((directory / name).read_text()), include_attributes=False))
        for name in (
            "population.py",
            "population_contract.py",
            "population_comments.py",
            "population_batches.py",
            "population_records.py",
            "world_review.py",
            "population_derived.py",
            "population_directories.py",
            "population_quality.py",
            "population_amendments.py",
            "population_activity.py",
            "population_episodes.py",
            "population_dialogue.py",
            "bulk.py",
            "bulk_layer.py",
            "state_seed.py",
            "seed_calls.py",
        )
    }
    aid = app["app_id"]
    return digest(
        {
            "core": core_hashes,
            "world": project_world(world, aid),
            "app": app,
            "plan": [p for p in plan if p["app_id"] == aid],
            "identities": {w: ids.get(aid) for w, ids in identities.items() if aid in grants.get(w, [])},
            "worker_apps": grants,
            "seed": config["generation"].get("seed", 0),
            "max_bytes": max_bytes_for(config, aid),
            "bulk_version": BULK_VERSION,
            "instructions": NATIVE_RULES,
            "implementation": implementation,
        }
    )
