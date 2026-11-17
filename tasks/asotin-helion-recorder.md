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

## Owner decisions (2026-11-16)
- Volumes, measured live: Decree Dis 0 in 2025 and 2026, so divorce is dropped. Lis Penden is
  ~2 a year. Death certificates are ~2 a month, but 7 real ones gave 0 leads, because none
  lists a parcel and none of the deceased owns an estate-marked Asotin parcel.
- The owner chose LIS PENDENS ONLY. The death-certificate reader stays in the module, tested
  but not wired.

## Phases
- [x] Phase 1: `asotin_wa_recorder.py`, `captcha.forget_token`, registry, tests. Codex NO-GO
      (inclusive windows, per-party orientation, paging that cannot repeat), fixed, then GO.
- [x] Phase 2: `health.py` `CANARY_ACCEPTS_VALIDATED_EMPTY` and migration 169 (one inactive
      pre_foreclosure row; 165-168 were taken on main).
  - The live runs found that a single result opens `/Document/Details` and that bursts get
    "Too Many Requests". Both are handled.
  - Codex NO-GO: the details page must be this search's redirect, and a block is recognised
    before an empty answer. Fixed, then GO.
  - Live run (2026-01-01..11-15): 2 cards, 2 leads, 0 held, both through the details redirect.
- [ ] Phase 3: the owner switches the connector on.

## Review
- Never a silent zero: every page is bound to its search. An empty answer must echo the search
  it answers. A block, a disclaimer, a skipped or repeated page, or a moved total fails the run.
- Each run costs one 2Captcha solve, plus one per session expiry. The portal rate-limits bursts:
  180-day windows, paced 5 s, with one 60 s backoff.
- One live lead has a 15-digit parcel (Asotin's are usually 17). Downstream enrichment holds it
  if the parcel is not found.
