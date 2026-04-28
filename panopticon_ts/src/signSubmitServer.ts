import http from "node:http";
import { randomBytes } from "node:crypto";

export interface ProtectedPayload {
  side: "BUY" | "SELL";
  price: number;
  size: number;
  time_in_force: "FOK" | "IOC";
  expires_in_seconds: number;
  expected_avg_price: number;
  slippage_tolerance: number;
}

export interface SubmitRequestBody {
  idempotency_key: string;
  decision_id: string;
  market_id?: string | null;
  asset_id?: string | null;
  protected_payload: ProtectedPayload;
}

export interface SubmitResponseBody {
  request_id: string;
  accepted: boolean;
  clob_order_id: string | null;
  tx_hash: string | null;
  raw_error: string | null;
  dry_run: boolean;
}

const idempotencyCache = new Map<string, SubmitResponseBody>();

function isRecord(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null && !Array.isArray(x);
}

function isProtectedPayload(x: unknown): x is ProtectedPayload {
  if (!isRecord(x)) return false;
  const side = x.side;
  const tif = x.time_in_force;
  return (
    (side === "BUY" || side === "SELL") &&
    typeof x.price === "number" &&
    typeof x.size === "number" &&
    (tif === "FOK" || tif === "IOC") &&
    typeof x.expires_in_seconds === "number" &&
    typeof x.expected_avg_price === "number" &&
    typeof x.slippage_tolerance === "number"
  );
}

export function validateSubmitRequest(body: unknown): SubmitRequestBody | null {
  if (!isRecord(body)) return null;
  const idem = body.idempotency_key;
  const did = body.decision_id;
  const pp = body.protected_payload;
  if (typeof idem !== "string" || idem.length < 8 || idem.length > 128) return null;
  if (typeof did !== "string" || did.length < 8 || did.length > 128) return null;
  if (!isProtectedPayload(pp)) return null;
  if (pp.expires_in_seconds < 1 || pp.expires_in_seconds > 600) return null;
  if (pp.size < 0 || pp.slippage_tolerance < 0) return null;
  const extraKeys = Object.keys(body).filter((k) => !["idempotency_key", "decision_id", "market_id", "asset_id", "protected_payload"].includes(k));
  if (extraKeys.length) return null;
  return {
    idempotency_key: idem,
    decision_id: did,
    market_id: typeof body.market_id === "string" ? body.market_id : body.market_id === null ? null : undefined,
    asset_id: typeof body.asset_id === "string" ? body.asset_id : body.asset_id === null ? null : undefined,
    protected_payload: pp,
  };
}

function newRequestId(): string {
  return `req_${randomBytes(8).toString("hex")}`;
}

function fakeTxHash(): string {
  return `0x${randomBytes(32).toString("hex")}`;
}

export function buildSubmitResponse(body: SubmitRequestBody, requestId: string): SubmitResponseBody {
  const dry = ["1", "true", "yes"].includes((process.env.L4_DRY_RUN ?? "1").toLowerCase());
  if (dry) {
    return {
      request_id: requestId,
      accepted: true,
      clob_order_id: `dry_${randomBytes(6).toString("hex")}`,
      tx_hash: fakeTxHash(),
      raw_error: null,
      dry_run: true,
    };
  }
  return {
    request_id: requestId,
    accepted: false,
    clob_order_id: null,
    tx_hash: null,
    raw_error: "NO_SIGNING_KEY_OR_CLOB_PIPELINE_CONFIGURED",
    dry_run: false,
  };
}

function readJsonBody(req: http.IncomingMessage): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (c) => chunks.push(Buffer.isBuffer(c) ? c : Buffer.from(c)));
    req.on("end", () => {
      try {
        const raw = Buffer.concat(chunks).toString("utf8");
        if (!raw) return resolve(null);
        resolve(JSON.parse(raw) as unknown);
      } catch (e) {
        reject(e);
      }
    });
    req.on("error", reject);
  });
}

function sendJson(res: http.ServerResponse, status: number, obj: unknown): void {
  const body = JSON.stringify(obj);
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Content-Length": Buffer.byteLength(body) });
  res.end(body);
}

export function createSignSubmitServer(): http.Server {
  return http.createServer(async (req, res) => {
    try {
      if (req.method === "GET" && req.url === "/healthz") {
        sendJson(res, 200, { ok: true, service: "panopticon-sign-submit" });
        return;
      }
      if (req.method === "POST" && req.url === "/v1/orders:submit") {
        const requestId = newRequestId();
        let parsed: unknown;
        try {
          parsed = await readJsonBody(req);
        } catch {
          sendJson(res, 400, { request_id: requestId, error: "invalid_json" });
          return;
        }
        const body = validateSubmitRequest(parsed);
        if (!body) {
          sendJson(res, 400, { request_id: requestId, error: "schema_validation_failed" });
          return;
        }
        const cached = idempotencyCache.get(body.idempotency_key);
        if (cached) {
          sendJson(res, 200, { ...cached, idempotent_replay: true });
          return;
        }
        const out = buildSubmitResponse(body, requestId);
        idempotencyCache.set(body.idempotency_key, out);
        sendJson(res, out.accepted ? 200 : 503, out);
        return;
      }
      sendJson(res, 404, { error: "not_found" });
    } catch (_e) {
      sendJson(res, 500, { error: "internal_error" });
    }
  });
}

export function startSignSubmitServer(host = "127.0.0.1", port = 3751): http.Server {
  const srv = createSignSubmitServer();
  srv.listen(port, host, () => {
    console.error(`signSubmitServer listening http://${host}:${port}`);
  });
  return srv;
}

if (process.argv[1]?.endsWith("signSubmitServer.js")) {
  const port = Number(process.env.L4_SIGN_SUBMIT_PORT ?? "3751");
  const host = process.env.L4_SIGN_SUBMIT_HOST ?? "127.0.0.1";
  startSignSubmitServer(host, port);
}
