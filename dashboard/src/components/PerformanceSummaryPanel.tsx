import {
  CartesianGrid,
  LabelList,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { PerformanceHistoryPoint, PerformancePeriod, PerformanceSnapshot } from "../types/dashboard";

type Props = {
  performance: PerformanceSnapshot;
  pnlHistory: PerformanceHistoryPoint[];
  selectedPeriod: PerformancePeriod;
  onPeriodChange: (period: PerformancePeriod) => void;
};

const PERIODS: PerformancePeriod[] = ["1d", "7d", "30d", "all"];

function yDomainWithPadding(points: PerformanceHistoryPoint[]): [number, number] {
  if (!points.length) {
    return [-10, 10];
  }
  const values = points.map((p) => p.cumulativePnlUsd);
  const min = Math.min(...values);
  const max = Math.max(...values);
  if (min === max) {
    const pad = Math.max(Math.abs(max) * 0.2, 5);
    return [min - pad, max + pad];
  }
  const span = max - min;
  const pad = Math.max(span * 0.15, 5);
  return [min - pad, max + pad];
}

export function PerformanceSummaryPanel({ performance, pnlHistory, selectedPeriod, onPeriodChange }: Props) {
  const profitFactorWarn = performance.profitFactor !== null && performance.profitFactor < 1.2;
  const slippageWarn = performance.slippageGap !== null && performance.slippageGap < -5;
  const domain = yDomainWithPadding(pnlHistory);
  const chartData = pnlHistory.map((point, index) => ({
    ...point,
    isLast: index === pnlHistory.length - 1,
    currentLabel: index === pnlHistory.length - 1 ? `$${point.cumulativePnlUsd.toFixed(2)}` : "",
  }));

  return (
    <section className="rounded-xl border border-slate-700 bg-panPanel p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-lg font-semibold text-slate-100">PnL</h2>
        <div className="flex gap-1">
          {PERIODS.map((period) => (
            <button
              key={period}
              onClick={() => onPeriodChange(period)}
              className={`rounded px-2 py-1 text-xs ${
                selectedPeriod === period ? "bg-panNeon text-slate-950" : "bg-slate-800 text-slate-300"
              }`}
            >
              {period.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      <div className="mb-4 h-64 rounded border border-slate-800 bg-slate-950/40 p-2">
        {chartData.length === 0 ? (
          <div className="flex h-full items-center justify-center text-sm text-slate-400">No live pnl history yet</div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 12, right: 96, bottom: 16, left: 20 }}>
              <CartesianGrid stroke="#334155" strokeDasharray="3 3" />
              <XAxis
                dataKey="ts"
                tickFormatter={(ts) => new Date(ts).toLocaleTimeString("zh-HK")}
                stroke="#94a3b8"
                minTickGap={28}
              />
              <YAxis stroke="#94a3b8" domain={domain} tickFormatter={(v) => `${Number(v).toFixed(0)}`} width={72} />
              <Tooltip
                labelFormatter={(v) => new Date(v as number).toLocaleString("zh-HK")}
                formatter={(v: number) => [`$${v.toFixed(2)}`, "Cumulative PnL"]}
              />
              <Line type="monotone" dataKey="cumulativePnlUsd" stroke="#22d3ee" strokeWidth={2} dot={false} isAnimationActive={false}>
                <LabelList
                  dataKey="currentLabel"
                  position="right"
                  fill="#22d3ee"
                  style={{ fontSize: "12px", fontWeight: 700 }}
                />
              </Line>
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      <div className="grid grid-cols-2 gap-2 text-sm">
        <div className="rounded bg-slate-900 p-2">Total PnL: {performance.totalPnlUsd.toFixed(2)}</div>
        <div className="rounded bg-slate-900 p-2">
          Win Rate: {performance.winRate === null ? "N/A" : `${(performance.winRate * 100).toFixed(1)}%`}
        </div>
        <div className="rounded bg-slate-900 p-2">Sharpe: {performance.sharpeRatio.toFixed(3)}</div>
        <div className="rounded bg-slate-900 p-2">MDD: {(performance.maxDrawdown.value * 100).toFixed(2)}%</div>
        <div className={`rounded p-2 ${profitFactorWarn ? "bg-yellow-900 text-yellow-300" : "bg-slate-900"}`}>
          Profit Factor: {performance.profitFactor === null ? "N/A" : performance.profitFactor.toFixed(2)}
        </div>
        <div className={`rounded p-2 ${slippageWarn ? "bg-red-900 text-red-300" : "bg-slate-900"}`}>
          Slippage Gap: {performance.slippageGap === null ? "N/A" : performance.slippageGap.toFixed(2)}
        </div>
      </div>
    </section>
  );
}
