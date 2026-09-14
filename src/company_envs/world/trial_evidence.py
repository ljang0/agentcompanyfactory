"""Observe committed proxy writes without changing worker access or app behavior."""

import json
import threading
from pathlib import Path

from company_envs.storage import digest, now

from .hub_app import UI_STATE_KEYS
from .hub_world import CompanyWorld
from .task_assessment import indexed_records


def record_changes(initial, final):
    changed = {}
    for app in sorted(set(initial) | set(final)):
        before, after = initial.get(app, {}), final.get(app, {})
        for collection in sorted(set(before) | set(after)):
            old, new = before.get(collection), after.get(collection)
            if collection in UI_STATE_KEYS or old == new:
                continue
            old_records = dict(indexed_records(collection, old))
            new_records = dict(indexed_records(collection, new))
            if old_records or new_records:
                for rid in sorted(set(old_records) | set(new_records)):
                    if old_records.get(rid) != new_records.get(rid):
                        changed[f"{app}.{collection}#{rid}"] = {
                            "before": old_records.get(rid),
                            "after": new_records.get(rid),
                        }
            else:
                changed[f"{app}.{collection}"] = {"before": old, "after": new}
    return changed


class ObservedWorld(CompanyWorld):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.evidence_lock = threading.Lock()
        self.evidence_sequence = 0

    def project_write(self, app_id, worker_id, previous, state):
        super().project_write(app_id, worker_id, previous, state)
        changes = record_changes({app_id: previous}, {app_id: state})
        if not changes:
            return
        with self.evidence_lock:
            self.evidence_sequence += 1
            entry = {
                "sequence": self.evidence_sequence,
                "at": now(),
                "sid": self.sid,
                "worker_id": worker_id,
                "app_id": app_id,
                "changes": changes,
                "basis": "committed write through the worker-bound proxy",
            }
            path = self.folder / "runtime/evidence/writes.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def reset(self):
        super().reset()
        path = self.folder / "runtime/evidence/writes.jsonl"
        if path.exists():
            archive = path.parent / "history" / f"{digest(path.read_bytes())}.jsonl"
            archive.parent.mkdir(parents=True, exist_ok=True)
            path.rename(archive)
        self.evidence_sequence = 0


def read_events(folder, sid):
    path = Path(folder) / "runtime/evidence/writes.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows = [r for r in rows if r.get("sid") == sid]
    if [r["sequence"] for r in rows] != list(range(1, len(rows) + 1)):
        raise ValueError("Trial write evidence has missing or duplicate sequence numbers")
    return rows
