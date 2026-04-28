import { buildProtectedLimitPayload } from "./orderPayload.js";

function assert(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}

function runOrderPayloadTests(): void {
  const p = buildProtectedLimitPayload({
    side: "BUY",
    quotePrice: 0.49,
    orderSize: 100,
    slippageTolerance: 0.009,
    kyleLambda: 0.00001,
    ttlSeconds: 20,
  });

  assert(p.time_in_force === "FOK", "time_in_force must be FOK");
  assert(p.expires_in_seconds <= 10, "ttl must be capped at 10s");
  assert(p.price >= 0.49, "price must be protected limit above quote");
  console.log("orderPayload tests passed");
}

if (process.argv[1]?.endsWith("orderPayloadTest.js")) {
  runOrderPayloadTests();
}

