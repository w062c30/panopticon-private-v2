# Detailed Reference — Architect Handoff Skill

## Role Reminder

**This skill is used by the CODING AGENT.** The ARCHITECT receives the handoff
and responds with rulings and/or an implementation plan. The coding agent then
executes that plan autonomously.

---

## Architect Plan Format (Expected Output)

The coding agent cannot control how the architect writes their plan, but the
following format helps the coding agent parse and execute it cleanly. Include
this note in escalation handoffs to signal the preferred response format:

```
> Architect: if providing an implementation plan, the format below helps me
> execute it autonomously without back-and-forth. Feel free to use a different
> structure if you have architectural reasons to.
```

### Preferred Architect Plan Format (for architect's response)

The architect typically responds with a `架構師交辦` document inside a single code fence.
You parse it and execute. Key fields to extract:

```
# 架構師交辦 — {date} D{N+1}

## 裁決 (Rulings)            ← close your Qs here; update status table
### Q{N} → RULING: Option A
  - What to do: {one sentence}
  - What NOT to do: {prohibition}
  - Constraint: {hard limit}

## 任務清單 (Task List)       ← your execution list; extract into status table
### TASK D{N+1}a: {name}
  Priority: 🔴 CRITICAL / 🟡 REQUIRED / 🟢 NICE-TO-HAVE
  Files: `file.py:L_start–L_end`
  What to build:
    - {concrete step}
  Success condition: {observable verifiable output — log key / SQL / test output}
  Do NOT:
    - ⛔ {prohibition}       ← treat as invariant, not suggestion

## 不變式提醒 (Invariant Reminders)  ← apply globally during this sprint
## 成功標準 (Sprint Success Criteria) ← your acceptance test
## 禁區 (Off-Limits)                  ← hard stops; do not touch these
## 備註 (Notes)                       ← landmines, timing gotchas, risk callouts
```

**Extraction checklist after receiving a plan:**
- [ ] All TASK blocks → status table rows (use task name as description)
- [ ] Priority (`🔴/🟡/🟢`) → implementation order (binding — do not reorder)
- [ ] All `Do NOT` bullets → hard constraints in your mental model
- [ ] `Success condition` → your acceptance test before marking ✅ DONE
- [ ] All rulings → echo before starting; update Qs in handoff to `→ RULING: Option X (received {date})`
- [ ] Version bumps (if any) → apply exactly as specified


---

**Sequence: CODE → PUSH → HANDOFF. Never reverse this order.**

The Architect reads live source from `https://github.com/w062c30/panopticon-private-v2`. If code is not pushed before the handoff is written, the Architect cannot verify any `file.py:L_start–L_end` reference.

### Mandatory Sequence

```
Step 1: git add -A
Step 2: git commit -m "D{XX}: {change summary}"
Step 3: git push
Step 4: ONLY THEN write the handoff document
```

### What to Push
- All modified `.py` source files
- `run/versions_ref.json`
- Schema migration changes (`db.py`)
- Config files (non-secret)
- Frontend `package.json` version bumps

### What to EXCLUDE (never commit)
- `.env`, `secrets/`, `*.pem`, `*.key`
- `data/*.db`, `data/*.sqlite`, `run/*.lock`, `run/*.pid`
- `temp_*.py`, `reports/*.md`, `graphify-out/`
- `temp_architect_handoffs/`, `FEATURE_INDEX.md`
- `run/monitor_results/all_clob_trades_*.json` — extract snippets into handoff if needed

---

## Option Completeness Check (full text)

Before presenting options to the Architect, self-audit:

```
BEFORE WRITING OPTIONS — ask yourself:
□ Have I considered the "do nothing / defer" option?
□ Have I considered the option that requires changing the caller instead of the callee?
□ Is there an option that avoids the tradeoff entirely (e.g., schema change upstream)?
□ Am I omitting an option because I dislike it, not because it's invalid?
```

If you find a new option after this check, add it. If you genuinely eliminated an option, note it briefly:

```
Options considered but eliminated:
- Option D (rewrite upstream caller): rejected — out of scope, requires Phase 4 work.
```

---

## Always Include Option Z (full text)

Every Q{N} that presents options MUST end with:

```
- **Z: Architect's call** — if none of the above fit the constraints I cannot
  see from my position, please specify. I will implement your direction.
```

**Why this matters**: The Architect may have system-level knowledge, prior design decisions, or constraints that are not visible in the code you can read. Option Z is not a fallback or a failure — it is a legitimate first-class choice.

**Rules for Option Z:**
- Do NOT describe it as a "last resort" or imply the other options are better by default
- Do NOT re-argue for your original suggestion if the Architect selects Option Z
- If Option Z is selected with a direction, echo it as a ruling immediately and implement

---

## Decision Framing Checklist (expanded)

State your confidence level on your Suggested lean:

```
**Suggested**: Option A — INSERT-first with NULL, then UPDATE on CLOB return.
**Confidence**: High — confirmed from 3 test runs. / Medium — untested under load. / Low — intuition only.
```

Low confidence = Architect should probe harder before ruling.

---

## Information Asymmetry Protocol

**Prohibited framing:**
❌ Omitting a known constraint because it complicates your preferred option
❌ Describing Option B as "complex" without quantifying the complexity
❌ Using "we discussed" or "as before" — Architect has no session memory across handoffs
❌ Saying "see the file" without a specific `file.py:L{n}` reference

**Required framing:**
✅ If two options have different risk profiles, state the worst-case failure mode for each
✅ If you have seen this pattern fail before, say so explicitly
✅ Reference prior rulings by date: "Per Architect ruling 2026-04-24 Q1: Option A"
✅ For every claim about what the code does, provide `file.py:L{start}–L{end}` so the Architect can verify

---

## Architect Response Protocol

When the architect responds, the response typically has one of these forms:

| Response type | What it means | What to do |
|--------------|---------------|--------|
| `→ Ruling: Option A` | Architect chose an option | Implement immediately; close the Q |
| `→ Ruling: Option B with modification` | Option chosen but modified | Apply modification; confirm in writing |
| `→ Option B — NO {pattern}` | Explicit rejection | Mark Q closed; do not implement the rejected pattern |
| `→ Needs more info` | Insufficient to decide | Provide exactly the missing data |
| `→ Escalate to Q{N+1}` | Question was wrong | Update handoff with the new Q |
| `→ Option Z` | Architect provides a new direction | Echo verbatim, implement without re-arguing |

**Closing the loop:** When a ruling is received, update the handoff:
```markdown
### Q1: ... → **RULING: Option A** (received {date})
```

---

## Document Type Templates

### Type 1: Handoff with Pending Decisions

```markdown
# Architect Handoff — {date}

## 任務狀態追蹤

| # | 任務 | Priority | Status | Notes |
|---|------|----------|--------|-------|
| 1 | Task description | P0 | ✅ DONE | Notes |
| 2 | Task description | P1 | ⏳ PENDING | Waiting on X |

## Diagnostic Findings
### What you observed
[Hard data: DB counts, log lines, error messages — NO theories]

### Root cause hypothesis
[Only if confirmed. Otherwise: "Cannot confirm until X"]

## ⚠️ Unchecked Assumptions
- [ ] {assumption} — verify at {file.py:L_n}

***

## 待決事項

### Q1: {specific question}
**Scope**: [Reversible in < 1 hour] / [Schema change — hard to revert]
**Relevant code**: `{file.py:L_start–L_end}`
Options:
- A: {what you propose} → {pros}/{cons}
- **Z: Architect's call** — if neither fits, please specify.
**Suggested**: Option A
**Confidence**: High
→ Needs ruling: {exact question}

## 架構師職責（Architect Responsibilities）
[Full checklist — see SKILL.md]
```

### Type 2: Completion Report

```markdown
# Architect Report — {date}

## 任務狀態追蹤

| # | 任務 | Priority | Status | Notes |
|---|------|----------|--------|-------|
| 1 | Task description | P0 | ✅ DONE | Notes |

## Verification
- Tests: {n} passed
- DB schema: {verified/new columns}
- Observed: {what the system did when run}

## No pending decisions

## 架構師職責（Architect Responsibilities）
[Full checklist — see SKILL.md]
```

### Type 3: Escalation (urgent)

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

### Root cause hypothesis
[Only if confirmed; otherwise: "Cannot confirm — hypothesis: ..."]
[Point to suspect code: `file.py:L_n` for Architect to verify]

### Request for Architect Ruling

**Q{N}: {specific question}**
**Scope**: [Reversible in < 1 hour]
**Relevant code**: `{file.py:L_start–L_end}`
Options:
- A: {description} → {pros}/{cons}
- **Z: Architect's call** — please specify.
**Suggested**: {lean}
**Confidence**: High
→ Needs ruling: {exact question}

## 架構師職責（Architect Responsibilities）
[Full checklist — see SKILL.md]
```
