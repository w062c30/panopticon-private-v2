import type { ReadinessSnapshot } from "../types/dashboard";

type Props = {
  readiness: ReadinessSnapshot;
};

export function ReadinessGauge({ readiness }: Props) {
  const tradeProgress = Math.min(1, readiness.currentPaperTrades / Math.max(readiness.targetTrades, 1));
  const dayProgress = Math.min(1, readiness.runningDays / Math.max(readiness.targetDays, 1));
  const winProgress =
    readiness.currentWinRate === null ? 0 : Math.min(1, readiness.currentWinRate / 0.55);
  const totalProgress = ((tradeProgress + dayProgress + winProgress) / 3) * 100;
  return (
    <section className="mb-4 rounded-xl border border-slate-700 bg-panPanel p-4">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-lg font-semibold text-slate-100">Go-Live Readiness</h2>
        <span className={readiness.isReady ? "text-panGood font-bold" : "text-panWarn font-bold"}>
          {readiness.isReady ? "Ready for Live Execution" : "Shadow Mode: Data Gathering"}
        </span>
      </div>
      <div className="h-3 w-full rounded bg-slate-800">
        <div
          className={`h-3 rounded ${readiness.isReady ? "bg-panGood" : "bg-panWarn"}`}
          style={{ width: `${totalProgress.toFixed(1)}%` }}
        />
      </div>
      <div className="mt-2 grid grid-cols-3 gap-2 text-xs text-slate-300">
        <div>樣本數: {readiness.currentPaperTrades}/{readiness.targetTrades}</div>
        <div>運行天數: {readiness.runningDays}/{readiness.targetDays}</div>
        <div>
          勝率: {readiness.currentWinRate === null ? "N/A" : `${(readiness.currentWinRate * 100).toFixed(1)}%`}
        </div>
      </div>
    </section>
  );
}

