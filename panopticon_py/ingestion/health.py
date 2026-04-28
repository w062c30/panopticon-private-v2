from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from panopticon_py.db import ShadowDB
from panopticon_py.market_data.clob_series import registry_clob_base


def clob_base_url() -> str:
    return os.getenv("POLYMARKET_CLOB_BASE", registry_clob_base()).rstrip("/")


def check_clob_reachable(timeout_sec: float = 8.0) -> bool:
    url = f"{clob_base_url()}/"
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "panopticon-health/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return resp.status in (200, 204, 301, 302, 404)
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def check_moralis_configured() -> bool:
    return bool(os.getenv("MORALIS_API_KEY", "").strip())


def check_sqlite_writable(db: ShadowDB) -> bool:
    try:
        db.conn.execute("CREATE TABLE IF NOT EXISTS _health_probe (x INTEGER PRIMARY KEY);")
        db.conn.execute("INSERT OR IGNORE INTO _health_probe (x) VALUES (1);")
        db.conn.commit()
        return True
    except Exception:
        return False


def check_event_schema_present() -> bool:
    root = Path(__file__).resolve().parents[2]
    p = root / "shared" / "contracts" / "panopticon-event.schema.json"
    if not p.is_file():
        return False
    try:
        json.loads(p.read_text(encoding="utf-8"))
        return True
    except json.JSONDecodeError:
        return False


def run_observation_health_checks(db: ShadowDB) -> tuple[bool, list[str]]:
    """Return (ok, issues). Does not expose secrets."""
    issues: list[str] = []
    if not check_event_schema_present():
        issues.append("event_schema_missing_or_invalid")
    if not check_sqlite_writable(db):
        issues.append("sqlite_not_writable")
    if not check_clob_reachable():
        issues.append("clob_unreachable")
    if check_moralis_configured() is False and os.getenv("REQUIRE_MORALIS", "0").lower() in ("1", "true", "yes"):
        issues.append("moralis_required_but_missing")
    return (len(issues) == 0, issues)


def paper_gate_counters(db: ShadowDB) -> dict[str, int]:
    """Counts aligned with docs/paper_to_live_gate.md narrative (signals + sim fills)."""
    by_layer = db.count_raw_events_by_layer()
    l2 = int(by_layer.get("L2", 0))
    l3 = int(by_layer.get("L3", 0))
    sim_fills = db.count_execution_accepted()
    return {
        "raw_l2_signals": l2,
        "raw_l3_decisions": l3,
        "simulated_fills_accepted": sim_fills,
    }
