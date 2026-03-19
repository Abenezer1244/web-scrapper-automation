# CAPTCHA Solving for ATIP Enrichment — Plan

## How ATIP's reCAPTCHA works
1. Page loads reCAPTCHA v2 with sitekey: `6Lcv5V0qAAAAADbB5-O6mhR9xb5q294gpfvabKcT`
2. User solves the CAPTCHA, gets a token
3. Every API call includes `recaptcha-response: <token>` header
4. API endpoint: `GET /api/publicRecaptcha?filter={"criteria":[{"eq":["parcelNumber","XXXX"]}]}`
5. Returns JSON with parcel data (property address, mailing address)

## Implementation

### Step 1: Add 2Captcha dependency + settings
- Add `twocaptcha-python` to requirements.txt
- Add `CAPTCHA_API_KEY` to settings.py + .env.example
- Add `CAPTCHA_ENABLED` flag (default: false)

### Step 2: Create captcha solver module
- `src/scrapers/enrichment/captcha.py`
- `async def solve_recaptcha(site_url, sitekey) -> str` — returns token
- Sends to 2Captcha API, polls for result (~10-20s)
- Caches token for reuse (tokens valid for ~2 minutes)

### Step 3: Update ATIP enrichment to use solved token
- Update `_enrich_pierce_api()` in `parcel.py`
- Get a solved reCAPTCHA token via 2Captcha
- Pass as `recaptcha-response` header
- Cache token across multiple parcel lookups (reuse until expired)

### Cost
- 2Captcha: $2.99 per 1000 solves
- One token can be reused for ~2 minutes → ~20-30 lookups per token
- 300 records ÷ 25 per token = ~12 solves = $0.04 per job
