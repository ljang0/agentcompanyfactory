"""Record-level access for shared files and Slack's keyed native collections."""

from copy import deepcopy


def role(record, person):
    if not record.get("ownerId"):
        return "unscoped"
    uid = (person or {}).get("id") or (person or {}).get("userId")
    if record["ownerId"] == uid:
        return "owner"
    for share in record.get("sharedWith", []):
        if share.get("userId") == uid:
            return share.get("permission") or share.get("role") or "viewer"
    link = record.get("linkSharing", {})
    return link.get("permission", "viewer") if link.get("enabled") else None


def file_and_chat_view(state, person):
    view = dict(state)
    for key in ("documents", "items"):
        rows = state.get(key)
        if isinstance(rows, dict):
            view[key] = {k: v for k, v in rows.items() if not isinstance(v, dict) or role(v, person)}
    if isinstance(state.get("documents"), dict) and isinstance(state.get("comments"), list):
        view["comments"] = [r for r in state["comments"] if r.get("docId") in view["documents"]]
    if isinstance(state.get("messages"), dict) and isinstance(state.get("channels"), list):
        uid = (person or {}).get("id") or (person or {}).get("userId")
        view["channels"] = [
            c for c in state["channels"] if not c.get("isPrivate") or uid in c.get("members", [])
        ]
        allowed = {c["channelId"] for c in view["channels"]}
        allowed.update(d["dmId"] for d in view.get("dms", []))
        view["messages"] = {k: v for k, v in state["messages"].items() if k in allowed}
        if isinstance(state.get("threads"), dict):
            view["threads"] = {
                k: v for k, v in state["threads"].items() if (v.get("channelId") or v.get("dmId")) in allowed
            }
    return view


def access_errors(previous, incoming, base, person):
    """Check intentional edits against current permissions, including hidden-ID injection."""
    from .hub_identity import _json_equal, visible_to

    view = visible_to(previous, person)
    uid = (person or {}).get("id") or (person or {}).get("userId")
    errors = []
    for key in ("documents", "items"):
        old, new, seen = previous.get(key), incoming.get(key), (base or {}).get(key, {})
        if not isinstance(old, dict) or not isinstance(new, dict):
            continue
        for rid in set(new) | set(seen):
            if rid in new and rid in seen and _json_equal(new[rid], seen[rid]):
                continue  # An unchanged stale value cannot overwrite a peer's edit.
            original = old.get(rid)
            if original is None:
                if rid in new and role(new[rid], person) not in {"owner", "unscoped"}:
                    errors.append(f"{key}/{rid}: new file must belong to this worker")
                continue
            if rid not in view.get(key, {}):
                errors.append(f"{key}/{rid}: inaccessible file")
                continue
            permission = role(original, person)
            if permission in {"owner", "editor", "unscoped"}:
                continue
            before, after = deepcopy(seen.get(rid, original)), deepcopy(new.get(rid))
            # Star/access time are personal navigation, not document authorship.
            for value in (before, after):
                if isinstance(value, dict):
                    for field in ("starred", "accessedAt"):
                        value.pop(field, None)
            if not _json_equal(before, after):
                errors.append(f"{key}/{rid}: {permission} cannot edit this file")
    if isinstance(previous.get("documents"), dict) and isinstance(incoming.get("comments"), list):
        seen = {r["id"]: r for r in (base or {}).get("comments", [])}
        current = {r["id"]: r for r in previous.get("comments", [])}
        for comment in incoming["comments"]:
            if _json_equal(comment, seen.get(comment["id"])):
                continue
            doc = previous["documents"].get(comment.get("docId"), {})
            if not doc or role(doc, person) not in {"owner", "editor", "commenter", "unscoped"}:
                errors.append(f"comments/{comment['id']}: comment access required")
            if comment["id"] in current and comment["id"] not in seen:
                errors.append(f"comments/{comment['id']}: inaccessible comment")
            original = current.get(comment["id"])
            if original is None and comment.get("userId") != uid:
                errors.append(f"comments/{comment['id']}: cannot impersonate another author")
            if (
                original
                and any(comment.get(k) != original.get(k) for k in ("content", "userId", "docId"))
                and original.get("userId") != uid
            ):
                errors.append(f"comments/{comment['id']}: cannot rewrite another author's comment")
            old_replies = {r["id"]: r for r in (original or {}).get("replies", [])}
            for reply in comment.get("replies", []):
                prior = old_replies.get(reply.get("id"))
                if prior == reply:
                    continue
                if reply.get("userId") != uid or prior and prior.get("userId") != uid:
                    errors.append(f"comments/{comment['id']}: cannot impersonate a reply author")
    for key in ("emails", "drafts"):
        if not isinstance(incoming.get(key), list):
            continue
        old = {r["id"]: r for r in previous.get(key, [])}
        seen = {r["id"]: r for r in (base or {}).get(key, [])}
        address = (person or {}).get("email", "").casefold()
        for message in incoming[key]:
            if _json_equal(message, seen.get(message["id"])):
                continue
            # Read/star/folder controls are mailbox actions; composing another
            # person's text or sending under their address is impersonation.
            original = old.get(message["id"])
            sender = message.get("from", {})
            sender = sender.get("email", "") if isinstance(sender, dict) else str(sender)
            content_changed = original is None or any(
                message.get(k) != original.get(k) for k in ("from", "to", "cc", "bcc", "subject", "body")
            )
            if content_changed and sender.casefold() != address:
                errors.append(f"{key}/{message['id']}: cannot write another sender's message")
    if isinstance(previous.get("messages"), dict) and isinstance(incoming.get("messages"), dict):
        old = {
            m["messageId"]: m
            for rows in previous["messages"].values()
            for m in rows
            if isinstance(m, dict) and "messageId" in m
        }
        seen = {
            m["messageId"]: m
            for rows in (base or {}).get("messages", {}).values()
            for m in rows
            if isinstance(m, dict) and "messageId" in m
        }
        for rows in incoming["messages"].values():
            for message in rows:
                rid = message.get("messageId")
                if _json_equal(message, seen.get(rid)):
                    continue
                original = old.get(rid)
                if original is None and message.get("senderId") != uid:
                    errors.append(f"messages/{rid}: cannot impersonate another sender")
                elif (
                    original
                    and any(message.get(k) != original.get(k) for k in ("content", "senderId", "threadId"))
                    and original.get("senderId") != uid
                ):
                    errors.append(f"messages/{rid}: cannot rewrite another sender's message")
    # A worker cannot modify or inject records in hidden mailboxes or chat groups.
    for key in ("emails", "drafts", "events", "dms", "messages", "threads", "channels"):
        new, old, allowed = incoming.get(key), previous.get(key), view.get(key)
        if isinstance(old, dict) and isinstance(new, dict) and isinstance(allowed, dict):
            for rid in new:
                if rid in old and rid not in allowed:
                    errors.append(f"{key}/{rid}: inaccessible records")
        if isinstance(old, list) and isinstance(new, list) and isinstance(allowed, list):

            def ids(rows):
                return {
                    r.get("id") or r.get("dmId") or r.get("channelId") for r in rows if isinstance(r, dict)
                } - {None}

            if ids(new) & (ids(old) - ids(allowed)):
                errors.append(f"{key}: inaccessible records")
    return errors
