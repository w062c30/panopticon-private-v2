import tempfile
import unittest
from pathlib import Path

from panopticon_py.committee_shadow import (
    CommitteeMemberScore,
    append_shadow_observation,
    committee_score,
    disagreement_index,
)


class CommitteeShadowTests(unittest.TestCase):
    def test_score_and_disagreement(self) -> None:
        members = [
            CommitteeMemberScore(model="a", score=0.2),
            CommitteeMemberScore(model="b", score=0.6),
            CommitteeMemberScore(model="c", score=0.8),
        ]
        self.assertAlmostEqual(committee_score(members), 0.5333333333, places=6)
        self.assertAlmostEqual(disagreement_index(members), 0.6, places=6)

    def test_append_requires_experiment_id(self) -> None:
        with self.assertRaises(ValueError):
            append_shadow_observation(
                output_path="data/committee_shadow_scores.jsonl",
                experiment_id="",
                decision_id="d1",
                market_id="m1",
                members=[],
            )

    def test_append_writes_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "shadow.jsonl"
            row = append_shadow_observation(
                output_path=out.as_posix(),
                experiment_id="exp-1",
                decision_id="d1",
                market_id="m1",
                members=[CommitteeMemberScore(model="a", score=0.5)],
            )
            self.assertEqual(row["experiment_id"], "exp-1")
            payload = out.read_text(encoding="utf-8")
            self.assertIn('"committee_score": 0.5', payload)


if __name__ == "__main__":
    unittest.main()
