# Asotin recorder (Helion) — divorce, death_certificate, pre_foreclosure

Owner decision 2026-11-16: the 2Captcha route. The recorder portal's disclaimer carries a
reCAPTCHA v2 (sitekey 6LcAudwsAAAAAKt72O-7gSUlQb-fhs_d_HiRiLR1). The disclaimer text holds
no automated-access prohibition. One owner-approved probe established the facts below.

## Facts (live probe, 2026-11-16)
- Portal: https://pras.asotincountywa.gov/DigitalResearchRoom/ (Helion, the same platform as
  whatcom_wa.py). GET / redirects to /Disclaimer.
- One solve + submit unlocks the session; Advanced search and paging then need no captcha.
- The date inputs are `type=date` (ISO yyyy-mm-dd).
- `Filter_DocumentSubtype` filters server-side: DEATH CERTIFICATE=109, Decree Dis=21,
  Lis Penden=39.
- Result cards (`.search-result`) carry:
  - DIRECT:/INDIRECT: party lines;
  - Doc Type + subtype;
  - `Tax Parcel # <17 digits>`, or SEE ATTACHED, or several parcels;
  - Date Recorded;
  - "Next" paging.

## Codex consult (2026-11-16) — folded in
- Invalidate the solver's 100 s token cache around each disclaimer submission.
- On session expiry (a redirect to the disclaimer) during a search or a page: re-solve within a
  bound and replay the same page.
- Fail loud on failed chunks, repeated pages, page-limit exhaustion, and empty results without
  explicit empty evidence. Do not inherit Whatcom's skip-and-continue.
- pre_foreclosure = subtype 39 only, labelled Lis Pendens. Not Notice / Notice of Intent /
  TRUSTEE'S SALE (103) yet.
- divorce = subtype 21, with a local exact rule. Confirm its marital meaning on a saved record.
- Every parcel kept as a string. Several parcels: one record per instrument/parcel, with the
  full list in metadata. SEE ATTACHED: no invented APN, and provenance kept.
- Migration: the next free revision (head is 164). Three INACTIVE rows with lowercase state.
  The legacy ['probate','pre_foreclosure'] row stays untouched. Seed per record type, so the
  existence guard cannot suppress rows two and three.
- Fixtures: expired session, rejected token, solving disabled, real empty, repeated page,
  multi-parcel. Redacted; synthetic names only.

## Phases (<= 5 files each)
- [ ] Phase 1 — scraper `src/scrapers/asotin_wa_recorder.py`, `registry.py` allowlist,
      `doc_types.py` Asotin tokens, `tests/test_asotin_recorder.py`, and the redacted fixtures.
- [ ] Phase 2 — migration 165 (three inactive connectors) and a read-only live run of each type
      (counts only); then Codex review, journal, PR, deploy.
- [ ] Phase 3 — owner switches the types on.

## Review
(to fill in)
