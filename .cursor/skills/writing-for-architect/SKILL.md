---
name: writing-for-architect
description: >-
  Communication protocol for the coding agent handing off to the architect agent.
  YOU ARE THE CODING AGENT. You write handoffs to the architect who reviews your work
  and provides the next implementation plan. Use when: session ends (always), architect
  delivers a plan (always), you hit a decision you cannot resolve (escalation), or
  explicit request: "write to 架構師", "handoff to architect", "send to architect".
  The architect reads live code at `https://github.com/w062c30/panopticon-private-v2`.
  Push all changes before writing any handoff.
---

# Coding Agent ↔ Architect Communication Protocol

## Role Clarity (Read This First)

**YOU are the CODING AGENT.** This skill tells you how to communicate with the
ARCHITECT — a separate agent who reviews your work, makes architectural decisions,
and provides the next implementation plan.

```
CODING AGENT (you)          ARCHITECT
─────────────────           ─────────────────────────────────
- Implements code           - Reviews holistically
- Writes handoffs     →     - Reads GitHub repo (cannot read local)
- Executes plans      ←     - Writes implementation plans
- Reports findings          - Makes rulings on your Qs
- Asks questions            - May reframe the entire problem
```

**Do NOT write the architect's response.** Your job ends when you submit the handoff.

---

## The Communication Loop

```
1. ARCHITECT delivers plan
       ↓
2. CODING AGENT executes plan tasks (autonomous)
       ↓
3. CODING AGENT hits blocker OR plan completes
       ↓
4. CODING AGENT: git push → write handoff → submit
       ↓
5. ARCHITECT reads handoff + GitHub → reviews + writes next plan / rulings
       ↓
   back to step 1
```

**git push is MANDATORY before writing the handoff.** The architect cannot read your
local files. If you do not push first, the architect cannot verify your `file:L_n`
references.

---

## When to Write a Handoff

| Trigger | Handoff type |
|---------|-------------|
| Coding session ends | Completion Report (always) |
| Architect delivers a plan (chat/file) | Acknowledge receipt → start executing |
| Blocker mid-plan (≥ 2 attempts failed) | Escalation |
| Decision requires architect authority | Q&A handoff |
| User explicit request | Whatever type fits |

**Act autonomously (no handoff needed):**
- Bug fixes where the correct approach is unambiguous
- Refactoring within an already-ruled pattern
- Tactical decisions that are fully reversible with no downstream contract implications

---

## Step 1 — Before Writing Any Handoff: Push First

```bash
git add -A
git commit -m "D{XX}: {change summary}"
git push
# ONLY THEN write the handoff
```

**What to push:** all modified `.py` files, `run/versions_ref.json`, schema changes,
frontend `package.json` version bumps, config files (non-secret).

**Never push:** `.env`, `secrets/`, `data/*.db`, `run/*.lock`, `run/*.pid`,
`temp_architect_handoffs/` content, `graphify-out/`.

---

## Step 2 — Write the Handoff

### Required structure (ALL handoffs):

```markdown
Use the "architect-response-when-receiving-handoff" skill.

# Architect Handoff — {date} {tag}

> Architect: read live code at https://github.com/w062c30/panopticon-private-v2

## 任務狀態追蹤 (Task Status)

| # | Task | Priority | Status | Notes |
|---|------|----------|--------|-------|
| 1 | {description} | P0 | ✅ DONE | {notes} |
| 2 | {description} | P1 | ⏳ PENDING | Waiting on X |
| 3 | {description} | P2 | 🔒 BLOCKED | Needs architect ruling |

## Observations (hard facts only — no theories)

{Raw data: log lines, API responses, error messages, DB counts.
 NO guesses or interpretations — label any hypothesis explicitly as "Hypothesis: ..."}

## 待決事項 / Escalations

{See Q format below — only include if you have actual Qs}

## Architect Review Scaffolding

{See next section — select only items relevant to this handoff}
```

**Status values:** ✅ DONE / ⏳ PENDING / 🔄 IN_PROGRESS / 🔒 BLOCKED / ❌ FAILED / ⏭️ SKIPPED

---

## Step 3 — Writing Questions (Q format)

Only escalate when you genuinely cannot proceed. Do NOT ask Qs you can answer
from code inspection.

```markdown
### Q1: {exact decision the architect must make}
**Scope**: [Reversible < 1h] / [Schema change] / [API contract]
**Relevant code**: `file.py:L_start–L_end`

Options:
- A: {what} → pros / cons
- B: {what} → pros / cons
- **Z: Architect's call** — if none of the above fit constraints I cannot see.

**Suggested**: Option A — {1-sentence reason}
**Confidence**: High / Medium / Low
→ Needs ruling: {exact yes/no or A/B/Z question}
```

**Self-audit before writing any Q:**
```
□ Can I answer this myself with code inspection?  → if yes, don't escalate
□ Have I tried ≥ 2 approaches that failed?
□ Does this affect invariants, contracts, or architectural patterns?
□ Have I stated all options — including "do nothing" and "change the caller instead"?
□ Have I included Option Z?
□ Is my suggested lean stated honestly — not to steer the architect?
```

---

## Step 4 — Architect Review Scaffolding

This section is the **coding agent's signal to the architect** about which areas
need the architect's eyes. Select only items that are relevant to what this handoff
actually covers.

**HOW TO SELECT:**

| Handoff content | Include these items |
|----------------|---------------------|
| Bug fix / hotfix | Code quality items only (security, process guard, resource cleanup) |
| New feature | Code quality + plan completeness + task tracking |
| Completion report | Code quality + task tracking |
| Escalation | Code quality + task tracking; skip plan completeness |
| Pure Q&A ruling request | Only items directly relevant to the Q |

**IMPORTANT — include only items you actually touched.** Do not copy-paste all items
every time. If you did not change a WebSocket handler, omit the WebSocket item.
The architect can raise anything outside your list independently.

### Template (select relevant items):

```markdown
## 架構師職責 — Review Scaffolding
> 此段落用於標記 Coding Agent 需要 Architect 關注的審查重點。
> Architect 可自行擴展、忽略無關項目，或重新框架整個問題。

### Code Quality (select relevant)
- [ ] Contract: `shared/contracts/panopticon-event.schema.json` compat
- [ ] Audit trail: `Prior → Evidence → Posterior → Kelly → Action` chain intact
- [ ] Security: no hardcoded API keys / private keys in {files changed}
- [ ] Graphify isolation: `graphify-out/*` not entering signal/risk/execution path
- [ ] Process guard: `acquire_singleton()` in all entry points
- [ ] Version protocol: `PROCESS_VERSION` declared before `_lifespan`
- [ ] Resource cleanup: DB connections closed in `finally` blocks

### Plan Completeness *(only if architect's plan was executed — otherwise remove)*
- [ ] All task `file:L_start–L_end` references verified
- [ ] No missed dependency ordering
- [ ] Risk flags addressed: {list which ones applied}
- [ ] Version bumps: {process} {old} → {new}

### Task Status
- [ ] All completed tasks verified against expected behavior
- [ ] Blocked tasks have clear escalation path
```

---

## Step 5 — Receiving Architect's Response

When the architect replies:

1. **Parse the implementation plan** — extract tasks into your status table immediately
2. **Echo each ruling** before implementing:
   *"Understood — implementing Option A: INSERT-first. Will NOT implement Option B."*
3. Mark Qs as `→ RULING: Option A (received {date})`
4. Begin executing autonomously — only return when plan is complete OR you hit a blocker

### Ruling Type Reference

| Format | What it means | What YOU must do |
|--------|--------------|-----------------|
| `→ RULING: Option A` | Architect chose your option A | Echo + implement |
| `→ RULING: Option A with modification` | Option chosen but changed | Echo the modification explicitly |
| `→ RULING: Option B — NO {pattern}` | Option with explicit prohibition | Mark prohibition; do NOT implement prohibited pattern |
| `→ RULING: Option Z — {direction}` | Architect's new direction | Echo verbatim; do NOT re-argue or append caveats |
| `→ Defer to D{N+2}` | Task pushed forward | Do NOT implement; note as ⏭️ SKIPPED in status table |
| `→ Needs more info` | Architect cannot rule yet | Provide ONLY the missing data — no new Qs |

### Plan Execution Rules

When you receive a `架構師交辦` plan:

- **Priority ordering is binding** — implement 🔴 CRITICAL before 🟡 REQUIRED before 🟢 NICE-TO-HAVE. Do not reorder.
- **"Do NOT" items are hard stops** — not suggestions. Treat as invariants. Never implement a prohibited pattern even if it seems correct.
- **Success condition is the acceptance test** — you are not done until the success condition is observable.
- **File targets (`file.py:L_start–L_end`) define your scope** — do not refactor beyond the stated range without a new ruling.

**If architect selects Option Z:** Echo the direction verbatim. Do NOT re-argue.
Do NOT append caveats in favour of your original suggestion.

---

## Format Rules

- Title: `temp_architect_handoffs/YYYY-MM-DD_HHMM_<tag>.md`
- First line: `Use the "architect-response-when-receiving-handoff" skill.` (hard requirement)
- Prose: ≤ 350 lines (can be slightly eased if needed). Code blocks exempt.
- Language: Traditional Chinese headers; code/logs/paths/Qs in English
- Never commit handoff files to git

### Folder Management
- Keep only latest handoff in `temp_architect_handoffs/`
- Move older versions to `temp_architect_handoffs/old/`
- `old/` files cannot be referenced by the Architect

---

## Common Mistakes

| Mistake | Impact | Correction |
|---------|--------|------------|
| Writing theory as fact in Observations | Architect trusts wrong diagnosis | Label all guesses: "Hypothesis: ..." |
| Steering options toward your preferred answer | Architect gets tunnel vision | State options neutrally; lean is just a lean |
| Copying all checklist items regardless of content | Architect ignores the checklist | Select only items you actually touched |
| Forgetting to push before handoff | Architect cannot verify your file:L_n | Always: push → then write |
| Asking Qs you can answer yourself | Wastes architect's attention budget | Self-audit before escalating |
| Multiple unranked Qs | Architect answers wrong one first | Always rank: Q1 = blocking, Q2 = secondary |

For detailed examples, see [Mistake_Examples.md](Mistake_Examples.md).
For document templates and Pre-Handoff Push protocol, see [reference.md](reference.md).
