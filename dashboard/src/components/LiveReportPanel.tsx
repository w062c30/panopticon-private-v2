import type { LiveReportSnapshot } from "../types/dashboard";

type Props = {
  report: LiveReportSnapshot;
};

export function LiveReportPanel({ report }: Props) {
  return (
    <section className="rounded-xl border border-slate-700 bg-panPanel p-4">
      <h2 className="mb-3 text-lg font-semibold text-slate-100">數據累積與監察報告</h2>
      <div className="grid grid-cols-2 gap-2 text-sm">
        <div className="rounded bg-slate-900 p-2">Open Trades: {report.counts.openTrades}</div>
        <div className="rounded bg-slate-900 p-2">Closed Trades: {report.counts.closedTrades}</div>
        <div className="rounded bg-slate-900 p-2">Unique Markets: {report.counts.uniqueMarkets}</div>
        <div className="rounded bg-slate-900 p-2">Canonical Hit: {(report.counts.canonicalHitRate * 100).toFixed(1)}%</div>
        <div className="rounded bg-slate-900 p-2">Realized: {report.pnl.realizedTotalUsd.toFixed(2)}</div>
        <div className="rounded bg-slate-900 p-2">Unrealized: {report.pnl.unrealizedTotalUsd.toFixed(2)}</div>
        <div className="rounded bg-slate-900 p-2">Net: {report.pnl.netTotalUsd.toFixed(2)}</div>
        <div className="rounded bg-slate-900 p-2">Fallback Rate: {(report.quality.fallbackRate * 100).toFixed(1)}%</div>
      </div>
      <div className="mt-3 rounded bg-slate-900 p-3 text-xs text-slate-300">
        <div className="mb-2 font-semibold text-slate-200">監察到的重點</div>
        {report.findings.length === 0 ? (
          <div>暫無異常觀察。</div>
        ) : (
          <ul className="space-y-1">
            {report.findings.slice(0, 5).map((f, idx) => (
              <li key={`${idx}-${f}`}>- {f}</li>
            ))}
          </ul>
        )}
        <div className="mt-2 text-[11px] text-slate-500">更新時間：{new Date(report.updatedAt).toLocaleString("zh-HK")}</div>
      </div>
    </section>
  );
}

