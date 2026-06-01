#!/usr/bin/env node
/**
 * SessionStart hook: surfaces the standing security baseline + Codex-first
 * collaboration workflow into every session's context.
 *
 * Stdout from a SessionStart hook is injected as additionalContext, so this
 * makes the workflow impossible to miss at the start of each session.
 *
 * Wired in .claude/settings.json under hooks.SessionStart.
 */

const lines = [
  "=== BridgeLeads standing workflow (every session, every build) ===",
  "",
  "SECURITY BASELINE — docs/security/ + .claude/rules/security.md (auto-loaded):",
  "  - Run the Master Security Review (SECURITY_PROMPT_PACK.md §14) after every",
  "    meaningful feature / scraper / endpoint. Run it twice — until two passes are clean.",
  "  - Run the Pre-Launch Prompt (§15) before any production deploy.",
  "  - The pack is Abro-tuned (Next.js/Supabase). Apply the stack-translation table",
  "    in .claude/rules/security.md to map it onto FastAPI/Celery/Playwright/Pydantic.",
  "  - Non-negotiables: user_id filter on every query, validate_scraping_target() before",
  "    any user URL (SSRF), sanitize_for_csv() before export, secrets via env only.",
  "",
  "CODEX-FIRST COLLABORATION — .claude/rules/codex-collaboration.md:",
  "  - BEFORE touching any code or starting any build: brainstorm with Codex.",
  "    Use the `codex` skill in consult mode (or `codex` CLI) to pressure-test the",
  "    approach. Reconcile Codex's view with the plan BEFORE writing code.",
  "  - AFTER every build/feature: Codex reviews the diff (`codex review` / challenge).",
  "    Codex is a different model family — it catches what Claude misses, and vice versa.",
  "    Consensus findings take the higher severity; when CLAUDE.md/PRD is silent, Codex wins.",
  "  - Codex CLI is installed and ready (codex-cli).",
  "=================================================================",
];

process.stdout.write(lines.join("\n") + "\n");
process.exit(0);
