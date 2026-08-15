"""Deterministic acquirer candidate identification + scoring.

Given a resolved TargetProfile, finds candidate acquirers in up to three
tiers (primary sector, then the two most common adjacent sectors for deals
of a similar size), then scores each candidate on three independent axes:
primary (sector + deal size fit), secondary (optional-field fit against the
acquirer's own qualifying deals), and relevancy (recent closed-deal activity).

No ranking or filtering across these three scores is done here — that's left
to the downstream agentic layer, which has more context to weigh tradeoffs.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

MIN_CANDIDATES = 15
DEAL_SIZE_TOLERANCE = 0.2

SECTOR_TIER_SCORES = {1: 100, 2: 75, 3: 50}
RELEVANCY_WINDOWS = [(2, 100), (4, 75), (6, 50), (8, 25)]  # (lookback years, score if >1 closed deal in window)
QUARTER_START_MONTH = {"Q1": 1, "Q2": 4, "Q3": 7, "Q4": 10}

OPTIONAL_NUMERIC_FIELDS = ["target_revenue_mm", "target_ebitda_mm", "ebitda_margin_pct", "revenue_growth_pct"]
OPTIONAL_CATEGORICAL_FIELDS = ["geography", "target_ownership_pre"]
INTEGER_DISPLAY_COLUMNS = ("deal_year", "num_bidders")


def _clean_records(frame: pd.DataFrame) -> list[dict]:
    records = frame.replace({np.nan: None}).to_dict("records")
    for r in records:
        for col in INTEGER_DISPLAY_COLUMNS:
            if r.get(col) is not None:
                r[col] = int(r[col])
    return records


def _deal_size_range(deal_size_mm: float) -> tuple[float, float]:
    return deal_size_mm * (1 - DEAL_SIZE_TOLERANCE), deal_size_mm * (1 + DEAL_SIZE_TOLERANCE)


def _rank_other_sectors(df: pd.DataFrame, primary_sector: str, lo: float, hi: float) -> list[tuple[str, int]]:
    """Sectors other than primary, ranked by deal count within the size window (ties broken alphabetically)."""
    pool = df[(df["deal_size_mm"] >= lo) & (df["deal_size_mm"] <= hi) & (df["sector"] != primary_sector)]
    counts = pool["sector"].value_counts().to_dict()
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _new_entry(acquirer: str, df: pd.DataFrame) -> dict:
    all_deals = df[df["acquirer"] == acquirer]
    acquirer_type = all_deals["acquirer_type"].mode().iloc[0] if not all_deals.empty else None
    return {
        "acquirer_type": acquirer_type,
        "tier_deal_counts": {1: 0, 2: 0, 3: 0},
        "deals_by_tier": {1: [], 2: [], 3: []},
        "total_deal_count": int(len(all_deals)),
        "all_deals": _clean_records(all_deals),
    }


def _match_numeric(deal_value, resolved: dict) -> bool:
    if deal_value is None:
        return False
    lo, hi = resolved.get("min"), resolved.get("max")
    if lo is not None and deal_value < lo:
        return False
    if hi is not None and deal_value > hi:
        return False
    return True


def _match_geography(deal_value, selected: str) -> bool:
    if selected == "Other":
        return False
    if selected == "Regional":
        return deal_value not in ("Multi-Regional", "National")
    return deal_value == selected


def _match_categorical(deal_value, selected: str) -> bool:
    if selected == "Other":
        return False
    return deal_value == selected


def _deal_date(row: dict) -> date | None:
    year, quarter = row.get("deal_year"), row.get("deal_quarter")
    if year is None or quarter not in QUARTER_START_MONTH:
        return None
    return date(int(year), QUARTER_START_MONTH[quarter], 1)


def _relevancy_score(deals: list[dict]) -> dict:
    closed_dates = [d for d in (_deal_date(r) for r in deals if r.get("outcome") == "Closed") if d]
    today = date.today()
    for window_years, score in RELEVANCY_WINDOWS:
        count = sum(1 for d in closed_dates if (today - d).days / 365.25 <= window_years)
        if count > 1:
            return {"score": score, "window_years": window_years, "closed_deal_count_in_window": count}
    return {"score": 0, "window_years": None, "closed_deal_count_in_window": 0}


def _score_candidate(acquirer: str, entry: dict, optional_resolved: dict) -> dict:
    tier_deal_counts = entry["tier_deal_counts"]
    best_tier = next(t for t in (1, 2, 3) if tier_deal_counts[t] > 0)
    matching_deals = entry["deals_by_tier"][1] + entry["deals_by_tier"][2] + entry["deals_by_tier"][3]

    sector_score = SECTOR_TIER_SCORES[best_tier]
    primary_score = {"sector_score": sector_score, "deal_size_score": 100, "score": (sector_score + 100) / 2}

    field_scores = {}
    for field, resolved in optional_resolved.items():
        if field in OPTIONAL_NUMERIC_FIELDS:
            matches = sum(1 for d in matching_deals if _match_numeric(d.get(field), resolved))
        elif field == "geography":
            matches = sum(1 for d in matching_deals if _match_geography(d.get(field), resolved["value"]))
        else:
            matches = sum(1 for d in matching_deals if _match_categorical(d.get(field), resolved["value"]))
        field_scores[field] = round(matches / len(matching_deals) * 100, 1) if matching_deals else 0.0
    secondary_score = {
        "field_scores": field_scores,
        "score": round(sum(field_scores.values()) / len(field_scores), 1) if field_scores else None,
    }

    return {
        "acquirer": acquirer,
        "acquirer_type": entry["acquirer_type"],
        "tier": best_tier,
        "tier_deal_counts": {f"tier_{t}": tier_deal_counts[t] for t in (1, 2, 3)},
        "primary_score": primary_score,
        "secondary_score": secondary_score,
        "relevancy_score": _relevancy_score(matching_deals),
        "matching_deals": matching_deals,
        "total_deal_count": entry["total_deal_count"],
        "all_deals": entry["all_deals"],
    }


def find_candidates(profile: dict, df: pd.DataFrame) -> dict:
    sector = profile["sector"]
    deal_size = profile["deal_size_mm"]["value"]
    lo, hi = _deal_size_range(deal_size)

    pool: dict[str, dict] = {}
    tier_sectors: dict[int, str | None] = {1: sector, 2: None, 3: None}

    def add_tier(tier_num: int, sector_name: str | None) -> None:
        if not sector_name:
            return
        subset = df[(df["sector"] == sector_name) & (df["deal_size_mm"] >= lo) & (df["deal_size_mm"] <= hi)]
        for acquirer, group in subset.groupby("acquirer"):
            if acquirer not in pool:
                pool[acquirer] = _new_entry(acquirer, df)
            entry = pool[acquirer]
            entry["tier_deal_counts"][tier_num] += len(group)
            entry["deals_by_tier"][tier_num].extend(_clean_records(group))

    # Escalate tier by tier — only fall back to a weaker (adjacent-sector) match
    # when the stronger tier hasn't produced enough candidates on its own.
    add_tier(1, sector)

    ranked_other_sectors: list[tuple[str, int]] = []
    if len(pool) < MIN_CANDIDATES:
        ranked_other_sectors = _rank_other_sectors(df, sector, lo, hi)
        if ranked_other_sectors:
            tier_sectors[2] = ranked_other_sectors[0][0]
            add_tier(2, tier_sectors[2])

    if len(pool) < MIN_CANDIDATES and len(ranked_other_sectors) > 1:
        tier_sectors[3] = ranked_other_sectors[1][0]
        add_tier(3, tier_sectors[3])

    optional_resolved = {
        f: profile.get(f) for f in OPTIONAL_NUMERIC_FIELDS + OPTIONAL_CATEGORICAL_FIELDS if profile.get(f)
    }

    candidates = [_score_candidate(acquirer, entry, optional_resolved) for acquirer, entry in pool.items()]
    candidates.sort(key=lambda c: (c["tier"], -c["primary_score"]["score"], c["acquirer"]))

    return {
        "target_deal_size_mm": deal_size,
        "deal_size_range_mm": [round(lo, 1), round(hi, 1)],
        "tier_sectors": {f"tier_{t}": s for t, s in tier_sectors.items()},
        "candidate_count": len(candidates),
        "low_match_warning": len(candidates) < MIN_CANDIDATES,
        "candidates": candidates,
    }
