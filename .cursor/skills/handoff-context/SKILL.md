---
name: handoff-context
description: >-
  Condense conversation context for safe handoff to a new agent session.
  **TRIGGERS ONLY when the user EXPLICITLY asks** to hand off to another agent.
  **DOES NOT trigger automatically at the end of a coding session.**
  Examples that trigger: "handoff to another agent", "pass this to a new agent",
  "handoff to coding agent", "new session with this context".
  Examples that do NOT trigger: "continue", "keep going", "what's next",
  "finish the task", session end, task completion.
---

# Handoff Context — Condense for New Agent Session

## When This Skill Is Triggered

**EXPLICIT USER REQUEST REQUIRED.** This skill does not run automatically.

**Triggers — user explicitly asks:**
- "handoff to another agent"
- "handoff to a new coding agent"
- "pass this to a new agent"
- "handoff to coding agent"
- "handoff to another session"
- "new session with this context"

**Does NOT trigger (do not volunteer):**
- "continue", "keep going", "what's next"
- Task completion, session ending
- "write me a handoff file"
- Any form that could be confused with "handoff to architect"

If the user says "write me a handoff" without specifying who, ask them:
"Should this handoff go to **architect** (architect decisions) or **another coding agent** (coding tasks)?"

---

## What To Extract

Extract these 5 sections from the conversation:

### 1. Project Identity
- Project name and what it does (1-2 sentences)
- Key tech stack (Python asyncio, SQLite WAL, Polymarket CLOB, Hyperliquid OFI)

### 2. Active Work
- What was being done when the handoff happened
- What the last action was
- What results were observed

### 3. Pending Decisions / Blockers
- Any open questions that need architect or user decisions
- Any diagnostic findings waiting for approval
- Any constraints that say "do NOT modify X until Y"

### 4. Key Files Changed
- Files modified during this session
- Files that are newly created
- Any files with known issues

### 5. Diagnostic Logs / Observations
- Any real system output that was observed
- DB query results
- Any data anomalies

---

## Output Format

Write a markdown file called `handoff_<YYYY-MM-DD>.md` in the project root.

**Use this exact header format:**

```markdown
# Agent Handoff — <date>

**From session:** <brief description>
**Active work:** <what was happening>
**Last action:** <what was done last>
**Outcome:** <what was observed>

---

## Project Identity
[2-3 sentences]

## Active Work
[What was happening, what was tried, what failed]

## Pending Decisions
[Numbered list of open questions, with context]

## Key Files Changed
[File: brief description of change]

## Diagnostic Findings
[Any real system output, DB queries, observed data]

## Known Constraints
[Any "do NOT modify until X" rules from architect or user]
```

---

## Critical Rules

### Do NOT
- ❌ Do NOT copy-paste the entire conversation transcript
- ❌ Do NOT include agent reasoning or internal monologue
- ❌ Do NOT include tentative theories that weren't verified
- ❌ Do NOT dump raw JSON logs — summarize key values

### Do
- ✅ Include specific file paths with line numbers when referencing code
- ✅ Include exact error messages, DB counts, log outputs
- ✅ Preserve any "do NOT modify until architect decides" constraints
- ✅ Preserve the exact questions that need answers
- ✅ Keep it under 300 lines

### Handoff Quality Standards (adapted from architect skill)

New agent can decide in 2 minutes — same discipline as architect handoffs:

| Section | Limit |
|---------|-------|
| Prose per section | ≤ 80 words |
| Code snippets | 1–3 snippets, ≤ 20 lines each |
| Total document | ≤ 300 lines |

**If over limit: cut background, not data.**

**Information Asymmetry Protocol:**
- Surface information that would change a ruling, even if it makes prior work look worse
- Do NOT bury the question — put it first
- Give options with a suggested lean

### Interpretation Guidelines

When a new agent reads this handoff:
- Treat it as **authoritative source of truth** about what happened
- The "Pending Decisions" section is the **most important** — start there
- Do NOT repeat work already done — verify before assuming
- If something contradicts what you see in the code, **say so** rather than overriding
- Any constraint like "do NOT modify X until Y" must be respected
- If the handoff mentions a test was passing, run the tests before claiming anything works
