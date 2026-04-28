from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommitteeMemberScore:
    model: str
    score: float


def disagreement_index(scores: list[CommitteeMemberScore]) -> float:
    if not scores:
        return 0.0
    values = [float(s.score) for s in scores]
    hi = max(values)
    lo = min(values)
    return max(0.0, hi - lo)


def committee_score(scores: list[CommitteeMemberScore]) -> float:
    if not scores:
        return 0.0
    values = [float(s.score) for s in scores]
    return sum(values) / len(values)


def append_shadow_observation(
    *,
    output_path: str,
    experiment_id: str,
    decision_id: str,
    market_id: str,
    members: list[CommitteeMemberScore],
) -> dict[str, float | str]:
    exp_id = (experiment_id or "").strip()
    if not exp_id:
        raise ValueError("experiment_id is required for committee shadow observations")

    c_score = committee_score(members)
    d_index = disagreement_index(members)
    payload = {
        "experiment_id": exp_id,
        "decision_id": decision_id,
        "market_id": market_id,
        "committee_score": c_score,
        "disagreement_index": d_index,
        "members": [{"model": m.model, "score": float(m.score)} for m in members],
    }
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return {"committee_score": c_score, "disagreement_index": d_index, "experiment_id": exp_id}
