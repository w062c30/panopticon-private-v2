from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
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
PROCESS_VERSION = "v1.1.17-D113"   # ← AGENT: bump on every change  # D113: row_factory = sqlite3.Row + migration unified
acquire_singleton("backend", PROCESS_VERSION)

# ── Step 3: lifespan (now safely references PROCESS_VERSION above) ──
@asynccontextmanager
async def _lifespan(app: FastAPI):
    """
    D107: FastAPI lifespan context manager (replaces deprecated @app.on_event).
    Startup: bootstrap DB schema and ensure data/ directory exists.
    Shutdown: no-op (connections are per-request, no global pool to drain).
    """
    os.makedirs("data", exist_ok=True)
    try:
        from panopticon_py.db import ShadowDB
        _db = ShadowDB()
        _db.bootstrap()
        _db.close()
        logger.info("[APP] DB bootstrap complete — backend %s", PROCESS_VERSION)
    except Exception as exc:
        logger.warning("[APP] DB bootstrap warning: %s", exc)
    yield


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
                            "hit_id": r[0], "address": r[1], "market_id": r[2],
                            "entity_score": r[3], "entropy_z": r[4],
                            "sim_pnl_proxy": r[5], "outcome": r[6],
                            "payload_json": r[7], "created_ts_utc": r[8],
                        }
                        for r in hit_rows
                    ],
                    "wallet_obs": [
                        {
                            "obs_id": r[0], "address": r[1], "market_id": r[2],
                            "obs_type": r[3], "payload_json": r[4], "ingest_ts_utc": r[5],
                        }
                        for r in obs_rows
                    ],
                    "tracked_wallets": [
                        {
                            "wallet_address": r[0], "entity_id": r[1],
                            "all_time_pnl": r[2], "win_rate": r[3],
                            "discovery_source": r[4], "last_seen_ts_utc": r[5],
                            "last_updated_at": r[6],
                        }
                        for r in wallet_rows
                    ],
                    "raw_events": [
                        {
                            "event_id": r[0], "layer": r[1], "event_type": r[2],
                            "source": r[3], "market_id": r[4],
                            "payload_json": r[5], "ingest_ts_utc": r[6],
                        }
                        for r in event_rows
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

