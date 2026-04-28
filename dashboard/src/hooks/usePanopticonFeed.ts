import { useEffect, useMemo, useState } from "react";
import { WebSocketLiveAdapter } from "../adapters/webSocketLiveAdapter";
import type {
  PanopticonSnapshot,
  PanopticonStatus,
  PerformancePeriod,
} from "../types/dashboard";

function createLiveAdapter(): WebSocketLiveAdapter {
  return new WebSocketLiveAdapter();
}

export function usePanopticonFeed() {
  const [snapshot, setSnapshot] = useState<PanopticonSnapshot | null>(null);
  const [connected, setConnected] = useState(false);
  const [nonce, setNonce] = useState(0);
  const [period, setPeriod] = useState<PerformancePeriod>("all");

  useEffect(() => {
    setConnected(false);
    setSnapshot(null);
    const adapter = createLiveAdapter();
    adapter.setPerformancePeriod?.(period);
    const dispose = adapter.connect((next) => {
      setSnapshot(next);
      setConnected(!next.liveFeedDisconnected);
    });
    return () => {
      setConnected(false);
      dispose();
    };
  }, [nonce, period]);

  const status: PanopticonStatus = useMemo(
    () => ({
      mode: "live" as const,
      connected,
      lastUpdateTs: snapshot?.l1.ts ?? 0,
    }),
    [connected, snapshot?.l1.ts],
  );

  return {
    snapshots: snapshot,
    status,
    controls: {
      reconnect: () => setNonce((v) => v + 1),
      setPerformancePeriod: (nextPeriod: PerformancePeriod) => setPeriod(nextPeriod),
      performancePeriod: period,
    },
  };
}