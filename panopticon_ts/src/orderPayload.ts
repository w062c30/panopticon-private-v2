export type TimeInForce = "FOK" | "IOC";

export interface BuildPayloadInput {
  side: "BUY" | "SELL";
  quotePrice: number;
  orderSize: number;
  slippageTolerance: number;
  kyleLambda: number;
  ttlSeconds?: number;
}

export interface ProtectedOrderPayload {
  side: "BUY" | "SELL";
  price: number;
  size: number;
  time_in_force: TimeInForce;
  expires_in_seconds: number;
  expected_avg_price: number;
  slippage_tolerance: number;
}

export function buildProtectedLimitPayload(input: BuildPayloadInput): ProtectedOrderPayload {
  const ttl = Math.min(10, Math.max(1, input.ttlSeconds ?? 10));
  const expectedSlippage = input.orderSize * input.kyleLambda;
  const price = input.quotePrice + Math.max(0, input.slippageTolerance);
  const expectedAvgPrice = input.quotePrice + expectedSlippage;

  return {
    side: input.side,
    price,
    size: input.orderSize,
    time_in_force: "FOK",
    expires_in_seconds: ttl,
    expected_avg_price: expectedAvgPrice,
    slippage_tolerance: input.slippageTolerance,
  };
}

