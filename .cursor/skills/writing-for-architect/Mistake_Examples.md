# Common Mistakes — Detailed Examples

## Mistake 1: Including unverified theories in diagnostic findings

**Bad:**
```
### Root cause hypothesis
The Gamma API must be filtering out BTC markets because of some TTL issue.
```

**Good:**
```
### Root cause hypothesis
Cannot confirm — Gamma API returned 50 markets, zero matched T1 filter.
Hypothesis: BTC 5-min markets may not be in the first 50 results when ordered by default.
Verify at: run_radar.py:L112–L145
```

---

## Mistake 2: Asking multiple questions without ranking priority

**Bad:**
```
Q1: Should we use A or B?
Q2: Should we use C or D?
Q3: Should we use E or F?
```

**Good:**
```
### Q1 (CRITICAL — system blocked): Should we use A or B?
### Q2 (secondary — can wait): Should we use C or D?
```

---

## Mistake 3: No System Status table for escalations

When escalating a partially-working system, the architect needs to quickly distinguish between "system on fire" and "system stable but X is broken".

**Required for Type 3 (Escalation):**
```
| Component | Status | Evidence |
|-----------|--------|---------|
| WS connection | ✅ | event_type=last_trade_price arriving |
| Trade tick arriving | ✅ | FIRST at 13:09:11 on T3 market |
| Entropy fires | ✅ | z=-2.570 at 13:09:29 |
| T1 subscription | ❌ | t1=0 in [L1_SUBSCRIPTION] |
```

---

## Mistake 4: Not clearly marking ruling received

When architect responds, update the handoff immediately:

```markdown
### Q1: Should we adopt resolve_event_ref()? → **RULING: Option B — NO** (received 2026-04-24)
```

---

## Mistake 5: Embedding large code blocks instead of file references

**Bad:** Pasting 30-line functions into the handoff document

**Good:**
```
Relevant code: clob_client.py:L72–L128 (has_recent_clob_trades), L181–L197 (fetch_best_ask hybrid logic)
```
The Architect reads the live source on GitHub. Only embed if the snippet is ≤ 8 lines.

---

## Mistake 6: Embedding raw terminal output without formatting

**Bad:** Copy-pasting 500 lines of raw terminal output

**Good:**
```
## Diagnostic log (key lines):
2026-04-24 13:09:07,329 [WARNING] [L1_TIER1_ZERO] returned 0 tokens. Gamma API returned 50 raw markets.
2026-04-24 13:09:11,460 [INFO] [DIAG][TRADE_TICK] FIRST trade tick — asset_id=5505... side=SELL size=190.96
2026-04-24 13:09:29,762 [INFO] [DIAG][ENTROPY_FIRE] z=-2.570 fire=YES market=10526756...
```
Summarize key lines — don't dump the entire log.

---

## Anti-Patterns to Avoid in Handoff Writing

### 1. Repeating context the Architect already knows
❌ "我在做 Phase 2-C-2 的過程中，發現 clob_client 有一些職責問題..."
✅ "Q1: Should _process_event() INSERT and clob_client UPDATE, or vice versa?"

### 2. Writing background before the question
❌ "First, let me explain the history of this decision. In Phase 1 we had a problem..."
✅ Lead with the question. Background is optional at most.

### 3. Describing Option B as "complex" without quantifying
❌ "Option B is complex and hard to maintain."
✅ "Option B: Requires separate INSERT in clob_client — 2-phase commit risk if orchestrator crashes between phases."

### 4. Saying "the file we discussed"
❌ "The issue is in the _process_event file we discussed."
✅ "Relevant code: signal_engine.py:L374–L392"

---

## Example: Q with all best practices

```
### Q1: Should _process_event() INSERT first or UPDATE first for CLOB order ID?
**Scope**: Schema change — hard to revert
**Relevant code**: signal_engine.py:L374–L392, clob_client.py:L88–L110

Options:
- A: INSERT with clob_order_id=NULL → clob_client UPDATE after return
  pros: ABORT paths always recorded; cons: NULL column exists transiently
- B: clob_client INSERT (separate record)
  pros: clean separation; cons: _process_event ABORT paths unrecorded
- **Z: Architect's call** — if neither fits the system invariants I cannot
  see from my position, please specify. I will implement your direction.

**Suggested**: Option A — INSERT-first covers all ABORT paths cleanly.
**Confidence**: Medium — untested under concurrent load.

→ Needs ruling: Confirm INSERT-first layering is correct, or direct Option Z.
```
