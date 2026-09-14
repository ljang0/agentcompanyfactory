"""Author connected work episodes once, then project their facts into native apps."""

from pydantic import Field

from .population_batches import author_units
from .seed_calls import Contract

PROMPT = """Create ordinary historical work episodes for the supplied company and operating context.
The supplied records are fixed facts, not the whole story: add a plausible concrete customer need,
obstacle, choice and follow-through that DOES NOT contradict those facts. Put the new facts in the
context field without authoring labels. These episodes become canonical history used by every app projection.
A customer actually needs something; staff do work to resolve it. Do not write factual quizzes,
product-definition lessons, invoice arithmetic exercises, generic filing/handoff reminders, or staff
repeating job responsibilities. Show the reason for the exchange and a practical decision or next step.
Use varied business situations grounded in the supplied organizations, records and staff roles.
Only supplied participants may act. Their stated responsibilities, access and authority are limits;
do not assign a specialist's decision to a colleague who can only request it. Do not invent completed
transactions, approvals, prices, authority, policy, staff or performance. Preserve the actual status
of every source record; do not presume that a historical record is completed. Use no unsupported
numerical claim. Missing authority or an unresolved outcome stays unresolved.
All customer/staff messages, notes and outcomes must describe the SAME episode, with consistent facts.
The customer request precedes the conversation; the staff response and CRM note follow it. A completed
meeting may leave follow-up work open. No later events may be cited as known earlier.
These are regular staff, not trainees: use short concrete messages, varying voice and length.
No task creation, grading language, benchmark answers, or explanations of the generation process.
Return exactly the requested case IDs, with 2-6 messages and at least two allowed participants.
If a case explicitly supplies a message_count, honor it; otherwise choose the natural length.
Vary the opening speaker and exchange. Do not repeat an intake, revised-worksheet, send-it script.
No compulsory closing acknowledgement, procedural lesson or statement of evidentiary limits.
The request and response are plain-text email bodies (no invented signatures/names); code supplies
sender/recipient identities, IDs, times, formatting and linkage. The outcome must support the response.
Context: one short paragraph of new facts. Outcome: one short paragraph. Email bodies: 2-5 sentences.
Chat messages: normally 1-2 sentences each. CRM note: one compact factual paragraph, not a script.
"""


class EpisodeMessage(Contract):
    sender: str
    text: str = Field(min_length=3, max_length=700)


class WorkEpisode(Contract):
    id: str
    title: str = Field(min_length=4, max_length=120)
    context: str = Field(min_length=30, max_length=1200)
    outcome: str = Field(min_length=20, max_length=1000)
    request: str = Field(min_length=30, max_length=1200)
    response: str = Field(min_length=30, max_length=1200)
    note: str = Field(min_length=25, max_length=1000)
    messages: list[EpisodeMessage]


class EpisodeBatch(Contract):
    episodes: list[WorkEpisode]


def author_episodes(cases, models, work, *, batch_size=12, concurrency=4):
    for case in cases:
        if len(set(case["participants"])) < 2 or any(
            not case.get("staff_roles", {}).get(p) for p in case["participants"]
        ):
            raise ValueError("Episodes require at least two participants with supplied roles")

    def validate(case, value):
        messages = value["messages"]
        senders = {m["sender"] for m in messages}
        if (
            not 2 <= len(messages) <= 6
            or case.get("message_count") is not None
            and len(messages) != case["message_count"]
            or len(senders) < 2
            or senders - set(case["participants"])
        ):
            return ["Use 2-6 messages, any explicit count, and at least two permitted participants"]
        return []

    return author_units(
        cases,
        models,
        work,
        prompt=PROMPT,
        response_type=EpisodeBatch,
        field="episodes",
        validate=validate,
        batch_size=batch_size,
        concurrency=concurrency,
    )
