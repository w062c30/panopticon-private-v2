import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from panopticon_py.hunting import semantic_router as sr
from panopticon_py.strategy.bayesian_engine import load_cluster_mapping_for_engine


class TestSemanticRouter(unittest.TestCase):
    def test_fallback_no_key(self) -> None:
        with patch.dict("os.environ", {"NVIDIA_API_KEY": ""}, clear=False):
            out = sr.nvidia_extract_market_semantics("t", "d", [], api_key=None)
        self.assertEqual(out["Parent_Theme"], "UNKNOWN_CLUSTER")
        self.assertEqual(out["Directional_Vector"], 1)

    @patch("panopticon_py.hunting.semantic_router.post_nvidia_chat_completion_safe")
    def test_parse_valid_json(self, mock_post: MagicMock) -> None:
        mock_post.return_value = '{"Parent_Theme": "US_Election_2024", "Entities": ["A"], "Directional_Vector": -1}'
        out = sr.nvidia_extract_market_semantics("x", "y", [], api_key="k")
        self.assertEqual(out["Parent_Theme"], "US_Election_2024")
        self.assertEqual(out["Entities"], ["A"])
        self.assertEqual(out["Directional_Vector"], -1)

    @patch("panopticon_py.hunting.semantic_router.post_nvidia_chat_completion_safe")
    def test_fence_strip(self, mock_post: MagicMock) -> None:
        mock_post.return_value = '```json\n{"Parent_Theme": "X", "Entities": [], "Directional_Vector": 1}\n```'
        out = sr.nvidia_extract_market_semantics("t", "d", [])
        self.assertEqual(out["Parent_Theme"], "X")

    @patch("panopticon_py.hunting.semantic_router.post_nvidia_chat_completion_safe")
    def test_invalid_vector_fallback(self, mock_post: MagicMock) -> None:
        mock_post.return_value = '{"Parent_Theme": "X", "Entities": [], "Directional_Vector": 99}'
        out = sr.nvidia_extract_market_semantics("t", "d", [])
        self.assertEqual(out["Parent_Theme"], "UNKNOWN_CLUSTER")

    def test_atomic_write_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cluster_mapping.json"
            sr.write_cluster_mapping_atomic(p.as_posix(), {"m1": {"cluster_id": "C1", "internal_direction": 1}})
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(data["m1"]["cluster_id"], "C1")
            sr.write_cluster_mapping_atomic(p.as_posix(), {"m1": {"cluster_id": "C2", "internal_direction": -1}})
            data2 = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(data2["m1"]["cluster_id"], "C2")

    def test_load_cluster_mapping_for_engine(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.json"
            p.write_text(
                json.dumps({"abc": {"cluster_id": "K1", "internal_direction": 1}}),
                encoding="utf-8",
            )
            cmap = sr.load_cluster_mapping_for_engine(p.as_posix())
            self.assertEqual(cmap["abc"], "K1")
            cmap2 = load_cluster_mapping_for_engine(p.as_posix())
            self.assertEqual(cmap2, cmap)

    def test_gamma_market_id(self) -> None:
        self.assertEqual(sr.gamma_market_id({"conditionId": "0x123"}), "0x123")
        self.assertEqual(sr.gamma_market_id({"id": 42}), "42")

    def test_merge_row(self) -> None:
        m = sr.merge_market_cluster_row(
            {},
            "mid",
            {"Parent_Theme": "P", "Entities": ["e"], "Directional_Vector": 1},
            extra={"x": 1},
        )
        self.assertEqual(m["mid"]["cluster_id"], "P")
        self.assertEqual(m["mid"]["internal_direction"], 1)
        self.assertEqual(m["mid"]["x"], 1)
