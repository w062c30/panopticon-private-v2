from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger("panopticon.api")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from starlette.middleware.cors import CORSMiddleware

from panopticon_py.api.routers.performance import router as performance_router
from panopticon_py.api.routers.report import router as report_router
from panopticon_py.api.routers.recommendations import router as recommendations_router
from panopticon_py.api.routers.system_health import router as system_health_router
from panopticon_py.api.routers.wallet_graph import router as wallet_graph_router
from panopticon_py.api.routers.watchlist import router as watchlist_router
from panopticon_py.load_env import load_repo_env

load_repo_env()

# ── Step 2: PROCESS_VERSION must be before _lifespan (D108-1 fix) ──
from panopticon_py.utils.process_guard import acquire_singleton, get_all_versions, update_heartbeat
from panopticon_py.time_utils import utc_now_rfc3339_ms
PROCESS_VERSION = "v1.1.39-D137"   # ← AGENT: bump on every change  # D137-2: +GET /api/radar/active-markets
acquire_singleton("backend", PROCESS_VERSION)

# ── Step 3: lifespan (now safely references PROCESS_VERSION above) ──
async def _backend_heartbeat_loop() -> None:
    """Write heartbeat every 30s so watchdog can monitor backend liveness."""
    while True:
        try:
            update_heartbeat("backend")
        except Exception:
            pass
        await asyncio.sleep(30)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """
    D107: FastAPI lifespan context manager (replaces deprecated @app.on_event).
    Startup: bootstrap DB schema and ensure data/ directory exists.
    Shutdown: no-op (connections are per-request, no global pool to drain).
    D118: AsyncDBWriter stub added to app.state — backend is read-only, real writer
          lives in the orchestrator process.
    D113: Backend heartbeat task added for watchdog monitoring.
    """
    os.makedirs("data", exist_ok=True)
    try:
        from panopticon_py.db import ShadowDB
        _db = ShadowDB()
        _db.bootstrap()
        _db.close()
        logger.info("[APP] DB bootstrap complete — backend %s", PROCESS_VERSION)
        # D118: Wire AsyncDBWriter stub — backend is read-only; real writer runs in orchestrator.
        # Stub always shows running=False so /api/async-writer-health reflects reality.
        _writer = AsyncDBWriterStub()
        app.state.async_writer = _writer
        logger.info("[APP] AsyncDBWriter stub wired to app.state (backend is read-only)")
    except Exception as exc:
        logger.warning("[APP] DB bootstrap warning: %s", exc)

    # D113: Start heartbeat task — watchdog reads last_heartbeat_ts from process_manifest.json
    heartbeat_task = asyncio.create_task(_backend_heartbeat_loop(), name="backend-heartbeat")
    logger.info("[APP] Backend heartbeat task started")

    yield

    # Shutdown: cancel heartbeat task
    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass


class AsyncDBWriterStub:
    """D118/D119: Stub for the read-only backend process.
    Real writer lives in orchestrator; this stub reads a JSON snapshot
    written by the orchestrator every 30s (D119 cross-process health sharing).
    """

    def health(self) -> dict[str, Any]:
        """Read real writer health snapshot written by the orchestrator process."""
        try:
            snap_path = os.getenv(
                "ASYNC_WRITER_HEALTH_PATH", "data/async_writer_health.json"
            )
            with open(snap_path) as f:
                data: dict[str, Any] = json.load(f)
            written_at = data.get("written_at", "")
            if written_at:
                try:
                    age_sec = (
                        datetime.now(timezone.utc) - datetime.fromisoformat(written_at)
                    ).total_seconds()
                    if age_sec > 60:
                        data["stale"] = True
                        data["stale_sec"] = round(age_sec, 1)
                except Exception:
                    pass
            return data
        except FileNotFoundError:
            return {
                "running": False,
                "thread_alive": False,
                "queue_depth": 0,
                "queue_unfinished": 0,
                "error": "orchestrator snapshot not found",
            }
        except Exception as exc:
            return {
                "running": False,
                "thread_alive": False,
                "queue_depth": 0,
                "queue_unfinished": 0,
                "error": str(exc),
            }


app = FastAPI(title="Panopticon API", version="0.1.0", lifespan=_lifespan)

# Browser dev servers (Vite) use http://localhost:* while API may bind 127.0.0.1 — different origins → CORS required.
_extra_origins = [
    o.strip()
    for o in os.getenv("PANOPTICON_CORS_ORIGINS", "").split(",")
    if o.strip()
]
_default_origins = [
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:5175",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5174",
    "http://127.0.0.1:5175",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=[*set(_default_origins + _extra_origins)],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(recommendations_router)
app.include_router(performance_router)
app.include_router(report_router)
app.include_router(system_health_router)
app.include_router(wallet_graph_router)
app.include_router(watchlist_router)


# Serve built dashboard from disk
# Mount at /dashboard so it doesn't conflict with /api/*, /health, etc.
_dashboard_dist = os.path.normpath(
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "dashboard", "dist")
)
if os.path.isdir(_dashboard_dist):
    from starlette.staticfiles import StaticFiles

    # D37: Root redirect to /dashboard/
    @app.get("/", include_in_schema=False)
    async def root_redirect():
        return RedirectResponse(url="/dashboard/")

    app.mount("/dashboard", StaticFiles(directory=_dashboard_dist, html=True), name="dashboard")
    logger.info("[APP] Dashboard static files served at /dashboard from %s", _dashboard_dist)


# ── WebSocket stream for live dashboard ──────────────────────────────────────

class _WsConnectionManager:
    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def _connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)

    def _disconnect(self, ws: WebSocket) -> None:
        if ws in self._connections:
            self._connections.remove(ws)

    async def _broadcast(self, msg: dict) -> None:
        dead = []
        for ws in self._connections:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._disconnect(ws)


_ws_manager = _WsConnectionManager()


@app.websocket("/ws/stream")
async def ws_stream(ws: WebSocket) -> None:
    """Push live hunting/shadow data to connected dashboards every 5 seconds."""
    await _ws_manager._connect(ws)
    from panopticon_py.db import ShadowDB
    db: ShadowDB | None = None  # D109-1: declared before try so finally can safely close it
    try:
        db = ShadowDB()  # D109-1: init inside try so ShadowDB() exceptions are caught
        await ws.send_json({"type": "connected"})
        while True:
            await asyncio.sleep(5)
            update_heartbeat("backend")
            try:
                hit_rows = db.conn.execute("""
                    SELECT hit_id, address, market_id, entity_score, entropy_z,
                           sim_pnl_proxy, outcome, payload_json, created_ts_utc
                    FROM hunting_shadow_hits
                    ORDER BY created_ts_utc DESC LIMIT 50
                """).fetchall()
                obs_rows = db.conn.execute("""
                    SELECT obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc
                    FROM wallet_observations
                    ORDER BY ingest_ts_utc DESC LIMIT 50
                """).fetchall()
                wallet_rows = db.conn.execute("""
                    SELECT wallet_address, entity_id, all_time_pnl, win_rate,
                           discovery_source, last_seen_ts_utc, last_updated_at
                    FROM tracked_wallets
                    ORDER BY all_time_pnl DESC LIMIT 20
                """).fetchall()
                event_rows = db.conn.execute("""
                    SELECT event_id, layer, event_type, source, market_id,
                           payload_json, ingest_ts_utc
                    FROM raw_events
                    ORDER BY ingest_ts_utc DESC LIMIT 20
                """).fetchall()
                await ws.send_json({
                    "type": "live_update",
                    "hunting_hits": [
                        {
                            "hit_id":        dict_r["hit_id"],
                            "address":       dict_r["address"],
                            "market_id":     dict_r["market_id"],
                            "entity_score":  dict_r["entity_score"],
                            "entropy_z":     dict_r["entropy_z"],
                            "sim_pnl_proxy": dict_r["sim_pnl_proxy"],
                            "outcome":       dict_r["outcome"],
                            "payload_json":  dict_r["payload_json"],
                            "created_ts_utc":dict_r["created_ts_utc"],
                        }
                        for dict_r in (dict(r) for r in hit_rows)
                    ],
                    "wallet_obs": [
                        {
                            "obs_id": dict_r["obs_id"], "address": dict_r["address"], "market_id": dict_r["market_id"],
                            "obs_type": dict_r["obs_type"], "payload_json": dict_r["payload_json"], "ingest_ts_utc": dict_r["ingest_ts_utc"],
                        }
                        for dict_r in (dict(r) for r in obs_rows)
                    ],
                    "tracked_wallets": [
                        {
                            "wallet_address":  dict_r["wallet_address"],
                            "entity_id":       dict_r["entity_id"],
                            "all_time_pnl":    dict_r["all_time_pnl"],
                            "win_rate":        dict_r["win_rate"],
                            "discovery_source":dict_r["discovery_source"],
                            "last_seen_ts_utc":dict_r["last_seen_ts_utc"],
                            "last_updated_at": dict_r["last_updated_at"],
                        }
                        for dict_r in (dict(r) for r in wallet_rows)
                    ],
                    "raw_events": [
                        {
                            "event_id":     dict_r["event_id"],
                            "layer":        dict_r["layer"],
                            "event_type":   dict_r["event_type"],
                            "source":       dict_r["source"],
                            "market_id":    dict_r["market_id"],
                            "payload_json": dict_r["payload_json"],
                            "ingest_ts_utc":dict_r["ingest_ts_utc"],
                        }
                        for dict_r in (dict(r) for r in event_rows)
                    ],
                    "ts": time.time(),
                })
            except Exception as exc:
                # D109-1: debug-level log — DB read failures every 5s would flood logs at warning level
                logger.debug("[WS_STREAM] tick error (will retry): %s", exc)
    except WebSocketDisconnect:
        pass
    finally:
        if db is not None:  # D109-1: guard against ShadowDB() throwing before assignment
            db.close()
        _ws_manager._disconnect(ws)


# ── WebSocket + REST for RVF Live Metrics ─────────────────────────────────────

_rvf_ws_manager = _WsConnectionManager()


@app.websocket("/ws/rvf")
async def ws_rvf_metrics(ws: WebSocket) -> None:
    """
    Push RVF live metrics snapshot to connected dashboards every 1 second.

    Reads from data/rvf_live_snapshot.json (written by MetricsCollector.persist()
    inside run_radar.py every 60s). This JSON file is the cross-process
    communication channel between the orchestrator and the FastAPI server.
    """
    await _rvf_ws_manager._connect(ws)
    try:
        await ws.send_json({"type": "rvf_connected"})
        while True:
            await asyncio.sleep(1)
            try:
                snap = _read_rvf_snapshot()
                await ws.send_json({**snap, "type": "rvf_snapshot"})
            except Exception:
                await ws.send_json({"type": "rvf_snapshot", "error": True})
    except WebSocketDisconnect:
        _rvf_ws_manager._disconnect(ws)
    finally:
        _rvf_ws_manager._disconnect(ws)


@app.get("/api/rvf/snapshot")
def api_rvf_snapshot() -> dict:
    """REST polling fallback for RVF metrics (reads same snapshot file)."""
    return _read_rvf_snapshot()


def _read_rvf_snapshot() -> dict:
    """Read last MetricsCollector snapshot from JSON file."""
    try:
        snap_path = os.getenv("RVF_SNAPSHOT_PATH", "data/rvf_live_snapshot.json")
        with open(snap_path) as f:
            return json.load(f)
    except Exception:
        return {"error": "snapshot not available"}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/versions")
async def get_versions() -> dict:
    """
    Returns runtime process_manifest.json.
    Shows all running process versions, PIDs, start times, and version_match flags.
    Used by verification agents and dashboard.
    """
    return get_all_versions()


@app.get("/api/metrics/real_trade_ticks_60s")
def get_real_trade_ticks_60s_endpoint() -> dict:
    """
    D131: Debt-5 upper-bound proxy endpoint.
    DR-D125-c: real_trade_ticks_60s has dual increment paths
    (book_embedded + standalone last_trade_price). Same physical fill
    may increment both — exposed as coverage telemetry, not cardinality.
    """
    from panopticon_py.metrics import get_collector
    mc = get_collector()
    total = mc.get_trade_ticks_60s()
    real = mc.get_real_trade_ticks_60s()
    return {
        "real_trade_ticks_60s": real,
        "trade_ticks_60s": total,
        "ratio": round(real / total, 4) if total > 0 else 0.0,
        "note": "upper_bound_proxy_DR-D125-c",
        "ts": utc_now_rfc3339_ms(),
    }


@app.get("/api/arb/health")
async def get_arb_health() -> dict:
    """
    D134: Arb scanner health snapshot — read-only, zero arb_scanner overhead.
    Data sources:
      1. process_manifest.json → process liveness + heartbeat age
      2. arb_scanner.py in-memory state (via manifest version drift check)
    Note: arb_scanner stores opportunities in-memory only (no DB write).
    Health is determined by PID liveness + heartbeat freshness.
    """
    import json as _json
    from pathlib import Path

    manifest_path = Path("run/process_manifest.json")
    arb_entry: dict = {}
    heartbeat_age_s: float | None = None
    pid_alive = False

    if manifest_path.exists():
        try:
            manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
            arb_entry = manifest.get("arb_scanner", {})
            hb_ts_str = arb_entry.get("last_heartbeat_ts")
            if hb_ts_str:
                try:
                    hb_dt = datetime.fromisoformat(hb_ts_str)
                    if hb_dt.tzinfo is None:
                        hb_dt = hb_dt.replace(tzinfo=timezone.utc)
                    heartbeat_age_s = (datetime.now(timezone.utc) - hb_dt).total_seconds()
                except Exception:
                    heartbeat_age_s = None
        except Exception:
            pass

    pid = arb_entry.get("pid")
    if pid:
        try:
            import os as _os
            _os.kill(int(pid), 0)
            pid_alive = True
        except (OSError, ProcessLookupError):
            pid_alive = False

    return {
        "pid": pid,
        "pid_alive": pid_alive,
        "version": arb_entry.get("version"),
        "heartbeat_age_s": round(heartbeat_age_s, 1) if heartbeat_age_s is not None else None,
        "heartbeat_stale": (heartbeat_age_s or 9999) > 300,
        # D136-2: pid_alive but heartbeat not yet written = bootstrapping
        "heartbeat_bootstrapping": heartbeat_age_s is None and pid_alive,
        "ts": utc_now_rfc3339_ms(),
    }


@app.get("/api/radar/active-markets")
def get_radar_active_markets() -> dict:
    """
    D137-2: Radar current WS subscription market snapshot by tier.
    Source: data/radar_active_markets.json written by run_radar.py after each tier refresh.
    Zero DB write, zero impact on radar main loop.
    """
    snap_path = os.getenv("RADAR_ACTIVE_MARKETS_PATH", "data/radar_active_markets.json")
    try:
        with open(snap_path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"error": "snapshot not yet available — radar may still be starting"}
    except Exception as exc:
        return {"error": str(exc)}