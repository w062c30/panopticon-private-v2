/**
 * ArbScannerPanel — D148-4: Arb Scanner 狀態與統計面板
 *
 * Reads /api/arb/health (process liveness) and /api/arb/stats (DB time-series).
 * Polls every 30s. Zero impact on arb_scanner WS loop.
 */
import { useEffect, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8001";
const POLL_MS = 30_000;

interface ArbHealth {
  pid: number | null;
  pid_alive: boolean;
  status: string;
  version: string | null;
  heartbeat_age_s: number | null;
  heartbeat_stale: boolean;
  heartbeat_bootstrapping: boolean;
  crash_reason: string | null;
  reconnect_warning: boolean;  // D149-3
  reconnect_count: number;   // D149-3
  ts: string;
}

interface ArbStatRow {
  id: number;
  ts_utc: string;
  ws_connected: number;
  tokens_subscribed: number;
  active_tokens: number;
  total_updates: number;
  reconnect_count: number;
  opp_count_total: number;
  opp_count_1h: number;
  best_profit: number;
  tokens_total: number;
  tokens_kept: number;
  tokens_excluded: number;
}

interface ArbStatsResponse {
  stats: ArbStatRow[];
  count: number;
  ts: string;
  error?: string;
}

export function ArbScannerPanel() {
  const [health, setHealth] = useState<ArbHealth | null>(null);
  const [stats, setStats] = useState<ArbStatsResponse | null>(null);
  const [healthErr, setHealthErr] = useState(false);
  const [statsErr, setStatsErr] = useState(false);

  useEffect(() => {
    let timer: ReturnType<typeof setInterval>;

    async function fetchAll() {
      // health
      try {
        const res = await fetch(`${API_BASE}/api/arb/health`);
        if (!res.ok) throw new Error();
        setHealth(await res.json());
        setHealthErr(false);
      } catch {
        setHealthErr(true);
      }

      // stats — last 60 rows ≈ 1h
      try {
        const res = await fetch(`${API_BASE}/api/arb/stats?limit=60`);
        if (!res.ok) throw new Error();
        setStats(await res.json());
        setStatsErr(false);
      } catch {
        setStatsErr(true);
      }
    }

    fetchAll();
    timer = setInterval(fetchAll, POLL_MS);
    return () => clearInterval(timer);
  }, []);

  // ── Status badge ────────────────────────────────────────────────────────
  const statusColor = !health
    ? "bg-slate-500"
    : !health.pid_alive
    ? "bg-red-500"
    : health.heartbeat_bootstrapping
    ? "bg-blue-400"
    : health.heartbeat_stale
    ? "bg-yellow-400"
    : "bg-green-500";

  const statusLabel = !health
    ? "—"
    : !health.pid_alive
    ? "DEAD"
    : health.heartbeat_bootstrapping
    ? "STARTING"
    : health.heartbeat_stale
    ? "STALE"
    : "LIVE";

  // D149-3: reconnect warning
  const reconnectWarn = health?.reconnect_count > 10
    ? "severe"
    : health?.reconnect_warning
    ? "mild"
    : null;

  // ── Latest stats row ────────────────────────────────────────────────────
  const latest = stats?.stats?.[0] ?? null;

  // ── Build sparkline data from stats (last 30 rows for bar chart) ─────────
  const sparkData = stats?.stats?.slice(0, 30).reverse() ?? [];

  return (
    <div className="rounded-lg border border-slate-700 bg-panPanel p-4 space-y-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <span className="text-sm font-semibold text-slate-100">Arb Scanner</span>
        <div className="flex items-center gap-1.5">
          <span className={`w-2 h-2 rounded-full ${statusColor}`} />
          <span className="text-xs text-slate-400">{statusLabel}</span>
        </div>
      </div>

      {/* Crash reason banner */}
      {health?.crash_reason && (
        <div className="rounded bg-red-900/50 px-2 py-1 text-xs text-red-300 truncate">
          Crash: {health.crash_reason}
        </div>
      )}

      {/* D149-3: Reconnect warning banner */}
      {reconnectWarn === "severe" && (
        <div className="rounded bg-red-900/50 px-2 py-1 text-xs text-red-300 flex items-center gap-1">
          <span>WS 重連 {health.reconnect_count} 次</span>
          <span>— 建議檢查 Gamma API 或網路穩定性</span>
        </div>
      )}
      {reconnectWarn === "mild" && (
        <div className="rounded bg-orange-900/40 px-2 py-1 text-xs text-orange-300 flex items-center gap-1">
          <span>WS 重連 {health.reconnect_count} 次</span>
        </div>
      )}

      {/* WS + Connection row */}
      {health && (
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
          <span className="text-slate-400">PID / Ver</span>
          <span className="text-slate-100 font-mono">
            {health.pid ?? "—"} / {health.version?.replace("v", "") ?? "—"}
          </span>

          <span className="text-slate-400">Subscribed</span>
          <span className={`font-mono ${latest ? "text-slate-100" : "text-slate-500"}`}>
            {latest ? `${latest.tokens_subscribed} tokens` : "—"}
          </span>

          <span className="text-slate-400">Active tokens</span>
          <span className="font-mono">
            {latest
              ? `${latest.active_tokens} / ${latest.tokens_subscribed}`
              : "—"}
          </span>

          <span className="text-slate-400">Reconnects</span>
          <span className={`font-mono ${reconnectWarn === "severe" ? "text-red-400" : reconnectWarn === "mild" ? "text-orange-400" : "text-yellow-400"}`}>
            {latest?.reconnect_count ?? 0}
            {reconnectWarn === "severe" ? " ⚠" : reconnectWarn === "mild" ? " ⚠" : ""}
          </span>

          <span className="text-slate-400">Last HB</span>
          <span className={`font-mono ${health.heartbeat_stale ? "text-yellow-400" : "text-green-400"}`}>
            {health.heartbeat_age_s != null ? `${health.heartbeat_age_s}s` : "—"}
          </span>
        </div>
      )}

      {/* Opportunity stats */}
      {latest && (
        <div className="rounded border border-slate-700 bg-slate-900/50 p-2 space-y-1">
          <div className="flex justify-between text-xs">
            <span className="text-slate-400">Opp this session</span>
            <span className="font-mono text-panCyan">{latest.opp_count_total}</span>
          </div>
          <div className="flex justify-between text-xs">
            <span className="text-slate-400">Opp past 1h</span>
            <span className="font-mono text-panCyan">{latest.opp_count_1h}</span>
          </div>
          <div className="flex justify-between text-xs">
            <span className="text-slate-400">Best profit/100</span>
            <span className={`font-mono ${latest.best_profit > 0 ? "text-panGood" : "text-slate-500"}`}>
              {latest.best_profit > 0 ? `+$${latest.best_profit.toFixed(2)}` : "—"}
            </span>
          </div>
        </div>
      )}

      {/* Token filter summary */}
      {latest && (
        <div className="flex items-center gap-3 text-xs text-slate-400">
          <span>Tokens</span>
          <div className="flex gap-1 items-center">
            <span className="px-1.5 py-0.5 rounded bg-slate-800 font-mono text-slate-300">
              {latest.tokens_total}
            </span>
            <span>→</span>
            <span className="px-1.5 py-0.5 rounded bg-green-900/50 font-mono text-green-400">
              {latest.tokens_kept}
            </span>
            {latest.tokens_excluded > 0 && (
              <span className="px-1.5 py-0.5 rounded bg-red-900/40 font-mono text-red-400">
                −{latest.tokens_excluded}
              </span>
            )}
          </div>
          <span className="text-slate-600">fee filter</span>
        </div>
      )}

      {/* Book updates sparkline (30-row bar chart) */}
      {sparkData.length > 0 && (
        <div className="space-y-1">
          <span className="text-xs text-slate-500">Book updates (last 30m)</span>
          <div className="flex items-end gap-0.5 h-8">
            {sparkData.map((row, i) => {
              const maxVal = Math.max(...sparkData.map((r) => r.total_updates), 1);
              const h = Math.round((row.total_updates / maxVal) * 100);
              return (
                <div
                  key={i}
                  className="flex-1 bg-panCyan/40 hover:bg-panCyan/70 rounded-t transition-colors"
                  style={{ height: `${Math.max(h, 2)}%` }}
                  title={`${row.total_updates} updates at ${row.ts_utc?.slice(11, 16)}`}
                />
              );
            })}
          </div>
        </div>
      )}

      {/* Error state */}
      {(healthErr || statsErr) && (
        <p className="text-xs text-red-400">
          {healthErr ? "Health API unreachable. " : ""}
          {statsErr ? "Stats API unreachable." : ""}
        </p>
      )}

      <p className="text-xs text-slate-500 text-right">
        {health ? new Date(health.ts).toLocaleTimeString("zh-HK") : "—"}
      </p>
    </div>
  );
}