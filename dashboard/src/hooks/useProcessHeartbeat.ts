import { useEffect, useRef, useState } from "react";
import type { ProcessHeartbeat } from "../types/dashboard";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8001";
const POLL_INTERVAL_MS = 5000;

export function useProcessHeartbeat() {
  const [heartbeat, setHeartbeat] = useState<ProcessHeartbeat | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const fetchHeartbeat = async () => {
    abortRef.current?.abort();
    abortRef.current = new AbortController();

    try {
      const res = await fetch(`${API_BASE_URL}/api/versions`, {
        signal: abortRef.current.signal,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setHeartbeat({ ...data, timestamp: Date.now() });
      setLastUpdate(new Date());
      setError(null);
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        setError((e as Error).message);
      }
    }
  };

  useEffect(() => {
    fetchHeartbeat();
    intervalRef.current = setInterval(fetchHeartbeat, POLL_INTERVAL_MS);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
      abortRef.current?.abort();
    };
  }, []);

  return { heartbeat, error, lastUpdate };
}
