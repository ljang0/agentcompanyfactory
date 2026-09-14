"""Bounded content authoring: cache valid units and retry only rejected units."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from company_envs.storage import digest, read, write


def author_units(cases, models, work, *, prompt, response_type, field, validate, batch_size, concurrency):
    ids = [c["id"] for c in cases]
    if len(set(ids)) != len(ids) or any(not isinstance(i, str) or not i for i in ids):
        raise ValueError("Authoring cases need unique nonempty IDs")
    if batch_size < 1 or concurrency < 1:
        raise ValueError("Batch size and concurrency must be positive")

    def batch(offset):
        selected = cases[offset : offset + batch_size]
        keys = {c["id"]: digest({"prompt": prompt, "case": c}) for c in selected}
        paths = {i: Path(work) / "units" / f"{key}.json" for i, key in keys.items()}
        accepted, receipts = {}, []
        for case in selected:
            rid = case["id"]
            if paths[rid].exists():
                saved = read(paths[rid])
                value = saved["value"]
                # The same contract applies on resume, including permissions.
                value = response_type.model_validate({field: [value]}).model_dump()[field][0]
                if (
                    saved["inputs"] != keys[rid]
                    or saved["output_hash"] != digest(value)
                    or value["id"] != rid
                    or validate(case, value)
                ):
                    raise ValueError(f"Invalid authoring checkpoint: {rid}")
                accepted[rid] = value
        reused = len(accepted)
        feedback = []
        for _ in range(3):
            pending = [c for c in selected if c["id"] not in accepted]
            if not pending:
                break
            body = {"cases": pending}
            if feedback:
                body["repair"] = feedback
            result, receipt = models.call(
                "world_states", prompt + "\n" + json.dumps(body, ensure_ascii=False), response_type
            )
            receipts.append(receipt)
            values = result.model_dump()[field]
            written = {v["id"]: v for v in values}
            expected = {c["id"] for c in pending}
            if len(written) != len(values) or set(written) - expected:
                feedback = ["Return only requested IDs, exactly once each."]
                continue
            feedback = []
            for case in pending:
                rid = case["id"]
                value = written.get(rid)
                errors = validate(case, value) if value is not None else ["Missing requested ID"]
                if errors:
                    feedback.append({"id": rid, "errors": errors, "previous": value})
                    continue
                accepted[rid] = value
                write(
                    paths[rid],
                    {
                        "inputs": keys[rid],
                        "value": value,
                        "output_hash": digest(value),
                        "receipt": receipt,
                    },
                )
        if len(accepted) != len(selected):
            raise ValueError(f"Authoring failed after three attempts: {feedback}")
        values = [accepted[c["id"]] for c in selected]
        return {
            "inputs": digest(keys),
            "cases": selected,
            field: values,
            "output_hash": digest(values),
            "receipts": receipts,
            "reused": reused,
        }

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(batch, range(0, len(cases), batch_size)))
