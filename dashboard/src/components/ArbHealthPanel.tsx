/**
 * ArbHealthPanel — Arb Scanner 健康監控面板
 *
 * D134: Read-only health snapshot from backend /api/arb/health endpoint.
 * Zero arb_scanner overhead — backend reads manifest only (no DB).
 * Polls every 30s.
 */
import { useEffect, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8001";
const POLL_MS = 30_000;

interface ArbHealth {
  pid: number | null;
  pid_alive: boolean;
  version: string | null;
  heartbeat_age_s: number | null;
  heartbeat_stale: boolean;
  heartbeat_bootstrapping: boolean; // D136-2: pid alive but heartbeat not yet written
  ts: string;
}

export function ArbHealthPanel() {
  const [data, setData] = useState<ArbHealth | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let timer: ReturnType<typeof setInterval>;

    async function fetchHealth() {
      try {
        const res = await fetch(`${API_BASE}/api/arb/health`);
        if (!res.ok) throw new Error();
        setData(await res.json());
        setError(false);
      } catch {
        setError(true);
      }
    }

    fetchHealth();
    timer = setInterval(fetchHealth, POLL_MS);
    return () => clearInterval(timer);
  }, []);

  // D136-2: bootstrapping = pid alive but heartbeat not yet written → blue STARTING
  const statusColor = !data
    ? "bg-slate-500"
    : !data.pid_alive
    ? "bg-red-500"
    : data.heartbeat_bootstrapping
    ? "bg-blue-400"
    : data.heartbeat_stale
    ? "bg-yellow-400"
    : "bg-green-500";

  const statusLabel = !data
    ? "—"
    : !data.pid_alive
    ? "DEAD"
    : data.heartbeat_bootstrapping
    ? "STARTING"
    : data.heartbeat_stale
    ? "STALE"
    : "LIVE";

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

      {error && (
        <p className="text-xs text-red-400">API unreachable</p>
      )}

      {data && (
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
          <span className="text-slate-400">PID</span>
          <span className="text-slate-100 font-mono">{data.pid ?? "—"}</span>

          <span className="text-slate-400">Version</span>
          <span className="text-slate-100 font-mono">{data.version ?? "—"}</span>

          <span className="text-slate-400">HB age</span>
          <span className={`font-mono ${data.heartbeat_stale ? "text-yellow-400" : "text-slate-100"}`}>
            {data.heartbeat_age_s != null ? `${data.heartbeat_age_s}s` : "—"}
          </span>
        </div>
      )}

      <p className="text-xs text-slate-500 text-right">
        {data ? new Date(data.ts).toLocaleTimeString("zh-HK") : "—"}
      </p>
    </div>
  );
}
