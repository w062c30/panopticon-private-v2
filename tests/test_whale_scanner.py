"""
tests/test_whale_scanner.py
Tests for whale_scanner scoring logic.
"""
from panopticon_py.hunting.whale_scanner import _score_trade, _score_trade_with_book


def test_score_trade_whale_signal_fires():
    """Large whale bet on concentrated wallet should score >= 6."""
    trade = {"size": "50000", "price": "0.55", "side": "BUY"}
    history = [{"market": "market-a"}, {"market": "market-a"}]
    score, flags = _score_trade(trade, history, market_avg_size=1000.0)
    assert score >= 6.0, f"Expected score >= 6.0, got {score}"
    assert any("WHALE" in f or "FRESH" in f for f in flags)


def test_score_trade_small_trade_ignored():
    """Small normal trade should score < 6."""
    trade = {"size": "100", "price": "0.50", "side": "BUY"}
    history = [{"market": m} for m in ["a", "b", "c", "d", "e", "f", "g", "h"]]
    score, flags = _score_trade(trade, history, market_avg_size=500.0)
    assert score < 6.0, f"Expected score < 6.0, got {score}"


def test_score_trade_near_resolution():
    """Trade near resolution should get niche market flag."""
    trade = {"size": "1000", "price": "0.95", "side": "BUY"}
    history = [{"market": "a"}] * 5
    score, flags = _score_trade(trade, history, market_avg_size=100.0)
    assert any("NEAR_RESOLUTION" in f for f in flags), f"Expected NEAR_RESOLUTION flag, got {flags}"


def test_score_trade_concentrated_wallet():
    """Wallet with 100% concentration should get concentration flag."""
    trade = {"size": "5000", "price": "0.50", "side": "SELL"}
    history = [{"market": "only-one"}] * 4
    score, flags = _score_trade(trade, history, market_avg_size=500.0)
    assert any("CONCENTRATION" in f for f in flags), f"Expected CONCENTRATION flag, got {flags}"


def test_score_trade_large_bet_multiplier():
    """Bet 3x+ market avg should get LARGE_BET flag."""
    trade = {"size": "30000", "price": "0.50", "side": "BUY"}
    history = []
    score, flags = _score_trade(trade, history, market_avg_size=1000.0)
    assert any("LARGE_BET" in f or "WHALE" in f for f in flags), f"Expected LARGE_BET flag, got {flags}"


def test_score_trade_thin_book_signal_fires():
    """Large bet into thin orderbook should add THIN_BOOK flag."""
    score, flags = _score_trade_with_book(
        trade={"size": 30000 / 0.55, "price": 0.55},
        wallet_history=[{"market": "x"}] * 5,
        market_avg_size=5000.0,
        book_depth_ask_usd=40_000.0,
        book_depth_bid_usd=40_000.0,
    )
    assert score >= 1.0
    assert any("THIN_BOOK" in f for f in flags), f"Expected THIN_BOOK flag, got {flags}"


# ── D33: Normalize helper tests ────────────────────────────────────────────────

def test_normalize_wallet_handles_list_format():
    """D33: Polymarket CLOB may return maker as list [0x...]."""
    from panopticon_py.hunting.whale_scanner import _normalize_wallet

    assert _normalize_wallet(["0xabc123def456abc1"]) == "0xabc123def456abc1"
    assert _normalize_wallet(["0xdeadbeef12345678deadbeef12345678"]) == "0xdeadbeef12345678deadbeef12345678"


def test_normalize_wallet_rejects_invalid_inputs():
    """D33: Non-0x strings, empty values, and non-collection types return empty string."""
    from panopticon_py.hunting.whale_scanner import _normalize_wallet

    assert _normalize_wallet("") == ""
    assert _normalize_wallet([]) == ""
    assert _normalize_wallet(None) == ""
    assert _normalize_wallet(["not-an-address"]) == ""
    assert _normalize_wallet({"address": "not-hex"}) == ""
    assert _normalize_wallet("0xshort") == ""  # too short


def test_normalize_token_id_handles_nested_list():
    """D33: Gamma API may return clobTokenIds as nested list [[token_id]]."""
    from panopticon_py.hunting.whale_scanner import _normalize_token_id

    assert _normalize_token_id("0xtoken123") == "0xtoken123"
    nested = [["0xdeadbeef12345678deadbeef12345678"]]
    assert _normalize_token_id(nested) == "0xdeadbeef12345678deadbeef12345678"
    assert _normalize_token_id([]) == ""
    assert _normalize_token_id([[]]) == ""


def test_normalize_token_id_handles_json_string_list():
    """D33: Gamma API may return clobTokenIds as a JSON-string: '["token1","token2"]'."""
    from panopticon_py.hunting.whale_scanner import _normalize_token_id

    json_str = '["0xtoken1abc123def456", "0xtoken2abc123def456"]'
    result = _normalize_token_id(json_str)
    assert result in ("0xtoken1abc123def456", "0xtoken2abc123def456"), f"Expected first token, got {result}"
    assert _normalize_token_id('["single"]') == "single"


def test_whale_min_size_t2_accepts_small_trade():
    """D35: T2 floor is $75 (not $5000). T2 markets have retail sizing."""
    from panopticon_py.hunting.whale_scanner import _WHALE_MIN_SIZE_BY_TIER
    assert _WHALE_MIN_SIZE_BY_TIER["t2"] == 75.0


def test_whale_min_size_t1_btc5m_calibrated():
    """D72: T1 floor lowered to $50 (was $5000) to match BTC 5m CLOB trade sizing.

    D68 Phase 0 data showed BTC 5m trades range $0.10–$259 (retail-like).
    Old $5000 floor blocked all T1 wallets from whale_scanner → wallet_observations
    stayed empty for T1 → _collect_insider_sources returned 0 sources → INSUFFICIENT_CONSENSUS.
    """
    from panopticon_py.hunting.whale_scanner import _WHALE_MIN_SIZE_BY_TIER
    assert _WHALE_MIN_SIZE_BY_TIER["t1"] == 50.0


def test_whale_score_to_insider_score_at_threshold():
    """D37: score=2.0 maps to exactly 0.55 (just over INSIDER_SCORE_THRESHOLD)"""
    from panopticon_py.hunting.whale_scanner import _whale_score_to_insider_score
    result = _whale_score_to_insider_score(2.0)
    assert abs(result - 0.55) < 1e-9


def test_whale_score_to_insider_score_high():
    """D37: score=7.0 maps to 0.80"""
    from panopticon_py.hunting.whale_scanner import _whale_score_to_insider_score
    result = _whale_score_to_insider_score(7.0)
    assert abs(result - 0.80) < 1e-9


def test_whale_score_to_insider_score_capped():
    """D37: score=100.0 is capped at 0.95"""
    from panopticon_py.hunting.whale_scanner import _whale_score_to_insider_score
    result = _whale_score_to_insider_score(100.0)
    assert result == 0.95


def test_upsert_whale_wallet_skips_empty_wallet():
    """D37: empty wallet string must not raise"""
    import sqlite3
    from panopticon_py.hunting.whale_scanner import _upsert_whale_wallet_as_entity

    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE discovered_entities (
            address TEXT PRIMARY KEY,
            insider_score REAL,
            source TEXT,
            last_seen_market TEXT,
            updated_ts_utc TEXT
        )
    """)
    conn.commit()

    class FakeDB:
        pass
    fake_db = FakeDB()
    fake_db.conn = conn

    # Should not raise — empty wallet is skipped
    _upsert_whale_wallet_as_entity(fake_db, "", 5.0, "test-market")
    assert conn.execute("SELECT COUNT(*) FROM discovered_entities").fetchone()[0] == 0

    # Short wallet also skipped
    _upsert_whale_wallet_as_entity(fake_db, "0x123", 5.0, "test-market")
    assert conn.execute("SELECT COUNT(*) FROM discovered_entities").fetchone()[0] == 0


# ── D39: wallet_observations bridge tests ──────────────────────────────────────

def test_collect_trade_wallet_accumulates_distinct():
    """D39: _collect_trade_wallet accumulates distinct wallets per market."""
    import sqlite3
    from panopticon_py.hunting.whale_scanner import (
        _collect_trade_wallet,
        _clear_trade_wallets,
        _trade_wallets_seen,
    )

    _clear_trade_wallets()

    trade1 = {"proxyWallet": "0x1111111111111111111111111111111111111111", "size": "100", "price": "0.50"}
    trade2 = {"proxyWallet": "0x2222222222222222222222222222222222222222", "size": "200", "price": "0.60"}
    trade3 = {"proxyWallet": "0x1111111111111111111111111111111111111111", "size": "300", "price": "0.70"}  # duplicate

    _collect_trade_wallet(trade1, "market-a", "0xtoken1")
    _collect_trade_wallet(trade2, "market-a", "0xtoken1")
    _collect_trade_wallet(trade3, "market-a", "0xtoken1")

    assert len(_trade_wallets_seen) == 2
    assert "0x1111111111111111111111111111111111111111" in _trade_wallets_seen
    assert "0x2222222222222222222222222222222222222222" in _trade_wallets_seen
    _clear_trade_wallets()


def test_collect_trade_wallet_skips_invalid():
    """D39: _collect_trade_wallet ignores invalid wallets."""
    from panopticon_py.hunting.whale_scanner import (
        _collect_trade_wallet,
        _clear_trade_wallets,
        _trade_wallets_seen,
    )

    _clear_trade_wallets()

    _collect_trade_wallet({"size": "100", "price": "0.50"}, "market-a", "0xtoken1")  # no wallet
    _collect_trade_wallet({"proxyWallet": "0xshort"}, "market-a", "0xtoken1")  # too short
    _collect_trade_wallet({"proxyWallet": None}, "market-a", "0xtoken1")  # None

    assert len(_trade_wallets_seen) == 0
    _clear_trade_wallets()


def test_inject_trade_wallets_writes_clob_trade_obs():
    """D39: injected wallets appear in wallet_observations with obs_type=clob_trade."""
    import sqlite3
    from panopticon_py.hunting.whale_scanner import (
        _clear_trade_wallets,
        _collect_trade_wallet,
        _inject_trade_wallets_to_observations,
        _trade_wallets_seen,
    )

    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE wallet_observations (
            obs_id TEXT PRIMARY KEY,
            address TEXT NOT NULL,
            market_id TEXT,
            obs_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            ingest_ts_utc TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX idx_wo_addr_market_type
        ON wallet_observations(address, market_id, obs_type)
    """)
    conn.commit()

    class FakeDB:
        pass
    fake_db = FakeDB()
    fake_db.conn = conn

    _clear_trade_wallets()
    _collect_trade_wallet(
        {"proxyWallet": "0xaaa1111111111111111111111111111111111111", "size": "100", "price": "0.50"},
        "test-market-slug", "0xtoken1",
    )
    _collect_trade_wallet(
        {"proxyWallet": "0xbbb2222222222222222222222222222222222222", "size": "200", "price": "0.60"},
        "test-market-slug", "0xtoken1",
    )

    count = _inject_trade_wallets_to_observations(fake_db)

    assert count == 2
    rows = conn.execute(
        "SELECT address, market_id, obs_type FROM wallet_observations WHERE obs_type='clob_trade'"
    ).fetchall()
    assert len(rows) == 2
    addrs = {r[0] for r in rows}
    assert "0xaaa1111111111111111111111111111111111111" in addrs
    assert "0xbbb2222222222222222222222222222222222222" in addrs
    _clear_trade_wallets()


def test_inject_trade_wallets_dedups_across_cycles():
    """D39: unique index prevents same (address, market_id, obs_type) double-insert."""
    import sqlite3
    from panopticon_py.hunting.whale_scanner import (
        _clear_trade_wallets,
        _collect_trade_wallet,
        _inject_trade_wallets_to_observations,
        _trade_wallets_seen,
    )

    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE wallet_observations (
            obs_id TEXT PRIMARY KEY,
            address TEXT NOT NULL,
            market_id TEXT,
            obs_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            ingest_ts_utc TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX idx_wo_addr_market_type
        ON wallet_observations(address, market_id, obs_type)
    """)
    conn.commit()

    class FakeDB:
        pass
    fake_db = FakeDB()
    fake_db.conn = conn

    # Cycle 1
    _clear_trade_wallets()
    _collect_trade_wallet(
        {"proxyWallet": "0xccc3333333333333333333333333333333333333", "size": "100", "price": "0.50"},
        "dedup-market", "0xtoken1",
    )
    injected1 = _inject_trade_wallets_to_observations(fake_db)

    # Cycle 2: same wallet — explicit dedup check blocks duplicate
    _clear_trade_wallets()
    _collect_trade_wallet(
        {"proxyWallet": "0xccc3333333333333333333333333333333333333", "size": "200", "price": "0.60"},
        "dedup-market", "0xtoken1",
    )
    injected2 = _inject_trade_wallets_to_observations(fake_db)

    assert injected1 == 1
    assert injected2 == 0  # unique index blocked duplicate
    total = conn.execute(
        "SELECT COUNT(*) FROM wallet_observations WHERE address='0xccc3333333333333333333333333333333333333'"
    ).fetchone()[0]
    assert total == 1
    _clear_trade_wallets()


def test_inject_trade_wallets_empty_returns_zero():
    """D39: no wallets accumulated → returns 0, no DB writes."""
    import sqlite3
    from panopticon_py.hunting.whale_scanner import (
        _clear_trade_wallets,
        _inject_trade_wallets_to_observations,
        _trade_wallets_seen,
    )

    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE wallet_observations (
            obs_id TEXT PRIMARY KEY,
            address TEXT NOT NULL,
            market_id TEXT,
            obs_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            ingest_ts_utc TEXT NOT NULL
        )
    """)
    conn.commit()

    class FakeDB:
        pass
    fake_db = FakeDB()
    fake_db.conn = conn

    _clear_trade_wallets()  # already empty
    count = _inject_trade_wallets_to_observations(fake_db)
    assert count == 0
    total = conn.execute("SELECT COUNT(*) FROM wallet_observations").fetchone()[0]
    assert total == 0
    _clear_trade_wallets()


# ── D42: Active market registry tests ────────────────────────────────────────

def test_register_active_markets_populates_registry():
    """register_active_markets() populates _active_market_registry from token_tier_map."""
    from panopticon_py.hunting.whale_scanner import (
        register_active_markets,
        _active_market_registry,
    )

    # Clear first
    _active_market_registry.clear()

    token_map = {
        "tok_aaa": "t1",
        "tok_bbb": "t2",
        "tok_ccc": "t5",
    }
    register_active_markets(token_map)

    assert _active_market_registry == token_map
    assert _active_market_registry["tok_aaa"] == "t1"
    assert _active_market_registry["tok_bbb"] == "t2"
    assert _active_market_registry["tok_ccc"] == "t5"


def test_register_active_markets_clears_stale_entries():
    """register_active_markets() clears previous entries before updating."""
    from panopticon_py.hunting.whale_scanner import (
        register_active_markets,
        _active_market_registry,
    )

    _active_market_registry.clear()

    register_active_markets({"tok_old": "t1"})
    assert "tok_old" in _active_market_registry

    register_active_markets({"tok_new": "t3"})
    assert "tok_old" not in _active_market_registry
    assert "tok_new" in _active_market_registry


def test_classify_tier_respects_tier_override():
    """_classify_tier returns tier_override when provided, bypassing heuristic."""
    from panopticon_py.hunting.whale_scanner import _classify_tier

    # T1 override — even though slug doesn't match
    result = _classify_tier({"slug": "random-market"}, tier_override="t1")
    assert result == "t1"

    # T5 override
    result = _classify_tier({"slug": "nothing-like-sports"}, tier_override="t5")
    assert result == "t5"

    # No override — T1 slug but endDateIso far future (no discount) → T3
    result = _classify_tier({
        "slug": "btc-updown-5m-1234567890",
        "endDateIso": "2030-01-01T00:00:00Z",
    })
    assert result == "t3"

    # No override — T5 sports slug with endDateIso → detected as T5
    result = _classify_tier({
        "slug": "nba-game-miami-ny",
        "category": "sports",
        "endDateIso": "2026-04-27T00:00:00Z",
    })
    assert result == "t5"


def test_build_registry_market_creates_correct_dict():
    """_build_registry_market produces a market dict with market_tier override."""
    from panopticon_py.hunting.whale_scanner import _build_registry_market

    m = _build_registry_market("tok_xyz_123", "t1")
    assert m["token_id"] == "tok_xyz_123"
    assert m["clobTokenIds"] == "tok_xyz_123"
    assert m["market_tier"] == "t1"

    m2 = _build_registry_market("tok_abc", "t3")
    assert m2["market_tier"] == "t3"
    assert m2["token_id"] == "tok_abc"


# ── D46: Path B wallet promotion tests ─────────────────────────────────────────

def test_promote_frequent_path_b_wallets_promotes_repeat_wallet():
    """D46: wallet appearing >=2 times in same market gets insider_score=0.55."""
    import sqlite3
    import time
    from panopticon_py.hunting.whale_scanner import _promote_frequent_path_b_wallets

    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE wallet_observations (
            obs_id TEXT PRIMARY KEY,
            address TEXT NOT NULL,
            market_id TEXT NOT NULL,
            obs_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            ingest_ts_utc TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE discovered_entities (
            entity_id TEXT PRIMARY KEY,
            insider_score REAL,
            source TEXT,
            last_seen_market TEXT,
            updated_ts_utc TEXT,
            trust_score REAL,
            primary_tag TEXT,
            sample_size INTEGER,
            last_updated_at TEXT
        )
    """)
    conn.commit()

    class FakeDB:
        pass
    fake_db = FakeDB()
    fake_db.conn = conn

    # Insert same wallet twice in same market (within lookback window)
    now_utc = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())
    for _ in range(2):
        conn.execute("""
            INSERT INTO wallet_observations
                (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc)
            VALUES (?, ?, ?, 'clob_trade', '{}', ?)
        """, (f"obs_{_}", "0xaaa1111111111111111111111111111111111111", "tok_token1", now_utc))
    conn.commit()

    promoted = _promote_frequent_path_b_wallets(fake_db)

    assert promoted == 1
    row = conn.execute(
        "SELECT insider_score FROM discovered_entities WHERE entity_id='0xaaa1111111111111111111111111111111111111'"
    ).fetchone()
    assert row is not None
    assert row[0] == 0.55


def test_promote_promotes_single_occurrence_wallet():
    """D47: wallet appearing only ONCE is NOW promoted (threshold lowered from >=2 to >=1)."""
    import sqlite3
    import time
    from panopticon_py.hunting.whale_scanner import _promote_frequent_path_b_wallets

    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE wallet_observations (
            obs_id TEXT PRIMARY KEY,
            address TEXT NOT NULL,
            market_id TEXT NOT NULL,
            obs_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            ingest_ts_utc TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE discovered_entities (
            entity_id TEXT PRIMARY KEY,
            insider_score REAL,
            source TEXT,
            last_seen_market TEXT,
            updated_ts_utc TEXT,
            trust_score REAL,
            primary_tag TEXT,
            sample_size INTEGER,
            last_updated_at TEXT
        )
    """)
    conn.commit()

    class FakeDB:
        pass
    fake_db = FakeDB()
    fake_db.conn = conn

    # Insert only once — D47 threshold >= 1, so this SHOULD be promoted
    now_utc = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())
    conn.execute("""
        INSERT INTO wallet_observations
            (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc)
        VALUES (?, ?, ?, 'clob_trade', '{}', ?)
    """, ("single_obs", "0xbbb2222222222222222222222222222222222222", "tok_token2", now_utc))
    conn.commit()

    promoted = _promote_frequent_path_b_wallets(fake_db)

    # D47: obs_count >= 1, so single occurrence IS promoted
    assert promoted == 1
    row = conn.execute(
        "SELECT insider_score FROM discovered_entities WHERE entity_id='0xbbb2222222222222222222222222222222222222'"
    ).fetchone()
    assert row is not None
    assert row[0] == 0.55


def test_promote_does_not_downgrade_existing_high_score():
    """D46: wallet already in discovered_entities with higher score is NOT downgraded."""
    import sqlite3
    import time
    from panopticon_py.hunting.whale_scanner import _promote_frequent_path_b_wallets

    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE wallet_observations (
            obs_id TEXT PRIMARY KEY,
            address TEXT NOT NULL,
            market_id TEXT NOT NULL,
            obs_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            ingest_ts_utc TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE discovered_entities (
            entity_id TEXT PRIMARY KEY,
            insider_score REAL,
            source TEXT,
            last_seen_market TEXT,
            updated_ts_utc TEXT,
            trust_score REAL,
            primary_tag TEXT,
            sample_size INTEGER,
            last_updated_at TEXT
        )
    """)
    conn.commit()

    class FakeDB:
        pass
    fake_db = FakeDB()
    fake_db.conn = conn

    # Pre-existing high-score entry (from whale alert Path A)
    conn.execute("""
        INSERT INTO discovered_entities
            (entity_id, insider_score, source, last_seen_market, updated_ts_utc, trust_score, primary_tag, sample_size, last_updated_at)
        VALUES
            ('0xccc3333333333333333333333333333333333333', 0.80, 'whale_scanner', 'high-score-market',
             strftime('%Y-%m-%dT%H:%M:%fZ','now'), 80.0, 'whale_scanner', 5, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    """)
    conn.commit()

    # Insert same wallet twice (Path B trigger)
    now_utc = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())
    for i in range(2):
        conn.execute("""
            INSERT INTO wallet_observations
                (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc)
            VALUES (?, ?, ?, 'clob_trade', '{}', ?)
        """, (f"obs_{i}", "0xccc3333333333333333333333333333333333333", "tok_token3", now_utc))
    conn.commit()

    promoted = _promote_frequent_path_b_wallets(fake_db)

    # promoted may be 0 (existing entry) OR 1 (new entry) — behavior not specified
    # The key invariant: score must remain 0.80, not downgraded to 0.55
    row = conn.execute(
        "SELECT insider_score FROM discovered_entities WHERE entity_id='0xccc3333333333333333333333333333333333333'"
    ).fetchone()
    assert row is not None
    assert row[0] >= 0.80  # NOT downgraded


def test_promote_skips_unknown_address():
    """D46: wallet_observations with address='unknown' are not promoted."""
    import sqlite3
    import time
    from panopticon_py.hunting.whale_scanner import _promote_frequent_path_b_wallets

    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE wallet_observations (
            obs_id TEXT PRIMARY KEY,
            address TEXT NOT NULL,
            market_id TEXT NOT NULL,
            obs_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            ingest_ts_utc TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE discovered_entities (
            entity_id TEXT PRIMARY KEY,
            insider_score REAL,
            source TEXT,
            last_seen_market TEXT,
            updated_ts_utc TEXT,
            trust_score REAL,
            primary_tag TEXT,
            sample_size INTEGER,
            last_updated_at TEXT
        )
    """)
    conn.commit()

    class FakeDB:
        pass
    fake_db = FakeDB()
    fake_db.conn = conn

    # Insert 'unknown' address multiple times
    now_utc = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())
    for i in range(3):
        conn.execute("""
            INSERT INTO wallet_observations
                (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc)
            VALUES (?, ?, ?, 'clob_trade', '{}', ?)
        """, (f"unknown_{i}", "unknown", "tok_token4", now_utc))
    conn.commit()

    promoted = _promote_frequent_path_b_wallets(fake_db)

    assert promoted == 0
    rows = conn.execute("SELECT COUNT(*) FROM discovered_entities").fetchone()[0]
    assert rows == 0
