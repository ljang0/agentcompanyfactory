"""Import two historical designs and an explicitly synthetic rename control once."""

import argparse
import copy
import tomllib
from pathlib import Path

from company_envs.storage import digest, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Refusing to replace an existing regression snapshot")
    examples = []
    for company, task_id in [
        ("cedarspan", "account_variance_resolution"),
        ("pinecrest", "going_concern_assessment"),
    ]:
        path = args.archive / company / "company.toml"
        raw = path.read_bytes()
        doc = tomllib.loads(raw.decode())
        task = next(t for t in doc["task_types"] if t["id"] == task_id)
        examples.append(
            {
                "id": f"{company}_{task_id}",
                "origin": str(path),
                "origin_sha256": digest(raw),
                "company": doc["company"],
                "task": task,
            }
        )
    twin = copy.deepcopy(examples[0])
    twin["id"] = "renamed_account_variance_control"
    twin["derived_control"] = (
        "Synthetic rename of the first historical design; not an independently generated archived company."
    )
    twin["task"]["situation"] = (
        twin["task"]["situation"]
        .replace("Larkspur Fleet Services", "Juniper Fleet Services")
        .replace("Larkspur", "Juniper")
    )
    examples.append(twin)
    write(
        args.output,
        {
            "purpose": "Retrieval/calibration controls, not accepted new-corpus designs.",
            "examples": examples,
            "pairs": [
                {
                    "left": examples[0]["id"],
                    "right": twin["id"],
                    "expected": "variant",
                    "basis": "Only incidental client names changed; the decision and dependency structure are identical.",
                },
                {
                    "left": examples[0]["id"],
                    "right": examples[1]["id"],
                    "expected": "distinct",
                    "basis": "Account-balance correction versus forward-looking liquidity/financing assessment. Shared audit occupations do not make the decision problems identical.",
                },
            ],
        },
    )


if __name__ == "__main__":
    main()
