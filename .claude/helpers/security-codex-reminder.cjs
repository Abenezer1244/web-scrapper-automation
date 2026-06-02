#!/usr/bin/env node
/**
 * SessionStart hook: surfaces, into every session's context,
 *   1. the SECURITY-FIRST baseline (security is prioritized on every build),
 *   2. the Codex-first collaboration workflow, and
 *   3. the latest docs/BUILD_JOURNAL.md entry (so it is "read" at session start).
 *
 * Stdout from a SessionStart hook is injected as additionalContext.
 * Wired in .claude/settings.json under hooks.SessionStart.
 */
const fs = require("fs");
const path = require("path");

// Repo root = two levels up from .claude/helpers/ (robust regardless of cwd).
const repoRoot = path.resolve(__dirname, "..", "..");

function latestJournalEntry() {
  const journal = path.join(repoRoot, "docs", "BUILD_JOURNAL.md");
  try {
    const lines = fs.readFileSync(journal, "utf8").split(/\r?\n/);
    const start = lines.findIndex((l) => /^## /.test(l)); // first dated entry (newest)
    if (start === -1) return "(BUILD_JOURNAL.md has no entries yet)";
    let end = lines.findIndex((l, i) => i > start && /^## /.test(l));
    if (end === -1) end = lines.length;
    let entry = lines.slice(start, end);
    if (entry.length > 70) entry = entry.slice(0, 70).concat("…(truncated — read docs/BUILD_JOURNAL.md for the full entry)");
    return entry.join("\n");
  } catch {
    return "(docs/BUILD_JOURNAL.md not found — create it; see CLAUDE.md step 8)";
  }
}

const lines = [
  "=== BridgeLeads standing workflow (every session, every build) ===",
  "",
  "SECURITY IS PRIORITIZED ON EVERY BUILD — docs/security/ + .claude/rules/security.md (auto-loaded):",
  "  - Run the Master Security Review (SECURITY_PROMPT_PACK.md §14) after every feature/scraper/endpoint;",
  "    run the Pre-Launch Prompt (§15) before any production deploy. Run §14 until two passes are clean.",
  "  - Apply the stack-translation table in .claude/rules/security.md (the pack is Abro-tuned → FastAPI/Celery).",
  "  - Non-negotiables: user_id filter on every query, validate_scraping_target() before any user URL (SSRF),",
  "    sanitize_for_csv() before export, secrets via env only, no secret values in code/commits/docs.",
  "",
  "CODEX-FIRST — .claude/rules/codex-collaboration.md:",
  "  - BEFORE touching any code/build: brainstorm with Codex (consult). AFTER every build: Codex reviews the diff.",
  "  - Consensus takes the higher severity; any Critical/High from either reviewer = NO-GO until fixed.",
  "",
  "LATEST BUILD JOURNAL ENTRY (docs/BUILD_JOURNAL.md — read this before starting; append a new entry at session end):",
  "----------------------------------------------------------------",
  latestJournalEntry(),
  "----------------------------------------------------------------",
  "=================================================================",
];

process.stdout.write(lines.join("\n") + "\n");
process.exit(0);
