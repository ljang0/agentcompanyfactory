"""Source-bound conversation repair, without company or transaction assumptions."""

from copy import deepcopy

from pydantic import BaseModel, Field

from company_envs.storage import digest

from .population_batches import author_units
from .population_quality import instant, rows

PROMPT = """Write natural internal chat for the supplied historical work episodes.
The supplied request, reply, notes and outcomes are fixed facts. Do not invent additional
business outcomes, permissions, prices, purchases, approvals, documents or external events.
Conversations take place inside the supplied interval. Do not claim a later event already happened.
Each conversation needs 2-6 messages and at least two distinct permitted human speakers.
Use the supplied speaker roles and authority; someone who requests a decision does not gain
permission to make it. Missing authority stays unknown. Preserve the specific people and issue.
Choose the speakers, order and length that this particular exchange needs. There is no required
opening, handoff or closing message. Do not add filler just to use all speakers or hit six messages.
Use actual questions, corrections, partial replies, concrete alternatives and useful answers
where the fixed source supports them. Vary who opens and closes; use short, plain work language.
Never ask a person which thing they themselves linked, wrote or already explained. Don't narrate
someone's own actions to them in the third person. No lessons about evidentiary limits.
Return JSON only."""


class DialogueMessage(BaseModel):
    sender: str
    text: str = Field(min_length=1, max_length=600)


class Dialogue(BaseModel):
    id: str
    messages: list[DialogueMessage] = Field(min_length=2, max_length=6)


class DialogueBatch(BaseModel):
    dialogues: list[Dialogue]


def author_dialogue(cases, models, work, *, concurrency=4, batch_size=20):
    for case in cases:
        speakers = case.get("permitted_speakers")
        if not isinstance(speakers, dict) or len(speakers) < 2 or not all(speakers.values()):
            raise ValueError("Dialogue requires at least two permitted speakers with supplied roles")

    def validate(case, value):
        senders = {m["sender"] for m in value["messages"]}
        if len(senders) < 2 or senders - set(case["permitted_speakers"]):
            return ["Use at least two listed permitted human speakers"]
        return []

    batches = author_units(
        cases,
        models,
        work,
        prompt=PROMPT,
        response_type=DialogueBatch,
        field="dialogues",
        validate=validate,
        batch_size=batch_size,
        concurrency=concurrency,
    )
    return [d for b in batches for d in b["dialogues"]], batches


def dialogue_source(state, thread_id):
    """Exact native unit to inspect and hash before authoring a replacement."""
    thread = next(t for t in rows(state["threads"]) if t["threadId"] == thread_id)
    group = thread.get("channelId") or thread.get("dmId")
    records = state["messages"][group]
    ids = [thread["parentMessageId"], *thread["replies"]]
    if any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("Normalize and validate the native thread before dialogue repair")
    selected = [r for r in records if r["messageId"] in ids]
    if len(selected) != len(ids) or any(
        r.get("threadId") == thread_id and r["messageId"] not in ids for r in records
    ):
        raise ValueError("Thread index differs from its native messages")
    return {"thread": thread, "messages": selected}


def compile_dialogue(state, dialogues, bindings):
    """Replace only explicitly bound threads. Never fabricate traffic to meet a quota.

    Each binding has id, thread_id, source_hash (digest of dialogue_source),
    starts_at, ends_at, and permitted_speakers. Dates are source event boundaries,
    not a guessed work schedule. Independent records keep their IDs and content.
    """
    dialogues = DialogueBatch.model_validate({"dialogues": dialogues}).model_dump()["dialogues"]
    by_id = {b["id"]: b for b in bindings}
    if (
        len(by_id) != len(bindings)
        or len({b["thread_id"] for b in bindings}) != len(bindings)
        or len({d["id"] for d in dialogues}) != len(dialogues)
        or {d["id"] for d in dialogues} != set(by_id)
    ):
        raise ValueError("Dialogue must cover each explicitly bound thread exactly once")
    state = deepcopy(state)
    all_ids = [m["messageId"] for group in state["messages"].values() for m in group]
    if len(set(all_ids)) != len(all_ids):
        raise ValueError("Duplicate native message IDs")
    groups = {c["channelId"]: c for c in rows(state.get("channels"))}
    groups.update({c["dmId"]: c for c in rows(state.get("dms"))})
    for dialogue in dialogues:
        binding = by_id[dialogue["id"]]
        source = dialogue_source(state, binding["thread_id"])
        if digest(source) != binding["source_hash"]:
            raise ValueError(f"Dialogue source changed: {binding['id']}")
        thread = source["thread"]
        group_id = thread.get("channelId") or thread.get("dmId")
        group = groups[group_id]
        members = group["participants"] if thread.get("dmId") else group["members"]
        allowed = set(members) & set(binding["permitted_speakers"])
        speakers = {m["sender"] for m in dialogue["messages"]}
        if len(speakers) < 2 or speakers - allowed:
            raise ValueError("Dialogue needs two permitted speakers with native group access")
        start, end = instant(binding["starts_at"]), instant(binding["ends_at"])
        if group.get("createdAt"):
            start = max(start, instant(group["createdAt"]))
        count = len(dialogue["messages"])
        if (end - start).total_seconds() < count + 1:
            raise ValueError("Dialogue does not fit its source event window")
        old_ids = [thread["parentMessageId"], *thread["replies"]]
        ids = old_ids[:count] + [
            "chat-" + digest({"episode": binding["id"], "thread": binding["thread_id"], "index": i})[:24]
            for i in range(len(old_ids), count)
        ]
        if set(ids) & (set(all_ids) - set(old_ids)):
            raise ValueError("Generated message ID collides with unrelated history")
        removed = set(old_ids) - set(ids)
        if any(n.get("messageId") in removed for n in rows(state.get("notifications"))):
            raise ValueError("Removed dialogue message has a notification; repair its reference explicitly")
        original = {m["messageId"]: m for m in source["messages"]}
        new = []
        for i, message in enumerate(dialogue["messages"]):
            new.append(
                {
                    **original.get(ids[i], {}),
                    "messageId": ids[i],
                    "senderId": message["sender"],
                    "content": message["text"],
                    "timestamp": (start + (end - start) * ((i + 1) / (count + 1))).isoformat(),
                    "threadId": None if i == 0 else thread["threadId"],
                    "reactions": [],
                    "attachments": [],
                    "isEdited": False,
                }
            )
        state["messages"][group_id] = [
            m for m in state["messages"][group_id] if m["messageId"] not in old_ids
        ] + new
        thread["parentMessageId"], thread["replies"] = ids[0], ids[1:]
        all_ids = [i for i in all_ids if i not in old_ids] + ids
    return state
