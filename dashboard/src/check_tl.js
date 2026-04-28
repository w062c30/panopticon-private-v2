// Quick browser DevTools check for Trade List data
// Run this in browser console at http://localhost:5173

(async () => {
  const base = "http://localhost:8001";
  console.log("=== Checking Trade List data flow ===");

  // 1. Check readiness (fast)
  const ready = await fetch(`${base}/api/system_health/readiness`).then(r => r.json());
  console.log("Readiness:", ready);

  // 2. Check recommendations (slow ~25s)
  const start = Date.now();
  const recs = await fetch(`${base}/api/recommendations?limit=20`).then(r => r.json());
  console.log(`Recommendations took ${Date.now()-start}ms:`, recs.trades?.length, "trades");
  if (recs.trades?.length > 0) {
    console.log("Sample:", JSON.stringify(recs.trades[0], null, 2));
  }

  // 3. Simulate what webSocketLiveAdapter does
  const raw = recs.trades; // array
  console.log("raw is array:", Array.isArray(raw));
  console.log("raw.length:", raw?.length);
})();
