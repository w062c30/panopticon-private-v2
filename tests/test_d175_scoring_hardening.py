import json

from panopticon_py.hunting.four_d_classifier import FourDScores, compute_insider_score


class _DummyConn:
    def __init__(self):
        self.rows = []

    def execute(self, _sql, params):
        self.rows.append(params)
        return self


def test_invalid_weight_env_falls_back(monkeypatch):
    monkeypatch.setenv("INSIDER_W_IDI", "abc")
    monkeypatch.setenv("INSIDER_W_BURST", "-1")
    monkeypatch.setenv("INSIDER_W_TAKER", "3.5")
    monkeypatch.setenv("INSIDER_W_SIZE_ENT", "0.15")
    monkeypatch.setenv("INSIDER_W_CONC", "0.1")
    monkeypatch.setenv("INSIDER_W_FUND", "0.0")

    s = compute_insider_score(FourDScores(idi=0.5, burst=0.5, taker_ratio=0.5))
    assert 0.0 <= s <= 1.0


def test_inference_log_payload_trim_guard(monkeypatch):
    from panopticon_py.hunting import four_d_classifier as mod

    monkeypatch.setattr(mod, "_MAX_INFERENCE_PAYLOAD_BYTES", 20)
    conn = _DummyConn()
    compute_insider_score(FourDScores(idi=0.5, burst=0.5, taker_ratio=0.5), wallet_address="0xabc", db_conn=conn)
    payload = json.loads(conn.rows[0][1])
    assert payload["payload_trimmed"] is True


def test_inference_wallet_is_lower_and_capped():
    conn = _DummyConn()
    wallet = "0xABCDEF1234567890ABCDEF1234567890ABCDEF1234"
    compute_insider_score(FourDScores(idi=0.5, burst=0.5, taker_ratio=0.5), wallet_address=wallet, db_conn=conn)
    stored_wallet = conn.rows[0][0]
    assert stored_wallet == wallet.lower()[:42]
