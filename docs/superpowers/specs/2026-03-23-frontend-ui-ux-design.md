# BridgeLeads — Frontend UI/UX Design Spec

**Date:** 2026-03-23
**Status:** Draft
**Repo:** `bridgeleads-web` (Next.js 16, shadcn/ui, Framer Motion, TanStack Query)

---

## Design Vision

**"The Linear of Real Estate Lead Gen"** — premium dark-mode SaaS that makes every competitor look like a generic template. No PropTech company does dark mode well. BridgeLeads owns this space.

**Key Differentiators:**
- Dark-first design (OLED-optimized) with aurora glow accents
- Instant data delivery (cached records, no loading spinners)
- "New" badges that make users feel like they're getting fresh intel
- Bento grid feature showcase on landing page
- Micro-interactions on everything (not decorative — purposeful)

---

## Color System

### Dark Theme (Primary)

```css
:root {
  /* Backgrounds */
  --bg-primary:     #0A0F1A;    /* Page background — near-black navy */
  --bg-surface:     #111827;    /* Cards, panels */
  --bg-elevated:    #1E293B;    /* Hover states, elevated cards */
  --bg-overlay:     rgba(0, 0, 0, 0.6); /* Modal overlays */

  /* Brand */
  --brand-primary:  #0EA5E9;    /* Sky blue — trust, data, links */
  --brand-accent:   #F97316;    /* Orange — CTAs, action, urgency */
  --brand-glow:     rgba(14, 165, 233, 0.15); /* Blue ambient glow */
  --accent-glow:    rgba(249, 115, 22, 0.15);  /* Orange ambient glow */

  /* Text */
  --text-primary:   #F8FAFC;    /* Headlines, primary content */
  --text-secondary: #94A3B8;    /* Labels, metadata, descriptions */
  --text-muted:     #64748B;    /* Disabled, hints */

  /* Borders */
  --border-default: #1E293B;    /* Card borders */
  --border-hover:   #334155;    /* Hover state borders */

  /* Status */
  --status-success: #22C55E;    /* Done, active, healthy */
  --status-warning: #F59E0B;    /* Queued, pending */
  --status-error:   #EF4444;    /* Failed, error */
  --status-info:    #0EA5E9;    /* Scraping, processing */
  --status-enrich:  #A78BFA;    /* Enriching */

  /* "New" badge */
  --badge-new-bg:   rgba(14, 165, 233, 0.15);
  --badge-new-text: #38BDF8;
  --badge-new-border: rgba(14, 165, 233, 0.3);
}
```

### Light Theme (Secondary — accessible alternative)

```css
.light {
  --bg-primary:     #FAFAFA;
  --bg-surface:     #FFFFFF;
  --bg-elevated:    #F1F5F9;
  --text-primary:   #0F172A;
  --text-secondary: #475569;
  --border-default: #E2E8F0;
}
```

---

## Typography

### Font Stack

```css
/* Primary: Inter — the standard for premium SaaS */
--font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;

/* Monospace: JetBrains Mono — for parcel IDs, job IDs, data */
--font-mono: 'JetBrains Mono', 'Fira Code', monospace;
```

### Type Scale

| Element | Weight | Size | Letter Spacing | Line Height |
|---------|--------|------|---------------|-------------|
| Hero H1 | 800 | 72px (4.5rem) | -0.04em | 0.95 |
| Page H1 | 700 | 48px (3rem) | -0.03em | 1.1 |
| Section H2 | 600 | 32px (2rem) | -0.02em | 1.2 |
| Card H3 | 600 | 20px (1.25rem) | -0.01em | 1.3 |
| Body | 400 | 16px (1rem) | 0 | 1.6 |
| Small/Label | 500 | 13px (0.8125rem) | 0.04em | 1.4 |
| Mono Data | 400 | 14px (0.875rem) | 0 | 1.5 |

---

## Pages

### 1. Landing Page (`/`)

**Purpose:** Convert visitors to signups. Premium, dark, confident.

**Structure:**

```
[Sticky Nav]
  Logo | Features | Pricing | Counties | [Sign in] [Start Free Trial]

[Hero Section]
  Dark bg + grain overlay + dual-glow (blue left, orange right)
  H1: "Stop Researching. Start Closing."
  Sub: "BridgeLeads scrapes county public records daily and delivers
        motivated seller leads — probate, foreclosure, tax delinquent
        — straight to your inbox."
  [Start Free Trial] (orange filled)  [See How It Works] (ghost)
  Trust line: "5,200+ leads delivered • 39 WA counties • Updated daily"

[Logo Strip]
  "Trusted by investors in:" + city/community logos

[How It Works — 3 Steps]
  1. Pick Your Counties → screenshot of county picker
  2. We Scrape Daily → animated status cards
  3. Download Your Leads → CSV export preview

[Feature Sections — Alternating 2-col]
  - Daily Automated Scraping (screenshot + description)
  - Property & Mailing Address Enrichment (before/after card)
  - "New" Lead Badges (show the badge UI)
  - Multi-County Scheduling (calendar UI)

[Bento Grid Showcase]
  Apple-style grid: record counts, enrichment preview, export formats,
  county map, scheduling UI, status dashboard

[Testimonials]
  3 cards with investor photos, names, quotes, results

[Pricing]
  3 tiers: Starter / Pro / Agency
  Monthly/Annual toggle
  Center tier highlighted with orange border + "Most Popular"

[Final CTA]
  Full-width dark section with glow
  "Start finding motivated sellers today."
  [Start Free Trial]

[Footer]
  Minimal, dark. Logo, links, legal.
```

**Key Animations:**
- Hero: Subtle gradient animation (12s loop, barely visible)
- Scroll reveal: Elements fade up (translateY 24px → 0, 400ms)
- Bento cards: Hover scale(1.02) + border glow
- Numbers: Count-up animation on scroll into view
- Nav: Transparent → solid bg on scroll

### 2. Dashboard (`/dashboard`)

**Purpose:** At-a-glance overview of all scraper configs and recent activity.

**Layout:**
- Left sidebar (240px, collapsible): Logo, nav links, user menu
- Top: breadcrumb + "New Scraper" button
- Content: KPI row + scraper config cards

**KPI Row (4 cards):**
- Total Leads This Month (count + sparkline)
- Active Scrapers (count + green dot)
- New Leads Today (count + "NEW" badge)
- Enrichment Rate (percentage + progress ring)

**Scraper Config Cards:**
Each config shows: county name, state, record type, last run, record count, status badge, "View Records" button, new count badge.

### 3. Records Page (`/scrapers/[id]/records`)

**Purpose:** Show cached records with "new" badges. This is the money page.

**Layout:**
- Header: County name, record type, cache age, total/new counts
- Filters: Search bar, date range, record type dropdown
- Table: Sortable columns with "NEW" badges

**Table Columns:**
| NEW | Date | Party Name | Doc Type | Parcel ID | Property Address | Mailing Address | Actions |

**"NEW" Badge Design:**
```
Sky blue pill: bg-sky-500/15 text-sky-400 border border-sky-500/30
Text: "NEW" in 11px uppercase Inter 600
Appears left of the date column
Fades out on second visit (per-user tracking)
```

**Empty State:** "No records yet. This county will be scraped tonight at 2 AM UTC."

### 4. New Scraper Wizard (`/scrapers/new`)

**Purpose:** 4-step wizard to create a scraper config.

**Steps:**
1. **County** — State dropdown → County dropdown (with search)
2. **Record Types** — Checkbox grid: probate, foreclosure, tax delinquent, etc.
3. **Schedule** — Frequency selector: daily/weekly/manual + time picker
4. **Review & Create** — Summary card with all selections

**Design:** Horizontal stepper, each step is a card. Progress bar at top. "Back" ghost button, "Next" filled button.

### 5. Live Job Page (`/live/[id]`)

**Purpose:** Real-time streaming of job progress.

**Layout:**
- Status hero: large status badge + progress bar
- Log stream: SSE-powered terminal-style log viewer
- Stats: records found, pages scraped, time elapsed

### 6. Settings (`/settings`)

**Purpose:** Account settings, billing, API keys.

**Sections:** Profile, Plan & Billing, API Keys, Delivery (email), Notifications

### 7. Auth Pages (`/login`, `/register`)

**Design:** Centered card on dark gradient background. Logo above. Social login options if applicable. Minimal fields.

---

## Component Library

### Shared Components

| Component | Description | shadcn Base |
|-----------|-------------|-------------|
| `StatusBadge` | Colored pill: DONE/SCRAPING/FAILED/etc | Badge |
| `NewBadge` | Sky blue "NEW" pill for fresh records | Badge variant |
| `StatCard` | KPI with number, label, sparkline | Card |
| `DataTable` | Sortable, searchable, paginated | Table |
| `Sidebar` | Collapsible nav with icon + label | Sheet/custom |
| `StepWizard` | Multi-step form with progress | Tabs/custom |
| `LogStream` | Terminal-style SSE log viewer | ScrollArea |
| `CountyPicker` | State → County cascading select | Select + Command |
| `EmptyState` | Friendly illustration + CTA | Card |
| `LoadingSkeleton` | Shimmer animation for loading | Skeleton |

### Animation Patterns (Framer Motion)

```tsx
// Scroll reveal — use on every section
const fadeUp = {
  initial: { opacity: 0, y: 24 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.4, ease: "easeOut" },
};

// Card hover
const cardHover = {
  whileHover: { scale: 1.02, boxShadow: "0 0 30px rgba(14,165,233,0.15)" },
  transition: { duration: 0.2 },
};

// Stagger children
const stagger = {
  animate: { transition: { staggerChildren: 0.08 } },
};

// Number count-up
const countUp = { from 0, to target, duration 1.5s, on scroll-into-view };
```

---

## Responsive Breakpoints

| Breakpoint | Width | Sidebar | Layout |
|-----------|-------|---------|--------|
| Mobile | < 768px | Hidden (hamburger) | Single column |
| Tablet | 768-1024px | Icon-only (64px) | 2-column grid |
| Desktop | 1024-1440px | Full (240px) | 3-column grid |
| Wide | > 1440px | Full (240px) | max-w-7xl centered |

---

## Icon System

- **Lucide React** (already in the project)
- 24x24 viewBox, `w-5 h-5` default size
- `text-secondary` color, `text-primary` on hover
- NO emojis as icons

---

## Accessibility

- All interactive elements: visible focus ring (`ring-2 ring-brand-primary ring-offset-2 ring-offset-bg-primary`)
- Color is never the only indicator (icons + color for status)
- `prefers-reduced-motion`: disable all animations
- Minimum touch target: 44x44px
- All images: descriptive `alt` text
- Form inputs: visible `<label>` elements
- WCAG AA contrast minimum (4.5:1 text, 3:1 UI elements)

---

## Tech Stack Confirmation

| Layer | Choice |
|-------|--------|
| Framework | Next.js 16 (App Router) |
| Styling | Tailwind CSS 4 |
| Components | shadcn/ui |
| Animation | Framer Motion |
| Data Fetching | TanStack Query |
| Auth | NextAuth v5 |
| Icons | Lucide React |
| Charts | Recharts (for sparklines/KPIs) |
| Font | Inter (Google Fonts) + JetBrains Mono |
