# BridgeLeads — UX Spec & Information Architecture

---

## Design Philosophy

Three user types, one product — the design must serve all of them through **progressive disclosure**: simple by default, powerful when needed.

- **Mike (wholesaler)** — thinks "did my leads show up this morning?" UI feels like a morning newspaper. Results front and center, config buried.
- **Business user** — thinks in systems. Needs control, visibility, reliability. Multiple scrapers, job status, data trust.
- **Agency user** — thinks in clients. Manages many accounts, usage across clients, white-label experience.

> **Core principle:** Time to first CSV under 5 minutes from sign-up.

---

## Information Architecture

### Top-Level Navigation (5 items)

| Nav Item | Purpose | Primary User |
|----------|---------|--------------|
| **Today** | Morning digest — did my scrapers run, how many leads, download now | Mike / all users |
| **Scrapers** | Create, manage, browse county scrapers | All users |
| **Results** | All results, exports, live run view | All users |
| **Deliver** | Email schedule, webhooks, CRM integrations | Pro + Business |
| **Settings** | Account, billing, API keys, team members | All users |

### Sub-Pages Per Section

- **Today:** Lead digest · Run history · Alerts
- **Scrapers:** My scrapers · New scraper (wizard) · County browser
- **Results:** All results · Exports · Live run view
- **Deliver:** Email schedule · Webhooks · CRM integrations
- **Settings:** Account + billing · API keys · Team members

### Primary User Flow

Sign up → New scraper wizard → Run + watch live → Download CSV → Set schedule

---

## Screen Designs

### Screen 1 — Today (Home Screen)

Answers three questions instantly without clicking:
1. Did my scrapers run successfully last night?
2. How many new leads came in?
3. Where's my download?

**Layout:**

**Top bar:** Greeting + today's date

**Stat cards (3 across):**
- New leads today (large green number, delta vs last run)
- Scrapers active (count + "all ran successfully")
- Records this month (count + remaining on plan)

**Run list — each completed run shows:**
- Colored status dot (green = success, amber pulsing = running)
- Scraper name + date range
- Record count
- Preview button + Download CSV button (primary, always visible)

**Running scraper shows:**
- Pulsing amber dot
- "Running now · Started 6:14 AM · Page 3 of 8"
- "Watch live" button
- Records found so far

---

### Screen 2 — New Scraper Wizard (4 Steps)

Guided flow. Smart defaults at every step. Non-technical users never see raw selectors.

**Step 1 — Source:**
- County picker (searchable dropdown, browsable by state)
- Record type selector (chips: Probate, Pre-foreclosure, Tax delinquent, Divorce, Code violations, Eviction)
- County browser for users who don't know their county

**Step 2 — Fields:**
- Pre-selected defaults for each record type (probate auto-selects: date, party name, heirs, legal desc, parcel ID)
- Enrichment toggles: Parcel lookup (address + mailing addr), Skip tracing (phone/email — Business tier)
- Advanced: custom field mapping (hidden behind "Advanced" toggle)

**Step 3 — Schedule:**
- Frequency chips: Manual only · Daily · Weekly · Monthly
- Run time selector (default: 6:00 AM)
- Date range mode: Rolling 90 days · Custom range · Since last run
- Run summary card showing full config before proceeding

**Step 4 — Deliver:**
- Email delivery toggle (enter addresses)
- Webhook URL field (Business tier)
- CRM push selector (Business tier)
- Format selector: CSV · Excel · JSON

**Wizard principles:**
- Each step fits one screen, no scrolling
- Back button always available
- Summary card on step 3 shows complete config
- "Test run" button before saving (runs 1 page, shows sample data)

---

### Screen 3 — Live Run View

Users should feel like they're watching the scraper work, not staring at a spinner.

**Header:** Scraper name + date range + pulsing "Running" badge

**Progress section:**
- "Page 3 of 8" with progress bar
- Three mini-stats: Pages done · Records found · Est. remaining

**Live log stream (SSE):**
- Monospace font, scrolling
- Color-coded lines: green = success events, amber = warnings/retries, purple = active step
- Shows real events: form submitted, page loaded, records extracted, parcel lookup, retry

**Live preview table:**
- Last 3 records found, updating in real time
- Columns: Date · Party name · Parcel ID · Property addr · Mailing addr
- "Enriching..." placeholder while parcel lookup is in flight

---

### Screen 4 — Results & Export

**Header:** Scraper name + run time + record count + green "Complete" badge

**Action row:**
- Format selector (CSV / Excel / JSON chips)
- Download button (primary, always visible)
- Send to email button
- Push to CRM button

**Filter bar:** Free-text search + filter dropdowns (has heirs, has property addr)

**Data table:** Full results with all columns. Hover highlights rows. Sortable columns.

**Delivery card at bottom:** Shows current auto-delivery config with edit button.

---

## Design System

### Typography

| Role | Size | Weight | Notes |
|------|------|--------|-------|
| Body | 13–14px | 400 | Inter or DM Sans |
| Labels | 12px | 500 | Uppercase, letter-spacing 0.04em |
| Headings | 16–18px | 500 | — |
| Mono | system stack | 400 | Log view, IDs, timestamps |

### Color Palette

| Role | Color | Usage |
|------|-------|-------|
| Primary action | `#534AB7` | Buttons, active states, links |
| Success / leads | `#1D9E75` | Record counts, success badges |
| Running / active | `#BA7517` | In-progress indicators |
| Error / failed | `#E24B4A` | Failed runs, error states |
| Backgrounds | CSS variables | Full dark mode support |

### Component Patterns

- **Status dots:** 10px circle, color-coded, pulsing animation for running state
- **Chips:** Pill-shaped selectors for frequency, format, record type. Selected = purple fill.
- **Cards:** 12px border-radius, 1px border, secondary background. No drop shadows.
- **Buttons:** Primary = purple fill. Secondary = ghost with border. Always labeled.
- **Progress bars:** 6px height, purple fill, rounded ends.
- **Tables:** Zebra-free. Hover row highlight. Sticky header. Sortable columns with subtle arrow.

### Dark Mode

All colors via CSS variables. Full dark mode support from day one — investors often check results early morning on dark-themed devices.

---

## Key UX Decisions

### 1. Onboarding Flow

First screen after signup: "Pick your county" — not a blank dashboard. Guide to first run in under 3 minutes.

1. Pick county (searchable, shows popular: Pierce, King, Snohomish)
2. Pick record type (Probate pre-selected, explained simply: "heirs inheriting property")
3. Run now (one big button)
4. Watch it work (live run view)
5. Download your CSV
6. "Set up daily delivery?" upsell to schedule

**Measure:** Time to first CSV download. Target: under 5 minutes.

### 2. Empty States

Never show a blank screen. Before first run:
- Show a pre-filled example with sample Pierce County probate data
- "This is what your leads will look like" framing
- CTA: "Run your first scraper"

### 3. Failure Communication

When a scraper fails, never show a raw error. Show:

> "Pierce County ran into an issue this morning. Our team has been notified and will investigate within 24 hours. Your last successful run from yesterday is still available to download."

Always include: last successful run link + expected resolution time.

### 4. Mobile Experience

Investors check results on their phone. Mobile priorities:
- Today screen is fully functional on mobile
- Download button always thumb-accessible
- Live run view readable on small screen
- Wizard works on mobile (one step per screen naturally fits)

### 5. Upgrade Prompts

Gate features visually but don't hide them. Show locked features with a lock icon and one-line upgrade prompt:

> "Skip tracing is available on Business plan · Upgrade"

This creates aspiration without blocking the core workflow.

---

## Accessibility

- WCAG AA minimum contrast on all text
- All interactive elements keyboard-navigable
- Status indicators never rely on color alone (always + icon or text)
- Focus rings visible in all states
- Screen reader labels on all icon buttons

---

## Metrics to Track

| Metric | Target | Why |
|--------|--------|-----|
| Time to first CSV | < 5 min | Core activation metric |
| Wizard completion rate | > 70% | Drop-off = UX friction |
| Daily active opens (Today screen) | > 60% of paid users | Habit formation |
| Download click rate | > 80% of completed runs | Are they actually using leads? |
| Schedule setup rate | > 50% of Pro users | Retention driver |
| Support tickets about UI | Trending down | Clarity indicator |

---

## Next Steps

- [ ] High-fidelity mockups for Today screen, Wizard steps 1–4, Live run view, Results screen
- [ ] Component library in Figma (buttons, chips, cards, tables, status indicators)
- [ ] Onboarding flow prototype — test with 3 non-technical users
- [ ] Mobile breakpoint designs for Today + Results screens
- [ ] Empty state designs for all 5 nav sections
- [ ] Error state designs (failed run, partial run, CAPTCHA encountered)
