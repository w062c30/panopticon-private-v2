export type PerformancePeriod = "1d" | "7d" | "30d" | "all";

export type L1Snapshot = {
  ts: number;
  deltaHZ: number;
  ofi: number;
  kyleLambda: number;
  latencyMs: number;
};

export type L2Snapshot = {
  idi: number;
  burstiness: number;
  takerRatio: number;
  smurfSync: number;
  entityTrustScore: number;
};

export type L34Snapshot = {
  pPrior: number;
  lr: number;
  pPosterior: number;
  evGross: number;
  latencyImpact: number;
  slippageLambda: number;
  takerFees: number;
  evNetTimeAdj: number;
  kellyFraction: number;
  kellyUsd: number;
};

export type ExecutionState = {
  canExecute: boolean;
  rejectReason: string | null;
};

export type TradeListItem = {
  tradeId: string;
  marketId: string;
  eventName: string;
  eventUrl?: string | null;
  linkType?: "canonical_event" | "canonical_embed" | "search_fallback";
  linkSource?: "cache" | "live_api" | "fallback";
  linkReason?: "missing_slug" | "stale_mapping" | "recovered_from_api" | "ok";
  direction: "YES" | "NO";
  confidence: number | null;
  openReason: string;
  entryPrice: number | null;
  exitPrice: number | null;
  positionSizeUsd: number | null;
  estimatedEvUsd: number | null;
  realizedPnlUsd: number | null;
  unrealizedPnlUsd: number | null;
  status: "open" | "closed";
  markPrice: number | null;
  updatedAt: string;
  closeCondition: string;
  openedAt: string;
  closedAt: string;
  source?: "paper" | "live" | "db_settlement" | "open_position";
};

export type LiveReportSnapshot = {
  counts: {
    openTrades: number;
    closedTrades: number;
    uniqueMarkets: number;
    canonicalHitRate: number;
  };
  pnl: {
    realizedTotalUsd: number;
    unrealizedTotalUsd: number;
    netTotalUsd: number;
  };
  quality: {
    fallbackRate: number;
    unresolvedCount: number;
  };
  findings: string[];
  updatedAt: string;
};

export type PerformanceSnapshot = {
  period: PerformancePeriod;
  totalPnlUsd: number;
  winRate: number | null;
  sharpeRatio: number;
  maxDrawdown: {
    value: number;
    peakTs: string | null;
    troughTs: string | null;
    fromTradeId: string | null;
    toTradeId: string | null;
  };
  profitFactor: number | null;
  slippageGap: number | null;
  tradeCount: number;
};

export type PerformanceHistoryPoint = {
  ts: number;
  cumulativePnlUsd: number;
};

export type SystemStatusSnapshot = {
  state: string;
  message: string;
  lastEventTs: string | null;
  lastDecisionId: string | null;
  lastExecutionReason: string | null;
  lastRejectReason: string | null;
};

export type ReadinessSnapshot = {
  currentPaperTrades: number;
  targetTrades: number;
  runningDays: number;
  targetDays: number;
  currentWinRate: number | null;
  isReady: boolean;
};

export type PanopticonSnapshot = {
  l1: L1Snapshot;
  l2: L2Snapshot;
  l34: L34Snapshot;
  execution: ExecutionState;
  trades: TradeListItem[];
  performance: PerformanceSnapshot;
  pnlHistory: PerformanceHistoryPoint[];
  systemStatus: SystemStatusSnapshot;
  report: LiveReportSnapshot;
  readiness: ReadinessSnapshot;
  liveFeedDisconnected: boolean;
};

export type PanopticonStatus = {
  mode: "live";
  connected: boolean;
  lastUpdateTs: number;
};

export type WalletGraphNode = {
  id: string;
  label: string;
  fullAddress: string;
  entityId: string;
  pnl: number;
  winRate: number;
  source: string;
  quality: string;
  color: string;
  value: number;
};

export type WalletGraphEdge = {
  from: string;
  to: string;
  relation: "SAME_ENTITY" | "SHARED_MARKET" | "SIMILAR_TIMING";
  weight: number;
  markets: string[];
  title: string;
};

export type WalletGraph = {
  nodes: WalletGraphNode[];
  edges: WalletGraphEdge[];
};

export interface PanopticonFeedAdapter {
  connect(onSnapshot: (next: PanopticonSnapshot) => void): () => void;
  setPerformancePeriod?(period: PerformancePeriod): void;
}

export type ProcessInfo = {
  pid: number;
  version: string;
  expected: string;
  version_match: boolean;
  start_time: string;
  last_heartbeat_ts?: string;
  host: string;
  status: "running" | "stopped";
};

export type ProcessHeartbeat = {
  timestamp: number;
} & Record<string, ProcessInfo | { status: string } | number>;
