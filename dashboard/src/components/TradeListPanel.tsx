import type { TradeListItem } from "../types/dashboard";

type Props = {
  trades: TradeListItem[];
};

function calcTotalEv(trades: TradeListItem[]): number {
  return trades.reduce((sum, t) => sum + (t.estimatedEvUsd ?? 0), 0);
}

function fmtTs(ts: string): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString("en-US", {
      month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
  } catch {
    return ts;
  }
}

export function TradeListPanel({ trades }: Props) {
  const totalEv = calcTotalEv(trades);
  const paperTrades = trades.filter((t) => t.source === "paper");
  const liveTrades = trades.filter((t) => t.source === "live");
  const settledTrades = trades.filter((t) => t.source === "db_settlement");

  // Sort by updatedAt descending (latest first)
  const sortedTrades = [...trades].sort((a, b) => {
    const timeA = new Date(a.updatedAt || a.closedAt || 0).getTime();
    const timeB = new Date(b.updatedAt || b.closedAt || 0).getTime();
    return timeB - timeA;
  });

  return (
    <section className="rounded-xl border border-slate-700 bg-panPanel p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold text-slate-100">Trade list</h2>
        <div className="flex flex-wrap gap-3 text-xs">
          <span className="rounded bg-amber-900/40 px-2 py-1 text-amber-300">
            Paper: {paperTrades.length}
          </span>
          <span className="rounded bg-emerald-900/40 px-2 py-1 text-emerald-300">
            Live: {liveTrades.length}
          </span>
          <span className="rounded bg-slate-700/40 px-2 py-1 text-slate-300">
            Settled: {settledTrades.length}
          </span>
          <span className={`rounded px-2 py-1 font-medium ${totalEv >= 0 ? "bg-emerald-900/40 text-emerald-300" : "bg-red-900/40 text-red-300"}`}>
            Total EV: {totalEv >= 0 ? "+" : ""}{totalEv.toFixed(2)} USD
          </span>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[900px] text-left text-xs text-slate-300">
          <thead className="sticky top-0 bg-slate-900">
            <tr>
              <th className="p-2">Event</th>
              <th className="p-2">Open Time</th>
              <th className="p-2">Source</th>
              <th className="p-2">Status</th>
              <th className="p-2">方向</th>
              <th className="p-2">置信度</th>
              <th className="p-2">開倉原因</th>
              <th className="p-2">開倉價</th>
              <th className="p-2">Mark</th>
              <th className="p-2">大小</th>
              <th className="p-2">EV</th>
              <th className="p-2">Realized</th>
              <th className="p-2">Unrealized</th>
              <th className="p-2">平倉時間</th>
              <th className="p-2">平倉條件</th>
            </tr>
          </thead>
          <tbody>
            {sortedTrades.map((trade) => {
              const displayName = trade.eventName.includes('...')
                ? `Market: ${trade.marketId}`
                : trade.eventName;
              const tooltip = trade.eventName.includes('...')
                ? `Full ID: ${trade.marketId}`
                : trade.eventName;

              return (
              <tr key={trade.tradeId} className="border-b border-slate-800">
                <td className="p-2">
                  <span title={tooltip} className={trade.eventUrl ? "cursor-pointer" : ""}>
                    {trade.eventUrl ? (
                      <a
                        href={trade.eventUrl}
                        target="_blank"
                        rel="noreferrer"
                        className="text-cyan-300 underline decoration-dotted hover:text-cyan-200"
                        title={`${displayName}\n${trade.linkType ? `Type: ${trade.linkType}` : ''}\n${trade.linkSource ? `Source: ${trade.linkSource}` : ''}`}
                      >
                        {displayName}
                      </a>
                    ) : (
                      <span className="text-slate-400 cursor-help" title={`Full ID: ${trade.marketId}`}>
                        {displayName}
                      </span>
                    )}
                  </span>
                  {(trade.linkType || trade.linkSource) && (
                    <div className="mt-1 text-[10px] text-slate-500">
                      {trade.linkType ?? "n/a"} · {trade.linkSource ?? "n/a"}
                    </div>
                  )}
                </td>
                <td className="p-2 whitespace-nowrap text-slate-400">{fmtTs(trade.openedAt)}</td>
                <td className="px-2 py-1">
                  <span
                    className={`text-[10px] px-1.5 py-0.5 rounded font-mono uppercase tracking-wide border ${
                      trade.source === "paper"
                        ? "bg-amber-900/40 text-amber-300 border-amber-600"
                        : trade.source === "db_settlement"
                          ? "bg-slate-700/40 text-slate-300 border-slate-500"
                          : "bg-emerald-900/40 text-emerald-300 border-emerald-600"
                    }`}
                  >
                    {trade.source ?? "live"}
                  </span>
                </td>
                <td className="p-2">
                  <span
                    className={`rounded px-2 py-0.5 ${
                      trade.status === "open" ? "bg-blue-900/60 text-blue-300" : "bg-slate-700 text-slate-200"
                    }`}
                  >
                    {trade.status}
                  </span>
                </td>
                <td className="p-2">{trade.direction}</td>
                <td className="p-2">{((trade.confidence ?? 0) * 100).toFixed(1)}%</td>
                <td className="p-2">{trade.openReason}</td>
                <td className="p-2">{trade.entryPrice?.toFixed(3) ?? "N/A"}</td>
                <td className="p-2">{trade.markPrice === null ? "N/A" : trade.markPrice.toFixed(3)}</td>
                <td className="p-2">${(trade.positionSizeUsd ?? 0).toFixed(2)}</td>
                <td className={`p-2 font-medium ${(trade.estimatedEvUsd ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                  {(trade.estimatedEvUsd ?? 0) >= 0 ? "+" : ""}
                  {(trade.estimatedEvUsd ?? 0).toFixed(2)}
                </td>
                <td className={`p-2 font-semibold ${(trade.realizedPnlUsd ?? 0) >= 0 ? "text-panGood" : "text-panDanger"}`}>
                  {(trade.realizedPnlUsd ?? 0) >= 0 ? "+" : ""}
                  {(trade.realizedPnlUsd ?? 0).toFixed(2)}
                </td>
                <td className={`p-2 font-semibold ${(trade.unrealizedPnlUsd ?? 0) >= 0 ? "text-panGood" : "text-panDanger"}`}>
                  {(trade.unrealizedPnlUsd ?? 0) >= 0 ? "+" : ""}
                  {(trade.unrealizedPnlUsd ?? 0).toFixed(2)}
                </td>
                <td className="p-2 whitespace-nowrap text-slate-400">{fmtTs(trade.closedAt)}</td>
                <td className="p-2">{trade.closeCondition}</td>
              </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
