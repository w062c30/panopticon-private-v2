import type {
  LiveReportSnapshot,
  PanopticonSnapshot,
  PerformanceHistoryPoint,
  PerformancePeriod,
  PerformanceSnapshot,
  ReadinessSnapshot,
  SystemStatusSnapshot,
  TradeListItem,
} from "../types/dashboard";

const toFloat = (v: unknown): number | null => {
  if (v === null || v === undefined) return null;
  if (typeof v === "number") return isNaN(v) ? null : v;
  const s = String(v).trim().toLowerCase();
  if (s === "none" || s === "null" || s === "nan" || s === "") return null;
  const n = Number(s);
  return isNaN(n) ? null : n;
};

export function defaultPerformance(period: PerformancePeriod): PerformanceSnapshot {
  return {
    period,
    totalPnlUsd: 0,
    winRate: null,
    sharpeRatio: 0,
    maxDrawdown: {
      value: 0,
      peakTs: null,
      troughTs: null,
      fromTradeId: null,
      toTradeId: null,
    },
    profitFactor: null,
    slippageGap: null,
    tradeCount: 0,
  };
}

export function defaultReadiness(): ReadinessSnapshot {
  return {
    currentPaperTrades: 0,
    targetTrades: 100,
    runningDays: 0,
    targetDays: 14,
    currentWinRate: null,
    isReady: false,
  };
}

export function defaultSystemStatus(): SystemStatusSnapshot {
  return {
    state: "idle",
    message: "等待後端即時資料",
    lastEventTs: null,
    lastDecisionId: null,
    lastExecutionReason: null,
    lastRejectReason: null,
  };
}

export function defaultReport(): LiveReportSnapshot {
  return {
    counts: {
      openTrades: 0,
      closedTrades: 0,
      uniqueMarkets: 0,
      canonicalHitRate: 0,
    },
    pnl: {
      realizedTotalUsd: 0,
      unrealizedTotalUsd: 0,
      netTotalUsd: 0,
    },
    quality: {
      fallbackRate: 0,
      unresolvedCount: 0,
    },
    findings: [],
    updatedAt: new Date().toISOString(),
  };
}

export function normalizeTrades(payload: unknown): TradeListItem[] {
  if (!Array.isArray(payload)) {
    return [];
  }
  return payload
    .filter((row): row is TradeListItem => typeof row === "object" && row !== null && "tradeId" in row)
    .filter((row) => {
      const mid = (row as unknown as Record<string, unknown>).marketId as string | undefined;
      const name = (row as unknown as Record<string, unknown>).eventName as string | undefined;
      const midLower = (mid ?? "").toLowerCase();
      const nameLower = (name ?? "").toLowerCase();
      if (midLower.startsWith("demo") || midLower.startsWith("test") || midLower === "unknown") return false;
      if (nameLower.startsWith("demo") || nameLower.startsWith("paper event") || nameLower === "demo-market") return false;
      return true;
    })
    .map((row) => {
      const r = row as unknown as Record<string, unknown>;
      return {
        ...row,
        confidence:        toFloat(r.confidence),
        entryPrice:       toFloat(r.entryPrice),
        exitPrice:        toFloat(r.exitPrice),
        positionSizeUsd:  toFloat(r.positionSizeUsd),
        estimatedEvUsd:    toFloat(r.estimatedEvUsd),
        realizedPnlUsd:   toFloat(r.realizedPnlUsd),
        unrealizedPnlUsd: toFloat(r.unrealizedPnlUsd),
        markPrice:        toFloat(r.markPrice),
        status: (r.status as "open" | "closed") ?? "closed",
        source: (r.source as "paper" | "live" | "db_settlement" | "open_position") ?? "live",
        updatedAt: String(r.updatedAt ?? r.closedAt ?? ""),
      };
    })
    .slice(0, 200);
}

export function normalizePerformance(payload: unknown, period: PerformancePeriod): PerformanceSnapshot {
  if (!payload || typeof payload !== "object") {
    return defaultPerformance(period);
  }
  const row = payload as Record<string, unknown>;
  return {
    period,
    totalPnlUsd: Number(row.totalPnlUsd ?? 0),
    winRate: row.winRate === null || row.winRate === undefined ? null : Number(row.winRate),
    sharpeRatio: Number(row.sharpeRatio ?? 0),
    maxDrawdown: {
      value: Number((row.maxDrawdown as Record<string, unknown> | undefined)?.value ?? 0),
      peakTs: String((row.maxDrawdown as Record<string, unknown> | undefined)?.peakTs ?? "") || null,
      troughTs: String((row.maxDrawdown as Record<string, unknown> | undefined)?.troughTs ?? "") || null,
      fromTradeId: String((row.maxDrawdown as Record<string, unknown> | undefined)?.fromTradeId ?? "") || null,
      toTradeId: String((row.maxDrawdown as Record<string, unknown> | undefined)?.toTradeId ?? "") || null,
    },
    profitFactor: row.profitFactor === null || row.profitFactor === undefined ? null : Number(row.profitFactor),
    slippageGap: row.slippageGap === null || row.slippageGap === undefined ? null : Number(row.slippageGap),
    tradeCount: Number(row.tradeCount ?? 0),
  };
}

export function normalizePerformanceHistory(payload: unknown): PerformanceHistoryPoint[] {
  if (!payload || typeof payload !== "object") {
    return [];
  }
  const points = (payload as Record<string, unknown>).points;
  if (!Array.isArray(points)) {
    return [];
  }
  return points
    .map((row) => {
      const record = row as Record<string, unknown>;
      const tsRaw = String(record.ts ?? "");
      const ts = Date.parse(tsRaw);
      return {
        ts: Number.isFinite(ts) ? ts : 0,
        cumulativePnlUsd: Number(record.cumulativePnlUsd ?? 0),
      };
    })
    .filter((row) => row.ts > 0)
    .slice(-300);
}

export function normalizeSystemStatus(payload: unknown): SystemStatusSnapshot {
  if (!payload || typeof payload !== "object") {
    return defaultSystemStatus();
  }
  const row = payload as Record<string, unknown>;
  return {
    state: String(row.state ?? "idle"),
    message: String(row.message ?? "等待後端即時資料"),
    lastEventTs: String(row.lastEventTs ?? "") || null,
    lastDecisionId: String(row.lastDecisionId ?? "") || null,
    lastExecutionReason: String(row.lastExecutionReason ?? "") || null,
    lastRejectReason: String(row.lastRejectReason ?? "") || null,
  };
}

export function normalizeReport(payload: unknown): LiveReportSnapshot {
  if (!payload || typeof payload !== "object") {
    return defaultReport();
  }
  const row = payload as Record<string, unknown>;
  const counts = (row.counts as Record<string, unknown> | undefined) ?? {};
  const pnl = (row.pnl as Record<string, unknown> | undefined) ?? {};
  const quality = (row.quality as Record<string, unknown> | undefined) ?? {};
  const findings = Array.isArray(row.findings) ? row.findings.map((x) => String(x)) : [];
  return {
    counts: {
      openTrades: Number(counts.openTrades ?? 0),
      closedTrades: Number(counts.closedTrades ?? 0),
      uniqueMarkets: Number(counts.uniqueMarkets ?? 0),
      canonicalHitRate: Number(counts.canonicalHitRate ?? 0),
    },
    pnl: {
      realizedTotalUsd: Number(pnl.realizedTotalUsd ?? 0),
      unrealizedTotalUsd: Number(pnl.unrealizedTotalUsd ?? 0),
      netTotalUsd: Number(pnl.netTotalUsd ?? 0),
    },
    quality: {
      fallbackRate: Number(quality.fallbackRate ?? 0),
      unresolvedCount: Number(quality.unresolvedCount ?? 0),
    },
    findings,
    updatedAt: String(row.updatedAt ?? new Date().toISOString()),
  };
}

export function normalizeReadiness(payload: unknown): ReadinessSnapshot {
  if (!payload || typeof payload !== "object") {
    return defaultReadiness();
  }
  const row = payload as Record<string, unknown>;
  return {
    currentPaperTrades: Number(row.currentPaperTrades ?? 0),
    targetTrades: Number(row.targetTrades ?? 100),
    runningDays: Number(row.runningDays ?? 0),
    targetDays: Number(row.targetDays ?? 14),
    currentWinRate: row.currentWinRate === null || row.currentWinRate === undefined ? null : Number(row.currentWinRate),
    isReady: Boolean(row.isReady),
  };
}

export function normalizeLivePayload(payload: unknown, period: PerformancePeriod = "all"): PanopticonSnapshot {
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    if (record.l1 && record.l2 && record.l34 && record.execution) {
      const fallback = buildEmptyLiveSnapshot(period);
      return {
        ...fallback,
        l1: record.l1 as PanopticonSnapshot["l1"],
        l2: record.l2 as PanopticonSnapshot["l2"],
        l34: record.l34 as PanopticonSnapshot["l34"],
        execution: record.execution as PanopticonSnapshot["execution"],
        liveFeedDisconnected: false,
      };
    }
  }
  return buildEmptyLiveSnapshot(period);
}

export function isLiveCorePayload(payload: unknown): payload is Pick<PanopticonSnapshot, "l1" | "l2" | "l34" | "execution"> {
  if (!payload || typeof payload !== "object") {
    return false;
  }
  const row = payload as Record<string, unknown>;
  return Boolean(row.l1 && row.l2 && row.l34 && row.execution);
}

export function buildEmptyLiveSnapshot(period: PerformancePeriod): PanopticonSnapshot {
  return {
    l1: {
      ts: Date.now(),
      deltaHZ: 0,
      ofi: 0,
      kyleLambda: 0,
      latencyMs: 0,
    },
    l2: {
      idi: 0,
      burstiness: 0,
      takerRatio: 0,
      smurfSync: 0,
      entityTrustScore: 0,
    },
    l34: {
      pPrior: 0,
      lr: 0,
      pPosterior: 0,
      evGross: 0,
      latencyImpact: 0,
      slippageLambda: 0,
      takerFees: 0,
      evNetTimeAdj: 0,
      kellyFraction: 0,
      kellyUsd: 0,
    },
    execution: {
      canExecute: false,
      rejectReason: "Live feed disconnected",
    },
    trades: [],
    performance: defaultPerformance(period),
    pnlHistory: [],
    systemStatus: defaultSystemStatus(),
    readiness: defaultReadiness(),
    report: defaultReport(),
    liveFeedDisconnected: true,
  };
}
