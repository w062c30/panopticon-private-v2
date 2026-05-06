import json

from panopticon_py.hunting.four_d_classifier import FourDScores, compute_insider_score, scores_from_parents
from panopticon_py.hunting.trade_aggregate import ParentTrade


def _parent(side: int, volume: float, t0: float) -> ParentTrade:
    return ParentTrade(
        taker="0xabc",
        side=side,
        volume=volume,
        first_ts_ms=t0,
        last_ts_ms=t0 + 1,
        child_count=1,
        market_id=None,
    )


def test_taker_ratio_directional_when_not_assume_all_taker():
    parents = [_parent(1, 100.0, 0.0), _parent(1, 100.0, 2.0)]
    scores = scores_from_parents(parents, assume_all_taker=False)
    assert scores.taker_ratio == 1.0


def test_taker_ratio_bidirectional_uses_soft_floor():
    parents = [_parent(1, 100.0, 0.0), _parent(-1, 100.0, 2.0)]
    scores = scores_from_parents(parents, assume_all_taker=False)
    assert scores.taker_ratio == 0.3


def test_assume_all_taker_still_returns_1():
    scores = scores_from_parents([], assume_all_taker=True)
    assert scores.taker_ratio == 1.0


def test_compute_insider_score_without_db_still_works():
    scores = FourDScores(idi=0.5, burst=0.5, taker_ratio=0.5)
    s = compute_insider_score(scores)
    assert 0.0 <= s <= 1.0


def test_inference_log_writes_to_db():
    class _DummyConn:
        def __init__(self):
            self.rows = []

        def execute(self, _sql, params):
            self.rows.append(params)
            return self

    conn = _DummyConn()
    scores = FourDScores(idi=0.5, burst=0.5, taker_ratio=0.5)
    compute_insider_score(scores, wallet_address="0xabc", db_conn=conn)
    assert conn.rows
    payload = json.loads(conn.rows[0][1])
    assert "insider_score" in payload
    assert "w_idi" in payload
