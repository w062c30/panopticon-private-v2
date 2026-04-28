import { Bar, BarChart, CartesianGrid, Cell, LabelList, ReferenceLine, ResponsiveContainer, XAxis, YAxis } from "recharts";
import type { L34Snapshot } from "../types/dashboard";

type Props = {
  snapshot: L34Snapshot;
};

export function EvWaterfallPanel({ snapshot }: Props) {
  const series = [
    { name: "Gross EV", value: snapshot.evGross },
    { name: "Latency", value: snapshot.latencyImpact },
    { name: "Slippage λ", value: snapshot.slippageLambda },
    { name: "Taker Fees", value: snapshot.takerFees },
    { name: "Net EV", value: snapshot.evNetTimeAdj },
  ];

  return (
    <section className="rounded-xl border border-slate-700 bg-panPanel p-4">
      <h2 className="mb-3 text-lg font-semibold text-slate-100">淨期望值摩擦瀑布（L3/L4）</h2>
      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={series}>
            <CartesianGrid stroke="#334155" strokeDasharray="3 3" />
            <XAxis dataKey="name" stroke="#94a3b8" />
            <YAxis stroke="#94a3b8" />
            <ReferenceLine y={0} stroke="#64748b" />
            <Bar dataKey="value">
              {series.map((entry, index) => (
                <Cell key={`${entry.name}-${index}`} fill={entry.value >= 0 ? "#22c55e" : "#ef4444"} />
              ))}
              <LabelList dataKey="value" position="top" fill="#cbd5e1" formatter={(v: number) => v.toFixed(2)} />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </section>
  );
}

