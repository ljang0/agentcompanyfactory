"""Live pinned-embedding smoke test over historical controls; not a novelty judge."""

from pathlib import Path

import numpy as np

from company_envs.diversity import Embeddings
from company_envs.storage import read, write

root = Path(__file__).resolve().parents[1]
fixture = read(root / "tests" / "fixtures" / "historical.json")
pin = read(root / "catalogs" / "embedding.json")
encoder = Embeddings(root, **pin)
vectors = {}
for example in fixture["examples"]:
    task = example["task"]
    text = " ".join([task["situation"], task["deliverable"], *task.get("hardness", {}).values()])
    vectors[example["id"]] = encoder.encode(text)
results = [
    {**pair, "cosine": float(np.dot(vectors[pair["left"]], vectors[pair["right"]]))}
    for pair in fixture["pairs"]
]
assert results[0]["cosine"] > results[1]["cosine"], results
write(
    root / "tests" / "fixtures" / "retrieval-result.json",
    {
        "embedding": pin,
        "pairs": results,
        "interpretation": "The name-only variant ranks ahead of a genuinely different audit task. No universal cosine threshold is asserted.",
    },
)
print(results)
