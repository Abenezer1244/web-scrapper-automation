# BridgeLeads Landing Page — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a premium dark-mode landing page that converts visitors to signups — hero, features, pricing, testimonials, CTA.

**Architecture:** Public marketing page at `/` (exempt from auth middleware). Dashboard moves to `/dashboard`. Uses existing design system (dark theme, Syne + DM Sans fonts, amber accent) enhanced with gradient glows, Framer Motion scroll animations, and bento grid layout.

**Tech Stack:** Next.js 16 (App Router), Tailwind CSS 4, Framer Motion, Lucide React, shadcn/ui components.

**Frontend repo:** `C:\Users\Windows\OneDrive - Seattle Colleges\Desktop\bridgeleads-web`

**UI/UX Spec:** `docs/superpowers/specs/2026-03-23-frontend-ui-ux-design.md`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `middleware.ts` | Modify | Add `/` to public routes |
| `app/(marketing)/layout.tsx` | Create | Marketing layout (no sidebar, public) |
| `app/(marketing)/page.tsx` | Create | Landing page (hero + features + pricing + CTA) |
| `app/(marketing)/pricing/page.tsx` | Create | Standalone pricing page |
| `components/landing/hero.tsx` | Create | Hero section with gradient glow + CTAs |
| `components/landing/nav.tsx` | Create | Sticky nav with scroll-triggered bg |
| `components/landing/features.tsx` | Create | Feature sections with scroll reveal |
| `components/landing/bento-grid.tsx` | Create | Bento grid showcase |
| `components/landing/pricing.tsx` | Create | 3-tier pricing with toggle |
| `components/landing/testimonials.tsx` | Create | Testimonial cards |
| `components/landing/footer.tsx` | Create | Footer with links |
| `components/landing/cta-section.tsx` | Create | Final CTA section |
| `components/landing/how-it-works.tsx` | Create | 3-step flow |
| `app/globals.css` | Modify | Add gradient glow + grain overlay styles |
| `app/(dashboard)/page.tsx` | Existing | Stays at `/` under `(dashboard)` group — route collision fix needed |

---

### Task 1: Route Setup — Make Landing Page Public

**Files:**
- Modify: `middleware.ts`
- Create: `app/(marketing)/layout.tsx`
- Move: Current dashboard from `/` to explicit `/dashboard` route

The current setup has the dashboard `page.tsx` at `app/(dashboard)/page.tsx` which maps to `/`. We need `/` to be the landing page (public) and the dashboard to be at a different route. Since `(dashboard)` is a route group, its `page.tsx` maps to `/` — which conflicts.

- [ ] **Step 1: Update middleware to allow public marketing routes**

In `middleware.ts`, add marketing routes to the public list:

```typescript
// Add to the public routes array:
const publicPaths = ["/login", "/register", "/api/auth", "/", "/pricing"]
```

The middleware should check `pathname === "/"` or `pathname === "/pricing"` to allow unauthenticated access.

- [ ] **Step 2: Create marketing route group layout**

Create `app/(marketing)/layout.tsx`:

```tsx
export default function MarketingLayout({ children }: { children: React.ReactNode }) {
  return <>{children}</>
}
```

- [ ] **Step 3: Create placeholder landing page**

Create `app/(marketing)/page.tsx`:

```tsx
export default function LandingPage() {
  return (
    <div className="min-h-screen bg-bg text-text-primary">
      <h1 className="text-4xl font-display font-bold text-center pt-32">
        BridgeLeads
      </h1>
      <p className="text-text-secondary text-center mt-4">
        Landing page coming soon
      </p>
    </div>
  )
}
```

- [ ] **Step 4: Fix route collision — move dashboard to /dashboard**

Rename `app/(dashboard)/page.tsx` to `app/(dashboard)/dashboard/page.tsx` (or move the "Today" page content). The `(dashboard)` route group's root page needs to not conflict with the marketing `/` page.

Create `app/(dashboard)/dashboard/page.tsx` with the current content of `app/(dashboard)/page.tsx`, then delete the old `app/(dashboard)/page.tsx`.

Update sidebar nav link from `/` to `/dashboard` in `app/(dashboard)/layout.tsx`.

- [ ] **Step 5: Verify routing**

```bash
cd bridgeleads-web && npm run dev
```
- Visit `http://localhost:3000/` → should show landing page (no auth required)
- Visit `http://localhost:3000/dashboard` → should redirect to login (auth required)

- [ ] **Step 6: Commit**

```bash
git add middleware.ts app/(marketing)/ app/(dashboard)/
git commit -m "feat: add marketing route group, move dashboard to /dashboard"
```

---

### Task 2: Global Styles — Gradient Glow + Grain Overlay

**Files:**
- Modify: `app/globals.css`

- [ ] **Step 1: Add landing page utility classes to globals.css**

Append to `app/globals.css`:

```css
/* ─── Landing Page Utilities ──────────────────────────────────────── */

/* Grain overlay for hero sections */
.grain-overlay {
  position: relative;
}
.grain-overlay::after {
  content: "";
  position: absolute;
  inset: 0;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.65' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.12'/%3E%3C/svg%3E");
  background-repeat: repeat;
  opacity: 0.12;
  mix-blend-mode: overlay;
  pointer-events: none;
  z-index: 1;
}

/* Gradient glow blobs */
.glow-blue {
  position: absolute;
  width: 600px;
  height: 600px;
  background: radial-gradient(circle, rgba(91, 156, 246, 0.15) 0%, transparent 70%);
  border-radius: 50%;
  filter: blur(80px);
  pointer-events: none;
}

.glow-amber {
  position: absolute;
  width: 500px;
  height: 500px;
  background: radial-gradient(circle, rgba(245, 166, 35, 0.12) 0%, transparent 70%);
  border-radius: 50%;
  filter: blur(80px);
  pointer-events: none;
}

/* Scroll-triggered fade up (used with Framer Motion) */
.section-padding {
  @apply py-24 px-6 md:px-12 lg:px-20;
}

/* Bento grid */
.bento-card {
  @apply bg-surface-1 border border-border-subtle rounded-2xl p-6
         transition-all duration-300 hover:border-amber/30
         hover:shadow-[0_0_30px_rgba(245,166,35,0.08)] cursor-pointer;
}

/* Pricing card highlight */
.pricing-popular {
  @apply border-amber shadow-[0_0_40px_rgba(245,166,35,0.15)] relative;
}
.pricing-popular::before {
  content: "Most Popular";
  @apply absolute -top-3 left-1/2 -translate-x-1/2 bg-amber text-bg
         text-xs font-semibold px-3 py-1 rounded-full;
}
```

- [ ] **Step 2: Verify styles compile**

```bash
npm run dev
```
No build errors.

- [ ] **Step 3: Commit**

```bash
git add app/globals.css
git commit -m "feat: add gradient glow, grain overlay, bento card styles"
```

---

### Task 3: Sticky Navigation

**Files:**
- Create: `components/landing/nav.tsx`

- [ ] **Step 1: Build the sticky nav component**

Create `components/landing/nav.tsx`:

```tsx
"use client"

import { useState, useEffect } from "react"
import Link from "next/link"
import { motion } from "framer-motion"
import { Menu, X } from "lucide-react"

export function LandingNav() {
  const [scrolled, setScrolled] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 50)
    window.addEventListener("scroll", onScroll)
    return () => window.removeEventListener("scroll", onScroll)
  }, [])

  return (
    <motion.nav
      initial={{ y: -20, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      transition={{ duration: 0.4 }}
      className={`fixed top-0 left-0 right-0 z-50 transition-all duration-300 ${
        scrolled
          ? "bg-bg/80 backdrop-blur-xl border-b border-border-subtle"
          : "bg-transparent"
      }`}
    >
      <div className="max-w-7xl mx-auto flex items-center justify-between px-6 py-4">
        {/* Logo */}
        <Link href="/" className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-lg bg-amber flex items-center justify-center">
            <span className="text-bg font-display font-bold text-sm">BL</span>
          </div>
          <span className="font-display font-semibold text-lg text-text-primary">
            BridgeLeads
          </span>
        </Link>

        {/* Desktop links */}
        <div className="hidden md:flex items-center gap-8">
          <a href="#features" className="text-text-secondary hover:text-text-primary transition-colors text-sm">
            Features
          </a>
          <a href="#pricing" className="text-text-secondary hover:text-text-primary transition-colors text-sm">
            Pricing
          </a>
          <a href="#how-it-works" className="text-text-secondary hover:text-text-primary transition-colors text-sm">
            How It Works
          </a>
        </div>

        {/* CTA buttons */}
        <div className="hidden md:flex items-center gap-3">
          <Link
            href="/login"
            className="text-text-secondary hover:text-text-primary transition-colors text-sm"
          >
            Sign in
          </Link>
          <Link
            href="/register"
            className="btn-amber px-4 py-2 rounded-lg text-sm font-medium"
          >
            Start Free Trial
          </Link>
        </div>

        {/* Mobile hamburger */}
        <button
          onClick={() => setMobileOpen(!mobileOpen)}
          className="md:hidden text-text-secondary hover:text-text-primary"
        >
          {mobileOpen ? <X size={24} /> : <Menu size={24} />}
        </button>
      </div>

      {/* Mobile menu */}
      {mobileOpen && (
        <motion.div
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: "auto" }}
          className="md:hidden bg-surface-1 border-t border-border-subtle px-6 py-4 space-y-3"
        >
          <a href="#features" className="block text-text-secondary hover:text-text-primary text-sm">Features</a>
          <a href="#pricing" className="block text-text-secondary hover:text-text-primary text-sm">Pricing</a>
          <a href="#how-it-works" className="block text-text-secondary hover:text-text-primary text-sm">How It Works</a>
          <hr className="border-border-subtle" />
          <Link href="/login" className="block text-text-secondary text-sm">Sign in</Link>
          <Link href="/register" className="block btn-amber text-center px-4 py-2 rounded-lg text-sm font-medium">
            Start Free Trial
          </Link>
        </motion.div>
      )}
    </motion.nav>
  )
}
```

- [ ] **Step 2: Commit**

```bash
git add components/landing/nav.tsx
git commit -m "feat: add sticky landing nav with scroll effect"
```

---

### Task 4: Hero Section

**Files:**
- Create: `components/landing/hero.tsx`

- [ ] **Step 1: Build the hero section**

Create `components/landing/hero.tsx`:

```tsx
"use client"

import Link from "next/link"
import { motion } from "framer-motion"
import { ArrowRight, Play } from "lucide-react"

export function Hero() {
  return (
    <section className="relative min-h-screen flex items-center justify-center overflow-hidden grain-overlay">
      {/* Gradient glow blobs */}
      <div className="glow-blue -top-40 -left-40" />
      <div className="glow-amber -bottom-40 -right-40" />

      <div className="relative z-10 max-w-5xl mx-auto text-center px-6 pt-32 pb-20">
        {/* Badge */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5 }}
          className="inline-flex items-center gap-2 px-4 py-1.5 rounded-full
                     bg-surface-1 border border-border-subtle text-text-secondary text-xs mb-8"
        >
          <span className="w-2 h-2 rounded-full bg-green animate-pulse" />
          Now scraping 39 WA counties daily
        </motion.div>

        {/* Headline */}
        <motion.h1
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.1 }}
          className="font-display font-extrabold text-5xl sm:text-6xl lg:text-7xl
                     tracking-tight leading-[0.95] text-text-primary"
        >
          Stop Researching.
          <br />
          <span className="text-amber">Start Closing.</span>
        </motion.h1>

        {/* Subheadline */}
        <motion.p
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.2 }}
          className="mt-6 text-lg sm:text-xl text-text-secondary max-w-2xl mx-auto leading-relaxed"
        >
          BridgeLeads scrapes county public records daily and delivers motivated
          seller leads — probate, foreclosure, tax delinquent — straight to your inbox.
        </motion.p>

        {/* CTAs */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.3 }}
          className="mt-10 flex flex-col sm:flex-row items-center justify-center gap-4"
        >
          <Link
            href="/register"
            className="btn-amber px-8 py-3.5 rounded-xl text-base font-semibold
                       inline-flex items-center gap-2 group"
          >
            Start Free Trial
            <ArrowRight size={18} className="group-hover:translate-x-1 transition-transform" />
          </Link>
          <a
            href="#how-it-works"
            className="btn-ghost px-8 py-3.5 rounded-xl text-base font-medium
                       inline-flex items-center gap-2"
          >
            <Play size={16} />
            See How It Works
          </a>
        </motion.div>

        {/* Trust line */}
        <motion.p
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.5, delay: 0.5 }}
          className="mt-12 text-sm text-text-secondary"
        >
          <span className="text-text-primary font-medium">5,200+</span> leads delivered
          <span className="mx-2 text-border-subtle">|</span>
          <span className="text-text-primary font-medium">39</span> WA counties
          <span className="mx-2 text-border-subtle">|</span>
          Updated <span className="text-text-primary font-medium">daily</span>
        </motion.p>
      </div>
    </section>
  )
}
```

- [ ] **Step 2: Commit**

```bash
git add components/landing/hero.tsx
git commit -m "feat: add hero section with gradient glow and CTAs"
```

---

### Task 5: How It Works Section

**Files:**
- Create: `components/landing/how-it-works.tsx`

- [ ] **Step 1: Build the 3-step section**

Create `components/landing/how-it-works.tsx` — a 3-step horizontal flow with numbered steps, icons, and descriptions. Uses Framer Motion scroll-triggered fade-up animation. Steps: "Pick Your Counties" → "We Scrape Daily" → "Download Your Leads".

Each step card: numbered circle, Lucide icon, heading, description. Connected by dashed lines on desktop.

- [ ] **Step 2: Commit**

```bash
git add components/landing/how-it-works.tsx
git commit -m "feat: add how-it-works 3-step section"
```

---

### Task 6: Features Section

**Files:**
- Create: `components/landing/features.tsx`

- [ ] **Step 1: Build alternating 2-column feature sections**

Create `components/landing/features.tsx` — 4 feature blocks, each with heading, description, and a mock screenshot placeholder on alternating sides. Features:
1. Daily Automated Scraping
2. Property & Mailing Address Enrichment
3. "New" Lead Badges
4. Multi-County Scheduling

Uses Framer Motion `whileInView` for scroll-triggered entrance.

- [ ] **Step 2: Commit**

```bash
git add components/landing/features.tsx
git commit -m "feat: add alternating feature sections with scroll reveal"
```

---

### Task 7: Bento Grid Showcase

**Files:**
- Create: `components/landing/bento-grid.tsx`

- [ ] **Step 1: Build bento grid**

Create `components/landing/bento-grid.tsx` — Apple-style varied-size grid showing: record count stat, enrichment preview card, export format options, county map placeholder, scheduling UI, and status dashboard preview. Uses `.bento-card` class from globals.css.

Grid layout: 3 columns on desktop, 2 on tablet, 1 on mobile. Varied heights (`row-span-2` for feature items).

- [ ] **Step 2: Commit**

```bash
git add components/landing/bento-grid.tsx
git commit -m "feat: add bento grid feature showcase"
```

---

### Task 8: Testimonials Section

**Files:**
- Create: `components/landing/testimonials.tsx`

- [ ] **Step 1: Build testimonial cards**

Create `components/landing/testimonials.tsx` — 3 testimonial cards with investor name, role, quote, and results. Dark cards with subtle border. Scroll-triggered stagger animation.

- [ ] **Step 2: Commit**

```bash
git add components/landing/testimonials.tsx
git commit -m "feat: add testimonial cards section"
```

---

### Task 9: Pricing Section

**Files:**
- Create: `components/landing/pricing.tsx`

- [ ] **Step 1: Build 3-tier pricing with annual/monthly toggle**

Create `components/landing/pricing.tsx` — 3 pricing cards (Starter, Pro, Agency). Monthly/Annual toggle (20% discount on annual). Center card highlighted with `.pricing-popular` class. Features list per card (6 key features). CTA button per card linking to `/register?plan=X`.

- [ ] **Step 2: Commit**

```bash
git add components/landing/pricing.tsx
git commit -m "feat: add 3-tier pricing with annual toggle"
```

---

### Task 10: Final CTA + Footer

**Files:**
- Create: `components/landing/cta-section.tsx`
- Create: `components/landing/footer.tsx`

- [ ] **Step 1: Build final CTA section**

Create `components/landing/cta-section.tsx` — Full-width dark section with glow, headline "Start finding motivated sellers today.", single CTA button.

- [ ] **Step 2: Build footer**

Create `components/landing/footer.tsx` — Minimal dark footer. Logo, product links, legal links, copyright.

- [ ] **Step 3: Commit**

```bash
git add components/landing/cta-section.tsx components/landing/footer.tsx
git commit -m "feat: add final CTA section and footer"
```

---

### Task 11: Assemble Landing Page

**Files:**
- Modify: `app/(marketing)/page.tsx`

- [ ] **Step 1: Compose all sections into the landing page**

Update `app/(marketing)/page.tsx` to import and render all sections in order:

```tsx
import { LandingNav } from "@/components/landing/nav"
import { Hero } from "@/components/landing/hero"
import { HowItWorks } from "@/components/landing/how-it-works"
import { Features } from "@/components/landing/features"
import { BentoGrid } from "@/components/landing/bento-grid"
import { Testimonials } from "@/components/landing/testimonials"
import { Pricing } from "@/components/landing/pricing"
import { CtaSection } from "@/components/landing/cta-section"
import { Footer } from "@/components/landing/footer"

export default function LandingPage() {
  return (
    <div className="min-h-screen bg-bg text-text-primary">
      <LandingNav />
      <Hero />
      <HowItWorks />
      <Features />
      <BentoGrid />
      <Testimonials />
      <Pricing />
      <CtaSection />
      <Footer />
    </div>
  )
}
```

- [ ] **Step 2: Test full page**

```bash
npm run dev
```
Visit `http://localhost:3000/` — verify all sections render, scroll animations work, nav goes sticky.

- [ ] **Step 3: Test build**

```bash
npm run build
```
No build errors.

- [ ] **Step 4: Commit**

```bash
git add app/(marketing)/page.tsx
git commit -m "feat: assemble complete landing page with all sections"
```

---

### Task 12: Responsive & Accessibility Pass

**Files:**
- Modify: All `components/landing/*.tsx` files

- [ ] **Step 1: Test at 375px, 768px, 1024px, 1440px**

Verify layout, font sizes, spacing at each breakpoint. Fix any overflow or layout issues.

- [ ] **Step 2: Accessibility checks**

- All images have alt text
- Focus rings visible on all interactive elements
- Color contrast passes WCAG AA (4.5:1)
- `prefers-reduced-motion` respected (Framer Motion respects this by default)
- Touch targets minimum 44x44px

- [ ] **Step 3: Commit fixes**

```bash
git add .
git commit -m "fix: responsive and accessibility improvements"
```

- [ ] **Step 4: Push and deploy**

```bash
git push origin main
```
