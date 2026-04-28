import { buildEvent, type EventEnvelope, type L4Payload } from "./contracts.js";
import { buildProtectedLimitPayload } from "./orderPayload.js";

export interface PaperOrderInput {
  decisionId: string;
  action: "BUY" | "SELL" | "HOLD";
  quotePrice: number;
  size: number;
  bestBid: number;
  bestAsk: number;
  latencyMs: number;
  impactPct: number;
  kyleLambda: number;
  slippageTolerance: number;
}

export function executePaperOrder(input: PaperOrderInput): EventEnvelope<L4Payload> {
  if (input.latencyMs > 200) {
    return buildEvent("L4", "order_rejected", "execution_layer_ts", "v0.1.0:execution", {
      decision_id: input.decisionId,
      accepted: false,
      reason: "NETWORK_LATENCY_GUARD_TRIGGERED",
      impact_pct: input.impactPct,
    });
  }
  if (input.impactPct > 0.02) {
    return buildEvent("L4", "order_rejected", "execution_layer_ts", "v0.1.0:execution", {
      decision_id: input.decisionId,
      accepted: false,
      reason: "IMPACT_GUARD_TRIGGERED",
      impact_pct: input.impactPct,
    });
  }
  if (input.action === "HOLD") {
    return buildEvent("L4", "order_skipped", "execution_layer_ts", "v0.1.0:execution", {
      decision_id: input.decisionId,
      accepted: false,
      reason: "NO_TRADE_SIGNAL",
      impact_pct: input.impactPct,
    });
  }

  const payload = buildProtectedLimitPayload({
    side: input.action,
    quotePrice: input.quotePrice,
    orderSize: input.size,
    slippageTolerance: input.slippageTolerance,
    kyleLambda: input.kyleLambda,
    ttlSeconds: 10,
  });

  const payloadImpact = payload.expected_avg_price - input.quotePrice;
  if (payloadImpact > input.slippageTolerance || input.impactPct > 0.02) {
    return buildEvent("L4", "order_rejected", "execution_layer_ts", "v0.2.0:execution", {
      decision_id: input.decisionId,
      accepted: false,
      reason: "PROTECTED_PAYLOAD_IMPACT_EXCEEDED",
      impact_pct: input.impactPct,
    });
  }

  const mid = (input.bestBid + input.bestAsk) / 2;
  const fillPrice = input.action === "BUY" ? Math.min(payload.price, input.bestAsk) : Math.max(payload.price, input.bestBid);
  const accepted = input.action === "BUY" ? payload.price >= input.bestAsk : payload.price <= input.bestBid;

  return buildEvent("L4", accepted ? "order_filled" : "order_live", "execution_layer_ts", "v0.2.0:execution", {
    decision_id: input.decisionId,
    accepted,
    reason: accepted ? "FOK_LIMIT_FILLED" : "FOK_NOT_FILLED",
    simulated_fill_price: accepted ? fillPrice : mid,
    simulated_fill_size: input.size,
    impact_pct: input.impactPct,
  });
}

if (process.argv[1]?.endsWith("execution.js")) {
  const event = executePaperOrder({
    decisionId: "demo-decision-1",
    action: "BUY",
    quotePrice: 0.49,
    size: 40,
    bestBid: 0.48,
    bestAsk: 0.49,
    latencyMs: 110,
    impactPct: 0.013,
    kyleLambda: 0.000005,
    slippageTolerance: 0.009,
  });
  console.log(JSON.stringify(event, null, 2));
}
