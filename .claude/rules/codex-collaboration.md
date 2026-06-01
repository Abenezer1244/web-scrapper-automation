# Codex Collaboration Rules

**Standing workflow for BridgeLeads. Codex (OpenAI Codex CLI) works alongside Claude on every build.**
Codex CLI is installed locally (`codex-cli`). It is a different model family with different blind spots — that is the entire point: two independent reviewers catch more than either alone.

## 1. Brainstorm with Codex BEFORE touching any code or build
Before writing or modifying code, or starting any build/feature/scraper:
1. Form the approach (use `superpowers:brainstorming` for the design exploration).
2. **Consult Codex** to pressure-test it — invoke the `codex` skill in **consult** mode, or run the `codex` CLI directly. Ask: "Here is the plan to do X in this FastAPI/Celery/Playwright codebase. What breaks? What am I missing?"
3. **Reconcile** Codex's view with the plan before writing any code. If Codex raises a real concern, fold it into the plan. Document material disagreements in `tasks/todo.md`.

Do not start implementation until the brainstorm-with-Codex step has happened for non-trivial work.

## 2. Codex reviews EVERY build
After completing a feature, fix, scraper, or endpoint:
1. Claude self-reviews and runs the security Master Review (`.claude/rules/security.md`).
2. **Codex reviews the diff** — `codex review` (pass/fail gate) and/or `codex challenge` (adversarial). Use the `codex` skill.
3. Compare findings (per `docs/security/security-analyst-agent.md` cross-check doctrine):
   - **Both flag it** → high-confidence; use the **higher** of the two severities.
   - **Only Claude flags it** → finding stands.
   - **Only Codex flags it** → re-read the code; if Codex is right, adopt it at Codex's severity (adjust up, not down, without explicit reasoning).
   - **Disagreement** → if CLAUDE.md / PRD addresses it, the doc wins; if silent, **Codex wins by default**.
4. Any Critical or High finding in **either** reviewer = **NO-GO** until resolved.

## 3. When Claude is stuck
For a second implementation pass, deeper root-cause diagnosis, or to hand off a substantial coding task, use `codex:rescue`.

## How to invoke
- Brainstorm / second opinion: `codex` skill (consult mode) — has session continuity for follow-ups.
- Diff review gate: `codex` skill (review mode) → `codex review`.
- Adversarial break-it pass: `codex` skill (challenge mode).
- Stuck / rescue: `codex:rescue` skill.
