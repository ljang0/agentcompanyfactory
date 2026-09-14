"""Explicit input ablations in disposable worker views; canonical records stay intact."""

import json
from copy import deepcopy

from .hub_identity import WorkerAppProxy


def mask_records(state, references, app_id):
    masked = deepcopy(state)
    for reference in references:
        location, rid = reference.rsplit("#", 1)
        app, collection = location.split(".", 1)
        if app != app_id or collection not in masked:
            continue

        def prune(value, rid=rid):
            if isinstance(value, list):
                return [prune(item) for item in value if not matches(item)]
            if isinstance(value, dict):
                return {
                    key: prune(item) for key, item in value.items() if str(key) != rid and not matches(item)
                }
            return value

        def matches(value, rid=rid):
            return isinstance(value, dict) and any(
                str(value.get(key)) == rid for key in ("id", "messageId", "threadId")
            )

        masked[collection] = prune(masked[collection])
    return masked


class _Resources:
    def __init__(self, resources, references, app_id):
        self.resources, self.references, self.app_id = resources, references, app_id

    def __getattr__(self, name):
        return getattr(self.resources, name)

    def response(self, route, state):
        return self.resources.response(route, mask_records(state, self.references, self.app_id))


class InputAblationProxy(WorkerAppProxy):
    def __init__(self, *args, withheld=(), **kwargs):
        self.withheld = tuple(withheld)
        if kwargs.get("resources") is not None:
            kwargs["resources"] = _Resources(kwargs["resources"], self.withheld, kwargs["app_id"])
        super().__init__(*args, **kwargs)

    def rewrite_state(self, data, *, remember=True):
        payload = json.loads(data)
        if isinstance(payload.get("stored_state"), dict):
            payload["stored_state"] = mask_records(payload["stored_state"], self.withheld, self.app_id)
        return super().rewrite_state(json.dumps(payload).encode(), remember=remember)

    def write_state(self, body, path, headers, upstream_call, **kwargs):
        if mask_records(body["state"], self.withheld, self.app_id) != body["state"]:
            return (
                403,
                b'{"error":"This input is withheld for the recorded ablation"}',
                {"Content-Type": "application/json"},
            )
        status, data, out = super().write_state(body, path, headers, upstream_call, **kwargs)
        if 200 <= status < 300:
            payload = json.loads(data)
            if isinstance(payload, dict):
                for key in ("state", "stored_state", "current_state"):
                    if isinstance(payload.get(key), dict):
                        payload[key] = mask_records(payload[key], self.withheld, self.app_id)
                data = json.dumps(payload).encode()
        return status, data, out


def proxy_factory(world, specification):
    def create(upstream, item, worker):
        refs = specification["references"] if worker == specification["worker_id"] else ()
        return InputAblationProxy(
            upstream,
            withheld=refs,
            worker_id=worker,
            sid=world.sid,
            identity_key=item["identity_key"],
            user_record=item["users"].get(worker),
            user_pointer=item.get("pointers", {}).get(worker),
            canonical_user=item["canonical_user"],
            attribution_log=world.folder / "runtime/attribution" / f"{item['app_id']}.jsonl",
            host=world.host,
            placeholder_images=world.placeholder_images,
            resources=world.resources,
            app_id=item["app_id"],
            on_write=lambda worker_id, previous, state: world.project_write(
                item["app_id"], worker_id, previous, state
            ),
        ).start()

    return create
