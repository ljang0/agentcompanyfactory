"""Preserve all drafts consumed by repairs; distinguish model from session separation."""

from .storage import digest, read, write


def author_receipts(directory):
    path = directory / "authors.json"
    receipts = read(path).get("receipts", []) if path.exists() else []
    # Also recover pre-fix drafts from their immutable attempt records.
    for pattern in ("research-attempt-*.json", "expansion-attempt-*.json"):
        receipts += [read(p)["receipt"] for p in sorted(directory.glob(pattern)) if "receipt" in read(p)]
    return list({digest(r): r for r in receipts}.values())


def record_author(directory, receipt):
    if not receipt:
        raise ValueError("draft reused without an author receipt")
    path = directory / "authors.json"
    data = read(path) if path.exists() else {"receipts": []}
    data["receipts"] = list({digest(r): r for r in author_receipts(directory) + [receipt]}.values())
    role = receipt.get("job", "author")
    data["research" if role == "research_repair" else role] = receipt["model"]
    write(path, data)


def validate_separation(policy, reviewer, receipts):
    if policy not in ("different_model", "fresh_session"):
        raise ValueError(f"unknown review policy: {policy}")
    if policy == "different_model":
        if reviewer["model"] in {r["model"] for r in receipts}:
            raise ValueError("reviewer also authored this design")
    else:
        if not receipts or any(not r.get("session_id") or not r.get("call_id") for r in receipts):
            raise ValueError("missing author session provenance")
        if not reviewer.get("session_id") or not reviewer.get("call_id"):
            raise ValueError("missing reviewer session provenance")
        if reviewer["session_id"] in {r["session_id"] for r in receipts} or reviewer["call_id"] in {
            r["call_id"] for r in receipts
        }:
            raise ValueError("reviewer reused an author session or call")
