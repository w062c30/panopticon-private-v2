"""tests/test_rvf_metrics.py
RVF MetricsCollector + MetricsSnapshot tests.
"""
import os
from unittest.mock import MagicMock
from panopticon_py.metrics import MetricsCollector, MetricsSnapshot


class TestMetricsCollectorBasics:
    def test_collector_singleton(self):
        from panopticon_py.metrics import get_collector
        assert isinstance(get_collector(), MetricsCollector)

    def test_collect_returns_metrics_snapshot(self):
        mc = MetricsCollector()
        assert isinstance(mc.collect(), MetricsSnapshot)

    def test_collector_hooks_are_noop_when_noop(self):
        mc = MetricsCollector()
        mc.on_l1_subscription(t1=0, t2=0, t3=0, t5=0)
        mc.on_kyle_skip()
        mc.on_entropy_window_cleanup(0, 0)
        mc.on_gate_result(accepted=False)
        mc.on_signal_queued(depth=0, tier='', p_posterior=None, z=None)
        snap = mc.collect()
        assert snap.ws.t1 == 0
        assert snap.kyle.sample_count == 0


class TestPersistJsonAtomic:
    def test_persist_json_writes_atomic_file(self, tmp_path):
        mc = MetricsCollector()
        path = str(tmp_path / 'rvf.json')
        mc.persist_json(path=path)
        assert os.path.exists(path)
        assert not os.path.exists(path + '.tmp')

    def test_persist_json_creates_parent_dir(self, tmp_path):
        mc = MetricsCollector()
        path = str(tmp_path / 'sub' / 'rvf.json')
        mc.persist_json(path=path)
        assert os.path.exists(path)


class TestPersistDbOnly:
    def test_persist_db_does_not_write_json(self, tmp_path):
        mc = MetricsCollector()
        mock_db = MagicMock()
        mc.persist_db(db=mock_db)
        mock_db.write_rvf_snapshot.assert_called_once()


class TestGoLiveLockedField:
    def test_go_live_locked_true_when_kyle_insufficient(self):
        mc = MetricsCollector()
        mc._paper_trades_total = 100
        mc._paper_win_count = 60
        mc._paper_win_rate = 0.60
        for _ in range(499):
            mc.on_kyle_compute('0xSOMEASSET', 0.0001)
        snap = mc.collect()
        assert snap.go_live.locked is True

    def test_go_live_locked_false_when_all_thresholds_met(self):
        mc = MetricsCollector()
        mc._paper_trades_total = 100
        mc._paper_win_count = 60
        mc._paper_win_rate = 0.60
        for _ in range(500):
            mc.on_kyle_compute('0xSOMEASSET', 0.0001)
        snap = mc.collect()
        assert snap.go_live.locked is False

    def test_go_live_locked_false_when_kyle_exactly_500(self):
        mc = MetricsCollector()
        mc._paper_trades_total = 100
        mc._paper_win_count = 60
        mc._paper_win_rate = 0.60
        for _ in range(500):
            mc.on_kyle_compute('0xSOMEASSET', 0.0001)
        snap = mc.collect()
        assert snap.go_live.locked is False
        assert snap.go_live.kyle_total == 500

    def test_go_live_locked_true_when_paper_trades_insufficient(self):
        mc = MetricsCollector()
        mc._paper_trades_total = 99
        mc._paper_win_count = 60
        mc._paper_win_rate = 0.60
        for _ in range(500):
            mc.on_kyle_compute('0xSOMEASSET', 0.0001)
        snap = mc.collect()
        assert snap.go_live.locked is True

    def test_go_live_winrate_exactly_55_percent(self):
        mc = MetricsCollector()
        mc._paper_trades_total = 100
        mc._paper_win_count = 55
        mc._paper_win_rate = 0.55
        for _ in range(500):
            mc.on_kyle_compute('0xSOMEASSET', 0.0001)
        snap = mc.collect()
        assert snap.go_live.locked is False

    def test_go_live_winrate_54_percent_locked(self):
        mc = MetricsCollector()
        mc._paper_trades_total = 100
        mc._paper_win_count = 54
        mc._paper_win_rate = 0.54
        for _ in range(500):
            mc.on_kyle_compute('0xSOMEASSET', 0.0001)
        snap = mc.collect()
        assert snap.go_live.locked is True

    def test_go_live_snapshot_pct_fields_correct(self):
        mc = MetricsCollector()
        mc._paper_trades_total = 50
        mc._paper_win_count = 30
        mc._paper_win_rate = 0.60
        for _ in range(250):
            mc.on_kyle_compute('0xSOMEASSET', 0.0001)
        snap = mc.collect()
        assert snap.go_live.kyle_pct == 0.5
        assert snap.go_live.trades_pct == 0.5
        assert snap.go_live.winrate_pct == 1.0

    def test_go_live_to_dict_includes_locked(self):
        mc = MetricsCollector()
        for _ in range(10):
            mc.on_kyle_compute('0xTEST', 0.0001)
        data = mc.collect().to_dict()
        assert 'go_live' in data
        assert 'locked' in data['go_live']
        assert data['go_live']['locked'] is True

    def test_paper_win_rate_vs_paper_win_count_in_gate_stats(self):
        mc = MetricsCollector()
        mc._paper_trades_total = 100
        mc._paper_win_count = 60
        mc._paper_win_rate = 0.60
        for _ in range(500):
            mc.on_kyle_compute('0xSOMEASSET', 0.0001)
        snap = mc.collect()
        assert snap.gate.paper_win_rate == 0.60
        assert snap.gate.paper_win_count == 60
        assert snap.gate.paper_trades_total == 100


class TestHookWiring:
    def test_on_trade_tick_increments_counter(self):
        mc = MetricsCollector()
        for _ in range(5):
            mc.on_trade_tick()
        assert mc.collect().to_dict()['ws']['trade_ticks_60s'] == 5

    def test_on_book_event_increments_counter(self):
        mc = MetricsCollector()
        mc.on_book_event()
        mc.on_book_event()
        assert mc.collect().to_dict()['ws']['book_events_60s'] == 2

    def test_on_ws_connected_sets_flag(self):
        mc = MetricsCollector()
        assert mc.collect().to_dict()['ws']['connected'] == 0
        mc.on_ws_connected()
        assert mc.collect().to_dict()['ws']['connected'] == 1
        mc.on_ws_disconnected()
        assert mc.collect().to_dict()['ws']['connected'] == 0


class TestD27StartupAndBurstMetrics:
    def test_startup_t1_window_nonzero(self):
        import time
        mc = MetricsCollector()
        now_ts = int(time.time())
        t1_start = (now_ts // 300) * 300
        t1_end = t1_start + 300
        secs_left = max(0, t1_end - now_ts)
        mc.on_t1_window_rollover(window_start=t1_start, window_end=t1_end, secs_remaining=float(secs_left))
        snap = mc.collect().to_dict()
        assert snap['ws']['current_t1_window_start'] == t1_start
        assert snap['ws']['current_t1_window_end'] == t1_end
        assert 0 <= snap['ws']['secs_remaining_in_window'] <= 300

    def test_ws_connected_disconnected_toggles_flag(self):
        mc = MetricsCollector()
        assert mc.collect().to_dict()['ws']['connected'] == 0
        mc.on_ws_connected()
        assert mc.collect().to_dict()['ws']['connected'] == 1
        mc.on_ws_disconnected()
        assert mc.collect().to_dict()['ws']['connected'] == 0


class TestD56ConsensusTotal:
    def test_consensus_total_no_limit(self, tmp_path):
        import sqlite3
        from datetime import datetime, timezone
        from panopticon_py.metrics import MetricsCollector

        db_path = str(tmp_path / 'test_consensus.db')
        conn = sqlite3.connect(db_path)
        q = chr(39)
        conn.execute('CREATE TABLE discovered_entities (entity_id TEXT PRIMARY KEY, insider_score REAL NOT NULL, address TEXT, primary_tag TEXT DEFAULT ' + q + q + ')')
        conn.execute('CREATE TABLE wallet_observations (obs_id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL, market_id TEXT NOT NULL, obs_type TEXT NOT NULL, ingest_ts_utc TEXT NOT NULL)')
        conn.execute('CREATE TABLE polymarket_link_map (market_id TEXT PRIMARY KEY, token_id TEXT, event_slug TEXT, market_slug TEXT, canonical_event_url TEXT, canonical_embed_url TEXT, source TEXT NOT NULL, fetched_at TEXT NOT NULL)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_poly_link_token ON polymarket_link_map(token_id)')

        now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        for i in range(15):
            market_id = 'test_market_{:03d}'.format(i)
            for w in range(2):
                wallet = '0xWALLET{:02d}_{}'.format(i, w)
                conn.execute('INSERT INTO discovered_entities (entity_id, insider_score, address, primary_tag) VALUES (?, ?, ?, ?)', (wallet, 0.60, wallet, ''))
                conn.execute('INSERT INTO wallet_observations (address, market_id, obs_type, ingest_ts_utc) VALUES (?, ?, ?, ?)', (wallet, market_id, 'clob_trade', now_utc))
        conn.commit()

        class FakeDB:
            def __init__(self, c):
                self.conn = c

        mc = MetricsCollector()
        mc.sync_consensus_from_db(FakeDB(conn))
        snap = mc.collect().to_dict()
        consensus = snap['consensus']
        assert consensus['markets_consensus_total'] == 15, 'total: {}'.format(consensus['markets_consensus_total'])
        assert consensus['markets_consensus_ready'] == 10, 'ready: {}'.format(consensus['markets_consensus_ready'])
        assert consensus['markets_consensus_total'] >= consensus['markets_consensus_ready']
        assert len(consensus['consensus_markets']) == 10
        conn.close()

    def test_consensus_total_zero_when_no_markets(self, tmp_path):
        import sqlite3
        from panopticon_py.metrics import MetricsCollector

        db_path = str(tmp_path / 'empty.db')
        conn = sqlite3.connect(db_path)
        q = chr(39)
        conn.execute('CREATE TABLE discovered_entities (entity_id TEXT PRIMARY KEY, insider_score REAL NOT NULL, address TEXT, primary_tag TEXT DEFAULT ' + q + q + ')')
        conn.execute('CREATE TABLE wallet_observations (obs_id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL, market_id TEXT NOT NULL, obs_type TEXT NOT NULL, ingest_ts_utc TEXT NOT NULL)')
        conn.execute('CREATE TABLE polymarket_link_map (market_id TEXT PRIMARY KEY, token_id TEXT, event_slug TEXT, market_slug TEXT, canonical_event_url TEXT, canonical_embed_url TEXT, source TEXT NOT NULL, fetched_at TEXT NOT NULL)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_poly_link_token ON polymarket_link_map(token_id)')
        conn.commit()

        class FakeDB:
            def __init__(self, c):
                self.conn = c

        mc = MetricsCollector()
        mc.sync_consensus_from_db(FakeDB(conn))
        snap = mc.collect().to_dict()
        consensus = snap['consensus']
        assert consensus['markets_consensus_total'] == 0
        assert consensus['markets_consensus_ready'] == 0
        conn.close()
