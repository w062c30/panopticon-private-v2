import { useEffect, useState } from "react";
import { DecisionPanel } from "./components/DecisionPanel";
import { EntropyLinePanel } from "./components/EntropyLinePanel";
import { EvWaterfallPanel } from "./components/EvWaterfallPanel";
import { FingerprintRadarPanel } from "./components/FingerprintRadarPanel";
import { MarketWatchlistPanel } from "./components/MarketWatchlistPanel";
import { PerformanceSummaryPanel } from "./components/PerformanceSummaryPanel";
import { ProcessHeartbeatPanel } from "./components/ProcessHeartbeatPanel";
import { ReadinessGauge } from "./components/ReadinessGauge";
import { RvfMetricsPanel } from "./components/RvfMetricsPanel";
import { ArbHealthPanel } from "./components/ArbHealthPanel";
import { TradeListPanel } from "./components/TradeListPanel";
import { LiveReportPanel } from "./components/LiveReportPanel";
import { WalletNetworkPanel } from "./components/WalletNetworkPanel";
import { usePanopticonFeed } from "./hooks/usePanopticonFeed";
import type { L1Snapshot } from "./types/dashboard";

export default function App() {
  const { snapshots, status, controls } = usePanopticonFeed();
  const [series, setSeries] = useState<L1Snapshot[]>([]);

  useEffect(() => {
    if (!snapshots) {
      return;
    }
    setSeries((prev) => [...prev.slice(-39), snapshots.l1]);
  }, [snapshots]);

  if (!snapshots) {
    return <div className="min-h-screen bg-panBg p-8 text-slate-200">載入 Panopticon Feed...</div>;
  }

  return (
    <div className="min-h-screen bg-panBg p-4 text-slate-100">
      <ReadinessGauge readiness={snapshots.readiness} />
      <header className="mb-4 rounded-xl border border-slate-700 bg-panPanel p-4">
        <h1 className="text-2xl font-bold">Panopticon 量化主控台</h1>
        <p className="text-sm text-slate-400">L1-L5 映射｜繁體中文｜深色模式</p>
        <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
          <button className="rounded-md bg-slate-700 px-3 py-1" onClick={() => controls.reconnect()}>
            重連
          </button>
          <span className={status.connected ? "text-panGood" : "text-panDanger"}>{status.connected ? "已連線" : "未連線"}</span>
          {snapshots.liveFeedDisconnected && (
            <span className="rounded bg-red-900/60 px-2 py-1 text-red-300">Live feed disconnected</span>
          )}
        </div>
        <div className="mt-3 rounded-md border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-300">
          <span className="font-semibold text-slate-100">系統狀態：</span>
          <span className="ml-2">{snapshots.systemStatus.message}</span>
          {snapshots.systemStatus.lastEventTs && (
            <span className="ml-3 text-xs text-slate-400">更新於 {new Date(snapshots.systemStatus.lastEventTs).toLocaleString("zh-HK")}</span>
          )}
        </div>
      </header>

      <main className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <EntropyLinePanel latest={snapshots.l1} series={series} />
        <FingerprintRadarPanel snapshot={snapshots.l2} />
        <div className="xl:col-span-2 grid grid-cols-1 gap-4 xl:grid-cols-3">
          <div className="xl:col-span-2">
            <EvWaterfallPanel snapshot={snapshots.l34} />
          </div>
          <DecisionPanel snapshot={snapshots.l34} execution={snapshots.execution} />
        </div>
        <PerformanceSummaryPanel
          performance={snapshots.performance}
          pnlHistory={snapshots.pnlHistory}
          selectedPeriod={controls.performancePeriod}
          onPeriodChange={controls.setPerformancePeriod}
        />
        <LiveReportPanel report={snapshots.report} />
        <WalletNetworkPanel apiBaseUrl={import.meta.env.VITE_API_BASE_URL || "http://localhost:8001"} />
        <RvfMetricsPanel />
        <ArbHealthPanel />
        <div className="xl:col-span-2">
          <ProcessHeartbeatPanel />
        </div>
        <div className="xl:col-span-2">
          <TradeListPanel trades={snapshots.trades} />
        </div>
        <div className="xl:col-span-2">
          <MarketWatchlistPanel
            apiBaseUrl={import.meta.env.VITE_API_BASE_URL || "http://localhost:8001"}
          />
        </div>
      </main>
    </div>
  );
}