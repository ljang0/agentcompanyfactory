"""Bridge Gmail's UI draft folder and the declared separate native draft collection."""

from copy import deepcopy


def browser_mail(state):
    if not isinstance(state.get("emails"), list) or not isinstance(state.get("drafts"), list):
        return state
    state = deepcopy(state)
    existing = {r.get("id") for r in state["emails"]}
    state["emails"].extend({**r, "folder": "drafts"} for r in state["drafts"] if r.get("id") not in existing)
    return state


def canonical_mail(state):
    if not isinstance(state.get("emails"), list) or not isinstance(state.get("drafts"), list):
        return state
    state = deepcopy(state)
    state["drafts"] = [r for r in state["emails"] if r.get("folder") == "drafts"]
    state["emails"] = [r for r in state["emails"] if r.get("folder") != "drafts"]
    return state
