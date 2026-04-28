import { PolarAngleAxis, PolarGrid, Radar, RadarChart, ResponsiveContainer } from "recharts";
import type { L2Snapshot } from "../types/dashboard";

type Props = {
  snapshot: L2Snapshot;
};

export function FingerprintRadarPanel({ snapshot }: Props) {
  const data = [
    { name: "IDI", value: snapshot.idi },
    { name: "Burstiness", value: snapshot.burstiness },
    { name: "Taker Ratio", value: snapshot.takerRatio },
    { name: "Smurf Sync", value: snapshot.smurfSync },
  ];

  return (
    <section className="rounded-xl border border-slate-700 bg-panPanel p-4">
      <h2 className="mb-2 text-lg font-semibold text-slate-100">Insider 行為指紋辨識（L2）</h2>
      <div className={`mb-2 text-center text-2xl font-bold ${snapshot.entityTrustScore > 85 ? "text-panGood" : "text-slate-200"}`}>
        信任分數 {snapshot.entityTrustScore.toFixed(1)}%
      </div>
      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <RadarChart data={data}>
            <PolarGrid stroke="#334155" />
            <PolarAngleAxis dataKey="name" stroke="#cbd5e1" />
            <Radar dataKey="value" stroke="#38bdf8" fill="#22d3ee" fillOpacity={0.45} />
          </RadarChart>
        </ResponsiveContainer>
      </div>
    </section>
  );
}

