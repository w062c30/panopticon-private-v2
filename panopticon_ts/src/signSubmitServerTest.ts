import http from "node:http";
import { validateSubmitRequest, createSignSubmitServer, buildSubmitResponse } from "./signSubmitServer.js";

function assert(condition: boolean, message: string): void {
  if (!condition) throw new Error(message);
}

function runSignSubmitServerTests(): void {
  assert(validateSubmitRequest(null) === null, "null invalid");
  assert(validateSubmitRequest({}) === null, "empty invalid");
  const ok = validateSubmitRequest({
    idempotency_key: "idem_key_12345678",
    decision_id: "decision_id_abcdef",
    protected_payload: {
      side: "BUY",
      price: 0.5,
      size: 10,
      time_in_force: "FOK",
      expires_in_seconds: 10,
      expected_avg_price: 0.51,
      slippage_tolerance: 0.01,
    },
  });
  assert(ok !== null, "valid body");

  process.env.L4_DRY_RUN = "1";
  const r = buildSubmitResponse(ok!, "req_test");
  assert(r.accepted === true && r.tx_hash?.startsWith("0x") === true, "dry run accepts");

  const srv = createSignSubmitServer();
  srv.listen(0, "127.0.0.1", () => {
    const addr = srv.address();
    if (!addr || typeof addr === "string") throw new Error("bad address");
    const payload = JSON.stringify({
      idempotency_key: "idem_integration_12345678",
      decision_id: "decision_integration_abcd",
      protected_payload: {
        side: "BUY",
        price: 0.5,
        size: 10,
        time_in_force: "FOK",
        expires_in_seconds: 10,
        expected_avg_price: 0.51,
        slippage_tolerance: 0.01,
      },
    });
    const req = http.request(
      {
        host: "127.0.0.1",
        port: addr.port,
        path: "/v1/orders:submit",
        method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) },
      },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          const j = JSON.parse(data) as { accepted: boolean; idempotent_replay?: boolean };
          assert(j.accepted === true, "http accepted");
          const req2 = http.request(
            {
              host: "127.0.0.1",
              port: addr.port,
              path: "/v1/orders:submit",
              method: "POST",
              headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) },
            },
            (res2) => {
              let data2 = "";
              res2.on("data", (c) => (data2 += c));
              res2.on("end", () => {
                const j2 = JSON.parse(data2) as { idempotent_replay?: boolean };
                assert(j2.idempotent_replay === true, "idempotent replay");
                srv.close();
                console.log("signSubmitServer tests passed");
              });
            },
          );
          req2.on("error", (e) => {
            throw e;
          });
          req2.end(payload);
        });
      },
    );
    req.on("error", (e) => {
      throw e;
    });
    req.end(payload);
  });
}

if (process.argv[1]?.endsWith("signSubmitServerTest.js")) {
  runSignSubmitServerTests();
}
