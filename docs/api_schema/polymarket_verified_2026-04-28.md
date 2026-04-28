# Polymarket API — Verified Schemas
**Last verified**: 2026-04-28 (D68/D69 live testing)
**Method**: live curl + Phase 0 monitor (458 real trades, 10 min)

---

## ⚠️ CRITICAL RULE (RULE-API-1)
Before writing `raw.get("FIELD")` for ANY endpoint:
1. Run `curl <endpoint> | python3 -m json.tool` first
2. List every actual key in the response
3. Update this file with verified fields
4. Only THEN write code

**DO NOT assume field names from documentation or naming convention.**

---

## Endpoint: GET https://data-api.polymarket.com/trades

**Auth**: None required
**Rate limit**: Not documented; 4s poll confirmed safe
**Pagination**: `limit` (max 100) + `offset`

### Query Parameters (confirmed working)
| Parameter    | Type    | Notes |
|-------------|---------|-------|
| `market`    | string  | conditionId — filter by market |
| `user`      | string  | 0x wallet — filter by wallet |
| `limit`     | int     | max 100 |
| `offset`    | int     | pagination |
| `takerOnly` | bool    | "true"/"false" string |
| `filterType`| string  | "CASH" — enable min USD filter |
| `filterAmount`| float | min USD when filterType=CASH |

### Response Fields — VERIFIED (D68 Phase 0, 458 trades)
```json
{
  "proxyWallet":          "0x...",   // PRIMARY IDENTITY KEY
  "side":                 "BUY",     // "BUY" | "SELL"
  "outcome":              "Up",      // "Up" | "Down" | "Yes" | "No"
  "price":                0.93,      // float 0–1
  "size":                 29.849,    // shares (float)
  "timestamp":            1714512000000, // Unix MILLISECONDS
  "transactionHash":      "0x...",   // Polygon tx hash (100% coverage confirmed)
  "conditionId":          "0x...",
  "eventSlug":            "btc-updown-5m-1777357500",
  "asset":                "...",     // ERC1155 token_id
  "title":                "...",
  "slug":                 "...",
  "outcomeIndex":         0,
  "name":                 "peipeipei",   // display name, mutable
  "pseudonym":            "...",          // stable system alias
  "bio":                  "...",
  "profileImage":         "...",
  "profileImageOptimized":"...",

  // "usdcSize": ← ❌ FIELD DOES NOT EXIST IN RESPONSE
}
```

### ⚠️ KNOWN MISSING FIELDS (D68 Bug Report)
| Field Assumed | Reality | Correct Approach |
|--------------|---------|-----------------|
| `usdcSize` | **NOT in response** | Compute: `usdc_size = size × price` |

### Standard Parse Pattern (RULE-API-2 compliant)
```python
# CORRECT — verified 2026-04-28
usdc_size = round(
    float(raw.get("size")  or 0) *
    float(raw.get("price") or 0),
    4
)  # usdcSize NOT returned by API; compute from size×price

proxy_wallet     = raw.get("proxyWallet", "")      # verified field
transaction_hash = raw.get("transactionHash", "")  # verified field, 100% coverage
name             = raw.get("name", "")             # verified field
pseudonym        = raw.get("pseudonym", "")        # verified field
side             = raw.get("side", "")             # "BUY" | "SELL"
outcome          = raw.get("outcome", "")          # "Up" | "Down" | "Yes" | "No"
price            = float(raw.get("price") or 0)   # 0–1
size             = float(raw.get("size") or 0)     # shares
timestamp        = int(raw.get("timestamp") or 0)  # Unix ms
condition_id     = raw.get("conditionId", "")      # market ID
event_slug       = raw.get("eventSlug", "")        # market slug
asset            = raw.get("asset", "")            # ERC1155 token_id
```

---

## Endpoint: GET https://data-api.polymarket.com/activity

**Auth**: None required
**`user` parameter**: REQUIRED (0x-prefixed wallet address)
**Max limit**: 500

### Query Parameters (confirmed working)
| Parameter | Type   | Notes |
|-----------|--------|-------|
| `user`    | string | 0x wallet — REQUIRED |
| `type`    | string | "TRADE" | "SPLIT" | "MERGE" | "REDEEM" | "REWARD" |
| `market`  | string | conditionId — optional filter |
| `limit`   | int    | max 500 |
| `start`   | int    | Unix timestamp range start |
| `end`     | int    | Unix timestamp range end |

### Response Fields
Same schema as `/trades` response above.
`usdcSize` also absent — use same `size × price` calculation.

### Standard Usage
```python
# Pull full wallet history for insider analysis
r = requests.get(
    "https://data-api.polymarket.com/activity",
    params={
        "user":  proxy_wallet,   # required
        "type":  "TRADE",        # only trade events
        "limit": 500,
    },
    timeout=10,
)
trades = r.json() if r.ok else []
```

---

## WebSocket: wss://ws-subscriptions-clob.polymarket.com/ws/market

**Auth**: None required
**PING requirement**: Every 10 seconds — REQUIRED or connection drops

### Subscription Message
```json
{
  "assets_ids": ["<token_id>"],
  "type": "Market"
}
```

### Event Types Received
| event_type | Contains | Notes |
|-----------|---------|-------|
| `book` | bids/asks arrays | Orderbook snapshot |
| `price_change` | price delta | Price movement |
| `last_trade_price` | price, size, side, timestamp | Trade confirmed |

### ⚠️ KNOWN BEHAVIOUR
- `last_trade_price` events: **NO `proxyWallet`** — CLOB WS is price-only
- Identity data ONLY available via `data-api.polymarket.com/trades` (REST poll)
- 0 trades in CLOB WS ≠ no trading activity (AMM outer quotes suppress WS events)

### Response — last_trade_price
```json
{
  "event_type":    "last_trade_price",
  "asset_id":      "...",
  "price":         "0.93",
  "size":          "29.849",
  "side":          "BUY",
  "timestamp":     "1714512000000",
  "fee_rate_bps":  "0",
  "trader_side":   "TAKER"
}
```

---

## WebSocket: wss://ws-live-data.polymarket.com (RTDS)

**Auth**: None required
**PING requirement**: Every 5 seconds (stricter than CLOB WS)

### Subscription Message
```json
{
  "action": "subscribe",
  "subscriptions": [{
    "topic":   "crypto_prices",
    "type":    "update",
    "filters": "btcusdt"
  }]
}
```

### ⚠️ KNOWN BEHAVIOUR (D68 Phase 0)
- Connection succeeds (no error) BUT 0 price ticks received in 10-min test
- Do NOT report "RTDS running ✅" unless actual ticks confirmed
- Subscription format may have changed — verify before relying on this feed
- Does NOT provide trade/identity data — only crypto reference prices

---

## Endpoint: GET https://gamma-api.polymarket.com/markets

**Auth**: None required

### Query Parameters (confirmed working)
| Parameter | Type   | Notes |
|-----------|--------|-------|
| `slug`    | string | market slug, e.g. "btc-updown-5m-1777357500" |
| `conditionId` | string | direct lookup |

### Key Response Fields
```json
{
  "conditionId":   "0x...",           // needed for /trades filter
  "clobTokenIds":  "[\"0x...\"]",     // JSON string, parse with json.loads()
  "slug":          "btc-updown-5m-1777357500"
}
```

### ⚠️ KNOWN BEHAVIOUR
- `clobTokenIds` is returned as **JSON string**, not array
- Must parse: `ids = json.loads(m.get("clobTokenIds") or "[]")`

---

## Market Type Detection — VERIFIED APPROACH

### ❌ WRONG: Spread-based detection (D67 bug, root cause of accepted=1=0)
```python
# DO NOT USE — proven incorrect for hybrid markets
def is_amm_market(bid, ask):
    return (ask - bid) > 0.85  # BTC 5m spread=0.98 BUT has real CLOB trades
```

### ✅ CORRECT: Volume-based detection (D69 ruling, RULE-MKT-2)
```python
def has_recent_clob_trades(token_id, lookback_secs=300):
    """
    True = CLOB or hybrid market (real trades exist).
    False = possibly pure AMM (no trades in last 5 min).
    BTC 5m: returns True (458 trades/10min confirmed D68).
    """
    r = requests.get(
        "https://clob.polymarket.com/trades",
        params={"token_id": token_id, "limit": 1},
        timeout=3.0,
    )
    trades = r.json() if r.ok else []
    if not trades:
        return False
    t = trades[0] if isinstance(trades, list) else {}
    for k in ("timestamp", "matchTime", "createdAt", "time"):
        ts = t.get(k)
        if ts:
            trade_ts = float(ts)
            if trade_ts > 1e12:
                trade_ts /= 1000
            return (time.time() - trade_ts) <= lookback_secs
    return True  # trade exists, no timestamp → assume recent
```

### BTC 5m Market Classification (D68 confirmed)
- Outer AMM quotes: bid=0.01 / ask=0.99 (spread=0.98)
- Real CLOB activity: prices 0.30–0.70, confirmed 458 trades in 10 min
- Classification: **HYBRID AMM+CLOB** — treat as CLOB for trading purposes
- AMM spread guard MUST NOT block this market
