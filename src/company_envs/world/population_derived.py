"""Repair only native fields whose value follows from existing source records."""

from copy import deepcopy

from .population_activity import refresh_activity
from .population_quality import instant, normalize_slack, rows


def field_changes(before, after, path=()):
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict) and before.keys() == after.keys():
        return [c for k in before for c in field_changes(before[k], after[k], (*path, k))]
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [c for i, (a, b) in enumerate(zip(before, after)) for c in field_changes(a, b, (*path, i))]
    return [{"path": list(path), "before": before, "after": after}]


def repair_derived_state(app_id, state):
    """Return a copy and exact old/new edits. Never author prose or business outcomes."""
    candidate = deepcopy(state)
    if app_id == "hubspot_mock":
        refresh_activity(candidate)
    elif app_id == "slack_mock":
        normalize_slack(candidate)
        for dm in rows(candidate.get("dms")):
            messages = [m for m in candidate.get("messages", {}).get(dm["dmId"], []) if m.get("timestamp")]
            if messages:
                latest = max(reversed(messages), key=lambda m: instant(m["timestamp"]))
                dm.update(lastMessage=latest.get("content", ""), lastTime=latest["timestamp"])
    return candidate, field_changes(state, candidate)
