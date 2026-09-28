# 2b-ii Phase B: a machine-readable code on the run-refusal 402s (UX audit Q6 / F-035)

Branch `feat/run-eligibility-2b-ii-b` from `origin/main` `9ee0fac9`, worktree
`C:/Users/Windows/bl-wt/eligibility`. Follows Phase A (BE #375, FE #167, both LIVE). Outline:
`tasks/todo-run-eligibility-2b-ii.md` "Phase B".

## Problem
Three 402s that refuse a run are still a bare sentence in `detail`, so the FE cannot tell WHY:
- `POST /jobs` AI monthly limit (`jobs.py:257-261`, `eligibility.code == "ai_limit"`);
- `POST /jobs` account rule, `frozen | ended | over_limit` (same raise);
- `POST /batches` account rule (`batches.py:273-282`, `quota_block_reason`).
FE `toastError` routes every 402 to `toastUpgrade`, which can only give a bare sentence the
neutral "Manage billing" CTA. A frozen account (card failed) is not told "Update payment" in the
toast. Phase A fixed that on the Scrapers page (the row is disabled BEFORE the click), but the
wizard's Start run, the dashboard quick start, a batch, and any stale page still get the toast.

## Facts (verified)
- FastAPI `HTTPException` cannot add top-level body keys beside `detail`; a custom handler can.
  `main.py` registers only a catch-all `Exception` handler (500 + ref id); no HTTPException
  handler, so FastAPI's default serializes `{"detail": ...}`. Tests use `from main import app`
  (`tests/conftest.py:29`), so a handler registered in `main.py` is exercised by the suite.
- Starlette picks the handler by the exception's MRO, so a handler for a SUBCLASS of
  `HTTPException` wins over the default HTTPException handler, and if the handler were ever not
  registered the subclass still degrades to today's exact `{"detail": "<prose>"}` body.
- Entitlement 402s are already structured (`detail` = `{code, title, message}`,
  `entitlements.py:255`) and the 409 run_in_flight is `{code, job_id, message}`; both unchanged.
- Messages today (all under the FE's 160-char leak limit): frozen 156, ended 122, ai_limit 87,
  over_limit 106 (with reset) / 146 (no reset).
- `quota_block_reason` is a thin wrapper over 2b-i `run_eligibility(user, now)` which already
  returns `{can_run, code, message, resumes_at}`; `batches.py` can call `run_eligibility`.
- FE: `readErrorBody` (`lib/api.ts:603`) keeps ONLY an object `detail`; a string `detail` becomes
  the message and every other top-level key is dropped. `toastUpgrade` (`lib/errors.ts:170`)
  treats any detail with a displayable `message` as STRUCTURED and gives it "Upgrade plan", so
  simply exposing `{code: "frozen", message}` through `detail` would make a frozen account see
  "Upgrade plan", worse than today. The CTA must be chosen by code.
- Every toast CTA links to `BILLING_HREF` (`plan-limit-notice.tsx:91`); only the label varies.
- **No API-key client exists today** (read-only prod, 2026-09-28: `users` 7, `api_key_hash`
  set on 0). So no external consumer, strict or permissive, can observe the change now; the web
  FE is the only client, and its parser keeps working before and after (it ignores unknown
  top-level keys). The additive shape still matters for future API-key customers.

## Decision needed from the owner
**A (recommended, additive, non-breaking):** keep `detail` the SAME prose string, byte for byte,
and add top-level siblings: `{"detail": "<prose>", "code": "frozen", "resumes_at": null}`.
API-key clients that read `detail` as a string keep working.
**B (breaking):** `detail` becomes an object like the entitlement 402. One shape, but every
API-key client reading `detail` as a string breaks. Not recommended.
The rest of this plan assumes A.

## BE (PR 1) — 5 source files
1. `src/api/errors.py` (new, small): `RUN_REFUSAL_CODES = ("ai_limit", "frozen", "ended",
   "over_limit")` (the ONE list; the schema `Literal` is tested equal to it) and
   `class RunRefusedHTTPException(HTTPException)` with `code` and `resumes_at`; status 402,
   `detail` = the prose. Plus `async def run_refused_handler(request, exc) -> JSONResponse` whose
   content is `RunRefusalResponse(detail=..., code=..., resumes_at=...).model_dump(mode="json")`
   (Codex P1: a raw `datetime` would make `JSONResponse` raise, a 500 instead of the 402; the
   model dump is the same Pydantic datetime serializer `GET /scrapers` uses, so the two carry the
   identical string), and `headers=exc.headers` preserved.
2. `main.py`: `app.add_exception_handler(RunRefusedHTTPException, run_refused_handler)` next to
   the existing handler. (Starlette resolves by MRO: the subclass handler wins over FastAPI's
   HTTPException handler and the catch-all; endpoint-raised exceptions are handled inside
   `ExceptionMiddleware`, so CORS + security headers still wrap the response. Locked by tests.)
3. `src/api/routes/jobs.py` `enqueue_scrape_job`: the prose raise becomes
   `raise run_refusal_http(code, message, resumes_at)`, a pure helper in `errors.py` that returns
   `RunRefusedHTTPException` when `code in RUN_REFUSAL_CODES` and today's plain prose
   `HTTPException(402, detail=message)` for any other code (none reachable today: run_in_flight
   and not_entitled raise above, config_inactive never gets here), so an unexpected code can
   never become a wrong machine code. Being pure, that fallback is unit-tested directly (Codex
   P3) without faking the evaluator.
4. `src/api/routes/batches.py` step 4: capture `now = datetime.now(UTC)` once, immediately before
   the quota preflight (Codex P1), and call `run_eligibility(current_user, now)` instead of
   `quota_block_reason(current_user)`, raising the same exception (codes `frozen | ended |
   over_limit`). `detail` = `run_eligibility(...).message`, which is exactly the string
   `quota_block_reason` returns today (it is a wrapper).
5. `src/api/schemas.py`:
   - `RunRefusalResponse` (`detail: str`, `code: Literal[...]`, `resumes_at: datetime | None`),
     all required.
   - `EntitlementRefusalDetail` (`code: str`, `title: str`, `message: str`) and
     `EntitlementRefusalResponse` (`detail: EntitlementRefusalDetail`);
   - `PlainRefusalResponse` (`detail: str`) for a 402 that carries only prose (the batch plan
     gate).
   Declared as the 402 response for BOTH `POST /jobs` and `POST /batches` as all three (round 3
   P2: `/jobs` can also return the plain prose shape, through `run_refusal_http`'s
   unexpected-code fallback), via `responses={402: {"model": Union[...]}}`
   (Codex P1: an honest `anyOf` of every shape each route can return). Deliberately `anyOf`,
   not `oneOf` (Codex P2, round 2, option taken: accept the overlap): a run refusal also
   satisfies the plain `{detail: str}` shape. Making them exclusive would need
   `additionalProperties: false` on the plain model, which is exactly the strictness that would
   break a client the day a field is added. The ambiguity is stated in the description
   ("discriminate on the presence of `code`"), and a contract test pins the declared anyOf. The run model's description says the
   top-level `code` / `resumes_at` were ADDED beside the unchanged `detail` and that more
   top-level fields may be added (Codex P2: additive is only non-breaking for clients that
   tolerate unknown keys; the generated schema has no `additionalProperties: false`, and there
   is no other public API doc to update, checked).
6. `schema/openapi.json` regen (`bl-rescat-venv`, `--check`, structural diff: the new
   components and the two `responses` entries only).
Out of scope: `POST /batches` "Batch scrape requires a Pro plan or higher." (a plan gate, not a
run-eligibility code; it keeps the neutral CTA), the change-plan 402 (already structured).

## FE (PR 2, after BE is live) — 3 files
1. `lib/api-types.generated.ts` regen from the BE merge SHA (drift gate).
2. `lib/api.ts`: `StructuredErrorDetail` gains `kind?: "run_refusal"` and
   `resumes_at?: string | null`. `readErrorBody`: when `detail` is a non-empty string AND the
   body has a top-level string `code`, return `detail: {kind: "run_refusal", code, message:
   detail, resumes_at}` (resumes_at only if a string or null). The `kind` marker is PROVENANCE
   (Codex P1): only a top-level run refusal carries it, so an entitlement object whose `code`
   happens to equal a run code can never be routed as one. Object detail (entitlement, 409) and
   bare-string legacy bodies behave exactly as today.
3. `lib/run-refusal.ts` (new, pure, no React; Codex P3): `RunRefusalCode` = the generated
   schema's `RunRefusalResponse["code"]`, and `RUN_REFUSAL_CTA = {...} satisfies
   Record<RunRefusalCode, string>` (round 2 P3: `tsc` fails if the backend adds a code the FE has
   no CTA for) = `frozen` -> "Update payment", `ended` -> "Resubscribe", `ai_limit` /
   `over_limit` -> "Upgrade plan". The Scrapers
   page's `RUN_BLOCK_CTA` (#167) is replaced by this map (not_entitled keeps "Upgrade plan"
   there), so the page and the toast cannot name different fixes.
4. `lib/errors.ts` `toastUpgrade`: FIRST, if `detail.kind === "run_refusal"`: a KNOWN code ->
   its mapped CTA, no title, message = the backend sentence (leak-guarded as today); an UNKNOWN
   code -> the bare-string path ("Manage billing", neutral; Codex P1). Otherwise unchanged:
   entitlement object with message -> "Upgrade plan" (+ title); bare string -> "Manage billing".
Order: BE first (the FE parser ignores the new keys until PR 2; nothing breaks in between).

## Tests
BE, real DB (`bridgeleads_eligibility_test`), each RED on today's code (no top-level `code`):
- `POST /jobs` for `ai_limit` (resumes_at = next UTC month start), `frozen`, `ended`,
  `over_limit` with a reset and without (term ends first -> null): status 402, `detail`
  byte-identical to today's prose, `code`, and the RAW serialized `resumes_at` string (from
  `response.text`, not a parsed datetime; Codex P2) equal to the raw string `GET /scrapers`
  emits for that config on the same pinned clock (parity), including null.
- `POST /batches` for `frozen`, `ended`, `over_limit` (with and without reset): same.
- Entitlement 402 (object detail) and 409 run_in_flight bodies unchanged, and the batch plan
  gate still `{"detail": "Batch scrape requires a Pro plan or higher."}` exactly.
- Handler (Codex P2): the subclass is handled by the new handler (body has `code`), top-level
  keys exactly `{detail, code, resumes_at}`, `exc.headers` preserved (a raise with a header set
  in the test), `application/json`, no stack trace; with an allowed `Origin` the 402 carries
  `Access-Control-Allow-Origin`, `Vary: Origin`, `Access-Control-Expose-Headers` including
  `Retry-After`, a preserved `Retry-After` when the exception set one (Codex P2, round 2), and
  the security headers (the same set a 200 on that route carries).
- `run_refusal_http` with a code outside `RUN_REFUSAL_CODES` returns a plain `HTTPException`
  whose body is today's `{"detail": "<prose>"}` (unit test of the pure helper).
- `RUN_REFUSAL_CODES` equals `get_args` of the schema `Literal`.
- OpenAPI: `POST /jobs` and `POST /batches` 402 = anyOf(run, entitlement, plain); all fields
  required.
FE (no runner; Codex P2 matrix): the scratchpad stub rig drives the REAL wizard Start run, the
dashboard quick start and a batch create against: top-level `frozen`, `ended`, `ai_limit`,
`over_limit` with and without reset, an UNKNOWN top-level code, a nested entitlement object, a
nested object whose `code` is "frozen" (collision), and a bare-string legacy body; asserts the
toast message, CTA label and title presence/absence for each. The Scrapers page rig from #167
is re-run (shared CTA map). tsc, eslint, build, drift check.

## Steps
- [x] Codex plan review until PLAN: GO (round 4).
- [x] Owner confirms: **A, additive** (2026-09-28).
- [x] BE: tests RED -> implement -> GREEN; ruff; full suite (8 parts); openapi regen + diff.
- [x] Security §14.
- [ ] Codex diff review (`origin/main...HEAD`) until GATE: PASS.
- [ ] Quiesce, merge, verify prod. A live 402 cannot be produced read-only (it needs a refused
      account), and prod does not serve `/openapi.json` (`openapi_url` is off when `DEBUG` is
      false; round 3 P3). So: api/worker/beat on the merge SHA, clean boot, api logs free of
      errors on `POST /jobs` / `POST /batches`, the committed `schema/openapi.json` at that SHA,
      and the parity tests; say plainly that the live 402 body itself was not observed.
- [ ] FE PR: build, rig proof, Codex, merge, verify.

## Codex consult
Round 1: NO-GO. Design confirmed (subclass handler wins over both existing handlers). P1s
adopted: serialize via `RunRefusalResponse.model_dump(mode="json")` (raw datetime would 500);
honest `anyOf` of every 402 shape per route; FE `kind: "run_refusal"` provenance + unknown code
-> neutral CTA; one clock in batches. P2s adopted: handler/header/CORS/security-header tests;
additive-fields note in the schema description (no other public API doc exists, checked); raw
`resumes_at` string parity incl. null; the full FE matrix incl. unknown and colliding codes.
P3s adopted: pure `lib/run-refusal.ts` shared with the Scrapers page; `RUN_REFUSAL_CODES` single
list tested against the `Literal`.
Round 2: NO-GO, no P1; round-1 items resolved except as follows, all adopted: `anyOf` kept
deliberately (overlap accepted, stated in the schema, pinned by a contract test; exclusivity
would need `additionalProperties: false`, the very strictness that breaks clients later);
CORS test asserts `Access-Control-Expose-Headers: Retry-After` + a preserved `Retry-After`;
strict-client impact VERIFIED instead of documented: 0 of 7 prod accounts hold an API key;
FE map `satisfies Record<RunRefusalCode, string>`; pure `run_refusal_http` with a unit test
for the unexpected-code fallback.
Round 3: NO-GO, no P1. P2 adopted: `/jobs` 402 declares the plain shape too (the fallback can
return it). P3 adopted: prod verification no longer claims a served OpenAPI (off in prod).
Round 4: **PLAN: GO**, no findings.

## Review
BE built as planned (owner chose A). Files: `src/api/errors.py` (new), `main.py`,
`src/api/schemas.py`, `src/api/routes/jobs.py`, `src/api/routes/batches.py`,
`schema/openapi.json`, `tests/test_run_refusal.py` (new, 17 tests), `tests/conftest.py` +
`tests/test_config_eligibility.py` (the `connectors` fixture moved to conftest so both modules
share it; ruff flagged importing a fixture across test modules).
- RED on unfixed code: 14 of 17 fail for the right reasons (9 HTTP tests: body is only
  `{'detail'}`; 2 OpenAPI: no declared 402; 3 unit: no module). The 3 that pass there are the
  intended regression guards (409 / entitlement shapes unchanged, batch plan gate still a bare
  sentence, CORS + security headers on a 402).
- Mutation-proven: handler unregistered -> 9 fail; unknown codes given a machine code -> 1
  fails; raw datetime in the body -> 4 fail. Files restored byte-identical (cmp).
- The pre-existing 2b-i tests pin `detail` to today's exact sentence on both routes
  (`test_post_jobs_402_carries_todays_prose`, `test_post_batches_402_uses_run_eligibility`)
  and stay green: the byte-identical proof.
- `Retry-After` preservation is proven at the handler (unit test with a header set); no real
  route sets one on these 402s today. The CORS test asserts `Access-Control-Expose-Headers`
  includes it on a real 402.
- ruff exit 0. OpenAPI regen `--check` OK; structural diff vs main = 4 new components
  (`RunRefusalResponse`, `EntitlementRefusalResponse`, `EntitlementRefusalDetail`,
  `PlainRefusalResponse`) + the `402` response on `POST /jobs` and `POST /batches`, nothing else.
- Full suite, 8 parts on `bridgeleads_eligibility_test` (pre-rebase, main `9ee0fac9`):
  **5240 passed, 0 failed** (425 + 654 + 711 + 795 + 811 + 584 + 625 + 635). Part 6 first
  showed 2 failures in `test_rls_isolation.py`, "permission denied for table results /
  delivered_records": the known landmine (roles are cluster-scoped, grants per-DB; the fixture
  grants only on role creation). Verified `has_table_privilege` = false on this DB, applied the
  documented grant, both passed, and the whole of part 6 re-ran clean. Parts 2 and 3 overlapped
  once (a foreground timeout moved part 2 to the background); both passed.
- Rebased onto main `29afc82e` (#379, migration 105, no overlap with these files): test DB
  upgraded to 105; 140 related tests green (incl. #379's new index test, RLS, batches, 2b-i,
  Phase A); ruff exit 0; OpenAPI `--check` OK. CI runs the full suite on the PR merge.
- Security §14: no new input or egress; tenancy unchanged (same evaluator and account rule
  decide; only the response shape changed); the body is exactly three known fields built through
  a Pydantic model, so no stack trace / DB error can reach it (key-set assertions); CORS and
  security headers present on the 402 (test).
