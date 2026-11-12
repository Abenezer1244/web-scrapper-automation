# Asotin County, WA — seven record types (branch `feat/asotin-wa-seven-record-types`)

Research done 2026-11-12 (plain HTTP reads of public pages, no logins, no forms submitted).

## Sources found

| Need | Source | Access | Notes |
|---|---|---|---|
| Recorder index (probate, pre_foreclosure, divorce, death_certificate, NTS recordings) | Helion Digital Research Room `https://pras.asotincountywa.gov/DigitalResearchRoom` | Public disclaimer **gated by Google reCAPTCHA v2** (`g-recaptcha`, sitekey on page) | Same platform as Whatcom (`src/scrapers/whatcom_wa.py`), which has no captcha. Not linked from the county site. Doc-type list not yet seen (behind the captcha). |
| Property enrichment | TrueAutomation PACS `https://propertysearch.trueautomation.com/PropertyAccess/?cid=10` | Public, no captcha | 16-digit 6-6-4 parcel ids. Fits existing `enrichment/pacs_parcel.py` `PacsSite` table + `_PACS_MAILING_COUNTIES`. |
| Auction (trustee_sale) | Lewiston Tribune legal notices, iPublish `nwmarket.com/marketplace/image/notices/<id>/<id>_pdf.pdf` | Public; robots allows (crawl-delay 20); terms page has no scraping clause | Real 2026 Asotin NTS PDFs seen (sale at Asotin County Courthouse). Parse with shared `nts_tacoma_index.parse_nts_notice` like `nts_pdf.py`. |
| Tax delinquent | Treasurer eNoticesOnline `enoticesonline.com/ASO` (per-parcel pay lookup); foreclosure packets on paper at the office; Public Surplus storefront (0 current auctions) | No published delinquent list | Only path is a whole-county per-parcel balance crawl → needs owner approval (broad crawl). |
| Code violation | County: complaint-driven junk ordinance (2 written complaints), ordinance text only. Clarkston: complaints via police dispatch | No case-level public data | Unsupported by available public data. |
| Probate / divorce (court) | Superior Court Clerk: in-person viewing or paid copy request; WA Odyssey portal | Paid/manual; Odyssey not used anywhere in repo | Recorder index is the product's source for these (as for other counties). |
| Death certificate | WA DOH vital records restricted (RCW 70.58A); Digital Archives = historical only | Recorder index only, if Asotin records death certs | Depends on Helion doc types. |

## Status per record type (current)

- probate, pre_foreclosure, divorce, death_certificate — **blocked on decision**: Helion reCAPTCHA.
- trustee_sale — **buildable** from nwmarket NTS PDFs.
- tax_delinquent — **blocked on decision**: no list; per-parcel crawl only.
- code_violation — **unsupported by available public data**.

## Owner decisions (2026-11-12)
- Helion reCAPTCHA: solve with existing 2Captcha helper (King precedent). Live solves only on approved validation runs.
- tax_delinquent: build the rate-limited per-parcel crawl; first broad run needs separate approval.

## Live proofs (read-only)
- nwmarket PDF 155936 → `nts_pdf` + `parse_nts_notice` extract every field (17-digit parcel `10040300800020000`, sale 2026-10-09, principal $303,723.49).
- `pacs_parcel._resolve_one` on Asotin cid=10: that parcel → `found` + mailing; 13-digit prefix → `ambiguous`.

## Codex consult (folded in)
- Phase 1 is ~8 files → split 1a / 1b. Recorder connector rows wait for Phase 2 (no row pointing at an unbuilt class).
- Inactive rows are hidden by registry/API (`registry.py:106,294`, `scrapers.py:437`). Seed `assessor_url` (`enrich.py:1543`).
- `notice_to_row` defaults to Pierce: pass source + county; filter to Asotin BEFORE assigning county. Own listing pagination; pace listing AND PDFs at 20 s; empty listing ≠ extraction failure.
- Keep the 17-digit id as a string; never truncate.
- Whatcom Helion flow drops rows without an APN (`whatcom_wa.py:353`) — measure before copying.
- **No general owner/parcel/mailing hold exists** (only code-violation, `lead_qualification.py:47`). The brief assumed one → Asotin delivery criteria must be defined explicitly (raise with owner before Phase 2).
- Tax source must be in the trust allowlist (`dedup.py:165,224`); needs delinquent-YEAR balances, not just current balance.

## Plan
Phase 1a (≤5 files) — assessor enrichment
- [x] `asotin` PacsSite (cid=10, unrestricted) in `pacs_parcel.py`
- [x] `asotin` in `_PACS_MAILING_COUNTIES` (`county_gis.py`)
- [x] tests: real Asotin results + detail fixtures (owner AND deed-history names redacted); exact 17-digit → found, 13-digit prefix → ambiguous / parcel_mismatch; registry list in `test_gis_mailing_recovery.py` updated
- [x] ruff clean; new assertions run directly and pass
- [ ] pytest run — BLOCKED: needs owner's TEST_DATABASE_URL(+_SYNC) and Redis (reading .env was denied)
- [x] Codex review: 1 Medium (owner name left in mailing cell of fixture) — fixed

Phase 1b (≤5 files) — trustee_sale
- [ ] `src/scrapers/sources/nts_nwmarket.py` (listing → PDF ids, Asotin filter)
- [ ] crawl task in `nts_crawler.py` + `scheduler.py` entry
- [ ] `AsotinWATrusteeSaleScraper` + `asotin` in `NTS_MATCH_COUNTIES`
- [ ] migration 140: Asotin trustee_sale row, `active=false`, `assessor_url` set
- [ ] tests with real PDF fixture

Phase 2 — Helion recorder (probate / pre_foreclosure / divorce / death_certificate) + delivery criteria decision.
Phase 3 — tax_delinquent crawl.

## Review
(to fill in)
