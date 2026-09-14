"""Keep shared native Google documents consistent between the Docs and Drive apps."""

import html
from copy import deepcopy

from .population_quality import DOC_MIME, instant, plain


def project_document_edits(app_id, before, after, peer):
    """Apply only changed fields; preserve formatting when Drive merely renames a file."""
    peer = deepcopy(peer)
    if app_id == "google_docs_mock":
        old, new = before.get("documents", {}), after.get("documents", {})
        for rid in old.keys() | new.keys():
            if old.get(rid) == new.get(rid):
                continue
            item = peer.get("items", {}).get(rid)
            if item and item.get("mimeType") != DOC_MIME:
                continue  # Office downloads are separate saved exports, not live Google documents.
            if rid not in new:
                if item:
                    item["trashed"] = True
                continue
            doc = new[rid]
            item = peer.setdefault("items", {}).setdefault(
                rid,
                {
                    "id": rid,
                    "type": "doc",
                    "mimeType": DOC_MIME,
                    "parentId": None,
                    "starred": False,
                    "trashed": False,
                    "size": 0,
                },
            )
            for source, target in (("title", "name"), ("ownerId", "ownerId")):
                if doc.get(source) != old.get(rid, {}).get(source):
                    item[target] = doc[source]
            if doc.get("content") != old.get(rid, {}).get("content"):
                item["content"] = plain(doc["content"])
            for source, target in (("created", "createdAt"), ("updated", "modifiedAt")):
                if doc.get(source) != old.get(rid, {}).get(source):
                    item[target] = int(instant(doc[source]).timestamp() * 1000)
            if doc.get("sharedWith") != old.get(rid, {}).get("sharedWith"):
                item["sharedWith"] = [
                    {"userId": r["userId"], "role": r["permission"]} for r in doc.get("sharedWith", [])
                ]
            if doc.get("linkSharing") != old.get(rid, {}).get("linkSharing"):
                item["linkSharing"] = deepcopy(doc.get("linkSharing", {"enabled": False}))
    elif app_id == "google_drive_mock":
        old, new = before.get("items", {}), after.get("items", {})
        for rid in old.keys() | new.keys():
            if old.get(rid) == new.get(rid):
                continue
            item = new.get(rid) or old[rid]
            if item.get("mimeType") != DOC_MIME:
                continue
            if rid not in new or item.get("trashed"):
                peer.get("documents", {}).pop(rid, None)
                peer["comments"] = [c for c in peer.get("comments", []) if c.get("docId") != rid]
                continue
            doc = peer.setdefault("documents", {}).setdefault(
                rid,
                {
                    "id": rid,
                    "starred": False,
                    "content": "",
                    "linkSharing": {"enabled": False, "permission": "viewer"},
                },
            )
            for source, target in (("name", "title"), ("ownerId", "ownerId")):
                if item.get(source) != old.get(rid, {}).get(source) or target not in doc:
                    doc[target] = item[source]
            if item.get("content") != old.get(rid, {}).get("content"):
                doc["content"] = (
                    "<p>" + html.escape(item.get("content", "")).replace("\n", "</p><p>") + "</p>"
                )
            for source, target in (("createdAt", "created"), ("modifiedAt", "updated")):
                if item.get(source) != old.get(rid, {}).get(source) or target not in doc:
                    doc[target] = instant(item[source]).isoformat()
            if item.get("sharedWith") != old.get(rid, {}).get("sharedWith") or "sharedWith" not in doc:
                doc["sharedWith"] = [
                    {"userId": r["userId"], "permission": r["role"]} for r in item.get("sharedWith", [])
                ]
            if item.get("linkSharing") != old.get(rid, {}).get("linkSharing"):
                doc["linkSharing"] = deepcopy(item.get("linkSharing", {"enabled": False}))
    return peer
