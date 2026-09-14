"""Count native records and continue literal authorship without replacing saved work."""

import json

from .seed_calls import AppStateResult, skill_for_call
from .state_seed import bound_states, merge_records, parse_json, record_count, record_ids


def missing_targets(state, plan, app_id):
    def count(collection):
        value = state.get(collection)
        ids = record_ids(value)
        return len(set(ids)) if ids else record_count(value)

    return [
        {
            **p,
            "existing_records": count(p["collection"]),
            "missing_records": max(0, p["target_records"] - count(p["collection"])),
        }
        for p in plan
        if p["app_id"] == app_id and count(p["collection"]) < p["target_records"]
    ]


def author_records(state, target, payload, *, models, instructions, save_partial):
    """Fill a collection in bounded batches; incomplete output remains a shortfall."""
    app_id, collection = target["app_id"], target["collection"]
    pending = missing_targets(state, [target], app_id)
    if not pending:
        return []
    receipts = []
    for batch in range((pending[0]["missing_records"] + 19) // 20 + 2):
        pending = missing_targets(state, [target], app_id)
        if not pending:
            break
        count = min(20, pending[0]["missing_records"])
        authored = bound_states(
            {app_id: state},
            limit=150_000,
            seed=f"population:{app_id}:{collection}:{pending[0]['existing_records']}",
        )[app_id]
        body = {
            **payload,
            "already_authored_collections": authored,
            "population_batch": {
                "count": count,
                "index": batch,
                "existing_ids": record_ids(state[collection]),
                "window": [target["first_date"], target["last_date"]],
            },
            "output_contract": (
                f"Return exactly {count} NEW literal {collection} records in the native collection shape. "
                "Use distinct supported matters and dates from the canonical records and history. "
                "Write complete readable content, preserving supplied identities, facts and authority. "
                "Use ordinary requests, answers, coordination and the recorded completed history. "
                "Replies should advance the conversation. Do not turn known facts into hypothetical "
                "review exercises or add disclaimers about what a message does not prove. "
                "No templates, invented business outcomes, task answers or repeated paraphrases of "
                "existing records. Existing records are immutable; use unique new IDs. References must "
                "resolve to supplied native records in already_authored_collections. "
                "That context may be sampled; never copy sampling markers into output. "
                "Follow the target's purpose and record unit; calendar recurrence occurrences do not "
                "become separate appointments merely to meet a count."
            ),
        }
        result, receipt = models.call(
            "world_states",
            skill_for_call(instructions, "app_state") + "\n" + json.dumps(body),
            AppStateResult,
        )
        addition = parse_json(result.state_json, f"{app_id} batch", expected=[collection])
        state[collection] = merge_records(state[collection], addition[collection])
        receipts.append(receipt)
        save_partial()
    return receipts
