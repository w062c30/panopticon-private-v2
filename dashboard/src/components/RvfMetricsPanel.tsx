/**
 * RvfMetricsPanel — Live RVF pipeline health panel.
 *
 * Connects directly to /ws/rvf for 1s updates via Vite proxy (or VITE_API_BASE_URL).
 * Shows L1 WS status, Kyle accumulation, EntropyWindow state, Queue depth,
 * EV Gate results, Series intelligence, and Go-Live readiness gauges.
 */
import { useEffect, useState } from "react";

const WS_URL = (import.meta.env.VITE_API_BASE_URL || "http://localhost:8001").replace(/^http/, "ws") + "/ws/rvf";
const REST_URL = (import.meta.env.VITE_API_BASE_URL || "http://localhost:8001") + "/api/rvf/snapshot";

interface RvfSnapshot {
  ts_utc?: string;
  ws?: {
    connected?: boolean;
    t1?: number; t2?: number; t3?: number; t5?: number;
    trade_ticks_60s?: number;
    book_events_60s?: number;
    current_t1_window_start?: number;
    current_t1_window_end?: number;
    secs_remaining_in_window?: number;
    t1_rollover_count_today?: number;
    elapsed_since_last_ws_msg?: number;
  };
  kyle?: {
    sample_count?: number;
    distinct_assets?: number;
    p75_estimate?: number;
    last_compute_elapsed_sec?: number;
    last_compute_status?: string;
  };
  window?: {
    active_entropy_windows?: number;
    last_cleanup_count?: number;
  };
  queue?: {
    depth?: number;
    processed_60s?: number;
    mean_p_posterior_t1?: number;
    mean_p_posterior_t2?: number;
    mean_z_t1?: number;
    mean_z_t2?: number;
  };
  gate?: {
    evaluated_60s?: number;
    pass_count_60s?: number;
    abort_count_60s?: number;
    paper_trades_total?: number;
    paper_win_rate?: number;
    avg_ev?: number;
  };
  series?: {
    deadline_ladders?: number;
    rolling_windows?: number;
    total_series?: number;
    monotone_violations?: number;
    last_violation_slug?: string;
    last_violation_gap?: number;
    catalyst_events_today?: number;
    oracle_high_risk?: number;
  };
  consensus?: {
    qualifying_wallets?: number;
    new_candidates?: number;
    path_b_promoted?: number;
    markets_consensus_ready?: number;
    markets_consensus_total?: number;
    consensus_markets?: Array<{ slug: string; wallet_count: number }>;
    price_debug?: {
      last_source?: string;
      last_spread?: number;
      no_price_count_24h?: number;
    };
  };
  readiness?: {
    kyle_pct?: number;
    trades_pct?: number;
    winrate_pct?: number;
    all_ready?: boolean;
  };
  go_live?: {
    locked?: boolean;
    kyle_pct?: number;
    trades_pct?: number;
    winrate_pct?: number;
    kyle_total?: number;
    paper_trades_total?: number;
    paper_win_count?: number;
  };
  error?: boolean;
}

function GaugeBar({ pct, label, color }: { pct: number; label: string; color: string }) {
  const pctClamped = Math.min(100, Math.max(0, pct * 100));
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="w-20 text-slate-400">{label}</span>
      <div className="flex-1 h-2 rounded bg-slate-800 overflow-hidden">
        <div
          className={`h-full ${color} transition-all duration-500`}
          style={{ width: `${pctClamped}%` }}
        />
      </div>
      <span className="w-10 text-right text-slate-300">{(pctClamped).toFixed(0)}%</span>
    </div>
  );
}

function SectionHeader({ title, dot }: { title: string; dot?: "green" | "red" | "yellow" }) {
  const dotColor = dot === "green" ? "bg-panGood" : dot === "red" ? "bg-red-500" : dot === "yellow" ? "bg-yellow-500" : "bg-slate-500";
  return (
    <div className="flex items-center gap-2 mb-1">
      {dot && <span className={`w-2 h-2 rounded-full ${dotColor}`} />}
      <span className="text-xs font-semibold text-slate-300 uppercase tracking-wider">{title}</span>
    </div>
  );
}

function fmtNum(n: number | undefined, decimals = 0): string {
  if (n === undefined || n === null) return "—";
  return n.toFixed(decimals);
}

function fmtTs(ts: number | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toISOString().slice(11, 19);
}

// ── Hover tooltip for consensus-ready markets ────────────────────────────────

interface MarketEntry {
  slug: string;
  wallet_count: number;
}

function HoverableMarketsRow({
  marketsConsensusReady,
  marketsConsensusTotal,
  consensusMarkets,
}: {
  marketsConsensusReady: number;
  marketsConsensusTotal: number;
  consensusMarkets: MarketEntry[];
}) {
  const [show, setShow] = useState(false);
  const displayLabel = marketsConsensusTotal > marketsConsensusReady
      ? `${marketsConsensusTotal} (top ${marketsConsensusReady})`
      : `${marketsConsensusTotal}`;
  return (
    <div className="relative inline-block">
      <span
        className="text-xs text-slate-300 cursor-default underline decoration-dotted decoration-slate-500"
        onMouseEnter={() => setShow(true)}
        onMouseLeave={() => setShow(false)}
      >
        準備好共識的Market: <span className="text-panGood">{displayLabel}</span>
      </span>
      {show && consensusMarkets.length > 0 && (
        <div className="absolute z-50 left-0 top-5 bg-gray-800 bg-opacity-90 border border-slate-600 rounded-lg p-3 shadow-xl min-w-48">
          <div className="text-xs font-semibold text-slate-300 mb-2 border-b border-slate-600 pb-1">
             最新Market / 合格錢包數
          </div>
          {consensusMarkets.map((m, i) => (
            <div key={i} className="flex justify-between gap-4 text-xs py-0.5">
              <span className="text-slate-300 truncate max-w-32">{m.slug}</span>
              <span className="text-yellow-400 font-mono">{m.wallet_count}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function RvfMetricsPanel() {
  const [snap, setSnap] = useState<RvfSnapshot | null>(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  // D48: fetch REST immediately on mount so panel shows data without waiting for WS
  useEffect(() => {
    fetch(REST_URL)
      .then((r) => r.json())
      .then((data: RvfSnapshot) => { if (!data.error) setSnap(data); })
      .catch(() => {/* WS will populate once connected */});
  }, []);

  // Manual refresh button handler
  const handleRefresh = () => {
    setRefreshing(true);
    fetch(REST_URL)
      .then((r) => r.json())
      .then((data: RvfSnapshot) => { if (!data.error) setSnap(data); })
      .catch(() => {/* keep existing snap on error */})
      .finally(() => setRefreshing(false));
  };

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retryTimeout: ReturnType<typeof setTimeout>;
    let retryDelay = 1000;

    function connect() {
      try {
        ws = new WebSocket(WS_URL);
      } catch {
        scheduleRetry();
        return;
      }

      ws.onopen = () => {
        setWsConnected(true);
        retryDelay = 1000;
      };

      ws.onmessage = (evt) => {
        try {
          const data: RvfSnapshot = JSON.parse(evt.data as string);
          if (!data.error) setSnap(data);
        } catch {
          // ignore parse errors
        }
      };

      ws.onerror = () => {
        setWsConnected(false);
        ws?.close();
      };

      ws.onclose = () => {
        setWsConnected(false);
        scheduleRetry();
      };
    }

    function scheduleRetry() {
      clearTimeout(retryTimeout);
      retryTimeout = setTimeout(() => {
        retryDelay = Math.min(retryDelay * 2, 30000);
        connect();
      }, retryDelay);
    }

    connect();
    return () => {
      clearTimeout(retryTimeout);
      ws?.close();
    };
  }, []);

  if (!snap || snap.error) {
    return (
      <div className="rounded-xl border border-slate-700 bg-panPanel p-4 text-sm text-slate-400">
        <div className="flex items-center justify-between mb-2">
          <span className="font-semibold text-slate-200">RVF 管線監控</span>
          <span className="text-xs text-slate-500">等待資料...</span>
        </div>
        <div className="text-xs text-slate-500">
          {wsConnected ? "已連線，正等待快照..." : `未連線至 ${WS_URL}`}
        </div>
      </div>
    );
  }

  const ws = snap.ws;
  const kyle = snap.kyle;
  const window = snap.window;
  const queue = snap.queue;
  const gate = snap.gate;
  const series = snap.series;
  const goLive = snap.go_live;

  // kylePct: fraction toward 500-sample threshold
  const kylePct = ((kyle?.sample_count ?? 0) / 500);

  return (
    <div className="rounded-xl border border-slate-700 bg-panPanel p-4 text-sm">
      {/* Header */}
      <div className="flex items-center justify-between mb-3">
        <span className="font-semibold text-slate-200">RVF 管線監控</span>
        <div className="flex items-center gap-2">
          {snap.ts_utc && (
            <span className="text-xs text-slate-500">{new Date(snap.ts_utc).toLocaleTimeString()}</span>
          )}
          <button
            onClick={handleRefresh}
            disabled={refreshing}
            className="flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-slate-700 hover:bg-slate-600 text-slate-300 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {refreshing ? (
              <span className="animate-spin">&#8635;</span>
            ) : (
              <span>&#8635;</span>
            )}
            {refreshing ? "更新中..." : "重新整理"}
          </button>
          <span className={`flex items-center gap-1 text-xs ${wsConnected ? "text-panGood" : "text-red-400"}`}>
            <span className={`w-2 h-2 rounded-full ${wsConnected ? "bg-panGood" : "bg-red-500"}`} />
            {wsConnected ? "已連線" : "未連線"}
          </span>
        </div>
      </div>

      {/* L1 WS */}
      <div className="mb-3">
        <SectionHeader title="L1 WS 訂閱" dot={ws?.connected ? "green" : "red"} />
        <div className="grid grid-cols-4 gap-2 text-xs">
          <div className="rounded bg-slate-800 p-2 text-center">
            <div className="text-lg font-mono text-panGood">{ws?.t1 ?? 0}</div>
            <div className="text-slate-500">T1</div>
          </div>
          <div className="rounded bg-slate-800 p-2 text-center">
            <div className="text-lg font-mono text-yellow-400">{ws?.t2 ?? 0}</div>
            <div className="text-slate-500">T2</div>
          </div>
          <div className="rounded bg-slate-800 p-2 text-center">
            <div className="text-lg font-mono text-slate-400">{ws?.t3 ?? 0}</div>
            <div className="text-slate-500">T3</div>
          </div>
          <div className="rounded bg-slate-800 p-2 text-center">
            <div className="text-lg font-mono text-blue-400">{ws?.t5 ?? 0}</div>
            <div className="text-slate-500">T5</div>
          </div>
        </div>
        <div className="mt-2 grid grid-cols-3 gap-2 text-xs text-slate-400">
          <div>交易tick: <span className="text-slate-200">{ws?.trade_ticks_60s ?? 0}/60s</span></div>
          <div>Book事件: <span className="text-slate-200">{ws?.book_events_60s ?? 0}/60s</span></div>
          <div>T1窗口: <span className="text-slate-200">{ws?.secs_remaining_in_window?.toFixed(0) ?? 0}s</span></div>
        </div>
      </div>

      {/* L1.kyle */}
      <div className="mb-3">
        <SectionHeader title="L1.kyle Lambda" />
        <div className="space-y-1">
          <GaugeBar pct={kylePct} label="樣本進度" color="bg-blue-500" />
          <div className="flex justify-between text-xs text-slate-400 mt-1">
            <span>{kyle?.sample_count ?? 0} / 500 樣本</span>
            <span>{kyle?.distinct_assets ?? 0} 資產</span>
            <span>P75: {fmtNum(kyle?.p75_estimate, 6)}</span>
          </div>
          <div className="flex justify-between text-xs text-slate-500">
            <span>最後計算: {kyle?.last_compute_status ?? "—"}</span>
            <span>{kyle?.last_compute_elapsed_sec?.toFixed(1) ?? "—"}s ago</span>
          </div>
        </div>
      </div>

      {/* L1 Window */}
      <div className="mb-3">
        <SectionHeader title="L1 EntropyWindow" />
        <div className="flex justify-between text-xs text-slate-400">
          <div>活躍窗口: <span className="text-slate-200">{window?.active_entropy_windows ?? 0}</span></div>
          <div>清理延遲: <span className="text-slate-200">{window?.last_cleanup_count ?? 0}</span></div>
        </div>
      </div>

      {/* L2/L3 Queue */}
      <div className="mb-3">
        <SectionHeader title="L2/L3 信號隊列" />
        <div className="grid grid-cols-2 gap-x-4 text-xs text-slate-400">
          <div>隊列深度: <span className="text-slate-200">{queue?.depth ?? 0}</span></div>
          <div>已處理60s: <span className="text-slate-200">{queue?.processed_60s ?? 0}</span></div>
          <div>Mean p(T1): <span className="text-slate-200">{fmtNum(queue?.mean_p_posterior_t1, 3)}</span></div>
          <div>Mean z(T1): <span className="text-slate-200">{fmtNum(queue?.mean_z_t1, 3)}</span></div>
        </div>
      </div>

      {/* L4 EV Gate */}
      <div className="mb-3">
        <SectionHeader title="L4 EV Gate" />
        <div className="grid grid-cols-3 gap-x-4 text-xs text-slate-400 mb-1">
          <div>評估60s: <span className="text-slate-200">{gate?.evaluated_60s ?? 0}</span></div>
          <div>通過: <span className="text-panGood">{gate?.pass_count_60s ?? 0}</span></div>
          <div>否決: <span className="text-red-400">{gate?.abort_count_60s ?? 0}</span></div>
        </div>
        <div className="flex justify-between text-xs text-slate-500">
          <span>紙trade: {gate?.paper_trades_total ?? 0} / 100</span>
          <span>勝率: {fmtNum((gate?.paper_win_rate ?? 0) * 100, 1)}%</span>
          <span>Avg EV: {fmtNum(gate?.avg_ev, 2)}</span>
        </div>
      </div>

      {/* Series */}
      <div className="mb-3">
        <SectionHeader title="Series Intelligence" />
        <div className="grid grid-cols-2 gap-x-4 text-xs text-slate-400 mb-1">
          <div>Deadline ladders: <span className="text-slate-200">{series?.deadline_ladders ?? 0}</span></div>
          <div>Rolling windows: <span className="text-slate-200">{series?.rolling_windows ?? 0}</span></div>
          <div>Monotone violations: <span className={series?.monotone_violations ? "text-red-400" : "text-slate-200"}>{series?.monotone_violations ?? 0}</span></div>
          <div>Catalyst events: <span className="text-slate-200">{series?.catalyst_events_today ?? 0}</span></div>
        </div>
        {series?.monotone_violations && series?.last_violation_slug && (
          <div className="text-xs text-red-400 mt-1">
            Last violation: {series.last_violation_slug} ({series.last_violation_gap?.toFixed(1)}% gap)
          </div>
        )}
      </div>

      {/* Consensus / Wallet Readiness */}
      <div className="mb-3">
        <SectionHeader title="L5 共識錢包" />
        <div className="grid grid-cols-3 gap-x-4 gap-y-1 text-xs text-slate-400 mb-1">
          <div>合規錢包: <span className="text-slate-200">{snap.consensus?.qualifying_wallets ?? 0}</span></div>
          <div>新候選 (非PathB): <span className="text-slate-200">{snap.consensus?.new_candidates ?? 0}</span></div>
          <div>PathB晉升: <span className="text-yellow-400">{snap.consensus?.path_b_promoted ?? 0}</span></div>
        </div>
        <HoverableMarketsRow
          marketsConsensusReady={snap.consensus?.markets_consensus_ready ?? 0}
          marketsConsensusTotal={snap.consensus?.markets_consensus_total ?? 0}
          consensusMarkets={snap.consensus?.consensus_markets ?? []}
        />
        {/* D50c: Price source debug row */}
        {snap.consensus?.price_debug && (
          <div className="flex items-center gap-3 mt-1 text-xs">
            <span>
              價格來源:{" "}
              <span className={
                snap.consensus.price_debug.last_source === "mid" ? "text-panGood" :
                snap.consensus.price_debug.last_source === "last_trade" ? "text-yellow-400" :
                "text-red-400"
              }>
                {snap.consensus.price_debug.last_source ?? "—"}
              </span>
            </span>
            {snap.consensus.price_debug.last_spread !== undefined && (
              <span>
                Spread:{" "}
                <span className={
                  snap.consensus.price_debug.last_spread !== null && snap.consensus.price_debug.last_spread <= 0.10 ? "text-panGood" :
                  snap.consensus.price_debug.last_spread !== null && snap.consensus.price_debug.last_spread <= 0.30 ? "text-yellow-400" :
                  "text-red-400"
                }>
                  {snap.consensus.price_debug.last_spread !== null ? snap.consensus.price_debug.last_spread.toFixed(4) : "—"}
                </span>
              </span>
            )}
            {snap.consensus.price_debug.no_price_count_24h !== undefined && (
              <span className="text-slate-500">
                NO_PRICE (24h): {snap.consensus.price_debug.no_price_count_24h}
              </span>
            )}
          </div>
        )}
      </div>

      {/* Go-Live Readiness — go_live.locked is the authoritative gate */}
      <div className="rounded-lg border border-slate-600 p-3">
        <SectionHeader
          title="Go-Live 就緒狀態"
          dot={goLive?.locked === false ? "green" : "yellow"}
        />
        <div className="space-y-2">
          <GaugeBar pct={goLive?.kyle_pct ?? 0} label="Kyle 樣本" color="bg-blue-500" />
          <GaugeBar pct={goLive?.trades_pct ?? 0} label="Paper Trades" color="bg-purple-500" />
          <GaugeBar
            pct={goLive?.winrate_pct ?? 0}
            label="Win Rate vs 55%"
            color={(goLive?.winrate_pct ?? 0) >= 1 ? "bg-panGood" : "bg-yellow-500"}
          />
        </div>
        <div className="mt-2 grid grid-cols-3 gap-x-2 text-xs text-slate-500 mb-1">
          <div>Kyle: {goLive?.kyle_total ?? 0} / 500</div>
          <div>Trades: {goLive?.paper_trades_total ?? 0} / 100</div>
          <div>Wins: {goLive?.paper_win_count ?? 0}</div>
        </div>
        <div className="mt-1 flex items-center justify-between">
          <span className={`text-xs font-bold ${goLive?.locked === false ? "text-panGood" : "text-slate-400"}`}>
            {goLive?.locked === false
              ? " LIVE 解鎖 — 等待架構師批准"
              : " 等待累積"}
          </span>
          <span className="text-xs text-slate-600">
            {goLive?.locked
              ? `🔒 LOCKED`
              : "✅ READY"}
          </span>
        </div>
      </div>
    </div>
  );
}
