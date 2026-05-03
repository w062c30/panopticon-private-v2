// D103-FE: Watchlist API types
// D144: Split into two data sources:
//   - Radar subscription list  → /api/radar/active-markets  (live token discovery)
//   - Execution records        → /api/watchlist              (DB signals/trades)

export interface PolMarketEntry {
  market_id: string;
  event_slug: string | null;
  political_category: string;
  entity_keywords: string[];
  token_id: string | null;
  token_id_no: string | null;
  subscribed_at: string;
  last_signal_ts: string | null;
  // D106: Signal statistics (from execution_records LEFT JOIN)
  total_signals: number;
  accepted: number;
  last_activity_ts: string | null;
  avg_ev: number | null;
  avg_posterior: number | null;
}

export interface TierMarketEntry {
  market_id: string;
  market_tier: string;
  total_signals: number;
  accepted: number;
  last_signal_ts: string | null;
  avg_ev: number | null;
  avg_posterior: number | null;
}

export type TierKey = "t1" | "t2" | "t2_pol" | "t3" | "t4" | "t5";

export interface WatchlistResponse {
  pol_markets: PolMarketEntry[];
  t1_markets: TierMarketEntry[];
  t2_markets: TierMarketEntry[];
  t3_markets: TierMarketEntry[];
  t4_markets: TierMarketEntry[];
  t5_markets: TierMarketEntry[];
  tier_available: Record<TierKey, boolean>;
}

export interface MarketDebugStats {
  total_evaluations: number;
  total_paper: number;
  passed_paper: number;
  kyle_samples: number;
}

export interface DebugStatsResponse {
  enabled: boolean;
  markets: Record<string, MarketDebugStats>;
}

// D144: Radar subscription snapshot (reads data/radar_active_markets.json)
export interface RadarTierSnapshot {
  token_ids: string[];
  slugs: Record<string, string>;
  count: number;
  updated_ts: string;
}

export interface RadarActiveMarkets {
  t1: RadarTierSnapshot;
  t2: RadarTierSnapshot;
  t3: RadarTierSnapshot;
  t4: RadarTierSnapshot;
  t5: RadarTierSnapshot;
  error?: string;
}