"""Deterministic native projections and Stage 3 checks, without a runtime or reviewer."""

import base64
import html
import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from company_envs.storage import digest, read, write

from .bulk import BulkSpec, expand, native_ids, people_of, with_native_ids
from .documents import render_material
from .population_activity import activity_errors

VERSION = 1
DOC_MIME = "application/vnd.google-apps.document"
OFFICE_MIMES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def rows(value):
    return list(value.values()) if isinstance(value, dict) else value or []


def plain(content):
    return html.unescape(re.sub(r"<[^>]*>", " ", content or ""))


def instant(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, UTC)
    result = datetime.fromisoformat(value)
    return result if result.tzinfo else result.replace(tzinfo=UTC)


def commenters(document):
    return sorted(
        {document["ownerId"]}
        | {
            share["userId"]
            for share in document.get("sharedWith", [])
            if share.get("permission") in {"commenter", "editor", "owner"}
        }
    )


def normalize_slack(state):
    """Store a reply once, in messages, with IDs in the thread's native index."""
    messages = {r["messageId"]: r for group in state.get("messages", {}).values() for r in group}
    changes = []
    for thread in rows(state.get("threads")):
        parent = messages.get(thread.get("parentMessageId"))
        if parent is None:
            raise ValueError(f"missing Slack thread parent: {thread['threadId']}")
        parent["threadId"] = None
        group = state["messages"].setdefault(thread.get("channelId") or thread.get("dmId"), [])
        reply_ids = []
        for reply in thread.get("replies", []):
            rid = reply.get("messageId") if isinstance(reply, dict) else reply
            if rid not in messages:
                if not isinstance(reply, dict):
                    raise ValueError(f"missing Slack reply: {rid}")
                group.append(deepcopy(reply))
                messages[rid] = group[-1]
            if isinstance(reply, dict) and reply != messages[rid]:
                changes.append(rid)
            if messages[rid].get("threadId") != thread["threadId"]:
                raise ValueError(f"Slack reply belongs to another thread: {rid}")
            reply_ids.append(rid)
        # Include replies already in the rendered collection but absent from its index.
        reply_ids.extend(r["messageId"] for r in group if r.get("threadId") == thread["threadId"])
        thread["replies"] = list(dict.fromkeys(reply_ids))
    return {"conflicting_inline_copies_removed": sorted(set(changes))}


def sync_documents(docs, drive, *, drive_sources=()):
    """Project Docs into Drive; import Drive-only native Docs. Never infer wider sharing.

    Explicit drive_sources resolves audited collisions in favor of the saved Drive file.
    New personal documents live at the root with exactly their existing access grants.
    """
    documents, items = docs["documents"], drive["items"]
    imported, projected = [], []
    for rid, item in list(items.items()):
        if rid in drive_sources or item.get("mimeType") == DOC_MIME and rid not in documents:
            documents[rid] = {
                "id": rid,
                "title": item["name"],
                "content": "<p>" + html.escape(item.get("content", "")).replace("\n", "</p><p>") + "</p>",
                "ownerId": item["ownerId"],
                "starred": item.get("starred", False),
                "created": instant(item["createdAt"]).isoformat(),
                "updated": instant(item["modifiedAt"]).isoformat(),
                "sharedWith": [
                    {"userId": s["userId"], "permission": s["role"]} for s in item.get("sharedWith", [])
                ],
                "linkSharing": {"enabled": False, "permission": "viewer"},
            }
            imported.append(rid)
    for rid, doc in documents.items():
        previous = items.get(rid, {})
        if previous and previous.get("mimeType") != DOC_MIME:
            matches = (
                previous.get("mimeType") == OFFICE_MIMES[".docx"]
                and previous.get("name") == doc["title"]
                and plain(doc["content"]).split() == previous.get("content", "").split()
                and previous["ownerId"] == doc["ownerId"]
                and {(s["userId"], s["role"]) for s in previous.get("sharedWith", [])}
                == {(s["userId"], s["permission"]) for s in doc.get("sharedWith", [])}
            )
            if rid not in drive_sources and not matches:
                raise ValueError(f"Docs/Drive file ID collision needs an explicit source: {rid}")
            continue  # An explicitly selected Office source remains a downloadable Office file.
        created = int(instant(doc["created"]).timestamp() * 1000)
        item = {
            **previous,
            "id": rid,
            "name": doc["title"],
            "type": "doc",
            "mimeType": DOC_MIME,
            "parentId": previous.get("parentId"),
            "ownerId": doc["ownerId"],
            "content": plain(doc["content"]),
            "size": 0,
            "createdAt": created,
            "modifiedAt": int(instant(doc["updated"]).timestamp() * 1000),
            "sharedWith": [
                {"userId": s["userId"], "role": s["permission"], "addedAt": created}
                for s in doc.get("sharedWith", [])
            ],
            "starred": previous.get("starred", False),
            "trashed": False,
        }
        if not previous:
            projected.append(rid)
        items[rid] = item
    drive["storageUsed"] = sum(item.get("size", 0) for item in items.values())
    return {"imported_into_docs": imported, "projected_into_drive": projected}


def materialize_drive(folder, drive):
    """Render actual downloadable bytes once; retain text for search and source inspection."""
    folder = Path(folder)
    manifest_path = folder / "world/population/files/RESOURCES.json"
    resources = {r["id"]: r for r in read(manifest_path)} if manifest_path.exists() else {}
    for item in rows(drive.get("items")):
        suffix = Path(item.get("name", "")).suffix.lower()
        if (
            suffix not in OFFICE_MIMES
            or not item.get("content")
            or item.get("mimeType", "").startswith("application/vnd.google-apps.")
        ):
            continue
        thumbnail = item.get("thumbnailUrl", "") or ""
        if thumbnail.startswith("data:"):
            continue
        source_hash = digest({"content": item["content"], "suffix": suffix})
        path = folder / "world/population/files" / (source_hash + suffix)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(render_material(item["name"], item["content"]))
        data = path.read_bytes()
        item.update(
            size=len(data),
            mimeType=OFFICE_MIMES[suffix],
            thumbnailUrl=f"data:{OFFICE_MIMES[suffix]};base64," + base64.b64encode(data).decode(),
        )
        resources[item["id"]] = {
            "id": item["id"],
            "path": str(path.relative_to(folder)),
            "bytes": len(data),
            "sha256": digest(data),
            "source_hash": source_hash,
        }
    drive["storageUsed"] = sum(item.get("size", 0) for item in rows(drive.get("items")))
    write(manifest_path, list(resources.values()))
    return list(resources.values())


def quality_errors(states):
    """Blocking cross-copy, permission, and file-signature checks on actual output."""
    errors = activity_errors(states.get("hubspot_mock", {}))
    for deal in states.get("hubspot_mock", {}).get("deals", []):
        if deal.get("stage") == "closed_won" and deal.get("closedLostReason"):
            errors.append(f"deal {deal['id']}: won business retains a loss reason")
    slack = states.get("slack_mock", {})
    messages = {r["messageId"]: r for group in slack.get("messages", {}).values() for r in group}
    for thread in rows(slack.get("threads")):
        parent = messages.get(thread.get("parentMessageId"))
        for reply in thread.get("replies", []):
            if isinstance(reply, dict):
                errors.append(f"Slack {thread['threadId']}: inline reply object; expected a message ID")
            elif reply not in messages or messages[reply].get("threadId") != thread["threadId"]:
                errors.append(f"Slack {thread['threadId']}: invalid reply {reply}")
            elif (
                parent
                and parent.get("timestamp")
                and messages[reply].get("timestamp")
                and instant(messages[reply]["timestamp"]) < instant(parent["timestamp"])
            ):
                errors.append(f"Slack {reply}: reply predates parent {parent['messageId']}")
    for notification in rows(slack.get("notifications")):
        message = messages.get(notification.get("messageId"))
        if (
            message
            and notification.get("timestamp")
            and message.get("timestamp")
            and instant(notification["timestamp"]) < instant(message["timestamp"])
        ):
            errors.append(f"Slack {notification['notificationId']}: notification predates message")
    for dm in rows(slack.get("dms")):
        timestamps = [
            instant(m["timestamp"])
            for m in slack.get("messages", {}).get(dm["dmId"], [])
            if m.get("timestamp")
        ]
        if timestamps and dm.get("lastTime") and instant(dm["lastTime"]) < max(timestamps):
            errors.append(f"Slack {dm['dmId']}: conversation summary time is stale")
    for channel in rows(slack.get("channels")):
        timestamps = [
            instant(m["timestamp"])
            for m in slack.get("messages", {}).get(channel["channelId"], [])
            if m.get("timestamp")
        ]
        if timestamps and channel.get("createdAt") and min(timestamps) < instant(channel["createdAt"]):
            errors.append(f"Slack {channel['channelId']}: message predates channel creation")
    docs = {d["id"]: d for d in rows(states.get("google_docs_mock", {}).get("documents"))}
    for comment in rows(states.get("google_docs_mock", {}).get("comments")):
        doc = docs.get(comment.get("docId"))
        if doc is None:
            errors.append(f"comment {comment['id']}: missing document")
            continue
        for entry in [comment, *comment.get("replies", [])]:
            if entry.get("userId") not in commenters(doc):
                errors.append(f"comment {comment['id']}: author lacks comment access")
        if instant(comment["created"]) < instant(doc["created"]):
            errors.append(f"comment {comment['id']}: predates document")
        if comment.get("quotedText") and comment["quotedText"] not in plain(doc["content"]):
            errors.append(f"comment {comment['id']}: quote absent from document")
    drive = states.get("google_drive_mock", {})
    items = {d["id"]: d for d in rows(drive.get("items"))}
    if docs and drive:
        for rid, doc in docs.items():
            item = items.get(rid)
            if item is None:
                errors.append(f"Docs {rid}: missing Drive projection")
            elif item["ownerId"] != doc["ownerId"] or {
                (s["userId"], s["role"]) for s in item.get("sharedWith", [])
            } != {(s["userId"], s["permission"]) for s in doc.get("sharedWith", [])}:
                errors.append(f"Docs {rid}: Drive access differs")
            elif item.get("mimeType") == DOC_MIME and (
                item["name"] != doc["title"]
                or item.get("content") != plain(doc["content"])
                or instant(item["createdAt"]) != instant(doc["created"])
                or instant(item["modifiedAt"]) != instant(doc["updated"])
            ):
                errors.append(f"Docs {rid}: Drive document differs")
        for rid, item in items.items():
            if item.get("mimeType") == DOC_MIME and rid not in docs:
                errors.append(f"Drive {rid}: missing Docs projection")
    for rid, item in items.items():
        suffix = Path(item.get("name", "")).suffix.lower()
        if suffix not in OFFICE_MIMES or item.get("mimeType", "").startswith("application/vnd.google-apps."):
            continue
        data_url = item.get("thumbnailUrl", "") or ""
        try:
            data = base64.b64decode(data_url.split(";base64,", 1)[1], validate=True)
        except (ValueError, IndexError):
            errors.append(f"Drive {rid}: no binary download")
            continue
        signature = b"%PDF-" if suffix == ".pdf" else b"PK\x03\x04"
        if not data.startswith(signature) or len(data) != item.get("size"):
            errors.append(f"Drive {rid}: invalid download bytes/size")
    return errors


def resource_errors(folder):
    errors = []
    folder = Path(folder)
    work = folder / "world/population"
    manifests = [
        work / "ASSETS.json",
        work / "CAMPAIGNS.json",
        work / "transactions/RESOURCES.json",
        work / "files/RESOURCES.json",
    ]
    for path in manifests:
        if not path.exists():
            continue
        manifest = read(path)
        records = (
            manifest
            if isinstance(manifest, list)
            else manifest.get("resources", []) + manifest.get("exports", [])
        )
        for resource in records:
            local = resource.get("path") or resource.get("local_path")
            if not local or not resource.get("sha256"):
                errors.append(f"resource {resource.get('id')}: missing path/hash")
                continue
            target = (folder / local).resolve()
            if not target.is_relative_to(folder.resolve()) or not target.is_file():
                errors.append(f"resource {resource.get('id')}: missing/local path outside company")
            elif digest(target.read_bytes()) != resource["sha256"]:
                errors.append(f"resource {resource.get('id')}: changed bytes")
    return errors


def retime_bulk(state, human, specs, seed=0):
    """Migrate only generated date fields; retain IDs, business content and later repairs."""
    specs = [BulkSpec.model_validate(s) for s in specs]
    mapping = native_ids(human, specs)
    changes = []

    def patch(current, fresh, template):
        count = 0
        for key, value in template.items():
            if key not in current or key not in fresh:
                continue
            if isinstance(value, dict) and isinstance(current[key], dict):
                count += patch(current[key], fresh[key], value)
            elif (
                isinstance(value, str)
                and re.search(r"\{\{\s*date(?:time|[+-]\d+)?\s*\}\}", value)
                and current[key] != fresh[key]
            ):
                current[key] = fresh[key]
                count += 1
        return count

    for original in specs:
        if original.cadence == "random":
            continue  # Random windows already covered their span.
        spec = with_native_ids(original, mapping)
        native = state.get(spec.collection, [])
        if isinstance(native, dict) and any(isinstance(v, list) for v in native.values()):
            native = [r for group in native.values() for r in group]
        index = {r.get("id") or r.get("messageId") or r.get("threadId"): r for r in rows(native)}
        for record in expand(spec, people_of(human), seed):
            rid = record.get("id") or record.get("messageId") or record.get("threadId")
            if rid in index and patch(index[rid], record, spec.template):
                changes.append({"collection": spec.collection, "id": rid})
    return changes


def save_handoff(folder, core, world, states, entries, status, errors):
    """The normal command, not a pilot-only script, owns the Stage 3 handoff."""
    folder = Path(folder)
    work = folder / "world/population"
    write(work / "EFFECTIVE-WORLD.json", world)
    artifacts = {}
    for directory in [
        folder / "world/desktop",
        folder / "world/materials",
        work / "files",
        work / "assets",
        work / "exports",
        work / "desktop",
        work / "campaigns",
        work / "transactions",
    ]:
        for path in directory.rglob("*"):
            if path.is_file():
                artifacts[str(path.relative_to(folder))] = digest(path.read_bytes())
    for name in (
        "EXTENSION.json",
        "EFFECTIVE-WORLD.json",
        "targets.json",
        "ASSETS.json",
        "ledger.json",
        "AMENDMENTS.json",
        "CAMPAIGNS.json",
    ):
        path = work / name
        if path.exists():
            artifacts[str(path.relative_to(folder))] = digest(path.read_bytes())
    previous = read(folder / "world/POPULATION.json") if (folder / "world/POPULATION.json").exists() else {}
    write(
        folder / "world/POPULATION.json",
        {
            "schema_version": 2,
            "status": status,
            "core_hashes": read(folder / "world/CORE.json")["hashes"],
            "reference_date": core.reference_date,
            "effective_world": "world/population/EFFECTIVE-WORLD.json",
            "desktop_root": "world/population/desktop" if (work / "desktop").is_dir() else "world/desktop",
            "states": {
                a: {
                    "path": f"world/{a}.state.json",
                    "state_hash": digest(s),
                    "bulk_ids": entries[a]["bulk_ids"],
                }
                for a, s in states.items()
            },
            "artifacts": artifacts,
            "review": "Stage 4 not run",
            "runtime": "not started",
            "known_issues": previous.get("known_issues", []),
            "native_errors": errors,
        },
    )
