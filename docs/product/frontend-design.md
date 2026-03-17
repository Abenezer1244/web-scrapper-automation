# BridgeLeads — Frontend Design System & UI Spec

---

## Design Direction

**Aesthetic:** Dark command-center. Bloomberg terminal meets modern fintech. Not generic SaaS purple. Every pixel earns its place.

**Tone:** Editorial-utilitarian. Sharp. Data-dense. High-trust.

### Typography

| Role | Font | Usage |
|------|------|-------|
| Display | **Syne** | Headings, numbers, stat values — geometric, strong, financial |
| Body | **DM Sans** | UI text, labels — clean, readable, modern |
| Mono | **DM Mono** | Data, timestamps, IDs, logs — precise, trustworthy |

### Color System

| Role | Value | Usage |
|------|-------|-------|
| Background | `#0a0a0b` | Page base |
| Surface 1 | `#111113` | Sidebar, cards |
| Surface 2 | `#18181c` | Inputs, chips |
| Surface 3 | `#222228` | Hover states |
| Border | `#2a2a32` | Default borders |
| Text primary | `#f0efe8` | Main text |
| Text secondary | `#9998a0` | Labels, meta |
| Text tertiary | `#55545e` | Placeholders, hints |
| **Amber** | `#f5a623` | Primary accent — leads = money |
| Amber dim | `#7a4f08` | Amber borders |
| Amber bg | `#1a1208` | Amber card backgrounds |
| Green | `#22c77a` | Success, records found |
| Red | `#e54b4b` | Errors, failed runs |
| Blue | `#5b9cf6` | Active log lines |

### Design Principles

- Amber = money, urgency, warmth — the lead color in every sense
- Monospace for all data and timestamps — feels precise, auditable
- Green for successful records, amber for running, red for errors — semantic, never decorative
- Dark from day one — investors check leads early morning on dark-themed devices

---

## Tech Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Framework | Next.js 14 (App Router) | Best-in-class React SSR, file-based routing, API routes |
| Styling | Tailwind CSS + CSS variables | Utility-first, consistent token system |
| Components | shadcn/ui (unstyled primitives) | Accessible, customizable, no opinionated styles |
| Data fetching | TanStack Query (React Query) | Caching, background refetch, optimistic updates |
| Real-time | EventSource (SSE) | Live log streaming from FastAPI backend |
| Auth | NextAuth.js | JWT + session, easy provider add-ons |
| Forms | React Hook Form + Zod | Validation, type-safe, wizard state |
| Charts | Recharts | Lightweight, composable |
| Fonts | next/font (Google Fonts) | Syne + DM Sans + DM Mono, zero layout shift |
| Icons | Lucide React | Clean, consistent, tree-shakeable |
| Animation | Framer Motion | Page transitions, micro-interactions |

---

## File Structure

```
proppulse-web/
├── app/
│   ├── (auth)/
│   │   ├── login/page.tsx
│   │   └── register/page.tsx
│   ├── (dashboard)/
│   │   ├── layout.tsx          ← Sidebar + nav shell
│   │   ├── page.tsx            ← Today / home screen
│   │   ├── scrapers/
│   │   │   ├── page.tsx        ← Scraper list
│   │   │   ├── new/page.tsx    ← Wizard (4 steps)
│   │   │   └── [id]/page.tsx   ← Scraper detail + edit
│   │   ├── results/
│   │   │   ├── page.tsx        ← All results table
│   │   │   └── [id]/page.tsx   ← Single run results
│   │   └── live/
│   │       └── [id]/page.tsx   ← Live run view
├── components/
│   ├── ui/                     ← shadcn primitives
│   ├── run-card.tsx
│   ├── stat-card.tsx
│   ├── log-stream.tsx
│   └── live-preview-table.tsx
├── lib/
│   ├── api.ts                  ← FastAPI client (fetch wrappers)
│   ├── types.ts                ← Shared TypeScript types
│   └── utils.ts
├── hooks/
│   ├── use-jobs.ts             ← TanStack Query job hooks
│   ├── use-log-stream.ts       ← SSE log hook
│   └── use-scraper.ts
├── styles/
│   └── globals.css             ← CSS variables, base styles
└── public/
```

---

## Screens

### 1. Today (Home Screen)

**Purpose:** Morning digest. Answers three questions without clicking: did scrapers run, how many leads, where's my download.

**Layout:**
- Top bar: greeting + current date/time
- Stat cards (3 across): New leads today (amber, large) · Scrapers active · Records this month
- Section: Last runs list
- Each run card: status dot + name + time meta + badge + record count + Preview + Download CSV
- Running card: pulsing amber dot, "Watch live →" CTA

**Key interactions:**
- Download CSV: instant, no modal
- Watch live: navigates to `/live/[runId]`
- Click run card: navigates to `/results/[runId]`

---

### 2. New Scraper Wizard (4 Steps)

**Purpose:** Guide any user — technical or not — to their first running scraper in under 3 minutes.

**Step 1 — Source:**
- County picker (searchable dropdown, grouped by state)
- Record type chips: Probate · Pre-foreclosure · Tax delinquent · Divorce · Code violations · Eviction
- "Browse counties" link for unfamiliar users

**Step 2 — Fields:**
- Pre-selected defaults per record type
- Probate defaults: Date recorded · Party name · Heirs/associated · Legal description · Parcel ID
- Enrichment toggles: Parcel lookup (address + mailing) · Skip tracing (Business tier, locked)
- Advanced toggle for custom field mapping

**Step 3 — Schedule:**
- Frequency chips: Manual only · Daily · Weekly · Monthly
- Run time selector (default 6:00 AM)
- Date range mode: Rolling 90 days · Custom range · Since last run
- Summary card showing full config

**Step 4 — Deliver:**
- Email delivery (add addresses)
- Format: CSV · Excel · JSON
- Webhook URL (Business tier)
- CRM push (Business tier, locked with upgrade prompt)
- "Test run" button: runs 1 page, shows sample data before saving

**Wizard state:**
```tsx
type WizardState = {
  step: 1 | 2 | 3 | 4;
  source: { county: string; state: string; recordType: string };
  fields: { selected: string[]; enrichment: string[] };
  schedule: { frequency: string; time: string; rangeMode: string };
  deliver: { emails: string[]; format: string; webhookUrl?: string };
};
```

---

### 3. Live Run View

**Purpose:** Make the scraper feel alive. User watches it work, not a spinner.

**Layout:**
- Header: scraper name + date range + pulsing amber "Running" badge
- Progress card (amber border): "Page 3 of 8" + progress bar + mini stats (pages done · records found · est. remaining)
- Log stream card: monospace, scrolling, color-coded lines (green = success, amber = warning/retry, blue = active step, gray = info)
- Live preview table: last 3 records, updating in real time, "Enriching..." placeholder in amber

**SSE hook:**
```tsx
function useLogStream(runId: string) {
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [progress, setProgress] = useState({ page: 0, total: 0, records: 0 });

  useEffect(() => {
    const es = new EventSource(`/api/runs/${runId}/logs`);
    es.onmessage = (e) => {
      const event = JSON.parse(e.data);
      if (event.type === 'log') setLogs(prev => [...prev, event]);
      if (event.type === 'progress') setProgress(event.data);
    };
    return () => es.close();
  }, [runId]);

  return { logs, progress };
}
```

---

### 4. Results & Export

**Purpose:** Clean data preview, instant download, one-click delivery.

**Layout:**
- Header: scraper name + run time + record count + green "Complete" badge
- Action row: format chips (CSV/Excel/JSON) + Download button (primary) + Send email + Push to CRM
- Filter bar: free-text search + filter dropdowns
- Data table: all columns, hover highlight, sortable headers
- Delivery card at bottom: shows current auto-delivery config

**Columns for Mike's Pierce County probate:**
1. Date recorded
2. Party name
3. Heirs / associated
4. Legal description
5. Parcel ID
6. Property address
7. Mailing address

---

### 5. Settings

**Sections:**
- Account: name, email, password
- Billing: current plan, usage bar, upgrade CTA, invoice history
- API keys: generate, revoke, copy (Business tier)
- Team members: invite, roles (Agency tier)
- Notifications: email alerts for failed runs, daily digest toggle

---

## Component Patterns

### Status Dots
```tsx
// Green = success, amber pulsing = running, red = failed
<span className={cn(
  'w-2 h-2 rounded-full',
  status === 'success' && 'bg-green-500',
  status === 'running' && 'bg-amber-500 animate-pulse',
  status === 'failed' && 'bg-red-500'
)} />
```

### Chips (record type, frequency, format)
```tsx
<button
  onClick={() => toggle(value)}
  className={cn(
    'px-3 py-1.5 rounded-full text-xs border font-mono transition-all',
    selected
      ? 'bg-amber-950 border-amber-700 text-amber-400'
      : 'bg-surface-2 border-border text-text-3 hover:text-text-1'
  )}
>
  {label}
</button>
```

### Run Cards
- Fixed height, consistent layout
- Status dot always leftmost
- Record count in display font, right-aligned
- Actions always visible (never in dropdown)

### Tables
- No zebra striping — hover highlight only
- Monospace for IDs, dates, addresses
- DM Sans for names
- Sticky header on scroll
- `date` column in amber (primary identifier)

---

## API Integration

### FastAPI Client
```tsx
// lib/api.ts
const API = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

export const api = {
  jobs: {
    list: () => fetch(`${API}/jobs`).then(r => r.json()),
    create: (data: JobCreate) => fetch(`${API}/jobs`, { method: 'POST', body: JSON.stringify(data) }),
    get: (id: string) => fetch(`${API}/jobs/${id}`).then(r => r.json()),
    results: (id: string) => fetch(`${API}/jobs/${id}/results`).then(r => r.json()),
    download: (id: string, fmt: string) => fetch(`${API}/jobs/${id}/export?format=${fmt}`),
  },
  scrapers: {
    list: () => fetch(`${API}/scrapers`).then(r => r.json()),
    create: (data: ScraperConfig) => fetch(`${API}/scrapers`, { method: 'POST', body: JSON.stringify(data) }),
  },
};
```

### Key TypeScript Types
```tsx
type Job = {
  id: string;
  scraper_id: string;
  status: 'pending' | 'running' | 'done' | 'failed';
  page_current: number;
  page_total: number;
  record_count: number;
  started_at: string;
  finished_at?: string;
  error?: string;
};

type ScraperConfig = {
  name: string;
  county: string;
  state: string;
  record_type: string;
  fields: string[];
  enrichment: string[];
  schedule: { frequency: string; time: string; range_mode: string };
  deliver: { emails: string[]; format: string; webhook_url?: string };
};

type LogLine = {
  timestamp: string;
  message: string;
  level: 'info' | 'success' | 'warning' | 'error';
};
```

---

## UX Rules (Frontend Enforcement)

- **Download is always one click** — never behind a modal or dropdown
- **Status is always visible** — running indicator on sidebar nav item too
- **Empty states are never blank** — show sample data with "Run your first scraper" CTA
- **Upgrade prompts are visible, not blocking** — show locked features with lock icon + one-line prompt
- **Failure messages are human** — never raw errors, always "X ran into an issue. Our team has been notified."
- **Mobile-first Today screen** — investors check on phone, download button must be thumb-accessible
- **Time to first CSV < 5 minutes** — measure this as the core activation metric

---

## Onboarding Flow

First screen after signup is NOT a blank dashboard. It is:

1. **Pick your county** (searchable, Pierce pre-highlighted for WA users)
2. **Pick record type** (Probate pre-selected, plain-English description: "Heirs inheriting property — motivated sellers")
3. **Run now** (one big amber CTA — skip scheduling, just run it once first)
4. **Watch it live** (auto-navigate to live run view)
5. **Download your leads** (CSV ready, shown in full-screen success state)

Goal: user has leads in hand before they ever see the dashboard.
