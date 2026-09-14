"""Semantic retrieval proposes comparisons; independent review determines variants."""

import threading
from pathlib import Path

import numpy as np

from .catalogs import sector_targets
from .storage import digest, read, write


class Embeddings:
    def __init__(self, root, model, revision):
        self.path = Path(root) / "data" / "embeddings"
        self.model_name, self.revision = model, revision
        self.model = None
        self.lock = threading.Lock()

    def encode(self, text):
        key = digest({"text": text, "model": self.model_name, "revision": self.revision})
        path = self.path / f"{key}.json"
        if path.exists():
            return np.asarray(read(path), dtype=float)
        with self.lock:
            if self.model is None:
                from sentence_transformers import SentenceTransformer

                self.model = SentenceTransformer(self.model_name, revision=self.revision, device="cpu")
            # Encode every chunk rather than silently truncating long canonical descriptions.
            tokens = self.model.tokenizer.encode(text, add_special_tokens=False)
            chunks = [
                self.model.tokenizer.decode(tokens[i : i + 300]) for i in range(0, len(tokens), 300)
            ] or [text]
            vector = self.model.encode(chunks, normalize_embeddings=True, show_progress_bar=False).mean(
                axis=0
            )
            vector /= max(float(np.linalg.norm(vector)), 1e-12)
            write(path, vector.tolist())
            return vector


def nearest(workflow, others, encoder, k=10):
    query = encoder.encode(workflow.canonical_description)
    ranked = []
    for other in others:
        if other.id == workflow.id:
            continue
        score = float(np.dot(query, encoder.encode(other.canonical_description)))
        ranked.append({"workflow_id": other.id, "similarity": score, "workflow": other.model_dump()})
    return sorted(ranked, key=lambda x: (-x["similarity"], x["workflow_id"]))[:k]


def select(entries, companies, company_target, task_target, sectors, seed=0):
    """One representative per company, then marginal occupational/economic coverage and quality."""
    if task_target < company_target:
        raise ValueError("task target must cover every company")
    eligible = [e for e in entries if e["verdict"] == "accept" and e["novelty"] == "distinct"]
    by_company = {}
    for entry in eligible:
        by_company.setdefault(entry["company_id"], []).append(entry)
    targets = sector_targets(sectors, company_target)
    chosen_companies, counts, seen_socs = [], {}, set()
    while len(chosen_companies) < min(company_target, len(by_company)):
        choices = [cid for cid in by_company if cid not in chosen_companies]

        def rank(cid):
            co = companies[cid]
            deficit = targets.get(co.sector, 0) - counts.get(co.sector, 0)
            return (
                -deficit,
                -max(e["quality"] for e in by_company[cid]),
                -len({w.soc for w in co.workers} - seen_socs),
                digest([seed, cid]),
            )

        cid = min(choices, key=rank)
        chosen_companies.append(cid)
        co = companies[cid]
        counts[co.sector] = counts.get(co.sector, 0) + 1
        seen_socs.update(w.soc for w in co.workers)
    chosen, families = [], set()
    for cid in chosen_companies:
        for e in sorted(by_company[cid], key=lambda e: (-e["quality"], digest([seed, e["workflow_id"]]))):
            if e["family_id"] not in families:
                chosen.append(e)
                families.add(e["family_id"])
                break
    task_counts = {}
    for e in chosen:
        sec = companies[e["company_id"]].sector
        task_counts[sec] = task_counts.get(sec, 0) + 1
    task_targets = sector_targets(sectors, task_target)
    remaining = [
        e for e in eligible if e["company_id"] in chosen_companies and e["family_id"] not in families
    ]
    while remaining and len(chosen) < task_target:
        e = min(
            remaining,
            key=lambda e: (
                -e["quality"],
                -(
                    task_targets.get(companies[e["company_id"]].sector, 0)
                    - task_counts.get(companies[e["company_id"]].sector, 0)
                ),
                digest([seed, e["workflow_id"]]),
            ),
        )
        chosen.append(e)
        families.add(e["family_id"])
        sec = companies[e["company_id"]].sector
        task_counts[sec] = task_counts.get(sec, 0) + 1
        remaining = [r for r in remaining if r["family_id"] not in families]
    return chosen
