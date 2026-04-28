import { buildEvent, type EventEnvelope, type L1Payload } from "./contracts.js";

function shannonEntropy(probabilities: number[]): number {
  return probabilities.reduce((acc, p) => {
    if (p <= 0) return acc;
    return acc - p * Math.log2(p);
  }, 0);
}

function computeOfi(currentBidSize: number, currentAskSize: number): number {
  const denom = currentBidSize + currentAskSize;
  if (denom === 0) return 0;
  return (currentBidSize - currentAskSize) / denom;
}

function computeLambdaImpact(spread: number, depth: number): number {
  if (depth <= 0) return 0;
  return spread / depth;
}

export function buildL1Signal(): EventEnvelope<L1Payload> {
  const buyProb = 0.62;
  const sellProb = 0.38;
  const h = shannonEntropy([buyProb, sellProb]);
  const baselineH = 1.0;
  const deltaH = h - baselineH;

  const bestBid = 0.48;
  const bestAsk = 0.50;
  const spread = bestAsk - bestBid;
  const depth = 3500;
  const ofi = computeOfi(2300, 1200);
  const lambdaImpact = computeLambdaImpact(spread, depth);

  return buildEvent("L1", "micro_signal", "sensor_layer_ts", "v0.1.0:sensor", {
    delta_h: deltaH,
    ofi,
    lambda_impact: lambdaImpact,
    best_bid: bestBid,
    best_ask: bestAsk,
    latency_ms: 90,
  });
}

export function validateL1Payload(payload: L1Payload): string[] {
  const errors: string[] = [];
  if (payload.best_bid < 0 || payload.best_bid > 1) errors.push("best_bid out of range");
  if (payload.best_ask < 0 || payload.best_ask > 1) errors.push("best_ask out of range");
  if (payload.best_bid > payload.best_ask) errors.push("crossed book detected");
  if (payload.latency_ms < 0) errors.push("latency_ms must be >= 0");
  if (Math.abs(payload.ofi) > 1) errors.push("ofi must be in [-1, 1]");
  if (payload.lambda_impact < 0) errors.push("lambda_impact must be >= 0");
  return errors;
}

if (process.argv[1]?.endsWith("sensor.js")) {
  const e = buildL1Signal();
  const errors = validateL1Payload(e.payload);
  if (errors.length > 0) {
    console.error(JSON.stringify({ validation: "failed", errors }, null, 2));
    process.exit(1);
  }
  console.log(JSON.stringify(e, null, 2));
}
