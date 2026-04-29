import { useProcessHeartbeat } from "../hooks/useProcessHeartbeat";

type ProcessInfoLike = {
  pid: number;
  version: string;
  expected: string;
  version_match: boolean;
  start_time: string;
  last_heartbeat_ts?: string;
  status: string;
};

function formatTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  try {
    return new Date(iso).toLocaleTimeString("zh-HK", { hour12: false });
  } catch {
    return iso;
  }
}

function formatProcessName(name: string): string {
  return name
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function isProcessInfoLike(value: unknown): value is ProcessInfoLike {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.pid === "number" &&
    typeof candidate.version === "string" &&
    typeof candidate.expected === "string" &&
    typeof candidate.version_match === "boolean" &&
    typeof candidate.start_time === "string" &&
    typeof candidate.status === "string"
  );
}

function ProcessRow({
  name,
  info,
}: {
  name: string;
  info: ProcessInfoLike | undefined;
}) {
  if (!info) {
    return (
      <tr className="border-b border-slate-700/50">
        <td className="px-3 py-2 font-medium text-slate-200">{name}</td>
        <td className="px-3 py-2 text-slate-500" colSpan={6}>
          未連線
        </td>
      </tr>
    );
  }

  return (
    <tr className="border-b border-slate-700/50 hover:bg-slate-800/40">
      <td className="px-3 py-2 font-medium text-slate-200">{name}</td>
      <td className="px-3 py-2 font-mono text-xs text-slate-400">{info.pid}</td>
      <td className="px-3 py-2 font-mono text-xs text-slate-300">{info.version}</td>
      <td className="px-3 py-2">
        <span
          className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ${
            info.version_match
              ? "bg-emerald-900/50 text-emerald-300"
              : "bg-amber-900/50 text-amber-300"
          }`}
        >
          {info.version_match ? "OK" : "Mismatch"}
        </span>
      </td>
      <td className="px-3 py-2">
        <span
          className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ${
            info.status === "running"
              ? "bg-emerald-900/50 text-emerald-300"
              : "bg-red-900/50 text-red-300"
          }`}
        >
          {info.status}
        </span>
      </td>
      <td className="px-3 py-2 font-mono text-xs text-slate-400">{formatTime(info.start_time)}</td>
      <td className="px-3 py-2 font-mono text-xs text-slate-400">{formatTime(info.last_heartbeat_ts)}</td>
    </tr>
  );
}

export function ProcessHeartbeatPanel() {
  const { heartbeat, error, lastUpdate } = useProcessHeartbeat();
  const processEntries = Object.entries(heartbeat ?? {})
    .filter(([key]) => key !== "timestamp")
    .sort(([a], [b]) => a.localeCompare(b));

  return (
    <div className="rounded-xl border border-slate-700 bg-panPanel p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-lg font-semibold text-slate-100">程序心跳監控</h2>
        <div className="flex items-center gap-2 text-xs">
          {error ? (
            <span className="text-red-400">錯誤: {error}</span>
          ) : (
            <>
              <span className="relative inline-flex h-2 w-2">
                <span className={`absolute inline-flex h-full w-full rounded-full bg-emerald-400 ${heartbeat ? "animate-pulse" : "opacity-30"}`} />
                <span className={`relative inline-flex h-2 w-2 rounded-full ${heartbeat ? "bg-emerald-500" : "bg-slate-500"}`} />
              </span>
              <span className="text-slate-400">
                {lastUpdate
                  ? `更新: ${lastUpdate.toLocaleTimeString("zh-HK", { hour12: false })}`
                  : "更新中..."}
              </span>
              <span className="text-slate-500">(每 5 秒)</span>
            </>
          )}
        </div>
      </div>

      <div className="overflow-hidden rounded-lg border border-slate-700">
        <table className="w-full text-sm">
          <thead className="bg-slate-800/70">
            <tr>
              <th className="px-3 py-2 text-left font-medium text-slate-300">程序</th>
              <th className="px-3 py-2 text-left font-medium text-slate-300">PID</th>
              <th className="px-3 py-2 text-left font-medium text-slate-300">版本</th>
              <th className="px-3 py-2 text-left font-medium text-slate-300">版本匹配</th>
              <th className="px-3 py-2 text-left font-medium text-slate-300">狀態</th>
              <th className="px-3 py-2 text-left font-medium text-slate-300">啟動時間</th>
              <th className="px-3 py-2 text-left font-medium text-slate-300">最後心跳</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-700/50 bg-slate-900/30">
            {processEntries.length === 0 ? (
              <tr className="border-b border-slate-700/50">
                <td className="px-3 py-2 text-slate-500" colSpan={7}>
                  尚未收到程序資料
                </td>
              </tr>
            ) : (
              processEntries.map(([name, info]) =>
                isProcessInfoLike(info) ? (
                  <ProcessRow key={name} name={formatProcessName(name)} info={info} />
                ) : (
                  <tr key={name} className="border-b border-slate-700/50 hover:bg-slate-800/40">
                    <td className="px-3 py-2 font-medium text-slate-200">{formatProcessName(name)}</td>
                    <td className="px-3 py-2 text-slate-500">-</td>
                    <td className="px-3 py-2 text-slate-500">-</td>
                    <td className="px-3 py-2 text-slate-500">-</td>
                    <td className="px-3 py-2">
                      <span className="inline-flex items-center rounded bg-slate-700/60 px-1.5 py-0.5 text-xs font-medium text-slate-200">
                        {typeof info === "object" && info && "status" in info
                          ? String((info as { status?: unknown }).status ?? "unknown")
                          : "unknown"}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-slate-500">-</td>
                    <td className="px-3 py-2 text-slate-500">-</td>
                  </tr>
                ),
              )
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
