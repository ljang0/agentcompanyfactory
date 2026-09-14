"""Lossless actor history encoding for long desktop editing sessions."""

import json
from copy import deepcopy
from os.path import commonprefix


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


EDIT_VALUES = {
    "set": ("value",),
    "remove": (),
    "append": ("value",),
    "text_replace": ("offset", "old", "new"),
}


def _compact_events(events):
    """Share repeated event headers and edit layouts; retain every variable value."""
    tables = {name: [] for name in ("headers", "records", "paths", "layouts")}
    indexes = {name: {} for name in tables}

    def intern(name, value):
        key = json.dumps(value, sort_keys=True)
        if key not in indexes[name]:
            indexes[name][key] = len(tables[name])
            tables[name].append(value)
        return indexes[name][key]

    times = [e["at"] for e in events if "at" in e]
    affixes = None
    if times and all(isinstance(t, str) for t in times):
        prefix = commonprefix(times)
        suffix = commonprefix([t[len(prefix) :][::-1] for t in times])[::-1]
        affixes = [prefix, suffix]
    rows = []
    for event in events:
        fields = [k for k in ("sequence", "at") if k in event]
        fixed = {k: v for k, v in event.items() if k not in {*fields, "changes"}}
        header = intern("headers", {"fields": fields, "fixed": fixed})
        timing = [
            event[k][len(affixes[0]) : len(event[k]) - len(affixes[1]) if affixes[1] else None]
            if k == "at" and affixes is not None
            else event[k]
            for k in fields
        ]
        layout, values = [], []
        for reference, change in event["changes"].items():
            record = [intern("records", reference)]
            for name in ("edits", "before_sync"):
                if name not in change:
                    continue
                descriptors = []
                for edit in change[name]:
                    descriptors.append([edit["op"], intern("paths", edit["path"])])
                    values.extend(edit[k] for k in EDIT_VALUES[edit["op"]])
                record.append(descriptors)
            layout.append(record)
        rows.append([header, timing, intern("layouts", layout), values])
    return {**tables, "time_affixes": affixes, "events": rows}


def _expanded_events(encoded):
    for header_id, timing, layout_id, values in encoded["events"]:
        header = encoded["headers"][header_id]
        event = deepcopy(header["fixed"])
        event.update(zip(header["fields"], timing, strict=True))
        if "at" in event and encoded["time_affixes"] is not None:
            prefix, suffix = encoded["time_affixes"]
            event["at"] = prefix + event["at"] + suffix
        event["changes"] = {}
        values = iter(values)
        for reference_id, *groups in encoded["layouts"][layout_id]:
            change = {}
            for name, descriptors in zip(("edits", "before_sync"), groups):
                change[name] = [
                    {
                        "op": op,
                        "path": encoded["paths"][path_id],
                        **{k: next(values) for k in EDIT_VALUES[op]},
                    }
                    for op, path_id in descriptors
                ]
            event["changes"][encoded["records"][reference_id]] = change
        if list(values):
            raise ValueError("Unused values in compact actor evidence")
        yield event


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
        "encoding": "lossless_actor_edits_v2",
        "instructions": "Each event row is [header_index, timing_values, layout_index, edit_values]. All table indices are zero-based. A header supplies fixed metadata (including the actual actor) and ordered timing field names. If time_affixes is present, reconstruct at as prefix + its timing value + suffix. A layout contains [record_index, edits, optional before_sync] entries. Each edit descriptor is [operation, path_index]; consume its values from the event's flat edit_values in layout order: set and append take one value, remove takes none, text_replace takes offset, old, new. Start each record at its anchor. Paths contain object keys/list indices. set replaces a value; remove deletes a key/index; append adds a list element. text_replace replaces exactly old with new at the character offset. Apply before_sync without crediting the worker, then apply edits as that worker's changes. Full final changed records remain in the changes evidence item. All events, timestamps, actors, sequence numbers, business text and edits are preserved; repeated headers and layouts are only stored once.",
        "anchors": anchors,
        **_compact_events(rows),
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
    if encoded["encoding"] not in {"lossless_actor_edits_v1", "lossless_actor_edits_v2"}:
        raise ValueError("Unknown actor evidence encoding")
    events = (
        _expanded_events(encoded) if encoded["encoding"] == "lossless_actor_edits_v2" else encoded["events"]
    )
    for event in events:
        row = {key: deepcopy(value) for key, value in event.items() if key != "changes"}
        row["changes"] = {}
        for reference, change in event["changes"].items():
            before = apply_edits(current[reference], change.get("before_sync", []))
            after = apply_edits(before, change["edits"])
            row["changes"][reference] = {"before": before, "after": after}
            current[reference] = deepcopy(after)
        result.append(row)
    return result
