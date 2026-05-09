/**
 * RvfMetricsPanel — Live RVF pipeline health panel.
 *
 * Connects directly to /ws/rvf for 1s updates via Vite proxy (or VITE_API_BASE_URL).
 * D180: Pipeline derivative metrics, metric info icons, on-demand heavy diagnostics.
 */
import { useEffect, useState } from "react";

import { MetricInfoIcon } from "./MetricInfoIcon";
import { RVF_METRIC_DEFINITIONS } from "../data/rvfMetricDefinitions";

const D = RVF_METRIC_DEFINITIONS;

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://localhost:8001";
const WS_URL = API_BASE.replace(/^http/, "ws") + "/ws/rvf";
const REST_URL = API_BASE + "/api/rvf/snapshot";
const DIAG_URL = API_BASE + "/api/diagnostics/market_breakdown";
const MINUTE_MS = 60_000;
const BUCKETS_1H = 60;
const BUCKETS_24H = 24 * 60;

type RareCounterKey = "l2Eval" | "l3Eval" | "queueProcessed" | "gateEvaluated";
type RareCounterValues = Record<RareCounterKey, number>;

interface RareCounterBucket extends RareCounterValues {
  minuteTs: number;
}

interface KyleMinuteBucket {
  minuteTs: number;
  sampleCount: number;
}

interface PipelineSnap {
  z_ready_ratio?: number;
  entropy_warmup_ratio?: number;
  l0_locked_ratio?: number;
  fire_rate_60s?: number;
  gate_pass_rate_60s?: number;
  input_to_processed_ratio_60s?: number;
  kyle_readiness_ratio?: number;
  stale_seconds_max?: number;
  l2_eval_60s?: number;
  l3_eval_60s?: number;
  active_window_breakdown?: {
    ready?: number;
    warming?: number;
    locked?: number;
    total?: number;
  };
}

interface ArbSnap {
  present?: boolean;
  stale_seconds?: number | null;
  tokens_subscribed?: number;
  total_updates?: number;
  opp_count_total?: number;
  ws_connected?: boolean;
}

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
    last_cleanup_ts?: number;
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
  pipeline?: PipelineSnap;
  arb?: ArbSnap;
  error?: boolean;
}

interface DiagnosticRow {
  market_id: string;
  question?: string | null;
  slug?: string | null;
  abs_z_max: number;
  fire_count: number;
  kyle_n: number;
  events: number;
  h_hist: number;
  ev_count?: number;
  h_count?: number;
  locked: boolean;
  z_ready: boolean;
  last_fire_ts?: string | null;
}

interface DiagnosticPayload {
  generated_at?: string;
  elapsed_ms?: number;
  rows?: DiagnosticRow[];
  summary?: {
    total_markets_in_slice?: number;
    total_candidates_scanned?: number;
    with_question?: number;
    without_question?: number;
    entropy_tokens_loaded?: number;
    entropy_load_error?: string | null;
    entropy_snapshot_stale?: boolean;
    entropy_total_windows?: number;
    entropy_z_ready_count?: number;
  };
  cache_hit?: boolean;
  detail?: string;
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

function fmtHkt(ts: string | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return new Intl.DateTimeFormat("zh-HK", {
    timeZone: "Asia/Hong_Kong",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(d);
}

function minuteBucketTs(ts: string | undefined): number {
  const ms = Date.parse(ts ?? "");
  const safeMs = Number.isFinite(ms) ? ms : Date.now();
  return Math.floor(safeMs / MINUTE_MS) * MINUTE_MS;
}

function addOrMergeMinuteBucket(
  prev: RareCounterBucket[],
  minuteTs: number,
  nextValues: RareCounterValues,
): RareCounterBucket[] {
  const next = [...prev];
  const last = next[next.length - 1];
  if (last && last.minuteTs === minuteTs) {
    // Same minute: use max seen value to avoid over-counting on 1s snapshots.
    last.l2Eval = Math.max(last.l2Eval, nextValues.l2Eval);
    last.l3Eval = Math.max(last.l3Eval, nextValues.l3Eval);
    last.queueProcessed = Math.max(last.queueProcessed, nextValues.queueProcessed);
    last.gateEvaluated = Math.max(last.gateEvaluated, nextValues.gateEvaluated);
  } else {
    next.push({ minuteTs, ...nextValues });
  }
  if (next.length > BUCKETS_24H) {
    return next.slice(next.length - BUCKETS_24H);
  }
  return next;
}

function sumBuckets(
  buckets: RareCounterBucket[],
  key: RareCounterKey,
  takeLast: number,
): number {
  if (buckets.length === 0) return 0;
  return buckets
    .slice(Math.max(0, buckets.length - takeLast))
    .reduce((acc, b) => acc + b[key], 0);
}

function addOrMergeKyleBucket(
  prev: KyleMinuteBucket[],
  minuteTs: number,
  sampleCount: number,
): KyleMinuteBucket[] {
  const next = [...prev];
  const last = next[next.length - 1];
  if (last && last.minuteTs === minuteTs) {
    last.sampleCount = Math.max(last.sampleCount, sampleCount);
  } else {
    next.push({ minuteTs, sampleCount });
  }
  if (next.length > BUCKETS_24H) {
    return next.slice(next.length - BUCKETS_24H);
  }
  return next;
}

function maxKyleSampleCount(buckets: KyleMinuteBucket[], takeLast: number): number {
  const window = buckets.slice(Math.max(0, buckets.length - takeLast));
  return window.reduce((acc, b) => Math.max(acc, b.sampleCount), 0);
}

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
        <span className="inline-flex items-center gap-1">
          <MetricInfoIcon definition={D["consensus.markets_ready"]} />
          準備好共識的Market: <span className="text-panGood">{displayLabel}</span>
        </span>
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
  const [heavyLoading, setHeavyLoading] = useState(false);
  const [heavyError, setHeavyError] = useState<string | null>(null);
  const [heavyData, setHeavyData] = useState<DiagnosticPayload | null>(null);
  const [heavyOpen, setHeavyOpen] = useState(false);
  const [rareCounterBuckets, setRareCounterBuckets] = useState<RareCounterBucket[]>([]);
  const [kyleMinuteBuckets, setKyleMinuteBuckets] = useState<KyleMinuteBucket[]>([]);

  useEffect(() => {
    fetch(REST_URL)
      .then((r) => r.json())
      .then((data: RvfSnapshot) => { if (!data.error) setSnap(data); })
      .catch(() => {/* WS will populate once connected */});
  }, []);

  const handleRefresh = () => {
    setRefreshing(true);
    fetch(REST_URL)
      .then((r) => r.json())
      .then((data: RvfSnapshot) => { if (!data.error) setSnap(data); })
      .catch(() => {/* keep existing snap on error */})
      .finally(() => setRefreshing(false));
  };

  const handleHeavyDiagnostics = () => {
    setHeavyLoading(true);
    setHeavyError(null);
    fetch(`${DIAG_URL}?limit=30&sort=abs_z`)
      .then(async (r) => {
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          throw new Error((err as { detail?: string }).detail || r.statusText);
        }
        return r.json() as Promise<DiagnosticPayload>;
      })
      .then((data) => {
        setHeavyData(data);
        setHeavyOpen(true);
      })
      .catch((e: Error) => setHeavyError(e.message || "request failed"))
      .finally(() => setHeavyLoading(false));
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

  useEffect(() => {
    if (!snap || snap.error) return;
    const minuteTs = minuteBucketTs(snap.ts_utc);
    const sample: RareCounterValues = {
      l2Eval: snap.pipeline?.l2_eval_60s ?? 0,
      l3Eval: snap.pipeline?.l3_eval_60s ?? 0,
      queueProcessed: snap.queue?.processed_60s ?? 0,
      gateEvaluated: snap.gate?.evaluated_60s ?? 0,
    };
    setRareCounterBuckets((prev) => addOrMergeMinuteBucket(prev, minuteTs, sample));
  }, [
    snap?.ts_utc,
    snap?.pipeline?.l2_eval_60s,
    snap?.pipeline?.l3_eval_60s,
    snap?.queue?.processed_60s,
    snap?.gate?.evaluated_60s,
    snap?.error,
  ]);

  useEffect(() => {
    if (!snap || snap.error) return;
    const minuteTs = minuteBucketTs(snap.ts_utc);
    setKyleMinuteBuckets((prev) =>
      addOrMergeKyleBucket(prev, minuteTs, snap.kyle?.sample_count ?? 0),
    );
  }, [snap?.ts_utc, snap?.kyle?.sample_count, snap?.error]);

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
  const pipe = snap.pipeline;
  const l2Eval1h = sumBuckets(rareCounterBuckets, "l2Eval", BUCKETS_1H);
  const l2Eval24h = sumBuckets(rareCounterBuckets, "l2Eval", BUCKETS_24H);
  const l3Eval1h = sumBuckets(rareCounterBuckets, "l3Eval", BUCKETS_1H);
  const l3Eval24h = sumBuckets(rareCounterBuckets, "l3Eval", BUCKETS_24H);
  const queueProcessed1h = sumBuckets(rareCounterBuckets, "queueProcessed", BUCKETS_1H);
  const queueProcessed24h = sumBuckets(rareCounterBuckets, "queueProcessed", BUCKETS_24H);
  const gateEval1h = sumBuckets(rareCounterBuckets, "gateEvaluated", BUCKETS_1H);
  const gateEval24h = sumBuckets(rareCounterBuckets, "gateEvaluated", BUCKETS_24H);
  const kyleSample1h = maxKyleSampleCount(kyleMinuteBuckets, BUCKETS_1H);

  const kylePct = ((kyle?.sample_count ?? 0) / 500);

  return (
    <div className="rounded-xl border border-slate-700 bg-panPanel p-4 text-sm relative">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <span className="font-semibold text-slate-200">RVF 管線監控</span>
        <div className="flex items-center gap-2 flex-wrap">
          {snap.ts_utc && (
            <span className="text-xs text-slate-500">{new Date(snap.ts_utc).toLocaleTimeString()}</span>
          )}
          <button
            type="button"
            onClick={handleHeavyDiagnostics}
            disabled={heavyLoading}
            className="px-2 py-0.5 rounded text-xs bg-amber-900/60 hover:bg-amber-800/80 text-amber-100 disabled:opacity-50 border border-amber-700/50"
          >
            {heavyLoading ? "診斷中…" : "Heavy Diagnostics"}
          </button>
          <button
            type="button"
            onClick={handleRefresh}
            disabled={refreshing}
            className="flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-slate-700 hover:bg-slate-600 text-slate-300 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {refreshing ? <span className="animate-spin">&#8635;</span> : <span>&#8635;</span>}
            {refreshing ? "更新中..." : "重新整理"}
          </button>
          <span className={`flex items-center gap-1 text-xs ${wsConnected ? "text-panGood" : "text-red-400"}`}>
            <span className={`w-2 h-2 rounded-full ${wsConnected ? "bg-panGood" : "bg-red-500"}`} />
            {wsConnected ? "已連線" : "未連線"}
          </span>
        </div>
      </div>

      {heavyError && (
        <div className="mb-2 text-xs text-red-400">{heavyError}</div>
      )}

      {/* L1 WS */}
      <div className="mb-3">
        <SectionHeader title="L1 WS 訂閱" dot={ws?.connected ? "green" : "red"} />
        <div className="grid grid-cols-4 gap-2 text-xs">
          {(["t1", "t2", "t3", "t5"] as const).map((tier) => (
            <div key={tier} className="rounded bg-slate-800 p-2 text-center">
              <div className={`text-lg font-mono ${
                tier === "t1" ? "text-panGood" : tier === "t2" ? "text-yellow-400" : tier === "t3" ? "text-slate-400" : "text-blue-400"
              }`}>
                {tier === "t1" ? ws?.t1 ?? 0 : tier === "t2" ? ws?.t2 ?? 0 : tier === "t3" ? ws?.t3 ?? 0 : ws?.t5 ?? 0}
              </div>
              <div className="text-slate-500 flex items-center justify-center gap-0.5">
                <MetricInfoIcon definition={D[`ws.${tier}`]} />
                {tier.toUpperCase()}
              </div>
            </div>
          ))}
        </div>
        <div className="mt-2 grid grid-cols-3 gap-2 text-xs text-slate-400">
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["ws.trade_ticks_60s"]} />
            <span>交易tick: <span className="text-slate-200">{ws?.trade_ticks_60s ?? 0}/60s</span></span>
          </div>
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["ws.book_events_60s"]} />
            <span>Book事件: <span className="text-slate-200">{ws?.book_events_60s ?? 0}/60s</span></span>
          </div>
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["ws.t1_window"]} />
            <span>T1窗口: <span className="text-slate-200">{ws?.secs_remaining_in_window?.toFixed(0) ?? 0}s</span></span>
          </div>
        </div>
      </div>

      {/* L1.kyle */}
      <div className="mb-3">
        <SectionHeader title="L1.kyle Lambda" />
        <div className="space-y-1">
          <GaugeBar pct={kylePct} label="樣本進度" color="bg-blue-500" />
          <div className="flex justify-between text-xs text-slate-400 mt-1 flex-wrap gap-2">
            <span className="inline-flex items-center gap-1">
              <MetricInfoIcon definition={D["kyle.sample_count"]} />
              {kyle?.sample_count ?? 0} / 500 樣本 (1h: {kyleSample1h})
            </span>
            <span className="inline-flex items-center gap-1">
              <MetricInfoIcon definition={D["kyle.distinct_assets"]} />
              {kyle?.distinct_assets ?? 0} 資產
            </span>
            <span className="inline-flex items-center gap-1">
              <MetricInfoIcon definition={D["kyle.p75"]} />
              P75: {fmtNum(kyle?.p75_estimate, 6)}
            </span>
          </div>
          <div className="flex justify-between text-xs text-slate-500 flex-wrap gap-2">
            <span className="inline-flex items-center gap-1">
              <MetricInfoIcon definition={D["kyle.last_compute"]} />
              最後計算: {kyle?.last_compute_status ?? "—"}
            </span>
            <span>{kyle?.last_compute_elapsed_sec?.toFixed(1) ?? "—"}s ago</span>
          </div>
        </div>
      </div>

      {/* L1 Window */}
      <div className="mb-3">
        <SectionHeader title="L1 EntropyWindow" />
        <div className="flex justify-between text-xs text-slate-400 flex-wrap gap-2">
          <span className="inline-flex items-center gap-1">
            <MetricInfoIcon definition={D["window.active_entropy_windows"]} />
            活躍窗口: <span className="text-slate-200">{window?.active_entropy_windows ?? 0}</span>
          </span>
          <span className="inline-flex items-center gap-1">
            <MetricInfoIcon definition={D["window.cleanup"]} />
            清理延遲: <span className="text-slate-200">{window?.last_cleanup_count ?? 0}</span>
          </span>
        </div>
      </div>

      {/* D180 Pipeline */}
      <div className="mb-3 rounded-lg border border-slate-600/80 p-2 bg-slate-900/40">
        <SectionHeader title="Pipeline 衍生 (L1)" dot="yellow" />
        <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] text-slate-400">
          <span className="inline-flex items-center gap-1">
            <MetricInfoIcon definition={D["pipeline.z_ready_ratio"]} />
            z_ready: {fmtNum(pipe?.z_ready_ratio, 3)}
          </span>
          <span className="inline-flex items-center gap-1">
            <MetricInfoIcon definition={D["pipeline.entropy_warmup_ratio"]} />
            暖機: {fmtNum(pipe?.entropy_warmup_ratio, 3)}
          </span>
          <span className="inline-flex items-center gap-1">
            <MetricInfoIcon definition={D["pipeline.l0_locked_ratio"]} />
            L0 locked: {fmtNum(pipe?.l0_locked_ratio, 3)}
          </span>
          <span className="inline-flex items-center gap-1">
            <MetricInfoIcon definition={D["pipeline.fire_rate_60s"]} />
            fire/gate: {fmtNum(pipe?.fire_rate_60s, 3)}
          </span>
          <span className="inline-flex items-center gap-1">
            <MetricInfoIcon definition={D["pipeline.gate_pass_rate_60s"]} />
            gate通過率: {fmtNum(pipe?.gate_pass_rate_60s, 3)}
          </span>
          <span className="inline-flex items-center gap-1">
            <MetricInfoIcon definition={D["pipeline.input_to_processed_ratio_60s"]} />
            輸入/處理: {fmtNum(pipe?.input_to_processed_ratio_60s, 3)}
          </span>
          <span className="inline-flex items-center gap-1">
            <MetricInfoIcon definition={D["pipeline.l2_eval_60s"]} />
            L2 eval(1h|24h): {l2Eval1h} | {l2Eval24h}
          </span>
          <span className="inline-flex items-center gap-1">
            <MetricInfoIcon definition={D["pipeline.l3_eval_60s"]} />
            L3 eval(1h|24h): {l3Eval1h} | {l3Eval24h}
          </span>
          <span className="inline-flex items-center gap-1">
            <MetricInfoIcon definition={D["pipeline.kyle_readiness_ratio"]} />
            Kyle準備度: {fmtNum(pipe?.kyle_readiness_ratio, 3)}
          </span>
          <span className="inline-flex items-center gap-1">
            <MetricInfoIcon definition={D["pipeline.stale_seconds_max"]} />
            stale_max: {fmtNum(pipe?.stale_seconds_max, 1)}s
          </span>
        </div>
        {pipe?.active_window_breakdown && (
          <div className="mt-1 text-[11px] text-slate-500 inline-flex flex-wrap items-center gap-1">
            <MetricInfoIcon definition={D["pipeline.breakdown"]} />
            ready {pipe.active_window_breakdown.ready ?? 0} / warming {pipe.active_window_breakdown.warming ?? 0} / locked {pipe.active_window_breakdown.locked ?? 0} / total {pipe.active_window_breakdown.total ?? 0}
          </div>
        )}
        {l2Eval1h === 0 && l3Eval1h === 0 && (
          <div className="mt-1 text-[11px] text-slate-500">
            L2/L3=0/0 代表目前 1h 視窗內尚未有事件進入 `_process_event`，不是欄位缺失。
          </div>
        )}
      </div>

      {/* Arb snapshot (merged by radar after persist_json) */}
      {snap.arb?.present && (
        <div className="mb-3 rounded-lg border border-slate-600/60 p-2">
          <SectionHeader title="Arb Scanner (快照)" />
          <div className="grid grid-cols-2 gap-2 text-xs text-slate-400">
            <span className="inline-flex items-center gap-1">
              <MetricInfoIcon definition={D["arb.stale"]} />
              stale: {snap.arb.stale_seconds != null ? `${fmtNum(snap.arb.stale_seconds, 1)}s` : "—"}
            </span>
            <span className="inline-flex items-center gap-1">
              <MetricInfoIcon definition={D["arb.tokens_subscribed"]} />
              tokens: {snap.arb.tokens_subscribed ?? "—"}
            </span>
            <span className="inline-flex items-center gap-1">
              <MetricInfoIcon definition={D["arb.updates"]} />
              updates: {snap.arb.total_updates ?? 0}
            </span>
            <span>opp_total: {snap.arb.opp_count_total ?? 0}</span>
          </div>
        </div>
      )}

      {/* L2/L3 Queue */}
      <div className="mb-3">
        <SectionHeader title="L2/L3 信號隊列" />
        <div className="grid grid-cols-2 gap-x-4 text-xs text-slate-400">
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["queue.depth"]} />
            <span>隊列深度: <span className="text-slate-200">{queue?.depth ?? 0}</span></span>
          </div>
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["queue.processed_60s"]} />
            <span>已處理(1h|24h): <span className="text-slate-200">{queueProcessed1h} | {queueProcessed24h}</span></span>
          </div>
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["queue.mean_p_t1"]} />
            <span>Mean p(T1): <span className="text-slate-200">{fmtNum(queue?.mean_p_posterior_t1, 3)}</span></span>
          </div>
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["queue.mean_p_t2"]} />
            <span>Mean p(T2): <span className="text-slate-200">{fmtNum(queue?.mean_p_posterior_t2, 3)}</span></span>
          </div>
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["queue.mean_z_t1"]} />
            <span>Mean z(T1): <span className="text-slate-200">{fmtNum(queue?.mean_z_t1, 3)}</span></span>
          </div>
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["queue.mean_z_t2"]} />
            <span>Mean z(T2): <span className="text-slate-200">{fmtNum(queue?.mean_z_t2, 3)}</span></span>
          </div>
        </div>
      </div>

      {/* L4 EV Gate */}
      <div className="mb-3">
        <SectionHeader title="L4 EV Gate" />
        <div className="grid grid-cols-3 gap-x-4 text-xs text-slate-400 mb-1">
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["gate.evaluated_60s"]} />
            <span>評估(1h|24h): <span className="text-slate-200">{gateEval1h} | {gateEval24h}</span></span>
          </div>
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["gate.pass_60s"]} />
            <span>通過: <span className="text-panGood">{gate?.pass_count_60s ?? 0}</span></span>
          </div>
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["gate.abort_60s"]} />
            <span>否決: <span className="text-red-400">{gate?.abort_count_60s ?? 0}</span></span>
          </div>
        </div>
        <div className="flex justify-between text-xs text-slate-500 flex-wrap gap-2">
          <span className="inline-flex items-center gap-1">
            <MetricInfoIcon definition={D["gate.paper_trades"]} />
            紙trade: {gate?.paper_trades_total ?? 0} / 100
          </span>
          <span className="inline-flex items-center gap-1">
            <MetricInfoIcon definition={D["gate.paper_win_rate"]} />
            勝率: {fmtNum((gate?.paper_win_rate ?? 0) * 100, 1)}%
          </span>
          <span className="inline-flex items-center gap-1">
            <MetricInfoIcon definition={D["gate.avg_ev"]} />
            Avg EV: {fmtNum(gate?.avg_ev, 2)}
          </span>
        </div>
      </div>

      {/* Series */}
      <div className="mb-3">
        <SectionHeader title="Series Intelligence" />
        <div className="grid grid-cols-2 gap-x-4 text-xs text-slate-400 mb-1">
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["series.deadline"]} />
            <span>Deadline ladders: <span className="text-slate-200">{series?.deadline_ladders ?? 0}</span></span>
          </div>
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["series.rolling"]} />
            <span>Rolling windows: <span className="text-slate-200">{series?.rolling_windows ?? 0}</span></span>
          </div>
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["series.violations"]} />
            <span>Monotone violations: <span className={series?.monotone_violations ? "text-red-400" : "text-slate-200"}>{series?.monotone_violations ?? 0}</span></span>
          </div>
          <div>Catalyst events: <span className="text-slate-200">{series?.catalyst_events_today ?? 0}</span></div>
        </div>
        {series?.monotone_violations && series?.last_violation_slug && (
          <div className="text-xs text-red-400 mt-1">
            Last violation: {series.last_violation_slug} ({series.last_violation_gap?.toFixed(1)}% gap)
          </div>
        )}
      </div>

      {/* Consensus */}
      <div className="mb-3">
        <SectionHeader title="L5 共識錢包" />
        <div className="grid grid-cols-3 gap-x-4 gap-y-1 text-xs text-slate-400 mb-1">
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["consensus.qualifying"]} />
            <span>合規錢包: <span className="text-slate-200">{snap.consensus?.qualifying_wallets ?? 0}</span></span>
          </div>
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["consensus.new_candidates"]} />
            <span>新候選 (非PathB): <span className="text-slate-200">{snap.consensus?.new_candidates ?? 0}</span></span>
          </div>
          <div className="flex items-start gap-1">
            <MetricInfoIcon definition={D["consensus.path_b"]} />
            <span>PathB晉升: <span className="text-yellow-400">{snap.consensus?.path_b_promoted ?? 0}</span></span>
          </div>
        </div>
        <HoverableMarketsRow
          marketsConsensusReady={snap.consensus?.markets_consensus_ready ?? 0}
          marketsConsensusTotal={snap.consensus?.markets_consensus_total ?? 0}
          consensusMarkets={snap.consensus?.consensus_markets ?? []}
        />
        {snap.consensus?.price_debug && (
          <div className="flex items-center gap-3 mt-1 text-xs flex-wrap">
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

      {/* Readiness strip */}
      <div className="mb-2 text-xs text-slate-500 inline-flex items-center gap-1">
        <MetricInfoIcon definition={D["readiness.all_ready"]} />
        readiness.all_ready: <span className={snap.readiness?.all_ready ? "text-panGood" : "text-slate-400"}>{String(!!snap.readiness?.all_ready)}</span>
      </div>

      {/* Go-Live */}
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
          <div className="inline-flex items-center gap-1">
            <MetricInfoIcon definition={D["kyle.sample_count"]} />
            Kyle: {goLive?.kyle_total ?? 0} / 500
          </div>
          <div className="inline-flex items-center gap-1">
            <MetricInfoIcon definition={D["gate.paper_trades"]} />
            Trades: {goLive?.paper_trades_total ?? 0} / 100
          </div>
          <div>Wins: {goLive?.paper_win_count ?? 0}</div>
        </div>
        <div className="mt-1 flex items-center justify-between flex-wrap gap-2">
          <span className={`text-xs font-bold inline-flex items-center gap-1 ${goLive?.locked === false ? "text-panGood" : "text-slate-400"}`}>
            <MetricInfoIcon definition={D["go_live.locked"]} />
            {goLive?.locked === false
              ? " LIVE 解鎖 — 等待架構師批准"
              : " 等待累積"}
          </span>
          <span className="text-xs text-slate-600">
            {goLive?.locked ? "LOCKED" : "READY"}
          </span>
        </div>
      </div>

      {/* Heavy diagnostics modal */}
      {heavyOpen && heavyData && (
        <div
          className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="heavy-diag-title"
        >
          <div className="max-h-[85vh] w-full max-w-4xl overflow-hidden rounded-xl border border-slate-600 bg-slate-900 shadow-2xl flex flex-col">
            <div className="flex items-center justify-between border-b border-slate-600 px-4 py-2">
              <h2 id="heavy-diag-title" className="text-sm font-semibold text-slate-100">
                Heavy Diagnostics — Market breakdown
              </h2>
              <button
                type="button"
                className="rounded px-2 py-1 text-xs bg-slate-700 hover:bg-slate-600 text-slate-200"
                onClick={() => setHeavyOpen(false)}
              >
                Close
              </button>
            </div>
            <div className="px-4 py-2 text-[11px] text-slate-400 border-b border-slate-700">
              generated (HKT): <span title={heavyData.generated_at ?? ""}>{fmtHkt(heavyData.generated_at)}</span> | elapsed: {heavyData.elapsed_ms ?? "—"} ms
              {heavyData.cache_hit ? " | cached" : ""}
              {heavyData.summary && (
                <span className="ml-2">
                  candidates {heavyData.summary.total_candidates_scanned ?? "—"} | with Q {heavyData.summary.with_question ?? "—"}
                  {" | entropy "} {heavyData.summary.entropy_tokens_loaded ?? "—"}
                  {heavyData.summary.entropy_snapshot_stale && (
                    <span
                      className="ml-1 rounded px-1.5 py-[1px] font-semibold tracking-wide"
                      style={{
                        background: "var(--color-warning-highlight, #ddcfc6)",
                        color: "var(--color-warning, #964219)",
                      }}
                      title="Entropy snapshot is transiently empty or stale (startup grace period)"
                    >
                      STARTUP
                    </span>
                  )}
                </span>
              )}
            </div>
            <div className="overflow-auto flex-1 p-2">
              <table className="w-full text-[11px] text-left border-collapse">
                <thead className="sticky top-0 bg-slate-900 text-slate-400 border-b border-slate-600">
                  <tr>
                    <th className="p-1 font-medium">Question</th>
                    <th className="p-1 font-medium">|z| max</th>
                    <th className="p-1 font-medium">fires</th>
                    <th className="p-1 font-medium">kyle n</th>
                    <th className="p-1 font-medium">ev/h</th>
                    <th className="p-1 font-medium">locked</th>
                    <th className="p-1 font-medium">z_ready</th>
                  </tr>
                </thead>
                <tbody>
                  {(heavyData.rows ?? []).map((row) => (
                    <tr key={row.market_id} className="border-b border-slate-800 hover:bg-slate-800/50">
                      <td className="p-1 text-slate-200 max-w-[14rem] truncate" title={row.question || row.market_id}>
                        {row.question || row.slug || row.market_id.slice(0, 18) + "…"}
                      </td>
                      <td className="p-1 font-mono text-slate-300">{row.abs_z_max.toFixed(3)}</td>
                      <td className="p-1 font-mono">{row.fire_count}</td>
                      <td className="p-1 font-mono">{row.kyle_n}</td>
                      <td className="p-1 font-mono text-slate-400">
                        {(row.ev_count ?? row.events)}/{(row.h_count ?? row.h_hist)}
                      </td>
                      <td className="p-1">{row.locked ? "Y" : "—"}</td>
                      <td className="p-1">{row.z_ready ? "Y" : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
