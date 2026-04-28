import { executePaperOrder } from "./execution.js";
import { buildL1Signal } from "./sensor.js";

function runPaperExecutionDemo(): void {
  const l1 = buildL1Signal();
  const impact = l1.payload.lambda_impact;
  const latency = l1.payload.latency_ms;

  const result = executePaperOrder({
    decisionId: "l3-decision-demo",
    action: "BUY",
    quotePrice: l1.payload.best_ask,
    size: 25,
    bestBid: l1.payload.best_bid,
    bestAsk: l1.payload.best_ask,
    latencyMs: latency,
    impactPct: impact,
    kyleLambda: l1.payload.lambda_impact,
    slippageTolerance: 0.009,
  });

  console.log(
    JSON.stringify(
      {
        l1Signal: l1,
        execution: result,
      },
      null,
      2,
    ),
  );
}

if (process.argv[1]?.endsWith("paperMain.js")) {
  runPaperExecutionDemo();
}
