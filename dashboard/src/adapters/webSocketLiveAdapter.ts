import type { PanopticonFeedAdapter, PanopticonSnapshot, PerformancePeriod } from "../types/dashboard";
import {
  buildEmptyLiveSnapshot,
  isLiveCorePayload,
  normalizeLivePayload,
  normalizePerformance,
  normalizePerformanceHistory,
  normalizeReport,
  normalizeReadiness,
  normalizeSystemStatus,
  normalizeTrades,
} from "../data/liveAdapter";

// Server latency measured at ~13ms (D62 3-C). 3s = 230x safety margin over server + network variability.
const FETCH_TIMEOUT_MS = 3_000;

async function safeFetch<T>(url: string, signal: AbortSignal): Promise<T | null> {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
    const broader = AbortSignal.any([signal, controller.signal]);
    const resp = await fetch(url, { signal: broader });
    clearTimeout(timeout);
    if (!resp.ok) return null;
    return (await resp.json()) as T;
  } catch {
    return null;
  }
}

export class WebSocketLiveAdapter implements PanopticonFeedAdapter {
  private period: PerformancePeriod = "all";

  constructor(
    private readonly wsUrl = "ws://localhost:8001/ws/stream",
    private readonly apiBaseUrl = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8001",
  ) {}

  setPerformancePeriod(period: PerformancePeriod): void {
    this.period = period;
  }

  connect(onSnapshot: Parameters<PanopticonFeedAdapter["connect"]>[0]): () => void {
    let socket: WebSocket | null = null;
    let pollTimer: number | null = null;
    let abortController: AbortController | null = null;
    let latest: PanopticonSnapshot = buildEmptyLiveSnapshot(this.period);
    let wsConnected = false;

    const emit = () => onSnapshot(latest);

    const markDisconnected = () => {
      wsConnected = false;
      latest = {
        ...latest,
        liveFeedDisconnected: true,
        execution: {
          ...latest.execution,
          rejectReason: "Live feed disconnected",
        },
      };
      emit();
    };

    const mergeRest = async () => {
      abortController?.abort();
      abortController = new AbortController();
      const sig = abortController.signal;

      const [recs, perf, perfHistory, ready, status, report] = await Promise.allSettled([
        safeFetch<Record<string, unknown>>(`${this.apiBaseUrl}/api/recommendations?limit=20`, sig),
        safeFetch<Record<string, unknown>>(`${this.apiBaseUrl}/api/performance?period=${this.period}`, sig),
        safeFetch<Record<string, unknown>>(`${this.apiBaseUrl}/api/performance/history?period=${this.period}`, sig),
        safeFetch<Record<string, unknown>>(`${this.apiBaseUrl}/api/system_health/readiness`, sig),
        safeFetch<Record<string, unknown>>(`${this.apiBaseUrl}/api/system_health/status`, sig),
        safeFetch<Record<string, unknown>>(`${this.apiBaseUrl}/api/report/current`, sig),
      ]);

      let changed = false;

      if (recs.status === "fulfilled" && recs.value !== null) {
        latest = { ...latest, trades: normalizeTrades(recs.value.trades as unknown) };
        changed = true;
      }
      if (perf.status === "fulfilled" && perf.value !== null) {
        latest = { ...latest, performance: normalizePerformance(perf.value, this.period) };
        changed = true;
      }
      if (perfHistory.status === "fulfilled" && perfHistory.value !== null) {
        latest = { ...latest, pnlHistory: normalizePerformanceHistory(perfHistory.value) };
        changed = true;
      }
      if (ready.status === "fulfilled" && ready.value !== null) {
        latest = { ...latest, readiness: normalizeReadiness(ready.value) };
        changed = true;
      }
      if (status.status === "fulfilled" && status.value !== null) {
        latest = { ...latest, systemStatus: normalizeSystemStatus(status.value) };
        changed = true;
      }
      if (report.status === "fulfilled" && report.value !== null) {
        latest = { ...latest, report: normalizeReport(report.value) };
        changed = true;
      }

      // Only mark as connected once we receive at least one successful REST response
      if (!wsConnected) {
        wsConnected = true;
        latest = {
          ...latest,
          liveFeedDisconnected: false,
        };
        changed = true;
      }

      if (changed) emit();
    };

    try {
      socket = new WebSocket(this.wsUrl);
      socket.onopen = () => {
        wsConnected = true;
        latest = { ...latest, liveFeedDisconnected: false };
        emit();
      };
      socket.onmessage = (event) => {
        const parsed = JSON.parse(event.data as string) as unknown;
        if (isLiveCorePayload(parsed)) {
          const normalized = normalizeLivePayload(parsed, this.period);
          latest = {
            ...latest,
            ...normalized,
            // REST data takes precedence over stale WS data
            trades: latest.trades,
            performance: latest.performance,
            pnlHistory: latest.pnlHistory,
            systemStatus: latest.systemStatus,
            readiness: latest.readiness,
            liveFeedDisconnected: false,
          };
          emit();
        }
      };
      socket.onerror = () => {
        markDisconnected();
      };
      socket.onclose = () => {
        markDisconnected();
      };
    } catch {
      markDisconnected();
    }

    void mergeRest();
    pollTimer = window.setInterval(() => {
      void mergeRest();
    }, 10_000);

    return () => {
      if (pollTimer !== null) window.clearInterval(pollTimer);
      abortController?.abort();
      socket?.close();
    };
  }
}
