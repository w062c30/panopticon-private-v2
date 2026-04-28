---
name: writing-for-architect
description: >-
  Write effective handoff documents for the architect. This is the **DEFAULT handoff format** at
  the end of every coding session. Also triggers when user asks "write to 架構師", "handoff to
  architect", "send to architect", "ask architect", or "need architect review".
  **Unlike `handoff-context` (which requires explicit user request), this skill runs as the
  standard session-end handoff.**
---

# Writing for the Architect

## Core Principle

The architect needs to make decisions. Your job is to give them **exactly enough context to decide** — no more, no less. If you wouldn't bet $100 on the architect reading something, cut it.

---

## When to Act Autonomously vs. When to Escalate

### Handle YOURSELF (no handoff needed):
- Bug fixes where the correct approach is unambiguous
- Refactoring within an already-ruled pattern
- Tactical implementation decisions (loop structure, variable naming, minor optimizations)
- Any change that is fully reversible with no downstream contract implications

### Write a handoff ONLY when:
- A decision affects system invariants, API contracts, or architectural patterns
- You have ≥ 2 genuinely valid options and cannot break the tie with hard data
- A critical pipeline is broken and you have exhausted ≥ 2 fix attempts
- The Architect explicitly asked to be notified on completion

**Rule: If you would not bet $100 that this decision matters in 2 weeks, handle it yourself.**

---

## Two Types of Handoffs

### Type A: Session-End Handoff (most common — DEFAULT)
Run at the **end of every coding session**, even if all tasks completed.
Purpose: inform architect of what was done, current system state, and what remains.

This is the **standard output format** — do not wait to be asked.
When a session ends (success, blocker, or escalation), write to `handoff_YYYY-MM-DD_HHMM_<tag>.md`
in the project root following the format in Section 3 below.
Example: `handoff_2026-04-26_0345_d39_d40.md`

### Type B: Escalation Handoff (as-needed)
Run when you hit a **decision that requires architect authority**:
- Trade logic thresholds, consensus rules, fast_gate params
- 3+ failed fix attempts on the same bug
- Schema changes to execution_records, kyle_lambda_samples, whale_alerts
- Architecture decisions with ≥2 valid options and no hard data to break tie

Write to `temp_architect_handoffs/YYYY-MM-DD_HHMM_<tag>.md` and copy-paste to chat with "請審查並裁決".
Example: `temp_architect_handoffs/2026-04-26_0345_d39_d40_complete.md`

---

## Upgraded Handoff Rules

### 1. Unchecked Assumptions Field (REQUIRED for Type 1 and Type 3)

Every handoff with pending decisions MUST include this section:

```
## ⚠️ Unchecked Assumptions
- [ ] {assumption you made that the Architect cannot verify without file access}
- [ ] {constraint you believe exists but have not confirmed from source}
```

**Why**: The Architect cannot read files. You control what context they see.
Listing your unverified assumptions prevents you from unconsciously framing
options in a way that forecloses valid alternatives.

**Example:**
```
## ⚠️ Unchecked Assumptions
- [ ] Assumed clob_client never retries — not confirmed from clob_client.py source
- [ ] Assumed INSERT-first is safe because _process_event is single-threaded — not load-tested
```

If you have zero unchecked assumptions, write:
```
## ⚠️ Unchecked Assumptions
- None — all assumptions confirmed from source code or logs.
```

---

### 2. Option Completeness Check (before writing Q{N})

Before presenting options to the Architect, self-audit:

```
BEFORE WRITING OPTIONS — ask yourself:
□ Have I considered the "do nothing / defer" option?
□ Have I considered the option that requires changing the caller instead of the callee?
□ Is there an option that avoids the tradeoff entirely (e.g., schema change upstream)?
□ Am I omitting an option because I dislike it, not because it's invalid?
```

If you find a new option after this check, add it. If you genuinely eliminated
an option, note it briefly:

```
Options considered but eliminated:
- Option D (rewrite upstream caller): rejected — out of scope, requires Phase 4 work.
```

---

### 3. Decision Scope Declaration

At the top of every Q{N}, add one line:

```
**Scope**: [Reversible in < 1 hour] / [Schema change — hard to revert] / [API contract — affects callers]
```

This lets the Architect calibrate how much scrutiny to apply without reading
all the context first.

---

### 4. Confidence Signal

State your confidence level on your Suggested lean:

```
**Suggested**: Option A — INSERT-first with NULL, then UPDATE on CLOB return.
**Confidence**: High — confirmed from 3 test runs. / Medium — untested under load. / Low — intuition only.
```

Low confidence = Architect should probe harder before ruling.

---

### 5. Always Include Option Z

Every Q{N} that presents options MUST end with:

```
- **Z: Architect's call** — if none of the above fit the constraints
  I cannot see from my position, please specify. I will implement your direction.
```

**Why this matters**: The Architect may have system-level knowledge, prior design
decisions, or constraints that are not visible in the code you can read.
Option Z is not a fallback or a failure — it is a legitimate first-class choice
that explicitly grants the Architect authority to reframe the entire question.

**Rules for Option Z:**
- Do NOT describe it as a "last resort" or imply the other options are better by default
- Do NOT re-argue for your original suggestion if the Architect selects Option Z
- If Option Z is selected with a direction, echo it as a ruling immediately and implement

**Example Q with Option Z:**
```
### Q1: Should _process_event() INSERT first or UPDATE first for CLOB order ID?
**Scope**: Schema change — hard to revert
**Options**:
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

---

### 6. Handoff Size Discipline

| Section | Limit |
|---------|-------|
| Prose per Q | ≤ 150 words |
| Code snippets | 1–3 snippets, ≤ 20 lines each |
| Log output | Key lines only, fenced block, ≤ 30 lines |
| Total document | ≤ 350 lines prose (code blocks exempt) |

If you are over limit: cut background, not data.

---

## Information Asymmetry Protocol

The Architect cannot catch what you do not show them.
You are responsible for surfacing information that would change their ruling,
**even if it makes your suggested option look worse**.

**Prohibited framing:**
❌ Omitting a known constraint because it complicates your preferred option
❌ Describing Option B as "complex" without quantifying the complexity
❌ Using "we discussed" or "as before" — Architect has no session memory across handoffs

**Required framing:**
✅ If two options have different risk profiles, state the worst-case failure mode for each
✅ If you have seen this pattern fail before, say so explicitly
✅ Reference prior rulings by date: "Per Architect ruling 2026-04-24 Q1: Option A"

---

## Ruling Acknowledgement (non-negotiable)

When you receive a ruling, before implementing:

1. Echo the ruling back in one sentence: *"Understood — implementing Option A: INSERT-first with clob_order_id=NULL."*
2. State what you will NOT do: *"Will not implement Option B (separate INSERT in clob_client)."*
3. Update the handoff Q to: `→ **RULING: Option A** (received {date})`

If the ruling is ambiguous, ask exactly one clarifying question before proceeding.
Do not implement an interpretation and present it as a ruling.

If the Architect selects **Option Z** and provides a direction:
- Echo the direction verbatim
- Do NOT re-argue or append caveats in favour of your original suggestion
- Treat it as the highest-confidence ruling regardless of your prior lean

---

## Document Types

### Type 1: Handoff with Pending Decisions (most common)

Used when: you found problems, have options, need a ruling before proceeding.

**File name:** `temp_architect_handoff_{date}.md` in project root.

**Structure:**

```markdown
# Architect Handoff — {date}

## Phase N Completed
| Item | Status |
|------|--------|
| {change} — {file} | ✅ |

## Diagnostic Findings
### What you observed
[Hard data: DB counts, log lines, error messages — NO theories]

### Root cause hypothesis
[Only if confirmed. Otherwise: "Cannot confirm until X"]

## ⚠️ Unchecked Assumptions
- [ ] {assumption you made that the Architect cannot verify without file access}
- [ ] {constraint you believe exists but have not confirmed from source}

---

## Pending Decisions

### Q1: {specific question}
**Scope**: [Reversible in < 1 hour] / [Schema change — hard to revert] / [API contract — affects callers]
**Options**:
- A: {what you propose} → {pros}/{cons}
- B: {alternative} → {pros}/{cons}
- **Z: Architect's call** — if neither fits the constraints I cannot
  see from my position, please specify. I will implement your direction.
**Suggested**: {your lean and why}
**Confidence**: High / Medium / Low
→ Needs ruling: {exact question}

### Q2: {next question}
...

## Relevant Code
```python
# {function name} — {file}:{line}
{code snippet}
```

## Constraints (do NOT touch until architect rules)
- ⛔ {specific thing} — reason
```

### Type 2: Completion Report

Used when: all work done, no decisions needed, just informing.

**Structure:**

```markdown
# Architect Report — {date}

## Completed Work
| Item | File | Status |
|------|------|--------|
| {change} | {file} | ✅ |

## Verification
- Tests: {n} passed
- DB schema: {verified/new columns}
- Observed: {what the system did when run}

## No pending decisions
```

### Type 3: Escalation (urgent, system partially working)

Used when: system is stable but a critical pipeline is broken, issue recurs, or partial success was achieved. The goal is to escalate with a clear "TRIED / HAPPENED / NEED" structure so the architect can respond quickly.

**File name:** `temp_architect_handoff_{date}.md` — same as Type 1, but section header is **ESCALATION** not "Pending Decisions".

**Structure:**

```markdown
## ESCALATION: {one-line summary}

**Shell X running since {time} ({elapsed} elapsed)**

### Situation
[What is broken / partially working]

### What I Tried
- [Action 1]: [result]
- [Action 2]: [result]

### System Status (live indicators)
| Component | Status | Evidence |
|-----------|--------|---------|
| WS connection | ✅/❌ | {log line} |
| Trade tick arriving | ✅/❌ | {log line} |
| Entropy fires | ✅/❌ | z={value} |
| Gamma API | ✅/❌ | HTTP {code} |

### DB State
```
{kyle_lambda_samples: N rows}
{wallet_observations (15min): N}
```

### Root cause hypothesis
[Only if confirmed; otherwise: "Cannot confirm — hypothesis: ..."]

### Request for Architect Ruling

**Q{N}: {specific question}**
**Scope**: [Reversible in < 1 hour] / [Schema change — hard to revert] / [API contract — affects callers]
Options:
- A: {description} → {pros}/{cons}
- B: {description} → {pros}/{cons}
- **Z: Architect's call** — if neither fits the constraints I cannot
  see from my position, please specify. I will implement your direction.
**Suggested**: {lean}
**Confidence**: High / Medium / Low
→ Needs ruling: {exact question}
```

---

## Architect Response Protocol

When the architect responds, the response typically has one of these forms:

| Response type | What it means | What to do |
|--------------|---------------|-----------|
| `→ Ruling: Option A` | Architect chose an option | Implement immediately; close the Q in the handoff |
| `→ Ruling: Option B with modification` | Option chosen but modified | Apply modification; confirm in writing what changed |
| `→ Option B — NO {pattern}` | Explicit rejection of a pattern | Mark Q as closed; do not implement the rejected pattern |
| `→ Needs more info` | Insufficient to decide | Provide exactly the missing data (no more, no less) |
| `→ Escalate to Q{N+1}` | Question was wrong, a deeper question exists | Update handoff with the new Q |
| `→ Option Z` | Architect provides a new direction | Echo verbatim, implement without re-arguing |

**Closing the loop:** When a ruling is received, update the handoff:
```markdown
### Q1: ... → **RULING: Option A** (received {date})
```

---

## What Makes It Good

### Good: Architect can decide in 2 minutes

```
Q1: Should we use Option A (INSERT) or Option B (UPDATE) for recording CLOB order ID?
Context: _process_event() must record the gate decision BEFORE clob_client runs.
  If we INSERT first, ABORT paths are covered.
  If we UPDATE first, we need a placeholder row.
Suggested: Option A — INSERT with clob_order_id=NULL, then UPDATE after clob_client returns.
→ Needs ruling: Does the INSERT-first approach violate any invariant?
```

### Bad: Architect has to read 200 lines to find the question

```
"First, let me explain the history of this decision. In Phase 1 we had a problem
where the database wasn't properly initialized. Then in Phase 2 we tried various
approaches. After much deliberation..."
```

---

## Hard Rules

### Do
- ✅ Write the **exact question** the architect needs to answer — at the top of each Q
- ✅ Include **1-3 code snippets** (max 20 lines each) so architect sees the actual code
- ✅ Include **hard data** (DB counts, error messages, log lines) when presenting a problem
- ✅ State your **suggested lean** with reasoning for each option
- ✅ List **one file path + line numbers** for each snippet (architect can't open your IDE)
- ✅ Use ⛔ for things you will NOT touch until ruled
- ✅ Include a **"System Status" table** for escalations — architect needs to quickly assess if the system is stable or on fire

### Do NOT
- ❌ Write an essay — architect won't read it
- ❌ Bury the question — put it first, not last
- ❌ Give options without a suggested lean — you already analyzed it, say what you think
- ❌ Reference files without line numbers — include the actual code
- ❌ Use placeholder paths like "the file we discussed" — be specific
- ❌ Add known background information — architect already knows the system
- ❌ Proactively create handoffs — wait for user to ask

---

## Format Rules

- Title: `temp_architect_handoff_YYYY-MM-DD_HHMM.md` or `temp_architect_YYYY-MM-DD_HHMM.md`
- Language: Match the project (this project uses Traditional Chinese for headers)
- Code snippets: Always with file path + line numbers, e.g. ````python\n# func — file.py:42\n`
- No emoji in code blocks
- Prose sections: max **350 lines** per document
- Code blocks: **no line limit** — a 40-line function snippet is fine if it's the exact code at issue
- Diagnostic data: use fenced code blocks for raw log output; tables for structured metrics
- Delete or rename `temp_architect_handoff_...md` to `ARCHIVED_...md` after receiving ruling
- Never commit handoff files to git

### Folder Management Rules

`temp_architect_handoffs/` 目錄下**只保留最新一份 handoff 檔案**（以 `YYYY-04-XX_DN` 命名者）。
- `old/` 子目錄：所有舊檔案自動移入此處，**永不主動刪除**，`old/` 中的檔案**不可被 Architect 引用**
- 操作流程：每次建立新 handoff 前，先將 `temp_architect_handoffs/` 中非最新版本的所有檔案移至 `old/`
- `README.md` 保持在根目錄不動

---

## Common Mistakes to Avoid

### Mistake 1: Including unverified theories in diagnostic findings

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
```

### Mistake 2: Asking multiple questions without ranking priority

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

### Mistake 3: Not providing a system status table for escalations

When escalating a partially-working system, the architect needs to quickly distinguish between "system on fire, drop everything" and "system stable but X is broken". A status table makes this instant.

**Required for Type 3 (Escalation):**
```
| Component | Status | Evidence |
|-----------|--------|---------|
| WS connection | ✅ | `[WS] Connected to wss://` at 13:09 |
| Trade tick arriving | ✅ | `[DIAG][TRADE_TICK] FIRST trade tick` at 13:09:11 |
| Entropy fires | ✅ | `z=-2.570 fire=YES` at 13:09:29 |
| Gamma API | ✅ | HTTP 200 on all 3 calls |
| T1 subscription | ❌ | `t1=0` in `[L1_SUBSCRIPTION]` |
```

### Mistake 4: Not clearly marking ruling received

When architect responds, the handoff should be updated immediately:

```markdown
### Q1: Should we adopt resolve_event_ref()? → **RULING: Option B — NO** (received 2026-04-24)
```

### Mistake 5: Embedding raw terminal output without formatting

**Bad:** Copy-pasting 500 lines of raw terminal output

**Good:**
```
## Diagnostic log (key lines):
2026-04-24 13:09:07,329 [WARNING] [L1_TIER1_ZERO] returned 0 tokens. Gamma API returned 50 raw markets.
2026-04-24 13:09:11,460 [INFO] [DIAG][TRADE_TICK] FIRST trade tick — asset_id=5505... side=SELL size=190.96
2026-04-24 13:09:29,762 [INFO] [DIAG][ENTROPY_FIRE] z=-2.570 fire=YES market=10526756... taker=ws_unknown
```
Summarize key lines — don't dump the entire log.

---

## Example (from actual Panopticon work)

**Bad:**

```
我在做 Phase 2-C-2 的過程中，發現 clob_client 有一些職責問題。
本來它會寫入 execution_records，但後來我們決定把這個職責移回 signal_engine。
我做很多改動，包括新增了 update_execution_clob_result 函數。
```

**Good:**

```
Q1: Should _process_event() INSERT and clob_client UPDATE, or vice versa?
Context: _process_event must record ABORT paths (no CLOB call). clob_client must record CLOB result.
  Option A: _process_event INSERT with clob_order_id=NULL → clob_client UPDATE
  Option B: _process_event INSERT → clob_client INSERT (separate record)
Suggested: Option A — INSERT-first with NULL, then UPDATE on CLOB return.
→ Needs ruling: Confirm INSERT-first layering is correct?

Relevant code:
```python
# _process_event — signal_engine.py:380
db.append_execution_record({
    "execution_id": decision_id,
    "accepted": accepted,
    "clob_order_id": None,  # filled by clob_client UPDATE
    ...
})
```
```

---

## Escalation Example (from this session)

```
## ESCALATION: t1=0 Persists After Fix — Architect Intervention Required

**Shell 695953 running since 13:09 UTC (5 min elapsed)**

### Situation
[L1_SUBSCRIPTION] still shows t1=0 after restart with PANOPTICON_SHADOW=1.
[L1_TIER1_ZERO] diagnostic fired: "50 raw markets fetched, zero matched T1 filter."

### What I Tried
- Ran run_tier_diagnostic.py: Gamma API field names confirmed correct
- Applied isinstance guard to clobTokenIds parsing: no change
- Added [L1_TIER*_ZERO] diagnostic logs: confirm 0 T1 markets in every refresh cycle

### System Status
| Component | Status | Evidence |
|-----------|--------|---------|
| WS connection | ✅ | event_type=last_trade_price arriving |
| [DIAG][TRADE_TICK] | ✅ | FIRST at 13:09:11 on T3 market |
| EntropyWindow | ✅ | z=-2.570 at 13:09:29; z=-2.414 at 13:12:04 |
| Gamma API | ✅ | HTTP 200 on all calls |
| T1 subscription | ❌ | t1=0 in [L1_SUBSCRIPTION] |

### Request for Architect Ruling

Q6: How should we handle Gamma returning zero T1 markets?
  A: Increase limit=50 → limit=200, wider slug search
  B: Query BTC 5-min direct endpoint
  C: Accept t1=0, use T3 entropy fires as shadow proxy
  D: Fallback to top-5 most-active crypto markets as proxy T1
Suggested: Option A — smallest change, tests the pagination hypothesis.
→ Needs ruling: Approve Option A, or direct a different approach?
```

---

## Language

- Handoff headers: Traditional Chinese (e.g., `## 待決事項`, `## 診斷發現`)
- Code, log output, file paths: English only
- Q{N} question body: English (precision over familiarity)
- Ruling acknowledgements: Traditional Chinese