import { useEffect, useRef, useState } from "react";
import type { WalletGraph } from "../types/dashboard";

declare global {
  interface Window {
    vis: typeof import("vis-network");
  }
}

interface Props {
  apiBaseUrl: string;
}

const VIS_JS_CDN = "https://unpkg.com/vis-network/standalone/umd/vis-network.min.js";

function loadVisJS(): Promise<void> {
  if (window.vis) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = VIS_JS_CDN;
    script.onload = () => resolve();
    script.onerror = reject;
    document.head.appendChild(script);
  });
}

export function WalletNetworkPanel({ apiBaseUrl }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const networkRef = useRef<import("vis-network").Network | null>(null);
  const [graph, setGraph] = useState<WalletGraph | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pollInterval, setPollInterval] = useState(30_000);

  useEffect(() => {
    let aborted = false;
    let timer: ReturnType<typeof setTimeout>;

    const fetchGraph = async () => {
      if (aborted) return;
      setLoading(true);
      setError(null);
      try {
        const controller = new AbortController();
        const t = setTimeout(() => controller.abort(), 8_000);
        const resp = await fetch(`${apiBaseUrl}/api/wallet/graph?wallet_limit=80`, {
          signal: controller.signal,
        });
        clearTimeout(t);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data: WalletGraph = await resp.json();
        if (aborted) return;
        setGraph(data);
      } catch (e) {
        if (!aborted) setError(String(e));
      } finally {
        if (!aborted) setLoading(false);
      }
    };

    fetchGraph();
    timer = setInterval(fetchGraph, pollInterval);
    return () => {
      aborted = true;
      clearInterval(timer);
    };
  }, [apiBaseUrl, pollInterval]);

  useEffect(() => {
    if (!graph || !containerRef.current) return;

    let mounted = true;
    loadVisJS()
      .then(() => {
        if (!mounted || !containerRef.current || !window.vis) return;

        // Clean up previous network
        if (networkRef.current) {
          networkRef.current.destroy();
          networkRef.current = null;
        }

        const { Network, DataSet } = window.vis;

        const nodes = new DataSet(
          graph.nodes.map((n) => ({
            id: n.id,
            label: n.label,
            title: `Address: ${n.fullAddress}\nEntity: ${n.entityId}\nPnL: $${n.pnl.toFixed(2)}\nWin Rate: ${(n.winRate * 100).toFixed(1)}%\nSource: ${n.source}\nQuality: ${n.quality}`,
            color: { background: n.color, border: n.color, highlight: { background: "#fbbf24", border: "#fbbf24" } },
            font: { color: "#e2e8f0", size: 11 },
            size: Math.max(8, Math.min(n.value * 0.3, 30)),
          }))
        );

        const edges = new DataSet(
          graph.edges.map((e) => ({
            from: e.from,
            to: e.to,
            title: e.title,
            label: e.relation === "SAME_ENTITY" ? "SAME ENTITY" : `MARKETS:${e.weight}`,
            color: e.relation === "SAME_ENTITY" ? { color: "#f472b6", highlight: "#f472b6" } : { color: "#64748b", highlight: "#94a3b8" },
            width: e.relation === "SAME_ENTITY" ? 3 : Math.min(e.weight * 0.5, 4),
            dashes: e.relation !== "SAME_ENTITY",
          }))
        );

        const options = {
          nodes: {
            shape: "dot",
            borderWidth: 1.5,
            shadow: true,
          },
          edges: {
            smooth: { type: "continuous" as const },
          },
          physics: {
            enabled: true,
            forceAtlas2Based: {
              gravitationalConstant: -80,
              centralGravity: 0.01,
              springLength: 120,
              springConstant: 0.08,
            },
            stabilization: { iterations: 100 },
            maxVelocity: 50,
            minVelocity: 0.75,
            solver: "forceAtlas2Based" as const,
          },
          interaction: {
            hover: true,
            tooltipDelay: 200,
            hideEdgesOnDrag: true,
            navigationButtons: true,
            keyboard: true,
          },
        };

        const network = new Network(containerRef.current, { nodes, edges }, options);

        network.on("stabilizationIterationsDone", () => {
          network.setOptions({ physics: { enabled: false } });
        });

        networkRef.current = network;
      })
      .catch((e) => {
        console.error("vis.js load failed", e);
      });

    return () => {
      mounted = false;
      if (networkRef.current) {
        networkRef.current.destroy();
        networkRef.current = null;
      }
    };
  }, [graph]);

  return (
    <section className="rounded-xl border border-slate-700 bg-panPanel p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-lg font-semibold text-slate-100">Wallet Network</h2>
        <div className="flex items-center gap-2">
          {loading && <span className="text-xs text-slate-400">Loading...</span>}
          {error && <span className="text-xs text-red-400">{error}</span>}
          <span className="text-xs text-slate-500">
            {graph ? `${graph.nodes.length} wallets · ${graph.edges.length} edges` : "No data"}
          </span>
          <button
            className="rounded bg-slate-800 px-2 py-1 text-xs hover:bg-slate-700"
            onClick={() => {
              if (networkRef.current) {
                networkRef.current.fit({ animation: true });
              }
            }}
          >
            Fit
          </button>
        </div>
      </div>

      <div
        ref={containerRef}
        className="h-80 rounded border border-slate-800 bg-slate-950/60"
        style={{ width: "100%", height: "320px" }}
      />

      <div className="mt-2 flex flex-wrap gap-3 text-xs text-slate-400">
        <span className="flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-[#22d3ee]" /> Tier1 (Smart Money)
        </span>
        <span className="flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-[#a78bfa]" /> Tier2
        </span>
        <span className="flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-[#34d399]" /> Whale
        </span>
        <span className="flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-[#f87171]" /> Degen
        </span>
        <span className="flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-slate-500" /> Unknown
        </span>
        <span className="ml-2 flex items-center gap-1">
          <span className="h-0.5 w-4 bg-pink-400" /> Same Entity
        </span>
        <span className="flex items-center gap-1">
          <span className="h-px w-4 bg-slate-500" style={{ borderTop: "1px dashed" }} /> Shared Market
        </span>
      </div>
    </section>
  );
}
