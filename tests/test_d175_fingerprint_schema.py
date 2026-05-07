import json

from panopticon_py.hunting.four_d_classifier import normalize_fingerprint_payload, scores_from_parents


def test_normalize_fingerprint_defaults_when_none():
    normalized = normalize_fingerprint_payload(None)
    assert normalized["size_entropy"] == 0.0
    assert normalized["funding_source"] == 0.0
    assert normalized["concentration"]["max"] == 0.0
    assert normalized["insufficient_data"] is False


def test_scores_funding_source_absent_keeps_default_zero():
    fp = {
        "size_entropy": 0.72,
        "concentration": {"max": 0.4},
        "insufficient_data": False,
    }
    scores = scores_from_parents([], fingerprint=fp)
    assert scores.size_entropy == 0.72
    assert scores.concentration == 0.4
    assert scores.funding_source == 0.0


def test_scores_funding_source_valid_value_is_used():
    fp = {
        "size_entropy": 0.72,
        "concentration": {"max": 0.4},
        "funding_source": 0.7,
        "insufficient_data": False,
    }
    scores = scores_from_parents([], fingerprint=fp)
    assert scores.funding_source == 0.7


def test_scores_funding_source_invalid_values_are_sanitized():
    scores_text = scores_from_parents([], fingerprint={"funding_source": "high"})
    scores_hi = scores_from_parents([], fingerprint={"funding_source": 1.8})
    scores_lo = scores_from_parents([], fingerprint={"funding_source": -0.5})
    assert scores_text.funding_source == 0.0
    assert scores_hi.funding_source == 1.0
    assert scores_lo.funding_source == 0.0


def test_load_fingerprint_normalizes_payload():
    class _DummyConn:
        def execute(self, _sql, _params):
            class _Row:
                def fetchone(self):
                    payload = {
                        "size_entropy": "0.9",
                        "concentration": {"max": "2.0"},
                        "funding_source": "high",
                    }
                    return (json.dumps(payload),)

            return _Row()

    from panopticon_py.hunting.four_d_classifier import load_fingerprint_from_watchlist

    fp = load_fingerprint_from_watchlist("0xABC", _DummyConn())
    assert fp is not None
    assert fp["size_entropy"] == 0.9
    assert fp["concentration"]["max"] == 1.0
    assert fp["funding_source"] == 0.0
