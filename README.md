# M&A Acquirer Identification Engine

**Status: Phase 2 — CSV ingestion, Target Profile form, and deterministic
acquirer matching/scoring.** The agentic layer (LLM-synthesized rationale per
acquirer — the take-home's core deliverable) is not built yet; this phase
produces the structured, pre-scored candidate list it will reason over.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 run.py
```

Opens at `http://127.0.0.1:8000`. The sample dataset (`data/ma_transactions_500.csv`)
loads automatically on startup — no setup required.

## What's here

**CSV ingestion** (`backend/data_processing.py`)
Validates an uploaded (or default) CSV against the expected 24-column schema —
exact column names, numeric columns, and that `sector`/`deal_size_mm` have no
missing values. Any mismatch fails fast with a specific message (which columns,
which rows, which values) rather than a stack trace or a silently-wrong result.

For each row it then parses:
- **Categorical fields** (`sector`, `geography`, `target_ownership_pre`) into
  their distinct values. `geography` gets an extra `"Regional"` option that
  isn't in the raw data — the docx's own target profile describes the company
  as "regional," a scope the dataset's specific region names (Midwest,
  Southeast, ...) don't capture on their own.
- **Numeric fields** (`deal_size_mm`, `target_revenue_mm`, `target_ebitda_mm`,
  `ebitda_margin_pct`, `revenue_growth_pct`) into five bands **per sector**
  (Low / Low-Mid / Mid / Mid-High / High), split at that sector's own 20/40/60/80
  percentiles and rounded to clean numbers (e.g. `147.9` → `150`). Banding per
  sector matters because the fields vary by an order of magnitude across
  sectors — a $200M deal is large for Dental and small for Pharma/Biotech, so
  a global band would be meaningless for either. Whichever band contains the
  sector's median value is labeled `(Median)` so it reads clearly in the
  dropdown and in the resolved profile.

**Target profile form** (`frontend/`)
`sector` (dropdown) and `deal_size` (free numeric $mm) are required; everything
else is optional. Deal size is typed directly rather than banded, since a
banker knows the number — the sector's bands still run in the background
(shown as a range/median hint under the field, e.g. "Healthcare Services deals
range $20M–$2.5B · median $339M") and the submitted value is placed into its
sector band server-side for use in matching/precedent analysis later. Every
other field gets an escape hatch: `"Other"` for categorical fields, `"Lower"` /
`"Higher"` for banded ones — for when the real target falls outside every
value the historical data happened to produce. Submitting resolves the
selections into a `TargetProfile` object (`POST /api/target-profile`), shown
on the page as both a readable summary and raw JSON.

**Deterministic acquirer matching** (`backend/matching.py`)
Given the resolved `TargetProfile`, finds candidate acquirers and scores them
on three independent axes, without picking a final ranking — that's left to
the agentic layer, which has more context to weigh tradeoffs.

- *Candidate search (tiered, escalating):* Tier 1 = acquirers with a deal in
  the target's own sector within ±20% of the target deal size. If that's
  fewer than 15 acquirers, Tier 2 adds acquirers from the sector that's most
  common among *other*-sector deals in that same size window; if still short,
  Tier 3 adds the *second*-most-common such sector. Each tier is only computed
  if the previous one fell short — a sector/size combo with plenty of Tier 1
  precedent never gets diluted with weaker matches. Below 15 candidates after
  all three tiers, the search stops anyway (nothing further is defined) and a
  `low_match_warning` is surfaced in the UI: *"Data has low match rates.
  Proceeding with analysis."* The bundled sample data's own reference scenario
  (Healthcare Services, $200M) is a real example of this — it tops out at 13.
- *Primary score:* sector fit × deal size fit, evenly weighted. Deal size is
  100 for every candidate (qualifying requires a size match at every tier);
  sector is 100/75/50 by tier. So primary score is 100/87.5/75 by tier.
- *Secondary score:* for each optional field the user filled in, what fraction
  of the acquirer's own qualifying deals (their Tier 1/2/3 deals) also match
  that field's value or range — e.g. 4 of 5 matching deals in the selected
  ownership category is 80%. Averaged evenly across whichever fields were
  filled in; fields left blank don't count for or against the acquirer, and
  an acquirer with zero optional fields to check gets `secondary_score: null`
  rather than a misleading number.
- *Relevancy score:* recent closed-deal activity, from the same qualifying
  deal set. More than one closed deal in the past 2 years scores 100; past 4
  years, 75; past 6, 50; past 8, 25; otherwise 0. A single recent deal doesn't
  score above 0 here by design — this is a repeat-activity signal, not a
  last-deal-date signal.

Every candidate carries its tier tag, per-tier deal counts, all three scores,
the specific deals that qualified it, and its full deal history — everything
the agentic layer needs to write a grounded rationale without re-querying the
dataset.

## Architecture decisions

- **No build step.** Backend is FastAPI; frontend is vanilla HTML/CSS/JS
  served as static files by the same process. One `python3 run.py`, one port,
  nothing to compile. Given the size of this phase, a JS framework or a
  separate frontend server would be pure overhead.
- **In-memory dataset state.** The active DataFrame lives in a module-level
  dict in `backend/app.py`. Fine for a single-user local demo; would move to
  a request-scoped session or a real store before this touched more than one
  user.
- **Bands are computed off the currently active dataset**, not hardcoded —
  uploading a different CSV recomputes sectors, categories, and bands from
  scratch, so the form always reflects what's actually loaded.

## Assumptions

- `sector` and `deal_size_mm` must be non-null in every row of the source CSV
  (they're the two fields the spec marks mandatory); other columns may contain
  blanks (e.g. `days_to_close` is null for non-closed deals in the sample data).
- Percentile bands use quintiles (20/40/60/80) — five bands balance nuance
  against the sample sizes per sector (38–70 rows); a collision-avoidance
  step bumps any two edges that round to the same clean number apart by one
  rounding increment, which fires on exactly one sector/field combo in the
  sample data (Pharma/Biotech EBITDA margin) and produces a still-clean result.
- "Clean number" rounding uses a fixed step table keyed to magnitude (e.g.
  nearest $10M around $100M, nearest 1% under 20%). It's a heuristic, not a
  statistical method — tuned by inspecting the actual band output across all
  10 sectors until the ranges looked like something a banker would say out
  loud.
- **Matching engine judgment calls**, where the spec left room:
  - Optional-field matching uses the field's absolute numeric range (or exact
    category), applied to *any* candidate deal regardless of tier/sector —
    not re-derived per deal's own sector. The band was computed from the
    target's primary sector at form-fill time; that's a fixed cutoff once
    chosen, so it's applied uniformly.
  - `"Other"` (geography/ownership) matches nothing — every historical deal
    falls into a known category by construction, so a value the user
    explicitly flagged as *not* one of those can't be satisfied by any deal.
    It still counts in the field-average denominator, since the user did fill
    it in.
  - `"Regional"` for geography matches anything except `"Multi-Regional"` and
    `"National"`, per the explicit spec note.
  - Relevancy is computed from each acquirer's *qualifying* (Tier 1/2/3) deals
    only, not their full deal history — kept consistent with how secondary
    scoring also scopes to that same deal set, rather than introducing a
    second, broader pool the spec didn't call for.
  - Tier 2/3 sector ranking ties break alphabetically, for determinism.
  - Candidates are pre-sorted (tier asc, then primary score desc, then name)
    as a sane default for the results table — not a final ranking. The spec
    explicitly defers ranking/selection to the agentic layer.

## Known limitations

- No persistence — re-uploading or restarting the server resets to the
  bundled sample CSV.
- Single active dataset globally, not per-session; concurrent users would
  share (and overwrite) each other's uploads.
- Band edges can look odd at the extremes on skewed, small-sample sectors
  (e.g. a lower bound of "$0M" when the true minimum rounds down to it) —
  cosmetic, not a correctness issue, but worth tightening if this goes further.
- Relevancy scoring uses the real system clock (`date.today()`), so results
  drift as time passes even for an identical target profile and dataset —
  inherent to any recency-based score, not a bug, but worth knowing if two
  runs on different days produce different relevancy numbers.
- The candidates table in the UI is a verification view of the raw scoring
  output, not the final banker-facing deliverable — no acquirer detail view,
  no sorting/filtering, no export yet. That's intentionally deferred; the
  next phase's one-pagers are the real UI for this data.

## Next

The agentic layer: an LLM reasons over each candidate's tier, scores, and
attached deals to write the one-page rationale (overview, strategic fit
thesis, precedent activity, valuation context, risk flags, conviction level)
per the take-home spec — not yet implemented.
