"""Lossless actor history encoding for long desktop editing sessions."""

from copy import deepcopy


def _same(before, after):
    if type(before) is not type(after):
        return False
    if isinstance(before, dict):
        return before.keys() == after.keys() and all(
            _same(value, after[key]) for key, value in before.items()
        )
    if isinstance(before, list):
        return len(before) == len(after) and all(_same(a, b) for a, b in zip(before, after))
    return before == after


def edits(before, after, path=()):
    """JSON edits with checked string splices; no business text is dropped or summarized."""
    if _same(before, after):
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        rows = []
        for key in sorted(set(before) | set(after)):
            target = [*path, key]
            if key not in after:
                rows.append({"op": "remove", "path": target})
            elif key not in before:
                rows.append({"op": "set", "path": target, "value": deepcopy(after[key])})
            else:
                rows.extend(edits(before[key], after[key], tuple(target)))
        return rows
    if isinstance(before, list) and isinstance(after, list):
        rows = []
        for index in range(min(len(before), len(after))):
            rows.extend(edits(before[index], after[index], (*path, index)))
        for index in range(len(before) - 1, len(after) - 1, -1):
            rows.append({"op": "remove", "path": [*path, index]})
        for index in range(len(before), len(after)):
            rows.append({"op": "append", "path": list(path), "value": deepcopy(after[index])})
        return rows
    if isinstance(before, str) and isinstance(after, str):
        prefix = 0
        while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]:
            prefix += 1
        suffix = 0
        while suffix < min(len(before), len(after)) - prefix and before[-suffix - 1] == after[-suffix - 1]:
            suffix += 1
        return [
            {
                "op": "text_replace",
                "path": list(path),
                "offset": prefix,
                "old": before[prefix : len(before) - suffix],
                "new": after[prefix : len(after) - suffix],
            }
        ]
    return [{"op": "set", "path": list(path), "value": deepcopy(after)}]


def apply_edits(before, changes):
    value = deepcopy(before)
    for change in changes:
        path, operation = change["path"], change["op"]
        parent = value
        for part in path[:-1]:
            parent = parent[part]
        current = parent[path[-1]] if path and operation != "set" else value
        if operation == "remove":
            del parent[path[-1]]
        elif operation == "append":
            current.append(deepcopy(change["value"]))
        else:
            if operation == "text_replace":
                offset, old = change["offset"], change["old"]
                if current[offset : offset + len(old)] != old:
                    raise ValueError("Text edit does not match the actual prior evidence")
                updated = current[:offset] + change["new"] + current[offset + len(old) :]
            elif operation == "set":
                updated = deepcopy(change["value"])
            else:
                raise ValueError(f"Unknown evidence edit: {operation}")
            if path:
                parent[path[-1]] = updated
            else:
                value = updated
    return value


def encode_actors(events, changed_records):
    anchors, current, rows = {}, {}, []
    for event in events:
        row = {key: deepcopy(value) for key, value in event.items() if key != "changes"}
        row["changes"] = {}
        for reference, change in event.get("changes", {}).items():
            before, after = change["before"], change["after"]
            if reference not in current:
                if reference in changed_records and _same(changed_records[reference]["before"], before):
                    anchors[reference] = {"from_initial_changed_record": reference}
                else:
                    anchors[reference] = {"value": deepcopy(before)}
                current[reference] = deepcopy(before)
            encoded = {"edits": edits(before, after)}
            if not _same(current[reference], before):
                # App projections can change a related record between two recorded worker writes.
                # This synchronization is explicitly not attributed to the next worker.
                encoded["before_sync"] = edits(current[reference], before)
            row["changes"][reference] = encoded
            current[reference] = deepcopy(after)
        rows.append(row)
    result = {
        "encoding": "lossless_actor_edits_v1",
        "instructions": "Start each record at its anchor. Paths are arrays of object keys/list indices. set replaces a value; remove deletes a key/index; append adds one list element. text_replace replaces exactly old with new at the character offset, retaining the unchanged prefix and suffix. Each event keeps its actual actor and sequence. Apply before_sync without attributing it to the worker, then apply edits as that worker's actual changes. Full final changed records remain in the changes evidence item. No events, business text or changes are omitted.",
        "anchors": anchors,
        "events": rows,
    }
    if not _same(decode_actors(result, changed_records), events):
        raise ValueError("Actor evidence did not survive lossless encoding")
    return result


def decode_actors(encoded, changed_records):
    current = {
        reference: deepcopy(changed_records[anchor["from_initial_changed_record"]]["before"])
        if "from_initial_changed_record" in anchor
        else deepcopy(anchor["value"])
        for reference, anchor in encoded["anchors"].items()
    }
    result = []
    for event in encoded["events"]:
        row = {key: deepcopy(value) for key, value in event.items() if key != "changes"}
        row["changes"] = {}
        for reference, change in event["changes"].items():
            before = apply_edits(current[reference], change.get("before_sync", []))
            after = apply_edits(before, change["edits"])
            row["changes"][reference] = {"before": before, "after": after}
            current[reference] = deepcopy(after)
        result.append(row)
    return result
