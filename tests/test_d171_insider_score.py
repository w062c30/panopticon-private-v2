"""
D171 unit tests — Insider Score fingerprints + Transfer Graph.
Run: pytest tests/test_d171_insider_score.py -v
"""

import asyncio
import json
import math

import pytest

from panopticon_py.hunting.fingerprint_scrubber import (
    _normalized_shannon,
    compute_fingerprint,
    market_concentration,
    size_entropy,
    timing_entropy,
)
from panopticon_py.hunting.transfer_graph import (
    EntityLinker,
    TransferGraphBuilder,
    fund_source_score_from_graph,
    _wallet_to_topic,
)


# ── P4-T1: Entropy functions ───────────────────────────────────────────────────

class TestNormalizedShannon:
    def test_empty_returns_zero(self):
        assert _normalized_shannon([]) == 0.0

    def test_single_nonzero_returns_zero(self):
        assert _normalized_shannon([5]) == 0.0

    def test_uniform_dist_returns_one(self):
        counts = [1, 1, 1, 1]
        val = _normalized_shannon(counts)
        assert 0.99 < val <= 1.0

    def test_single_bucket_returns_zero(self):
        counts = [10]
        assert _normalized_shannon(counts) == 0.0

    def test_all_zero_returns_zero(self):
        assert _normalized_shannon([0, 0, 0]) == 0.0


class TestSizeEntropy:
    def test_empty_input(self):
        assert size_entropy([]) == 0.0

    def test_single_value(self):
        assert size_entropy([100.0]) == 0.0

    def test_all_same_size_no_entropy(self):
        vals = [100.0] * 10
        assert size_entropy(vals) == 0.0

    def test_diverse_sizes_high_entropy(self):
        vals = [10.0, 50.0, 100.0, 500.0, 1000.0, 5000.0, 10000.0, 50000.0, 100000.0, 500000.0]
        val = size_entropy(vals, n_buckets=10)
        assert val > 0.8

    def test_wrong_n_buckets(self):
        assert size_entropy([1.0, 2.0], n_buckets=1) == 0.0


class TestTimingEntropy:
    def test_empty_input(self):
        assert timing_entropy([]) == 0.0

    def test_single_timestamp(self):
        assert timing_entropy([1000]) == 0.0

    def test_intervals_all_same_low_entropy(self):
        # Every trade 1 hour apart → low entropy
        ts = list(range(0, 3600 * 12, 3600))
        val = timing_entropy(ts)
        assert val < 0.5  # should be low since intervals are uniform


class TestMarketConcentration:
    def test_empty(self):
        result = market_concentration([])
        assert result == {"max": 0.0}

    def test_all_same_category(self):
        result = market_concentration(["politics", "politics", "politics"])
        assert result["max"] == 1.0
        assert result["politics"] == 1.0

    def test_diverse_categories_low_concentration(self):
        cats = ["politics", "crypto", "sports", "economy"]
        result = market_concentration(cats)
        assert result["max"] <= 0.5


class TestComputeFingerprint:
    def test_empty_trades(self):
        result = compute_fingerprint("0xtest", [])
        assert result["size_entropy"] == 0.0
        assert result["timing_entropy"] == 0.0
        assert result["n_trades_sampled"] == 0

    def test_trades_with_all_fields(self):
        trades = [
            {"size": "100", "timestamp_seconds": 1700000000, "category": "politics"},
            {"size": "200", "timestamp_seconds": 1700000100, "category": "crypto"},
        ]
        result = compute_fingerprint("0xtest", trades)
        assert result["n_trades_sampled"] == 2
        assert 0.0 <= result["size_entropy"] <= 1.0
        assert "computed_at_utc" in result

    def test_skips_missing_size(self):
        trades = [{"timestamp_seconds": 1700000000, "category": "politics"}]
        result = compute_fingerprint("0xtest", trades)
        # n_trades_sampled counts all trades; sizes that can't float are skipped
        assert result["n_trades_sampled"] == 1
        assert result["size_entropy"] == 0.0  # no valid sizes → 0 entropy


# ── P4-T2: EntityLinker + TransferGraph ──────────────────────────────────────

class TestWalletToTopic:
    def test_known_wallet(self):
        topic = _wallet_to_topic("0x1234567890abcdef1234567890abcdef12345678")
        assert topic == "0x0000000000000000000000001234567890abcdef1234567890abcdef12345678"
        assert len(topic) == 66

    def test_already_lowercase(self):
        topic = _wallet_to_topic("abcdef1234567890abcdef1234567890abcdef12345678")
        assert topic.startswith("0x")


class TestEntityLinkerClassify:
    def test_unknown_returns_default(self):
        linker = EntityLinker()
        label, source, conf = linker.classify("0xdeadbeef00000000000000000000000000000000")
        assert label == "UNKNOWN"
        assert source == "default"
        assert 0.0 <= conf <= 1.0

    def test_case_normalized(self):
        linker = EntityLinker()
        label, _, _ = linker.classify("0xDEADBEEF00000000000000000000000000000000")
        label2, _, _ = linker.classify("0xdeadbeef00000000000000000000000000000000")
        assert label == label2  # both UNKNOWN — case normalised


class TestFundSourceScore:
    def test_empty_graph(self):
        linker = EntityLinker()
        assert fund_source_score_from_graph({}, linker) == 0.0
        assert fund_source_score_from_graph({"edges": []}, linker) == 0.0

    def test_all_clean_sources(self):
        linker = EntityLinker()
        # Use a non-blacklist address that classifies as UNKNOWN (not ANONYMIZER/DEX)
        graph = {
            "edges": [
                {"from_addr": "0x1111111111111111111111111111111111111111"},
                {"from_addr": "0x2222222222222222222222222222222222222222"},
            ]
        }
        score = fund_source_score_from_graph(graph, linker)
        # UNKNOWN → 0.3 per edge; avg = 0.3
        assert score == 0.3


# ── D171 integration note ─────────────────────────────────────────────────────

# These tests verify the building blocks are in place.
# Full integration (orchestrator wiring + soak verification) requires runtime execution.