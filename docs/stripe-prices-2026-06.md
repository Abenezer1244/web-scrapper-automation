# Stripe pricing migration — 2026-06 ($199 strategy)

Source of truth for the live Stripe Price/coupon IDs created for the new pricing
(see `docs/pricing-strategy-2026-06.md` and `scripts/stripe_pricing_migration_2026_06.py`).
Created on the **live** account (`sk_live_…`) on 2026-06-21.

## Products (reused, unchanged)
| Plan | Product ID |
|---|---|
| Pro | `prod_UANuoAMKafnDJ5` |
| Business | `prod_UANwwzFn0msFok` |
| Agency | `prod_UANxJNomPNWE5l` |

## New Price objects (USD, recurring)
| Env var | Plan / interval | Amount | Price ID |
|---|---|---|---|
| `STRIPE_PRICE_PRO` | Pro monthly | $199 | `price_1TkvRsHE9wT1C7yZvR1vxNOM` |
| `STRIPE_PRICE_PRO_ANNUAL` | Pro annual | $1,910 | `price_1TkvRtHE9wT1C7yZawWGUrla` |
| `STRIPE_PRICE_BUSINESS` | Business monthly | $499 | `price_1TkvRtHE9wT1C7yZqy35QxX3` |
| `STRIPE_PRICE_BUSINESS_ANNUAL` | Business annual | $4,790 | `price_1TkvRtHE9wT1C7yZQHlbQour` |
| `STRIPE_PRICE_AGENCY` | Agency monthly | $1,499 | `price_1TkvRuHE9wT1C7yZDnn1FdGl` |
| `STRIPE_PRICE_AGENCY_ANNUAL` | Agency annual | $14,390 | `price_1TkvRuHE9wT1C7yZAGlJX8b7` |

Set **all six** on BOTH the `api` and the `worker` Railway services.

## Coupon
| Coupon | Percent | Duration | Max redemptions | Status |
|---|---|---|---|---|
| `FOUNDING25` | 25% | forever | 25 | created, valid |
| ~~`8mX1xa35` (FOUNDING40, 40%)~~ | 40% | — | — | **deleted** (0 redemptions) |

## Notes
- Stripe Prices are immutable; the prior stale prices ($79/$758 Pro, $299 Business,
  $799 Agency) remain on the products but are unused. Archive later if desired.
- Before this migration, `STRIPE_PRICE_*` (api) wrongly held **product** IDs
  (`prod_…`), which made checkout fail. Setting the IDs above fixes that.
