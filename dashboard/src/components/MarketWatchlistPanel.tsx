import { useEffect, useState, useCallback } from "react";
import type {
  WatchlistResponse, DebugStatsResponse,
  TierKey, PolMarketEntry, TierMarketEntry,
  RadarActiveMarkets, RadarTierSnapshot,
} from "../types/watchlist";

// ── Constants ────────────────────────────────────────────────────────────

const TIER_LABELS: Record<TierKey, string> = {
  t1:     "T1 藍籌",
  t2:     "T2 聰明錢",
  t2_pol: "T2-POL 政治",
  t3:     "T3 標準",
  t4:     "T4 新興",
  t5:     "T5 體育",
};

const TIER_COLORS: Record<TierKey, string> = {
  t1:     "bg-blue-500/20   text-blue-300   border-blue-500/40",
  t2:     "bg-purple-500/20 text-purple-300 border-purple-500/40",
  t2_pol: "bg-amber-500/20  text-amber-300  border-amber-500/40",
  t3:     "bg-slate-500/20  text-slate-300  border-slate-500/40",
  t4:     "bg-green-500/20  text-green-300  border-green-500/40",
  t5:     "bg-cyan-500/20   text-cyan-300   border-cyan-500/40",
};

const RADAR_TIER_ORDER: TierKey[] = ["t1", "t2", "t3", "t4", "t5"];
const EXEC_TIER_ORDER: TierKey[] = ["t1", "t2", "t2_pol", "t3", "t4", "t5"];
const POLYMARKET_BASE = "https://polymarket.com/event/";
const REFRESH_MS        = 30_000;
const DEBUG_REFRESH_MS = 60_000;

// ── Helpers ─────────────────────────────────────────────────────────────

function formatRelativeTime(ts: string): string {
  if (!ts) return "—";
  const ms = Date.now() - new Date(ts).getTime();
  if (isNaN(ms)) return "—";
  const mins = Math.floor(ms / 60_000);
  if (mins < 60)  return `${mins}m 前`;
  const hrs = Math.floor(mins / 60);
  if (hrs  < 24)  return `${hrs}h 前`;
  return `${Math.floor(hrs / 24)}d 前`;
}

function getTierMarkets(
  data: WatchlistResponse, tier: TierKey
): PolMarketEntry[] | TierMarketEntry[] {
  if (tier === "t2_pol") return data.pol_markets;
  return data[`${tier}_markets` as keyof WatchlistResponse] as TierMarketEntry[];
}

function getTierCount(data: WatchlistResponse, tier: TierKey): number {
  const markets = getTierMarkets(data, tier);
  return (markets as unknown[])?.length ?? 0;
}

function getRadarTier(
  radar: RadarActiveMarkets, tier: TierKey
): RadarTierSnapshot | null {
  const snap = (radar as unknown as Record<string, RadarTierSnapshot>)[tier];
  if (!snap || typeof snap !== "object") return null;
  return snap;
}

// ── Main Component ─────────────────────────────────────────────────────

interface Props { apiBaseUrl: string; }

export function MarketWatchlistPanel({ apiBaseUrl }: Props) {
  const [execData,   setExecData]   = useState<WatchlistResponse | null>(null);
  const [radarData,  setRadarData]  = useState<RadarActiveMarkets | null>(null);
  const [loading,     setLoading]    = useState(true);
  const [execError,  setExecError]  = useState<string | null>(null);
  const [radarError, setRadarError] = useState<string | null>(null);
  const [debugStats, setDebugStats] = useState<DebugStatsResponse>({
    enabled: false, markets: {},
  });
  const [enabledTiers, setEnabledTiers] = useState<Set<TierKey>>(
    new Set(EXEC_TIER_ORDER)
  );

  // ── Execution records fetch (30s) ────────────────────────────────
  const fetchExec = useCallback(async () => {
    try {
      const res = await fetch(`${apiBaseUrl}/api/watchlist`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setExecData(await res.json());
      setExecError(null);
    } catch (e) {
      setExecError(e instanceof Error ? e.message : "fetch failed");
    }
  }, [apiBaseUrl]);

  // ── Radar subscription snapshot fetch (30s) ────────────────────
  const fetchRadar = useCallback(async () => {
    try {
      const res = await fetch(`${apiBaseUrl}/api/radar/active-markets`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setRadarData(json);
      setRadarError(null);
    } catch (e) {
      setRadarError(e instanceof Error ? e.message : "fetch failed");
    }
  }, [apiBaseUrl]);

  useEffect(() => {
    fetchExec();
    fetchRadar();
    const id = setInterval(() => { fetchExec(); fetchRadar(); }, REFRESH_MS);
    return () => clearInterval(id);
  }, [fetchExec, fetchRadar]);

  // ── Debug stats fetch (60s, silent) ─────────────────────────────
  useEffect(() => {
    const fetchDebug = async () => {
      try {
        const res = await fetch(
          `${apiBaseUrl}/api/watchlist/market-debug-stats`
        );
        if (!res.ok) return;
        setDebugStats(await res.json());
      } catch { /* silent */ }
    };
    fetchDebug();
    const id = setInterval(fetchDebug, DEBUG_REFRESH_MS);
    return () => clearInterval(id);
  }, [apiBaseUrl]);

  function toggleTier(tier: TierKey) {
    setEnabledTiers((prev) => {
      const next = new Set(prev);
      next.has(tier) ? next.delete(tier) : next.add(tier);
      return next;
    });
  }

  const loaded = execData !== null || radarData !== null;

  // ── Render ───────────────────────────────────────────────────────
  return (
    <div className="rounded-xl border border-slate-700 bg-panPanel p-4 flex flex-col"
         style={{ maxHeight: "calc(100vh - 8rem)", overflow: "hidden" }}>

      {/* Header */}
      <div className="mb-3 flex items-center gap-3 shrink-0">
        <h2 className="text-base font-semibold text-slate-100">
          市場監控清單
        </h2>
        {debugStats.enabled && (
          <span className="rounded border border-amber-700/50 bg-amber-900/40
                           px-2 py-0.5 text-xs text-amber-400">
            DEBUG 模式
          </span>
        )}
        <span className="ml-auto text-xs text-slate-500">每 30 秒更新</span>
      </div>

      {/* Loading */}
      {loading && !loaded && (
        <p className="text-sm text-slate-400 shrink-0">載入中...</p>
      )}

      {/* Scrollable two-column layout */}
      <div className="flex-1 overflow-y-auto min-h-0">
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">

        {/* ── Left column: Radar subscription list ──────────────────── */}
        <div>
          <div className="mb-2 flex items-center gap-2">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wide">
              Radar 訂閱清單
            </span>
            <span className="text-xs text-slate-600">
              資料來源：radar 動態發現
            </span>
          </div>

          {radarError && (
            <p className="text-xs text-red-400 mb-2">
              ⚠ Radar 快照無法讀取：{radarError}
            </p>
          )}

          {radarData?.error && (
            <p className="text-xs text-amber-400 mb-2">
              ⚠ {radarData.error}
            </p>
          )}

          {!radarData || radarData.error ? (
            <p className="text-xs text-slate-500">
              Radar 目前無監聽市場 — 請確認 radar 進程狀態
            </p>
          ) : (
            RADAR_TIER_ORDER.map((tier) => {
              const snap = getRadarTier(radarData, tier);
              if (!snap) return null;
              return (
                <RadarTierRow
                  key={tier}
                  tier={tier}
                  snap={snap}
                />
              );
            })
          )}
        </div>

        {/* ── Right column: Execution records (existing watchlist) ──── */}
        <div>
          <div className="mb-2 flex items-center gap-2">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wide">
              執行記錄
            </span>
            <span className="text-xs text-slate-600">
              資料來源：execution_records（48h 內有交易/信號）
            </span>
          </div>

          {/* Tier filter toggles */}
          <div className="mb-3 flex flex-wrap gap-1.5">
            {EXEC_TIER_ORDER.map((tier) => {
              const active = enabledTiers.has(tier);
              return (
                <button
                  key={tier}
                  onClick={() => toggleTier(tier)}
                  className={`rounded border px-2 py-0.5 text-xs transition-opacity
                              ${TIER_COLORS[tier]}
                              ${active ? "opacity-100" : "opacity-30"}`}
                >
                  {TIER_LABELS[tier]}
                  {execData && (
                    <span className="ml-1 opacity-70">
                      ({getTierCount(execData, tier)})
                    </span>
                  )}
                </button>
              );
            })}
          </div>

          {execError && (
            <p className="text-xs text-red-400 mb-2">
              ⚠ API 無回應：{execError}
            </p>
          )}

          {execData && EXEC_TIER_ORDER.filter((t) => enabledTiers.has(t)).map((tier) => (
            <TierSection
              key={tier}
              tier={tier}
              data={execData}
              available={execData.tier_available[tier]}
              debugStats={debugStats}
            />
          ))}
        </div>
        </div>
      </div>
    </div>
  );
}

// ── Radar subscription row ─────────────────────────────────────────────────

function RadarTierRow({
  tier, snap,
}: {
  tier: TierKey;
  snap: RadarTierSnapshot;
}) {
  const slugCount = Object.keys(snap.slugs).length;
  const cleanTs = snap.updated_ts ?? "";
  const updatedAgo = cleanTs ? formatRelativeTime(cleanTs) : "—";

  return (
    <div className="mb-2 rounded border border-slate-700/60 p-2">
      <div className={`mb-1 inline-block rounded border px-2 py-0.5
                       text-xs font-semibold ${TIER_COLORS[tier]}`}>
        {TIER_LABELS[tier]}
        <span className="ml-1.5 font-normal text-slate-400">
          {snap.count} 個代幣
        </span>
      </div>
      <div className="text-xs text-slate-500">
        {slugCount > 0
          ? <span className="text-slate-400">{slugCount} 個市場視窗</span>
          : <span className="text-amber-400/70">無 slug（結算市場）</span>}
        {" · "}
        更新 {updatedAgo}
      </div>
      {snap.token_ids.slice(0, 5).map((tid) => (
        <div key={tid} className="mt-0.5 truncate font-mono text-[10px] text-slate-600">
          {tid.slice(0, 20)}…
        </div>
      ))}
      {snap.token_ids.length > 5 && (
        <div className="text-[10px] text-slate-600">
          …+{snap.token_ids.length - 5} more
        </div>
      )}
    </div>
  );
}

// ── TierSection (existing execution records) ─────────────────────────────────

function TierSection({
  tier, data, available, debugStats,
}: {
  tier: TierKey;
  data: WatchlistResponse;
  available: boolean;
  debugStats: DebugStatsResponse;
}) {
  const textColor = TIER_COLORS[tier].split(" ")[1];

  if (!available) {
    return (
      <div className="mb-2 rounded-lg border border-slate-700/50 p-2">
        <span className={`text-xs font-semibold ${textColor}`}>
          {TIER_LABELS[tier]}
        </span>
        <p className="mt-0.5 text-xs text-slate-500">
          ⚠ 過去 48h 無執行記錄
          {tier === "t2_pol"
            ? "（Paper mode 下預期如此）"
            : ""}
        </p>
      </div>
    );
  }

  const markets = getTierMarkets(data, tier);

  return (
    <div className="mb-3">
      <div className={`mb-1 inline-block rounded border px-2 py-0.5
                       text-xs font-semibold ${TIER_COLORS[tier]}`}>
        {TIER_LABELS[tier]} — {markets.length} 個市場
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[560px] text-xs text-slate-300">
          <thead>
            <tr className="border-b border-slate-700 text-slate-500">
              <th className="pb-1 text-left">市場</th>
              {tier === "t2_pol" && (
                <>
                  <th className="pb-1 text-right">信號數</th>
                  <th className="pb-1 text-right">採納</th>
                  <th className="pb-1 text-right">avg EV</th>
                  <th className="pb-1 text-left">類別</th>
                </>
              )}
              {tier !== "t2_pol" && (
                <>
                  <th className="pb-1 text-right">信號數</th>
                  <th className="pb-1 text-right">採納</th>
                  <th className="pb-1 text-right">avg EV</th>
                </>
              )}
              <th className="pb-1 text-right">最後信號</th>
              {debugStats.enabled && (
                <>
                  <th className="pb-1 text-right text-amber-500/70">Kyle樣本</th>
                  <th className="pb-1 text-right text-amber-500/70">評估</th>
                  <th className="pb-1 text-right text-amber-500/70">Paper通過</th>
                </>
              )}
            </tr>
          </thead>
          <tbody>
            {tier === "t2_pol"
              ? (markets as PolMarketEntry[]).map((m) => (
                  <PolRow key={m.market_id} m={m} debugStats={debugStats} />
                ))
              : (markets as TierMarketEntry[]).map((m) => (
                  <TierRow key={m.market_id} m={m} debugStats={debugStats} />
                ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Row Components ─────────────────────────────────────────────────────────────

function DebugCells({ marketId, debugStats }: {
  marketId: string; debugStats: DebugStatsResponse;
}) {
  if (!debugStats.enabled) return null;
  const s = debugStats.markets[marketId];
  if (!s) return <><td /><td /><td /></>;
  return (
    <>
      <td className="py-1 text-right text-amber-400/60">{s.kyle_samples}</td>
      <td className="py-1 text-right text-amber-400/60">{s.total_evaluations}</td>
      <td className="py-1 text-right text-amber-400/60">
        {s.passed_paper}/{s.total_paper}
      </td>
    </>
  );
}

function PolRow({ m, debugStats }: {
  m: PolMarketEntry; debugStats: DebugStatsResponse;
}) {
  const slug = m.event_slug ?? m.market_id.slice(0, 20);
  const url  = m.event_slug ? `${POLYMARKET_BASE}${m.event_slug}` : null;
  return (
    <tr className="border-b border-slate-800 hover:bg-slate-800/30">
      <td className="py-1">
        {url
          ? <a href={url} target="_blank" rel="noreferrer"
               className="text-blue-400 hover:underline">{slug}</a>
          : <span className="font-mono text-slate-400">{slug}</span>}
        {m.entity_keywords.length > 0 && (
          <div className="text-[10px] text-slate-600">
            {m.entity_keywords.join(", ")}
          </div>
        )}
      </td>
      <td className="py-1 text-right">{m.total_signals}</td>
      <td className="py-1 text-right">{m.accepted}</td>
      <td className={`py-1 text-right ${
        m.avg_ev == null ? "text-slate-500"
        : m.avg_ev > 0   ? "text-green-400"
        :                   "text-red-400"
      }`}>
        {m.avg_ev != null ? m.avg_ev.toFixed(4) : "—"}
      </td>
      <td className="py-1 text-slate-400">{m.political_category}</td>
      <td className="py-1 text-right text-slate-500">
        {formatRelativeTime(m.last_signal_ts ?? "")}
      </td>
      <DebugCells marketId={m.market_id} debugStats={debugStats} />
    </tr>
  );
}

function TierRow({ m, debugStats }: {
  m: TierMarketEntry; debugStats: DebugStatsResponse;
}) {
  const evColor =
    m.avg_ev == null ? "text-slate-500"
    : m.avg_ev > 0   ? "text-green-400"
    :                   "text-red-400";
  return (
    <tr className="border-b border-slate-800 hover:bg-slate-800/30">
      <td className="py-1 font-mono text-slate-400">
        {m.market_id.length > 26
          ? `${m.market_id.slice(0, 26)}…`
          : m.market_id}
      </td>
      <td className="py-1 text-right">{m.total_signals}</td>
      <td className="py-1 text-right">{m.accepted}</td>
      <td className={`py-1 text-right ${evColor}`}>
        {m.avg_ev != null ? m.avg_ev.toFixed(4) : "—"}
      </td>
      <td className="py-1 text-right text-slate-500">
        {formatRelativeTime(m.last_signal_ts ?? "")}
      </td>
      <DebugCells marketId={m.market_id} debugStats={debugStats} />
    </tr>
  );
}
