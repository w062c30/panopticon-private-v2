from panopticon_py.hunting.four_d_classifier import (
    FourDScores,
    compute_insider_score,
    load_fingerprint_from_watchlist,
    scores_from_parents,
)


def test_scores_with_fingerprint():
    fp = {
        "size_entropy": 0.72,
        "concentration": {"max": 0.4},
        "insufficient_data": False,
    }
    scores = scores_from_parents([], fingerprint=fp)
    assert scores.size_entropy == 0.72
    assert scores.concentration == 0.4
    assert not scores.insufficient_data


def test_scores_without_fingerprint_degrades_gracefully():
    scores = scores_from_parents([])
    assert scores.size_entropy == 0.0
    assert scores.concentration == 0.0


def test_insufficient_data_halves_score():
    scores_full = FourDScores(
        idi=0.8,
        burst=0.8,
        taker_ratio=0.8,
        size_entropy=0.8,
        concentration=0.8,
    )
    scores_low = FourDScores(
        idi=0.8,
        burst=0.8,
        taker_ratio=0.8,
        size_entropy=0.8,
        concentration=0.8,
        insufficient_data=True,
    )
    assert compute_insider_score(scores_low) < compute_insider_score(scores_full)


def test_fingerprint_lookup_parses_valid_json():
    class _DummyConn:
        def execute(self, _sql, _params):
            class _Row:
                def fetchone(self):
                    return ('{"size_entropy":0.5}',)

            return _Row()

    fp = load_fingerprint_from_watchlist("0xABC", _DummyConn())
    assert fp is not None
    assert fp["size_entropy"] == 0.5

