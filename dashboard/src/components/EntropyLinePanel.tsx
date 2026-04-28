import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { L1Snapshot } from "../types/dashboard";

type Props = {
  latest: L1Snapshot;
  series: L1Snapshot[];
};

export function EntropyLinePanel({ latest, series }: Props) {
  const danger = latest.deltaHZ <= -4;
  return (
    <section className={`rounded-xl border p-4 ${danger ? "border-panDanger bg-red-950/40" : "border-slate-700 bg-panPanel"}`}>
      <h2 className="mb-3 text-lg font-semibold text-slate-100">即時香農熵與市場狀態（L1）</h2>
      <div className="mb-3 grid grid-cols-3 gap-2 text-sm">
        <div className="rounded-md bg-slate-900/80 p-2 text-slate-300">OFI: <span className="text-panNeon">{latest.ofi.toFixed(2)}</span></div>
        <div className="rounded-md bg-slate-900/80 p-2 text-slate-300">Kyle λ: <span className="text-panNeon">{latest.kyleLambda.toFixed(2)}</span></div>
        <div className={`rounded-md bg-slate-900/80 p-2 ${latest.latencyMs > 200 ? "text-panDanger" : "text-slate-300"}`}>
          延遲: <span>{latest.latencyMs.toFixed(0)}ms</span>
        </div>
      </div>
      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={series}>
            <CartesianGrid stroke="#334155" strokeDasharray="3 3" />
            <XAxis dataKey="ts" tickFormatter={(ts) => new Date(ts).toLocaleTimeString("zh-HK")} stroke="#94a3b8" />
            <YAxis stroke="#94a3b8" domain={[-6, 2]} />
            <Tooltip labelFormatter={(v) => new Date(v as number).toLocaleTimeString("zh-HK")} />
            <ReferenceLine y={-4} stroke="#ef4444" strokeDasharray="8 5" />
            <Line type="monotone" dataKey="deltaHZ" stroke="#22d3ee" strokeWidth={2} dot={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </section>
  );
}

