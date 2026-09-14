"""Grounded comment authorship: compact content in, native envelope compiled locally."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from company_envs.storage import digest, read, write

from .population_batches import author_units
from .population_quality import commenters, instant, plain, rows

COMMENT_PROMPT = """Write ordinary workplace document comments, using only each supplied document.
Return a short, specific question, clarification or editorial suggestion anchored to an exact
quote from its body. Do not invent external events, conversations, approvals, numbers or facts.
Respect the named author's role. Avoid repeating existing comments. One or two natural sentences
per comment. Preserve every supplied ID. Return only the requested content and quoted text;
code supplies identity, dates, permissions and native app fields. These are historical workplace
records, before task discovery; there are no benchmark tasks or answers to create."""

THREAD_PROMPT = """Repair a company's document discussion history using each supplied final document.
Write short, varied comments that a reader would actually leave: a concrete ambiguity, a precise
wording correction, a practical question, a useful interpretation, or an owner's terse work reminder.
Do not repeat an editorial-request skeleton across documents. Do not ask questions the quoted
sentence already answers. Owners must not sound like they are asking themselves to edit their work.
For shared documents, use the listed permitted colleagues as well as the owner; preserve private
documents' sole author. Use only permitted authors, and make every quote an exact substring.
Some discussions should have a natural reply. Replies can clarify the supplied text or describe
an editorial correction visible in the supplied final version. Mark resolved only when that reply
or the actual document answers the issue. Leave actual missing business information unresolved.
Never invent external approvals, customer outcomes, conversations, prices or changed policies.
These are ordinary document discussions before task discovery. Avoid grading language and
authoring labels. Do not pad with acknowledgements or a lecture about not assuming things.
Return all requested comment IDs exactly once; 1-2 short sentences per comment/reply, at most
two replies. Different documents should have different discussion shapes and voices."""


class ThreadReply(BaseModel):
    user_id: str
    content: str = Field(min_length=1, max_length=500)


class CommentThread(BaseModel):
    id: str
    user_id: str
    content: str = Field(min_length=1, max_length=500)
    quoted_text: str = Field(min_length=1, max_length=500)
    resolved: bool
    replies: list[ThreadReply] = Field(max_length=2)


class DocumentThreads(BaseModel):
    id: str
    comments: list[CommentThread]


class DocumentThreadBatch(BaseModel):
    documents: list[DocumentThreads]


def schedule_comment_threads(
    comments, documents, reference_date, timezone=None, *, first_date=None, last_date=None
):
    """Fit the entire discussion inside supplied history bounds, without guessing office hours."""
    comments = deepcopy(comments)
    for c in comments:
        first = instant(documents[c["docId"]]["updated"])
        if first_date:
            first = max(first, instant(first_date))
        last = min(instant(reference_date), instant(last_date)) if last_date else instant(reference_date)
        if timezone:
            first = first.astimezone(ZoneInfo(timezone))
        count = 1 + len(c.get("replies", []))
        if (last - first).total_seconds() < count + 1:
            raise ValueError(f"No comment window after document {c['docId']}")
        # Distribute roots over the supplied history, but keep a discussion
        # compact; a wide history target must not turn replies into months of silence.
        span = min((last - first) / 2, timedelta(hours=2)) if count > 1 else timedelta(0)
        weights = [1 + int(digest(f"{c['id']}:{i}")[:8], 16) % 100 for i in range(count + 1)]
        at = first + (last - first - span) * (weights[0] / 101)
        c["created"] = at.isoformat()
        for item, weight in zip(c.get("replies", []), weights[1:]):
            at += span * (weight / sum(weights[1:]))
            item["created"] = at.isoformat()
    return comments


def author_comment_threads(
    state,
    requests,
    models,
    work,
    *,
    reference_date,
    concurrency=4,
    timezone=None,
    first_date=None,
    last_date=None,
):
    """Cache and repair whole document discussions independently, then compile native fields."""
    documents = {d["id"]: d for d in rows(state["documents"])}
    users = {u["id"]: u for u in rows(state.get("users"))}
    groups = {}
    if len({c["id"] for c in requests}) != len(requests):
        raise ValueError("Comment requests need unique IDs")
    for request in requests:
        groups.setdefault(request["docId"], []).append(request)
    # Catch impossible date windows before spending a model call.
    schedule_comment_threads(
        requests, documents, reference_date, timezone, first_date=first_date, last_date=last_date
    )
    cases = [
        {
            "id": d,
            "title": documents[d]["title"],
            "body": plain(documents[d]["content"]),
            "owner_id": documents[d]["ownerId"],
            "permitted_authors": [users.get(u, {"id": u}) for u in commenters(documents[d])],
            "comment_ids": [c["id"] for c in group],
            "existing_comments": [
                c
                for c in rows(state.get("comments"))
                if c["docId"] == d and c["id"] not in {r["id"] for r in group}
            ],
        }
        for d, group in groups.items()
    ]

    def validate(case, value):
        written = {c["id"]: c for c in value["comments"]}
        errors = []
        if set(written) != set(case["comment_ids"]) or len(written) != len(value["comments"]):
            errors.append("Return exactly the requested comment IDs for this document")
        allowed = commenters(documents[case["id"]])
        for c in value["comments"]:
            if any(r["user_id"] not in allowed for r in [c, *c["replies"]]):
                errors.append(f"{c['id']}: use permitted authors only")
            if c["quoted_text"] not in case["body"]:
                errors.append(f"{c['id']}: quote must occur exactly in this document")
        return errors

    batches = author_units(
        cases,
        models,
        Path(work) / "comment-threads",
        prompt=THREAD_PROMPT + "\nReturn a documents list; each entry has its document id and comments.",
        response_type=DocumentThreadBatch,
        field="documents",
        validate=validate,
        batch_size=12,
        concurrency=concurrency,
    )
    compiled = []
    written = {c["id"]: c for b in batches for d in b["documents"] for c in d["comments"]}
    for request in requests:
        value = written[request["id"]]
        compiled.append(
            {
                **request,
                "userId": value["user_id"],
                "content": value["content"],
                "quotedText": value["quoted_text"],
                "resolved": value["resolved"],
                "replies": [
                    {
                        "id": request["id"] + f"-reply-{i + 1}",
                        "userId": r["user_id"],
                        "content": r["content"],
                        "created": "",
                    }
                    for i, r in enumerate(value["replies"])
                ],
            }
        )
    return schedule_comment_threads(
        compiled, documents, reference_date, timezone, first_date=first_date, last_date=last_date
    ), batches


class CommentText(BaseModel):
    id: str
    content: str = Field(min_length=1, max_length=650)
    quoted_text: str = Field(min_length=1, max_length=500)


class CommentBatch(BaseModel):
    comments: list[CommentText]


def author_comments(state, requests, models, work, *, concurrency=4):
    """Return grounded native comments and provenance. Saved batches survive interruption."""
    documents = {d["id"]: d for d in rows(state["documents"])}
    users = {u["id"]: u for u in rows(state.get("users"))}
    requests = deepcopy(requests)
    for request in requests:
        doc = documents[request["docId"]]
        if request.get("userId") not in commenters(doc):
            request["userId"] = doc["ownerId"]
        # There is no earlier revision body in this contract. A newly authored comment
        # can only refer to the supplied version once that version exists.
        if instant(request["created"]) < instant(doc["updated"]):
            request["created"] = doc["updated"]
        # Existing reply bodies need their own grounded authoring; never silently retain them.
        if request.get("replies"):
            raise ValueError(f"comment with existing replies needs separate repair: {request['id']}")

    def batch(offset):
        selected = requests[offset : offset + 20]
        selected_ids = {c["id"] for c in selected}
        doc_ids = {c["docId"] for c in selected}
        body = {
            "documents": [
                {"id": d, "title": documents[d]["title"], "body": plain(documents[d]["content"])}
                for d in sorted(doc_ids)
            ],
            "requests": [
                {
                    "id": c["id"],
                    "docId": c["docId"],
                    "author": users.get(c["userId"], {"id": c["userId"]}),
                    "date": c["created"],
                }
                for c in selected
            ],
            "existing_comments": [
                {k: c[k] for k in ("id", "docId", "content")}
                for c in rows(state.get("comments"))
                if c["docId"] in doc_ids and c["id"] not in selected_ids
            ],
        }
        inputs = digest({"instructions": COMMENT_PROMPT, "body": body, "envelopes": selected})
        path = work / "comments" / f"{inputs}.json"
        if path.exists():
            saved = read(path)
            if saved.get("output_hash") != digest(saved.get("comments")):
                raise ValueError("grounded comment checkpoint drift")
            return saved
        receipts = []
        for attempt in range(3):
            result, receipt = models.call(
                "world_states", COMMENT_PROMPT + "\n" + json.dumps(body, ensure_ascii=False), CommentBatch
            )
            receipts.append(receipt)
            written = {c.id: c for c in result.comments}
            errors = []
            if set(written) != selected_ids or len(written) != len(result.comments):
                errors.append("Return exactly the requested unique comment IDs.")
            for request in selected:
                text = written.get(request["id"])
                if text and text.quoted_text not in plain(documents[request["docId"]]["content"]):
                    errors.append(
                        f"{request['id']}: quoted_text must be an exact substring of its document body."
                    )
            if errors:
                body["revision_feedback"] = errors
                continue
            comments = [
                {**c, "content": written[c["id"]].content, "quotedText": written[c["id"]].quoted_text}
                for c in selected
            ]
            saved = {
                "inputs": inputs,
                "comments": comments,
                "output_hash": digest(comments),
                "receipts": receipts,
                "document_hashes": {d: digest(documents[d]) for d in doc_ids},
            }
            write(path, saved)
            return saved
        raise ValueError(f"grounded comment batch {offset} failed after two repair rounds: {errors}")

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        batches = list(pool.map(batch, range(0, len(requests), 20)))
    return [c for b in batches for c in b["comments"]], batches


def new_comment_requests(state, count, first_date, last_date):
    """Choose stable IDs and permitted authors before asking for any prose."""
    documents = sorted(rows(state["documents"]), key=lambda d: d["id"])
    documents = [d for d in documents if max(instant(d["updated"]), instant(first_date)) < instant(last_date)]
    if not documents:
        raise ValueError("no documents exist inside the comment window")
    existing = {c["id"] for c in rows(state.get("comments"))}
    requests, index = [], 1
    while len(requests) < count:
        rid = f"CMT-{index:05d}"
        index += 1
        if rid in existing:
            continue
        doc = documents[len(requests) % len(documents)]
        earliest = max(instant(first_date), instant(doc["updated"]))
        latest = instant(last_date)
        date = earliest + (latest - earliest) * ((len(requests) % 7 + 1) / 8)
        people = commenters(doc)
        requests.append(
            {
                "id": rid,
                "docId": doc["id"],
                "userId": people[len(requests) % len(people)],
                "content": "",
                "quotedText": "",
                "created": date.isoformat(),
                "resolved": False,
                "replies": [],
            }
        )
    return requests
