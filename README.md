# M&A Acquirer Identification Engine

**Status: CSV ingestion, Target Profile form, and deterministic acquirer
matching/scoring (Tiers 1–2 rule-based, Tier 3 agent-planned relaxation) are
built end-to-end.** The per-acquirer one-page rationale write-up (LLM-generated
Overview/Strategic Fit/Precedent/Valuation/Risk Flags/Conviction) from the
take-home spec is **not yet built**.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in ANTHROPIC_API_KEY
python3 run.py
```

Opens at `http://127.0.0.1:8000`. The sample dataset (`data/ma_transactions_500.csv`)
loads automatically on startup. CSV ingestion, the target profile form, and
Tier 1/2 acquirer matching all work with **no API key**. `ANTHROPIC_API_KEY`
is used automatically, inside "Build Target Profile" itself, whenever Tier 1+2
fall short of 10 candidates and the Tier 3 relaxation agent runs — no key just
means Tier 3 is skipped gracefully (`low_match_warning` fires, nothing
errors). `.env` is gitignored; `.env.example` documents every variable.

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
other banded numeric field gets a `"Lower"` / `"Higher"` escape hatch, for
when the real target falls outside every value the historical data happened
to produce; categorical fields (geography, ownership) are a closed choice
among the values actually observed in the dataset. Submitting resolves the
selections into a `TargetProfile` object (`POST /api/target-profile`), shown
on the page as both a readable summary and raw JSON. That endpoint streams
its progress as Server-Sent Events (e.g. "Finding Tier 1 matches in
Healthcare Services, $160M–$240M…", "Tier 3: found 12 candidates.") so the UI
can show what's happening in the backend in real time, rather than a
plain-JSON response the user just waits on.

**Deterministic acquirer matching** (`backend/matching.py`)
Given the resolved `TargetProfile`, finds candidate acquirers and scores them
on four independent axes, without picking a final ranking — that's left to
the agentic layer, which has more context to weigh tradeoffs.

- *Inferred values, computed once* — right after the target profile is built,
  `matching.compute_inferred_values()` derives `likely_sectors` (the sector(s)
  other than the target's own most common among deals of a similar size,
  ranked) and `top_strategic_tags` (the most common individual
  `strategic_rationale_tags` — pipe-delimited, parsed — among the target
  sector's own deals at this size). These are stored on `profile["inferred_values"]`
  and referenced everywhere downstream — Tier 2 below, the Tier 3 relaxation
  agent, and eventually the per-acquirer write-up agent — never recalculated.
- *Candidate search (tiered, escalating):* Tier 1 = acquirers with a deal in
  the target's own sector within ±20% of the target deal size. If that's
  fewer than 15 acquirers, Tier 2 adds acquirers from `inferred_values.likely_sectors[0]`
  (the most common adjacent sector) at the same ±20%. Each tier only runs if
  the previous one fell short — a sector/size combo with plenty of Tier 1
  precedent never gets diluted with weaker matches.
- *Tier 3, agent-planned:* if Tier 1+2 are still short of 15, a single
  relaxation-planning agent (`tier3_agent.py`) — one call per search, not per
  acquirer — proposes which sector(s) to widen to, how far to expand the deal
  size window, and/or whether to also match acquirers by shared strategic
  tags (using `top_strategic_tags`), each choice justified in plain language
  tied to the user's input or the inferred values. The agent never touches a
  row; `matching._run_tier3_relaxation()` executes its plan deterministically
  and reports back the real resulting candidate count. It gets up to 2 turns —
  if the first plan falls short, it sees the actual count and can propose
  something broader — and whichever attempt found more candidates is used.
  Every optional field the user filled in stays a hard filter throughout,
  no matter what the agent proposes; it can only relax sector, deal size, and
  tag matching. Still short of 15 after both turns (or no `ANTHROPIC_API_KEY`
  configured, in which case Tier 3 is skipped entirely) → `low_match_warning`:
  *"Data has low match rates. Proceeding with analysis."* Every attempted
  plan, which one was used, and its justification are returned in
  `tier3_relaxation` and shown as a banner above the candidates table, and the
  winning plan is also stored on each Tier 3 candidate individually
  (`candidate.tier3_relaxation`) for the write-up agent to cite later.
- *Primary score:* sector fit + deal size fit, evenly weighted, judged off the
  acquirer's own matching deals (not the tier it was discovered in). Sector:
  100 if any matching deal was in the target's own sector, 67 if the first
  implied sector (`inferred_values.likely_sectors[0]`), 33 if the second, else
  0. Deal size: 100 if any matching deal is within ±20% of the target deal
  size, 50 if within ±20-40%, else 0.
- *Secondary score:* for each optional field the user filled in, judged off the
  acquirer's own qualifying deals (their Tier 1/2/3 deals). Categorical fields
  (geography, ownership) use the acquirer's mode value — e.g. if 4 of 5
  matching deals were Southeast, they're treated as a Southeast acquirer: 100
  if that matches the user's selection, else 0. Numeric fields (revenue,
  EBITDA, margin, growth) use the acquirer's median value, banded the same way
  the target profile's sector-relative quintile bands are: 100 if it's the
  same band the user selected, 50 if one band above/below, else 0. Averaged
  evenly across whichever fields were filled in and stored per-field
  (`field_scores`) for the write-up agent to cite later; fields left blank
  don't count for or against the acquirer, and an acquirer with zero optional
  fields to check gets `secondary_score: null` rather than a misleading
  number.
- *Strategic match score:* does the acquirer's own most common
  (mode) individual `strategic_rationale_tags` value match one of the target
  sector's top 3 implied tags (`inferred_values.top_strategic_tags`)? 100 if
  it's the #1 tag, 67 if #2, 33 if #3, else 0 (including when the acquirer has
  no tags on record). Same mode-of-the-acquirer's-own-deals pattern as the
  secondary score's categorical fields, just against the tag field instead of
  a user-selected field.
- *Relevancy score:* exponential decay on recency of the acquirer's most
  recent CLOSED deal, from the same qualifying deal set —
  `relevancy = 100 * e^(-λ * years_since_most_recent_closed_deal)`, with no
  closed deals scoring 0. `λ = 0.35` gives an ~2-year half-life
  (`ln(2)/0.35 ≈ 2.0` years) — a deal's relevancy roughly halves every 2
  years. `λ` is a **tunable business hyperparameter, not a value fit to the
  dataset**: M&A strategic appetite shifts meaningfully within a couple of
  years, so a 2-year half-life is a reasonable default, and it's a one-line
  change (`RELEVANCY_DECAY_LAMBDA` in `matching.py`) to use a different decay
  rate. Exponential decay was chosen over hard time buckets (e.g. "100 within
  2 years, 75 within 4...") because it's smooth — two acquirers whose most
  recent deal closed 23 and 25 months ago score almost identically, instead of
  landing on opposite sides of an arbitrary bucket boundary.

Every candidate carries its tier tag, per-tier deal counts, all four scores,
the specific deals that qualified it, and its full deal history — including
the Tier 3 relaxation plan/justification where applicable — so that whatever
generates the per-acquirer write-up next doesn't need to re-query the dataset.

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
  - `"Regional"` for geography matches anything except `"Multi-Regional"` and
    `"National"`, per the explicit spec note.
  - Relevancy is computed from each acquirer's *qualifying* (Tier 1/2/3) deals
    only, not their full deal history — kept consistent with how secondary
    scoring also scopes to that same deal set, rather than introducing a
    second, broader pool the spec didn't call for.
  - Inferred-values sector ranking ties break alphabetically, for determinism.
  - Candidates are pre-sorted (tier group asc, then primary/secondary/strategic/
    relevancy score desc, in that order, then name) as a sane default for the
    results table — not a final ranking. The spec explicitly defers
    ranking/selection to the agentic layer.
  - **Tier 3 relaxation agent judgment calls:** across its up-to-2 turns, the
    attempt with the *higher* resulting candidate count is used, even if that
    happens to be the first one (turn 2 isn't assumed to always be better,
    since the agent could in principle propose something narrower). No API
    key configured degrades to "Tier 3 skipped" rather than blocking the
    whole search — the base app (CSV, form, Tier 1/2) has always worked
    without a key, and that stays true here. An empty/unusable plan (agent
    returns no sectors and no tag matching) just produces zero additional
    candidates rather than erroring, so a bad turn can't crash the search.

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
- The candidates table (step 4) is currently the final output of the app —
  it's a verification view of the raw deterministic scoring, not yet the
  one-page-per-acquirer banker deliverable the spec asks for (see Status above).

