export type Layer = "L1" | "L2" | "L3" | "L4" | "L5";

export interface EventEnvelope<TPayload> {
  event_id: string;
  layer: Layer;
  event_type: string;
  event_ts: string;
  ingest_ts_utc: string;
  source: string;
  source_event_id?: string | null;
  version_tag: string;
  market_id?: string | null;
  asset_id?: string | null;
  payload: TPayload;
}

export interface L1Payload {
  delta_h: number;
  ofi: number;
  lambda_impact: number;
  best_bid: number;
  best_ask: number;
  latency_ms: number;
}

export interface L4Payload {
  decision_id: string;
  accepted: boolean;
  reason: string;
  simulated_fill_price?: number;
  simulated_fill_size?: number;
  impact_pct?: number;
}

export function buildEvent<TPayload>(
  layer: Layer,
  eventType: string,
  source: string,
  versionTag: string,
  payload: TPayload,
): EventEnvelope<TPayload> {
  const ts = new Date().toISOString();
  const randomId = `${Date.now()}-${Math.floor(Math.random() * 1_000_000)}`;
  return {
    event_id: randomId,
    layer,
    event_type: eventType,
    event_ts: ts,
    ingest_ts_utc: ts,
    source,
    version_tag: versionTag,
    payload,
  };
}
