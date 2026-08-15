"""FastAPI backend: CSV ingestion + target profile API, serving the static frontend."""

from __future__ import annotations

import io
from pathlib import Path

import data_processing as dp
import matching
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from models import TargetProfileRequest

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CSV_PATH = ROOT_DIR / "data" / "ma_transactions_500.csv"
FRONTEND_DIR = ROOT_DIR / "frontend"

app = FastAPI(title="M&A Acquirer Identification Engine")

STATE: dict = {"df": None, "metadata": None, "source": None}


def _set_dataset(df, source: str) -> dict:
    STATE["df"] = df
    STATE["metadata"] = dp.parse_dataset(df)
    STATE["source"] = source
    return {"source": source, **STATE["metadata"]}


@app.on_event("startup")
def load_default_dataset() -> None:
    df = dp.load_and_validate(DEFAULT_CSV_PATH)
    _set_dataset(df, "default")


@app.get("/api/metadata")
def get_metadata():
    if STATE["metadata"] is None:
        raise HTTPException(503, "Dataset not loaded.")
    return {"source": STATE["source"], **STATE["metadata"]}


@app.post("/api/upload")
def upload_csv(file: UploadFile = File(...)):
    try:
        raw = file.file.read()
        df = dp.load_and_validate(io.BytesIO(raw))
    except dp.SchemaValidationError as e:
        raise HTTPException(400, {"message": e.message, "details": e.details}) from e
    except Exception as e:
        raise HTTPException(400, {"message": f"Could not parse CSV: {e}", "details": []}) from e
    return _set_dataset(df, file.filename)


@app.post("/api/reset")
def reset_to_default():
    df = dp.load_and_validate(DEFAULT_CSV_PATH)
    return _set_dataset(df, "default")


FIELD_TO_BAND_ATTR = {
    "target_revenue_mm": "target_revenue_band",
    "target_ebitda_mm": "target_ebitda_band",
    "ebitda_margin_pct": "ebitda_margin_band",
    "revenue_growth_pct": "revenue_growth_band",
}


def _resolve_deal_size(sector: str, value: float, metadata: dict) -> dict:
    """Deal size is free-text $mm; place it within the sector's precomputed bands for context."""
    field_bands = metadata["bands"][sector]["deal_size_mm"]
    bands = field_bands["bands"]
    entered = f"${value / 1000:g}B" if value >= 1000 else f"${value:g}M"

    if value < bands[0]["min"]:
        return {"value": value, "band": "lower", "label": f"{entered} — {field_bands['lower_label']}", "min": None, "max": None, "is_median": False}
    if value > bands[-1]["max"]:
        return {"value": value, "band": "higher", "label": f"{entered} — {field_bands['higher_label']}", "min": None, "max": None, "is_median": False}
    for b in bands:
        if b["min"] <= value <= b["max"]:
            return {"value": value, "band": b["key"], "label": f"{entered} — {b['label']}", "min": b["min"], "max": b["max"], "is_median": b["is_median"]}

    return {"value": value, "band": None, "label": entered, "min": None, "max": None, "is_median": False}  # unreachable: bands span the full range


def _resolve_band(field_key: str, sector: str, band_value: str | None, metadata: dict) -> dict | None:
    if not band_value:
        return None

    field_bands = metadata["bands"][sector][field_key]
    if band_value == "lower":
        return {"band": "lower", "label": field_bands["lower_label"], "min": None, "max": field_bands["bands"][0]["min"], "is_median": False}
    if band_value == "higher":
        return {"band": "higher", "label": field_bands["higher_label"], "min": field_bands["bands"][-1]["max"], "max": None, "is_median": False}

    for b in field_bands["bands"]:
        if b["key"] == band_value:
            return {"band": band_value, "label": b["label"], "min": b["min"], "max": b["max"], "is_median": b["is_median"]}

    raise ValueError(f"'{band_value}' is not a valid option for '{dp.FIELD_LABELS[field_key]}'.")


def _resolve_categorical(value: str | None, options: list[str]) -> dict | None:
    if not value:
        return None
    if value == "Other":
        return {"value": "Other"}
    if value not in options:
        raise ValueError(f"'{value}' is not a recognized option.")
    return {"value": value}


def build_target_profile(payload: TargetProfileRequest, metadata: dict) -> dict:
    if payload.sector not in metadata["sectors"]:
        raise ValueError(f"'{payload.sector}' is not a recognized sector.")

    profile = {"sector": payload.sector}
    profile["deal_size_mm"] = _resolve_deal_size(payload.sector, payload.deal_size_mm, metadata)
    for field_key, attr in FIELD_TO_BAND_ATTR.items():
        profile[field_key] = _resolve_band(field_key, payload.sector, getattr(payload, attr), metadata)

    profile["geography"] = _resolve_categorical(payload.geography, metadata["geography_options"])
    profile["target_ownership_pre"] = _resolve_categorical(payload.target_ownership_pre, metadata["ownership_options"])
    return profile


@app.post("/api/target-profile")
def submit_target_profile(payload: TargetProfileRequest):
    if STATE["metadata"] is None:
        raise HTTPException(503, "Dataset not loaded.")
    try:
        profile = build_target_profile(payload, STATE["metadata"])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    candidate_search = matching.find_candidates(profile, STATE["df"])
    return {"target_profile": profile, "candidate_search": candidate_search}


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
