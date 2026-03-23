# Dashboard + Records Page Redesign — Implementation Plan (Phase 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the dashboard with KPI sparklines and scraper config cards, build a new cached records page with "NEW" badges, and add the API client for the cached records endpoint.

**Architecture:** The dashboard shows scraper configs as cards (not job runs). Each card links to `/scrapers/[id]/records` which uses the new cached `GET /scrapers/{id}/records` endpoint. "NEW" badges highlight records added since the user's last visit. The existing job-based flow is kept for on-demand runs.

**Tech Stack:** Next.js 16, TanStack Query, Framer Motion, Lucide React, shadcn/ui. Existing design system (dark theme, Syne/DM Sans fonts, amber accent).

**Frontend repo:** `C:\Users\Windows\OneDrive - Seattle Colleges\Desktop\bridgeleads-web`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `lib/types.ts` | Modify | Add `CachedRecord`, `CachedResultsPage` types |
| `lib/api.ts` | Modify | Add `getCachedRecords()` API function |
| `components/scraper-card.tsx` | Create | Scraper config card with new count badge |
| `components/new-badge.tsx` | Create | Reusable "NEW" badge component |
| `components/data-table.tsx` | Create | Sortable, searchable records table |
| `components/kpi-card.tsx` | Create | Enhanced KPI card with sparkline |
| `app/(dashboard)/dashboard/page.tsx` | Modify | Redesign with KPI row + scraper config cards |
| `app/(dashboard)/scrapers/[id]/records/page.tsx` | Create | Cached records page with NEW badges |

---

### Task 1: Types + API Client

**Files:**
- Modify: `lib/types.ts`
- Modify: `lib/api.ts`

- [ ] **Step 1: Add cached record types to `lib/types.ts`**

Append after the `ResultsPage` interface:

```typescript
// ─── Cached records (from county_records cache) ─────────────────────────────
export interface CachedRecord {
  id: string;
  date_recorded: string | null;
  party_name: string | null;
  heirs: string | null;
  doc_type: string | null;
  legal_description: string | null;
  parcel_id: string | null;
  property_address: string | null;
  mailing_address: string | null;
  is_new: boolean;
  scraped_at: string | null;
}

export interface CachedResultsPage {
  config_id: string;
  county: string;
  state: string;
  total: number;
  new_count: number;
  cache_age: string | null;
  cache_stale: boolean;
  page: number;
  page_size: number;
  items: CachedRecord[];
}
```

- [ ] **Step 2: Add `getCachedRecords` to `lib/api.ts`**

Add after the existing `getResults` function:

```typescript
// ─── Cached records (pre-scraped county data) ────────────────────────────────
export async function getCachedRecords(
  configId: string,
  page = 1,
  pageSize = 50,
  q?: string
): Promise<CachedResultsPage> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  if (q) params.set("q", q);
  return apiFetch<CachedResultsPage>(
    `/scrapers/${configId}/records?${params.toString()}`
  );
}
```

Also add the import at the top of api.ts:
```typescript
import type { CachedResultsPage } from "./types";
```

- [ ] **Step 3: Verify**

```bash
npm run build
```

- [ ] **Step 4: Commit**

```bash
git add lib/types.ts lib/api.ts
git commit -m "feat: add CachedRecord types and getCachedRecords API"
```

---

### Task 2: NEW Badge Component

**Files:**
- Create: `components/new-badge.tsx`

- [ ] **Step 1: Create the reusable NEW badge**

Create `components/new-badge.tsx`:

```tsx
import { cn } from "@/lib/utils"

export function NewBadge({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider",
        "bg-blue/15 text-blue border border-blue/30",
        className
      )}
    >
      NEW
    </span>
  )
}
```

Note: Uses the existing `blue` color (`#5b9cf6`) from the design system for the NEW badge — it stands out against the dark bg and amber accent without clashing.

- [ ] **Step 2: Commit**

```bash
git add components/new-badge.tsx
git commit -m "feat: add reusable NEW badge component"
```

---

### Task 3: Scraper Config Card

**Files:**
- Create: `components/scraper-card.tsx`

- [ ] **Step 1: Create the scraper card component**

Create `components/scraper-card.tsx`:

```tsx
"use client"

import Link from "next/link"
import { motion } from "framer-motion"
import { MapPin, FileText, Clock, ChevronRight } from "lucide-react"
import { capitalize, timeAgo } from "@/lib/utils"
import type { ScraperConfig } from "@/lib/types"

interface ScraperCardProps {
  config: ScraperConfig
  newCount?: number
  lastRun?: string | null
  totalRecords?: number
}

export function ScraperCard({ config, newCount = 0, lastRun, totalRecords = 0 }: ScraperCardProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      whileHover={{ scale: 1.01 }}
      transition={{ duration: 0.2 }}
    >
      <Link
        href={`/scrapers/${config.id}/records`}
        className="block rounded-xl border p-5 transition-all duration-200 cursor-pointer
                   hover:border-amber/30 hover:shadow-[0_0_20px_rgba(245,166,35,0.06)]
                   group"
        style={{
          backgroundColor: "var(--color-surface-1)",
          borderColor: "var(--color-border)",
        }}
      >
        <div className="flex items-start justify-between">
          <div className="flex-1 min-w-0">
            {/* County + State */}
            <div className="flex items-center gap-2 mb-1">
              <MapPin className="w-4 h-4 text-amber flex-shrink-0" />
              <h3 className="font-display font-semibold text-text-primary truncate">
                {capitalize(config.county)}, {config.state}
              </h3>
              {newCount > 0 && (
                <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold
                               bg-blue/15 text-blue border border-blue/30 flex-shrink-0">
                  {newCount} NEW
                </span>
              )}
            </div>

            {/* Record type */}
            <div className="flex items-center gap-4 mt-2">
              <span className="flex items-center gap-1.5 text-xs text-text-secondary">
                <FileText className="w-3.5 h-3.5" />
                {capitalize(config.record_type.replace(/_/g, " "))}
              </span>
              {lastRun && (
                <span className="flex items-center gap-1.5 text-xs text-text-secondary">
                  <Clock className="w-3.5 h-3.5" />
                  {timeAgo(lastRun)}
                </span>
              )}
            </div>
          </div>

          {/* Right side — record count + arrow */}
          <div className="flex items-center gap-3 flex-shrink-0 ml-4">
            <div className="text-right">
              <div className="text-lg font-display font-bold text-text-primary">
                {totalRecords.toLocaleString()}
              </div>
              <div className="text-[11px] text-text-secondary">records</div>
            </div>
            <ChevronRight className="w-5 h-5 text-text-secondary group-hover:text-amber transition-colors" />
          </div>
        </div>
      </Link>
    </motion.div>
  )
}
```

- [ ] **Step 2: Commit**

```bash
git add components/scraper-card.tsx
git commit -m "feat: add scraper config card with new count badge"
```

---

### Task 4: Redesign Dashboard Page

**Files:**
- Modify: `app/(dashboard)/dashboard/page.tsx`

- [ ] **Step 1: Rewrite the dashboard page**

Read the current `app/(dashboard)/dashboard/page.tsx` first. Then rewrite it to show:

1. **Header row:** greeting + date + "New Scraper" button (keep existing)
2. **KPI row (4 cards):**
   - Total Leads (sum of all cached records across configs)
   - New Today (sum of new_count from cached results)
   - Active Scrapers (count of active configs)
   - Records This Month (from usage endpoint)
3. **Scraper Configs grid:** Replace the "Recent runs" job list with a grid of `ScraperCard` components. Each card shows county, record type, total records, new count, and links to `/scrapers/[id]/records`.

The key change: fetch cached record counts per scraper config. For each config, call `getCachedRecords(config.id, 1, 1)` (page_size=1 to just get counts) — or better, create a lightweight endpoint. For now, we'll show the data from the scrapers list + jobs.

Actually, the simplest approach: Keep the dashboard largely the same structure, but replace the "Recent runs" job list with scraper config cards. Each card uses data already available from `listScrapers()` + `listJobs()`. The actual record counts and "new" badges will show when the user clicks into the records page.

Redesigned dashboard:
```tsx
// Header: greeting + date + "New Scraper" button
// KPI row: 3 stat cards (same as now but with font-display)
// Scrapers section: grid of ScraperCard components
// Recent activity: last 3 job runs (collapsed)
```

Import `ScraperCard` and render a grid of user's scraper configs. For each config, find the latest done job to show "last run" time and record count.

- [ ] **Step 2: Verify**

```bash
npm run build
```

- [ ] **Step 3: Commit**

```bash
git add app/(dashboard)/dashboard/page.tsx
git commit -m "feat: redesign dashboard with scraper config cards"
```

---

### Task 5: Cached Records Page

**Files:**
- Create: `app/(dashboard)/scrapers/[id]/records/page.tsx`

- [ ] **Step 1: Build the records page**

Create `app/(dashboard)/scrapers/[id]/records/page.tsx`:

This is the core page where users see their leads. Key elements:

**Header:**
- Back link to /dashboard
- County name + state (large, font-display)
- Record type badge
- Cache age indicator ("Updated 2h ago" or "Updated today")
- Total records count + new count with NEW badge

**Search + Filters bar:**
- Search input (text-text-primary, bg-surface-1, border-border-subtle)
- Debounced search (300ms) that calls getCachedRecords with `q` param

**Data Table:**
- Columns: NEW badge | Date | Party Name | Doc Type | Parcel ID | Property Address | Mailing Address
- Rows with `is_new: true` get a subtle left border highlight in blue
- The NEW badge column shows the `NewBadge` component for new records
- Pagination at bottom (page/page_size from API response)
- Loading: skeleton shimmer rows
- Empty state: "No records yet. This county will be scraped tonight."

**Implementation:**
```tsx
"use client"

import { useState } from "react"
import { useParams, useRouter } from "next/navigation"
import { useSession } from "next-auth/react"
import { useQuery } from "@tanstack/react-query"
import { motion } from "framer-motion"
import { ArrowLeft, Search, Download, MapPin } from "lucide-react"
import Link from "next/link"
import { getCachedRecords, listScrapers } from "@/lib/api"
import { capitalize } from "@/lib/utils"
import { NewBadge } from "@/components/new-badge"

export default function CachedRecordsPage() {
  const { id } = useParams<{ id: string }>()
  const { data: session } = useSession()
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState("")
  const [debouncedSearch, setDebouncedSearch] = useState("")
  const pageSize = 50

  // Debounce search
  // ... (useEffect with 300ms timeout)

  // Fetch scraper config for county/state info
  const { data: scrapers } = useQuery({
    queryKey: ["scrapers"],
    queryFn: listScrapers,
    enabled: !!session,
  })
  const config = scrapers?.find(s => s.id === id)

  // Fetch cached records
  const { data, isLoading } = useQuery({
    queryKey: ["cached-records", id, page, debouncedSearch],
    queryFn: () => getCachedRecords(id, page, pageSize, debouncedSearch || undefined),
    enabled: !!session && !!id,
  })

  // ... render header, search, table, pagination
}
```

The full implementation should include:
- Debounced search with useEffect
- Pagination controls (prev/next buttons, page indicator)
- Sortable column headers (client-side sort for current page)
- Download/export button (links to existing export endpoint or future cached export)
- Responsive: table scrolls horizontally on mobile
- Each row: hover state with subtle bg change
- NEW rows: left border accent in blue

- [ ] **Step 2: Verify**

```bash
npm run build
```

- [ ] **Step 3: Commit**

```bash
git add app/(dashboard)/scrapers/[id]/records/page.tsx
git commit -m "feat: add cached records page with NEW badges and search"
```

---

### Task 6: Update Sidebar Nav

**Files:**
- Modify: `app/(dashboard)/layout.tsx`

- [ ] **Step 1: Add "Records" nav item that points to scraper selection**

The sidebar already has "Results" pointing to `/results` (old job-based results). Keep it but also consider updating the label or adding a new entry. For now, the scraper cards on the dashboard are the primary entry point to records.

No change needed — the ScraperCard already links to `/scrapers/[id]/records`.

- [ ] **Step 2: Commit (if changes made)**

---

### Task 7: Build + Push

**Files:**
- All modified files

- [ ] **Step 1: Full build check**

```bash
cd bridgeleads-web && npm run build
```

- [ ] **Step 2: Push**

```bash
git push origin main
```
