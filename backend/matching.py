"""
Deterministic acquirer candidate identification + scoring.

Given a resolved TargetProfile, finds candidate acquirers in up to three
tiers. Tier 1 is the target's own sector; Tier 2 is the single most common
adjacent sector at the same deal size — both driven off the profile's
`inferred_values` (computed once, see compute_inferred_values below), not
recomputed here. Tier 3 kicks in only if Tier 1+2 fall short of
MIN_CANDIDATES: a single relaxation-planning agent (tier3_agent.py) proposes
which sector(s)/deal-size window/tag-matching to use, and that plan is
executed here, deterministically, against the data — the agent never touches
a row itself. Every candidate is then scored on four independent axes:
primary (sector + deal size fit), secondary (optional-field fit against the
acquirer's own qualifying deals), strategic (modal strategic-tag match against
the target sector's top implied tags), and relevancy (recent closed-deal
activity).

No ranking or filtering across these scores is done here — that's left
to the downstream agentic layer, which has more context to weigh tradeoffs.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from datetime import date

import pandas as pd
import tier3_agent
from data_processing import BAND_NAMES, clean_records

# Constasnts for candidate identification and scoring
MIN_CANDIDATES = 10
DEAL_SIZE_TOLERANCE = 0.2
TIER3_WIDE_DEAL_SIZE_TOLERANCE = 0.4  # Tier 3's default deal-size tolerance if the relaxation agent doesn't specify one

SECTOR_MATCH_SCORES = (100, 67, 33)  # target sector / first implied sector / second implied sector
DEAL_MATCH_SCORES = (100, 50, 0)  # exact match (aka 20% band) / within wide range (aka 40% band) / no match
STRATEGIC_TAG_MATCH_SCORES = (100, 67, 33)  # acquirer's modal tag == 1st / 2nd / 3rd most common target-sector tag
# Exponential decay rate for relevancy scoring -- a tunable business assumption, not a value fit to the
# dataset. lambda=0.35 gives a ~2-year half-life (ln(2)/0.35 ≈ 2.0 years). See README for the full rationale.
RELEVANCY_DECAY_LAMBDA = 0.35
QUARTER_START_MONTH = {"Q1": 1, "Q2": 4, "Q3": 7, "Q4": 10}

OPTIONAL_NUMERIC_FIELDS = ["target_revenue_mm", "target_ebitda_mm", "ebitda_margin_pct", "revenue_growth_pct"]
OPTIONAL_CATEGORICAL_FIELDS = ["geography", "target_ownership_pre"]

# Ordered band keys (the edge bands + the 5 sector-relative quintiles), used to measure how many bands
# an acquirer's typical value sits from the target's selected band -- mirrors data_processing.py's BAND_NAMES
ORDERED_BAND_KEYS = ["lower"] + [n.lower().replace("-", "_") for n in BAND_NAMES] + ["higher"]

# Sets the range of deal sizes to consider for a given target deal size... Tolerance is set to +/- 20% right now, but I need to really validate if this is the right range froma business perspective  
# I also thought about using a log scale or percentile-based approach since having a fixed value can be limiting and something I'm usually uncomfortable with, but keeping it simple for the 3-4 effort window
def _deal_size_range(deal_size_mm: float, tolerance: float = DEAL_SIZE_TOLERANCE) -> tuple[float, float]:
    return deal_size_mm * (1 - tolerance), deal_size_mm * (1 + tolerance)

# Find other common sectors based on the main sector selected ... The idea here was to not have a static reference table of what sectors are related to each other, but rather to dynamically find relationships present in the data itself 
# For a production grade approach, we would definitely need to validate the assumptions being made here, but I wanted to keep a data-driven approach and allow for more flexibility in finding relevant acquirers, since any tool shouldn't ideally be tailored to a specific dataset 
def _rank_other_sectors(df: pd.DataFrame, primary_sector: str, lo: float, hi: float) -> list[tuple[str, int]]:
    """Sectors other than primary, ranked by deal count within the size window (ties broken alphabetically)."""
    pool = df[(df["deal_size_mm"] >= lo) & (df["deal_size_mm"] <= hi) & (df["sector"] != primary_sector)]
    counts = pool["sector"].value_counts().to_dict()
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

# Most common individual strategic_rationale_tags (pipe-delimited, so split first) among deals in the same sector within the deal-size window
def _top_strategic_tags(df: pd.DataFrame, sector: str, lo: float, hi: float, top_n: int = 3) -> list[str]:
    subset = df[(df["sector"] == sector) & df["deal_size_mm"].between(lo, hi)]
    counter: Counter = Counter()
    for tags in subset["strategic_rationale_tags"].dropna():
        counter.update(t.strip() for t in tags.split("|") if t.strip())
    return [tag for tag, _ in counter.most_common(top_n)]

# Formalized "inferred values" for the target profile based on the target's sector and deal size, which are used to drive Tier 2-3 candidate identification
def compute_inferred_values(sector: str, deal_size_mm: float, df: pd.DataFrame) -> dict:
    lo, hi = _deal_size_range(deal_size_mm)
    ranked = _rank_other_sectors(df, sector, lo, hi)
    return {
        "deal_size_range_mm": [round(lo, 1), round(hi, 1)],
        "likely_sectors": [s for s, _ in ranked[:2]],
        "top_strategic_tags": _top_strategic_tags(df, sector, lo, hi),
    }

# Create a new entry for an acquirer
def _new_entry(acquirer: str, df: pd.DataFrame) -> dict:
    all_deals = df[df["acquirer"] == acquirer]
    acquirer_type = all_deals["acquirer_type"].mode().iloc[0] if not all_deals.empty else None
    return {
        "acquirer_type": acquirer_type,
        "tier_deal_counts": {1: 0, 2: 0, 3: 0},
        "deals_by_tier": {1: [], 2: [], 3: []},
        "total_deal_count": int(len(all_deals)),
        "all_deals": clean_records(all_deals),
    }

# Match a deal's numeric field against the min/max range set above
def _match_numeric(deal_value, resolved: dict) -> bool:
    if deal_value is None:
        return False
    lo, hi = resolved.get("min"), resolved.get("max")
    if lo is not None and deal_value < lo:
        return False
    if hi is not None and deal_value > hi:
        return False
    return True

# Match a deal's geography against the selected geography, with "Regional" meaning any geography that is not Multi-Regional or National
# NOTE: I made an assumption here that "Regional" is a catch-all for any geography that is not Multi-Regional or National, since "Regional" wasn't present as a field in the data but listed on the target profile's description from William Blair
def _match_geography(deal_value, selected: str) -> bool:
    if selected == "Regional":
        return deal_value not in ("Multi-Regional", "National")
    return deal_value == selected

# Tier 3's relaxation agent may only relax sector and deal size, every other optional field the user actually filled in on the form is enforced here as a hard filter, not just scored afterward the way Tier 1/2 candidates are
def _apply_hard_optional_filters(subset: pd.DataFrame, profile: dict) -> pd.DataFrame:
    for field in OPTIONAL_NUMERIC_FIELDS:
        resolved = profile.get(field)
        if resolved:
            subset = subset[subset[field].apply(lambda v, r=resolved: _match_numeric(v, r))]
    geo = profile.get("geography")
    if geo:
        subset = subset[subset["geography"].apply(lambda v, g=geo["value"]: _match_geography(v, g))]
    own = profile.get("target_ownership_pre")
    if own:
        subset = subset[subset["target_ownership_pre"] == own["value"]]
    return subset

# Convert a deal's year/quarter into a date object for relevancy scoring
def _deal_date(row: dict) -> date | None:
    year, quarter = row.get("deal_year"), row.get("deal_quarter")
    if year is None or quarter not in QUARTER_START_MONTH:
        return None
    return date(int(year), QUARTER_START_MONTH[quarter], 1)

# Relevancy = exponential decay on time since the acquirer's most recent CLOSED deal (rather than hard time
# buckets), so relevancy declines smoothly instead of falling off a cliff at an arbitrary year boundary.
def _relevancy_score(deals: list[dict]) -> dict:
    closed_dates = [d for d in (_deal_date(r) for r in deals if r.get("outcome") == "Closed") if d]
    if not closed_dates:
        return {"score": 0.0, "years_since_most_recent_closed_deal": None}
    years_since = (date.today() - max(closed_dates)).days / 365.25
    score = round(100 * math.exp(-RELEVANCY_DECAY_LAMBDA * years_since), 1)
    return {"score": score, "years_since_most_recent_closed_deal": round(years_since, 2)}

# Primary score = average of sector match and deal size match, judged off the acquirer's own matching deals
# (not the tier it was found in) -- an acquirer only gets credit for the sector/size it actually transacted at
def _primary_score(matching_deals: list[dict], target_sector: str, likely_sectors: list[str], deal_size_mm: float) -> dict:
    deal_sectors = {d.get("sector") for d in matching_deals}
    if target_sector in deal_sectors:
        sector_score = SECTOR_MATCH_SCORES[0]
    elif len(likely_sectors) > 0 and likely_sectors[0] in deal_sectors:
        sector_score = SECTOR_MATCH_SCORES[1]
    elif len(likely_sectors) > 1 and likely_sectors[1] in deal_sectors:
        sector_score = SECTOR_MATCH_SCORES[2]
    else:
        sector_score = 0

    lo, hi = _deal_size_range(deal_size_mm)
    lo_wide, hi_wide = _deal_size_range(deal_size_mm, TIER3_WIDE_DEAL_SIZE_TOLERANCE)
    deal_sizes = [d["deal_size_mm"] for d in matching_deals if d.get("deal_size_mm") is not None]
    if any(lo <= v <= hi for v in deal_sizes):
        deal_size_score = DEAL_MATCH_SCORES[0]
    elif any(lo_wide <= v <= hi_wide for v in deal_sizes):
        deal_size_score = DEAL_MATCH_SCORES[1]
    else:
        deal_size_score = DEAL_MATCH_SCORES[2]

    return {"sector_score": sector_score, "deal_size_score": deal_size_score, "score": (sector_score + deal_size_score) / 2}

# Most common non-null value among a set of deals for a field (ties broken alphabetically, for determinism)
def _mode(values: list) -> str | None:
    counts = Counter(v for v in values if v is not None)
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

# Which band (lower/low/low_mid/mid/mid_high/high/higher) a value falls into, given that field's band_edges
def _band_index(value: float, edges: list[float]) -> int:
    if value < edges[0]:
        return 0
    if value > edges[-1]:
        return len(ORDERED_BAND_KEYS) - 1
    for i in range(len(edges) - 1):
        if edges[i] <= value <= edges[i + 1]:
            return i + 1
    return len(ORDERED_BAND_KEYS) - 2  # unreachable: edges span [edges[0], edges[-1]]

# Secondary score = average, across the optional fields the user filled in, of how well the acquirer's own
# qualifying deals fit that field. Categorical fields (geography, ownership) use the acquirer's mode value --
# 100 if it matches the user's selection, else 0. Numeric fields use the acquirer's median value, banded the
# same way the target profile's bands are -- 100 if it's the same band the user selected, 50 if one band
# above/below, else 0. Stored per-field (not just averaged) so the write-up agent can cite specifics later.
def _secondary_score(matching_deals: list[dict], optional_resolved: dict) -> dict:
    field_scores = {}
    for field, resolved in optional_resolved.items():
        if field in OPTIONAL_NUMERIC_FIELDS:
            values = [d[field] for d in matching_deals if d.get(field) is not None]
            if not values:
                field_scores[field] = 0.0
                continue
            acquirer_band = _band_index(statistics.median(values), resolved["band_edges"])
            target_band = ORDERED_BAND_KEYS.index(resolved["band"])
            diff = abs(acquirer_band - target_band)
            field_scores[field] = 100.0 if diff == 0 else 50.0 if diff == 1 else 0.0
        elif field == "geography":
            mode_value = _mode([d.get(field) for d in matching_deals])
            field_scores[field] = 100.0 if mode_value is not None and _match_geography(mode_value, resolved["value"]) else 0.0
        else:
            mode_value = _mode([d.get(field) for d in matching_deals])
            field_scores[field] = 100.0 if mode_value == resolved["value"] else 0.0

    return {
        "field_scores": field_scores,
        "score": round(sum(field_scores.values()) / len(field_scores), 1) if field_scores else None,
    }

# Most common individual strategic_rationale_tag (pipe-delimited, so split first) across a set of deals --
# the acquirer's "modal" tag, ties broken alphabetically for determinism
def _strategic_tag_mode(deals: list[dict]) -> str | None:
    counter: Counter = Counter()
    for d in deals:
        tags = d.get("strategic_rationale_tags")
        if tags:
            counter.update(t.strip() for t in tags.split("|") if t.strip())
    if not counter:
        return None
    return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

# Strategic match score: does the acquirer's own modal strategic tag match one of the target sector's top 3
# implied tags (inferred_values.top_strategic_tags)? 100 if 1st, 67 if 2nd, 33 if 3rd, else 0.
def _strategic_match_score(matching_deals: list[dict], top_strategic_tags: list[str]) -> dict:
    mode_tag = _strategic_tag_mode(matching_deals)
    score = 0
    if mode_tag is not None:
        for rank, tag in enumerate(top_strategic_tags[:3]):
            if mode_tag == tag:
                score = STRATEGIC_TAG_MATCH_SCORES[rank]
                break
    return {"mode_tag": mode_tag, "score": score}

# Score a candidate acquirer based on its best tier, the number of matching deals, and the optional fields provided in the target profile
def _score_candidate(
    acquirer: str, entry: dict, optional_resolved: dict, target_sector: str, likely_sectors: list[str],
    deal_size_mm: float, top_strategic_tags: list[str],
) -> dict:
    tier_deal_counts = entry["tier_deal_counts"]
    best_tier = next(t for t in (1, 2, 3) if tier_deal_counts[t] > 0)
    matching_deals = entry["deals_by_tier"][1] + entry["deals_by_tier"][2] + entry["deals_by_tier"][3]

    primary_score = _primary_score(matching_deals, target_sector, likely_sectors, deal_size_mm)
    secondary_score = _secondary_score(matching_deals, optional_resolved)
    strategic_score = _strategic_match_score(matching_deals, top_strategic_tags)

    return {
        "acquirer": acquirer,
        "acquirer_type": entry["acquirer_type"],
        "tier": best_tier,
        "tier_deal_counts": {f"tier_{t}": tier_deal_counts[t] for t in (1, 2, 3)},
        "primary_score": primary_score,
        "secondary_score": secondary_score,
        "strategic_score": strategic_score,
        "relevancy_score": _relevancy_score(matching_deals),
        "matching_deals": matching_deals,
        "total_deal_count": entry["total_deal_count"],
        "all_deals": entry["all_deals"],
        # Only present for Tier 3 candidates
        "tier3_relaxation": entry.get("tier3_relaxation"),
    }

# Accumulate candidate acquirers from a subset of deals into the pool, updating their tier counts and deal lists
def _accumulate(pool: dict, subset: pd.DataFrame, tier_num: int, df: pd.DataFrame) -> None:
    for acquirer, group in subset.groupby("acquirer"):
        if acquirer not in pool:
            pool[acquirer] = _new_entry(acquirer, df)
        entry = pool[acquirer]
        entry["tier_deal_counts"][tier_num] += len(group)
        entry["deals_by_tier"][tier_num].extend(clean_records(group))

# Tier 3: Relaxation-planning agent proposes a plan -- which sector(s) to widen to, how far to expand the deal size window, and/or whether to match on shared strategic tags
async def _run_tier3_relaxation(profile: dict, df: pd.DataFrame, pool: dict, lo: float, hi: float) -> dict:
    def execute_plan(plan: dict) -> tuple[pd.DataFrame, int]:
        tolerance = plan.get("deal_size_tolerance_pct") or TIER3_WIDE_DEAL_SIZE_TOLERANCE
        lo_wide, hi_wide = _deal_size_range(profile["deal_size_mm"]["value"], tolerance)
        in_narrow = df["deal_size_mm"].between(lo, hi)
        size_mask = df["deal_size_mm"].between(lo_wide, hi_wide) & ~in_narrow

        conditions = []
        sectors = [s for s in (plan.get("sectors") or []) if s]
        if sectors:
            conditions.append(df["sector"].isin(sectors) & size_mask)
        if plan.get("use_tag_matching"):
            top_tags = set(profile["inferred_values"]["top_strategic_tags"])
            if top_tags:
                tag_mask = df["strategic_rationale_tags"].fillna("").apply(
                    lambda tags: bool(top_tags & {t.strip() for t in tags.split("|")})
                )
                conditions.append(tag_mask & size_mask)

        if not conditions:
            subset = df.iloc[0:0]
        else:
            combined = conditions[0]
            for c in conditions[1:]:
                combined = combined | c
            subset = df[combined]

        subset = _apply_hard_optional_filters(subset, profile)
        total = len(set(pool.keys()) | set(subset["acquirer"].unique()))
        return subset, total

    result = await tier3_agent.plan_and_execute_tier3(profile, len(pool), MIN_CANDIDATES, execute_plan)
    used_plan, used_subset = result.get("used_plan"), result.pop("used_subset", None)
    if used_plan is not None and used_subset is not None and not used_subset.empty:
        _accumulate(pool, used_subset, 3, df)
        for acquirer in used_subset["acquirer"].unique():
            pool[acquirer]["tier3_relaxation"] = {"plan": used_plan, "attempts": result["attempts"]}
    return result  

# Main function to find candidate acquirers based on the target profile and the deals df
# async because Tier 3 may invoke the relaxation-planning agent, which is an LLM call.
# on_event, if given, is called with a small progress dict at each tier boundary -- lets the API layer
# stream "what's happening" to the UI without this function knowing anything about SSE/HTTP.
async def find_candidates(profile: dict, df: pd.DataFrame, on_event=None) -> dict:
    def emit(event: dict) -> None:
        if on_event:
            on_event(event)

    sector = profile["sector"]
    deal_size = profile["deal_size_mm"]["value"]
    lo, hi = _deal_size_range(deal_size)

    pool: dict[str, dict] = {}
    tier_sectors: dict[int, str | None] = {1: sector, 2: None, 3: None}

    # Escalate tier by tier, only progress to next tier if min 15 candidates not found yet
    # Tier 1 (deterministic, target's own sector and deal size window)
    emit({"type": "tier1_started", "sector": sector, "deal_size_range_mm": [round(lo, 1), round(hi, 1)]})
    tier1_subset = df[(df["sector"] == sector) & df["deal_size_mm"].between(lo, hi)]
    _accumulate(pool, tier1_subset, 1, df)
    emit({"type": "tier1_done", "candidate_count": len(pool)})

    # Tier 2 (deterministic, most common adjacent sector and own deal size window)
    likely_sectors = profile["inferred_values"]["likely_sectors"]
    if len(pool) < MIN_CANDIDATES and likely_sectors:
        tier_sectors[2] = likely_sectors[0]
        emit({"type": "tier2_started", "sector": tier_sectors[2]})
        tier2_subset = df[(df["sector"] == tier_sectors[2]) & df["deal_size_mm"].between(lo, hi)]
        _accumulate(pool, tier2_subset, 2, df)
        emit({"type": "tier2_done", "candidate_count": len(pool)})
    else:
        emit({"type": "tier2_skipped", "candidate_count": len(pool)})

    # Tier 3 (agentic, relaxation-planning, only if Tier 1 + 2 fall short of 15 candidates)
    tier3_relaxation = None
    if len(pool) < MIN_CANDIDATES:
        emit({
            "type": "tier3_started",
            "likely_sectors": likely_sectors,
            "top_strategic_tags": profile["inferred_values"]["top_strategic_tags"],
        })
        tier3_relaxation = await _run_tier3_relaxation(profile, df, pool, lo, hi)
        used_plan = tier3_relaxation.get("used_plan")
        if used_plan:
            plan_sectors = ", ".join(used_plan.get("sectors") or [])
            tier_sectors[3] = plan_sectors or ("tag-matched" if used_plan.get("use_tag_matching") else None)
        emit({"type": "tier3_done", "candidate_count": len(pool), "used_plan": used_plan is not None})
    else:
        emit({"type": "tier3_skipped", "candidate_count": len(pool)})

    optional_resolved = {
        f: profile.get(f) for f in OPTIONAL_NUMERIC_FIELDS + OPTIONAL_CATEGORICAL_FIELDS if profile.get(f)
    }

    top_strategic_tags = profile["inferred_values"]["top_strategic_tags"]
    candidates = [
        _score_candidate(acquirer, entry, optional_resolved, sector, likely_sectors, deal_size, top_strategic_tags)
        for acquirer, entry in pool.items()
    ]
    # Stack rank: tier group first, then primary/secondary/strategic/relevancy score, each descending, name as final tiebreak
    candidates.sort(key=lambda c: (
        c["tier"],
        -c["primary_score"]["score"],
        -(c["secondary_score"]["score"] or 0),
        -c["strategic_score"]["score"],
        -c["relevancy_score"]["score"],
        c["acquirer"],
    ))

    return {
        "target_deal_size_mm": deal_size,
        "deal_size_range_mm": [round(lo, 1), round(hi, 1)],
        "tier_sectors": {f"tier_{t}": s for t, s in tier_sectors.items()},
        # None if Tier 3 was never triggered (Tier 1 + 2 already had enough)
        # Tier 3 rationale surfaced in the UI so banker can see exactly what was widened and stored per-candidate for the write-up agent later.
        "tier3_relaxation": tier3_relaxation,
        "candidate_count": len(candidates),
        "low_match_warning": len(candidates) < MIN_CANDIDATES,
        "candidates": candidates,
    }
