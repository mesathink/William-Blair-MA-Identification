"""CSV ingestion: schema validation, categorical parsing, and sector-based numeric banding."""

from __future__ import annotations
import math
import numpy as np
import pandas as pd

# Expected schema (24 columns, based on sample data from William Blari)

STRING_COLUMNS = ["transaction_id", "target_company", "acquirer", "strategic_rationale_tags"]
CATEGORICAL_COLUMNS = [
    "sector", "sub_sector", "deal_quarter", "deal_type", "geography",
    "financing_type", "outcome", "acquirer_type", "target_ownership_pre",
]
INTEGER_COLUMNS = ["deal_year", "num_bidders"]
NUMERIC_COLUMNS = [
    "deal_size_mm", "target_revenue_mm", "target_ebitda_mm", "ebitda_margin_pct",
    "revenue_growth_pct", "ev_ebitda_multiple", "ev_revenue_multiple",
    "synergy_pct_of_deal", "days_to_close",
]
EXPECTED_COLUMNS = STRING_COLUMNS + CATEGORICAL_COLUMNS + INTEGER_COLUMNS + NUMERIC_COLUMNS

# Fields that drive the target profile form.
PROFILE_NUMERIC_FIELDS = [
    "deal_size_mm", "target_revenue_mm", "target_ebitda_mm",
    "ebitda_margin_pct", "revenue_growth_pct",
]
MANDATORY_FIELDS = ["sector", "deal_size_mm"] # Make these mandatory for the target profile form to be valid so there is some basis to the profile / search 

# Field units and labels for display in UI
FIELD_UNITS = {
    "deal_size_mm": "dollar",
    "target_revenue_mm": "dollar",
    "target_ebitda_mm": "dollar",
    "ebitda_margin_pct": "percent",
    "revenue_growth_pct": "percent",
}
FIELD_LABELS = {
    "deal_size_mm": "Deal Size (EV)",
    "target_revenue_mm": "Target Revenue",
    "target_ebitda_mm": "Target EBITDA",
    "ebitda_margin_pct": "EBITDA Margin",
    "revenue_growth_pct": "Revenue Growth",
}


class SchemaValidationError(ValueError):
    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or []

# CSV ingestion and validation functions
def validate_schema(df: pd.DataFrame) -> None:
    actual = list(df.columns)
    missing = [c for c in EXPECTED_COLUMNS if c not in actual]
    unexpected = [c for c in actual if c not in EXPECTED_COLUMNS]
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"Missing columns: {', '.join(missing)}")
        if unexpected:
            details.append(f"Unexpected columns: {', '.join(unexpected)}")
        raise SchemaValidationError(
            f"CSV schema mismatch — expected {len(EXPECTED_COLUMNS)} columns, found {len(actual)}.",
            details,
        )

    type_errors = []
    for col in INTEGER_COLUMNS + NUMERIC_COLUMNS:
        coerced = pd.to_numeric(df[col], errors="coerce")
        bad_mask = coerced.isna() & df[col].notna()
        if bad_mask.any():
            bad_rows = (df.index[bad_mask][:5] + 2).tolist()  # +2: header row + 1-index
            examples = df.loc[bad_mask, col][:5].tolist()
            type_errors.append(
                f"Column '{col}' expected numeric values but found {examples} at row(s) {bad_rows}."
            )
    if type_errors:
        raise SchemaValidationError(f"CSV type mismatch in {len(type_errors)} column(s).", type_errors)

    missing_value_errors = []
    for col in MANDATORY_FIELDS:
        n_missing = int(df[col].isna().sum())
        if n_missing:
            missing_value_errors.append(f"Column '{col}' is mandatory but has {n_missing} missing value(s).")
    if missing_value_errors:
        raise SchemaValidationError("Mandatory fields contain missing values.", missing_value_errors)


def load_and_validate(source) -> pd.DataFrame:
    df = pd.read_csv(source, dtype=str)
    validate_schema(df)
    for col in INTEGER_COLUMNS + NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def clean_records(frame: pd.DataFrame) -> list[dict]:
    """DataFrame rows -> JSON-safe dicts: NaN -> None, integer columns -> int (not 2020.0)."""
    records = frame.replace({np.nan: None}).to_dict("records")
    for r in records:
        for col in INTEGER_COLUMNS:
            if r.get(col) is not None:
                r[col] = int(r[col])
    return records


# Percentile banding to make dropdowns for numeric fields instead of continuous inputs ... Made this decision for now to keep the comparison simple
# Later version would do a more robust comparison of the target profile to the dataset with continuous inputs and more scientific scoring system... I've used Gower's distance, knowledge graphs, etc. before which takes a more mathematical approach to similarity matching and layering an agent / LLM pipeline on top of that to provide rationale / explainability

# Quintile cut points -> 5 bands per sector/field, each ~20% of that sector's observations.
BAND_CUT_FRACTIONS = [0.2, 0.4, 0.6, 0.8]
BAND_NAMES = ["Low", "Low-Mid", "Mid", "Mid-High", "High"]

# Helper function to determine the rounding step for bands
def _step_for(value: float, unit: str) -> float:
    """The 'clean number' rounding increment for a value's magnitude."""
    magnitude = abs(value)
    if unit == "percent":
        if magnitude < 20:
            return 1
        if magnitude < 50:
            return 2
        return 5
    if magnitude < 10:
        return 1
    if magnitude < 50:
        return 5
    if magnitude < 100:
        return 10
    if magnitude < 500:
        return 25
    if magnitude < 2000:
        return 100
    if magnitude < 10000:
        return 500
    return 1000

# Make sure bands are "clean" numbers that scale with magnitude
def _round_clean(value: float, unit: str, direction: str = "nearest") -> float:
    """Round to a step that scales with magnitude, e.g. 147.9 -> 150, 622.5 -> 600."""
    step = _step_for(value, unit)
    if direction == "floor":
        return math.floor(value / step) * step
    if direction == "ceil":
        return math.ceil(value / step) * step
    return round(value / step) * step

# Bands should be based on percentile values so they are relative to the data / sector selected
def _percentile(sorted_vals: list[float], p: float) -> float:
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    k = (n - 1) * p
    f = math.floor(k)
    c = min(f + 1, n - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)

def _format_value(value: float, unit: str) -> str:
    if unit == "dollar":
        return f"${value / 1000:.1f}B" if value >= 1000 else f"${value:.0f}M"
    return f"{value:.0f}%"

def _band_label(lo: float, hi: float, unit: str) -> str:
    return f"{_format_value(lo, unit)}–{_format_value(hi, unit)}"

def _build_field_bands(values: pd.Series, unit: str) -> dict | None:
    vals = sorted(v for v in values.dropna().astype(float).tolist())
    if not vals:
        return None

    median = _percentile(vals, 0.5)
    raw_edges = [min(vals)] + [_percentile(vals, f) for f in BAND_CUT_FRACTIONS] + [max(vals)]

    edges = []
    for i, e in enumerate(raw_edges):
        if i == 0:
            edges.append(_round_clean(e, unit, "floor"))
        elif i == len(raw_edges) - 1:
            edges.append(_round_clean(e, unit, "ceil"))
        else:
            edges.append(_round_clean(e, unit, "nearest"))

    # Keep bands strictly increasing even on small or low-variance samples
    for i in range(1, len(edges)):
        if edges[i] <= edges[i - 1]:
            edges[i] = edges[i - 1] + _step_for(edges[i - 1], unit)

    bands = []
    for i, name in enumerate(BAND_NAMES):
        band_lo, band_hi = edges[i], edges[i + 1]
        is_last = i == len(BAND_NAMES) - 1
        contains_median = band_lo <= median <= band_hi if is_last else band_lo <= median < band_hi
        label = _band_label(band_lo, band_hi, unit)
        bands.append({
            "key": name.lower().replace("-", "_"),
            "label": f"{label} (Median)" if contains_median else label,
            "min": band_lo,
            "max": band_hi,
            "is_median": contains_median,
        })

    return {
        "unit": unit,
        "median": round(median, 1),
        "bands": bands,
        "lower_label": f"Lower (below {_format_value(edges[0], unit)})",
        "higher_label": f"Higher (above {_format_value(edges[-1], unit)})",
    }

# Parse the dataset and extract relevant metadata
def parse_dataset(df: pd.DataFrame) -> dict:
    sectors = sorted(df["sector"].dropna().unique().tolist())
    geography_options = sorted(set(df["geography"].dropna().unique().tolist()) | {"Regional"})
    ownership_options = sorted(df["target_ownership_pre"].dropna().unique().tolist())

    bands: dict[str, dict] = {}
    for sector in sectors:
        sector_df = df[df["sector"] == sector]
        bands[sector] = {
            field: _build_field_bands(sector_df[field], FIELD_UNITS[field])
            for field in PROFILE_NUMERIC_FIELDS
        }

    return {
        "row_count": int(len(df)),
        "sectors": sectors,
        "geography_options": geography_options,
        "ownership_options": ownership_options,
        "numeric_fields": [
            {"key": f, "label": FIELD_LABELS[f], "unit": FIELD_UNITS[f], "mandatory": f in MANDATORY_FIELDS}
            for f in PROFILE_NUMERIC_FIELDS
        ],
        "mandatory_fields": MANDATORY_FIELDS,
        "bands": bands,
    }
