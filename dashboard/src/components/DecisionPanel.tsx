import type { ExecutionState, L34Snapshot } from "../types/dashboard";

type Props = {
  snapshot: L34Snapshot;
  execution: ExecutionState;
};

export function DecisionPanel({ snapshot, execution }: Props) {
  return (
    <section className="rounded-xl border border-slate-700 bg-panPanel2 p-4">
      <h2 className="mb-3 text-lg font-semibold text-slate-100">貝氏決策與凱利倉位</h2>
      <div className="mb-4 grid grid-cols-3 gap-2 text-sm">
        <div className="rounded-md bg-slate-900 p-2 text-slate-300">Prior p: <span className="text-panNeon">{(snapshot.pPrior * 100).toFixed(1)}%</span></div>
        <div className="rounded-md bg-slate-900 p-2 text-slate-300">LR: <span className="text-panNeon">{snapshot.lr.toFixed(2)}</span></div>
        <div className="rounded-md bg-slate-900 p-2 text-slate-300">Posterior p: <span className="text-panNeon">{(snapshot.pPosterior * 100).toFixed(1)}%</span></div>
      </div>
      <p className="text-2xl font-bold text-slate-100">
        建議倉位 {Math.round(snapshot.kellyFraction * 1000) / 10}% / ${snapshot.kellyUsd.toFixed(2)}
      </p>
      <button
        disabled={!execution.canExecute}
        className={`mt-3 w-full rounded-md px-4 py-2 text-sm font-bold ${
          execution.canExecute ? "bg-panGood text-slate-950" : "cursor-not-allowed bg-slate-700 text-slate-300"
        }`}
      >
        {execution.canExecute ? "EXECUTE" : `禁用：${execution.rejectReason ?? "風控拒絕"}`}
      </button>
    </section>
  );
}

