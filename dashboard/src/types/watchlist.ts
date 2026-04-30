// D103-FE: Watchlist API types

export interface PolMarketEntry {
  market_id: string;
  event_slug: string | null;
  political_category: string;
  entity_keywords: string[];
  token_id: string | null;
  token_id_no: string | null;
  subscribed_at: string;
  last_signal_ts: string | null;
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