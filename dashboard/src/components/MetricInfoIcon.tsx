/**
 * D180: Inline “i” tooltip for RVF metric labels — hover/focus, ESC to dismiss.
 */
import { useEffect, useId, useRef, useState } from "react";
import type { MetricDefinition } from "../data/rvfMetricDefinitions";

export function MetricInfoIcon({ definition }: { definition?: MetricDefinition }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLSpanElement>(null);
  const uid = useId();

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  if (!definition) return null;

  return (
    <span ref={wrapRef} className="relative inline-flex items-center align-middle">
      <button
        type="button"
        aria-label={`${definition.label} 說明`}
        aria-expanded={open}
        aria-controls={uid}
        className="inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full border border-slate-500 text-[9px] font-serif leading-none text-slate-400 hover:border-slate-400 hover:text-slate-200 focus:outline-none focus:ring-1 focus:ring-slate-400"
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
      >
        i
      </button>
      {open && (
        <span
          id={uid}
          role="tooltip"
          className="absolute left-5 top-0 z-[60] w-[min(100vw-2rem,20rem)] rounded-lg border border-slate-600 bg-slate-900 p-2 text-[11px] leading-snug text-slate-200 shadow-xl"
        >
          <span className="block font-semibold text-slate-100">{definition.label}</span>
          <span className="mt-1 block text-slate-300">
            <span className="font-medium text-slate-400">含義：</span>
            {definition.meaning}
          </span>
          <span className="mt-1 block text-slate-300">
            <span className="font-medium text-slate-400">預期：</span>
            {definition.expected}
          </span>
          <span className="mt-1 block text-slate-300">
            <span className="font-medium text-slate-400">控制：</span>
            {definition.controls}
          </span>
        </span>
      )}
    </span>
  );
}
