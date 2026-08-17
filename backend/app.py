"""FastAPI backend: CSV ingestion + target profile API, serving the static frontend."""

from __future__ import annotations
import asyncio
import io
import json
import os
from pathlib import Path
from typing import Awaitable, Callable
import data_processing as dp
import docx_export
import matching
import report_agent
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from models import TargetProfileRequest


# Create paths relative to the project root, so that the backend can be run from any working directory
ROOT_DIR = Path(__file__).resolve().parent.parent
# Set a default CSV path as the sample data provided by William Blair. This can be overridden by uploading a new CSV via the API
DEFAULT_CSV_PATH = ROOT_DIR / "data" / "ma_transactions_500.csv"
FRONTEND_DIR = ROOT_DIR / "frontend"

load_dotenv(ROOT_DIR / ".env")

# Set up FastAPI app and global state for the dataset and metadata
app = FastAPI(title="M&A Acquirer Identification Engine")

STATE: dict = {"df": None, "metadata": None, "source": None, "last_report_run": None}


def _set_dataset(df, source: str) -> dict:
    STATE["df"] = df
    STATE["metadata"] = dp.parse_dataset(df)
    STATE["source"] = source
    return {"source": source, **STATE["metadata"]}


@app.on_event("startup")
def load_default_dataset() -> None:
    df = dp.load_and_validate(DEFAULT_CSV_PATH)
    _set_dataset(df, "default")

# Get metadata endpoint: returns the current dataset's metadata, including sectors, bands, and options for categorical fields. If no dataset is loaded, returns a 503 error
@app.get("/api/metadata")
def get_metadata():
    if STATE["metadata"] is None:
        raise HTTPException(503, "Dataset not loaded.")
    return {"source": STATE["source"], **STATE["metadata"]}

# Upload CSV endpoint: accepts a CSV file upload, validates it, and sets it as the current dataset. Returns the new metadata. If validation fails, returns a 400 error with details
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

# Reset to default dataset endpoint: resets the current dataset to the default CSV provided by William Blair. Returns the default metadata
@app.post("/api/reset")
def reset_to_default():
    df = dp.load_and_validate(DEFAULT_CSV_PATH)
    return _set_dataset(df, "default")

# Map from TargetProfileRequest fields to the corresponding band attribute names in the request payload for users to seelct in the frontend form
FIELD_TO_BAND_ATTR = {
    "target_revenue_mm": "target_revenue_band",
    "target_ebitda_mm": "target_ebitda_band",
    "ebitda_margin_pct": "ebitda_margin_band",
    "revenue_growth_pct": "revenue_growth_band",
}

# Helper functions to resolve deal size, bands, and categorical values into structured dictionaries for the target profile. Also helps dispplay the lower, higher, median, etc. values
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
    # The 6 boundary points spanning the 5 sector-relative quintiles, carried on the resolved band so matching.py
    # can bucket an acquirer's own median value into the same bands without re-deriving them from metadata.
    band_edges = [field_bands["bands"][0]["min"]] + [b["max"] for b in field_bands["bands"]]
    if band_value == "lower":
        return {"band": "lower", "label": field_bands["lower_label"], "min": None, "max": field_bands["bands"][0]["min"], "is_median": False, "band_edges": band_edges}
    if band_value == "higher":
        return {"band": "higher", "label": field_bands["higher_label"], "min": field_bands["bands"][-1]["max"], "max": None, "is_median": False, "band_edges": band_edges}

    for b in field_bands["bands"]:
        if b["key"] == band_value:
            return {"band": band_value, "label": b["label"], "min": b["min"], "max": b["max"], "is_median": b["is_median"], "band_edges": band_edges}

    raise ValueError(f"'{band_value}' is not a valid option for '{dp.FIELD_LABELS[field_key]}'.")

def _resolve_categorical(value: str | None, options: list[str]) -> dict | None:
    if not value:
        return None
    if value not in options:
        raise ValueError(f"'{value}' is not a recognized option.")
    return {"value": value}

# Use the user's inputs to build a structured target profile dictionary, resolving deal size, bands, and categorical values. Raises ValueError if any input is invalid
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

# Serializes one SSE event
def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"

# Runs `work(on_event)` as a background task, streaming whatever it emits via on_event() as SSE
# events, finishing with a 'done' event carrying work's return value, or an 'error' event if it
# raises. `work` keeps running server-side (asyncio.create_task) even if the client disconnects.
def _stream_sse(work: Callable[[Callable[[dict], None]], Awaitable[dict]]) -> StreamingResponse:
    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        def on_event(event: dict) -> None:
            queue.put_nowait(event)

        async def run_job():
            try:
                done_payload = await work(on_event)
                await queue.put({"type": "done", **done_payload})
            except Exception as e:
                await queue.put({"type": "error", "message": str(e)})

        job = asyncio.create_task(run_job())
        while True:
            event = await queue.get()
            yield _sse(event)
            if event["type"] in ("done", "error"):
                break
        await job

    return StreamingResponse(event_stream(), media_type="text/event-stream")

# Endpoint to submit a target profile and get matching candidates. Validates the dataset is loaded, builds the
# target profile, then streams live Tier 1/2/3 progress (SSE) so the UI can show what's happening in the backend,
# finishing with a 'done' event carrying the same {target_profile, candidate_search} shape a plain response used to.
# Async now: Tier 3 (inside matching.find_candidates) may invoke the relaxation-planning agent when Tier 1+2 fall short.
@app.post("/api/target-profile")
async def submit_target_profile(payload: TargetProfileRequest):
    if STATE["metadata"] is None:
        raise HTTPException(503, "Dataset not loaded.")
    try:
        profile = build_target_profile(payload, STATE["metadata"])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    # Formalized "inferred values" (likely adjacent sectors + top strategic tags) computed once here
    # and carried on the profile from this point on -- Tier 1/2 and the Tier 3 agent both reference
    # this instead of recalculating it themselves.
    profile["inferred_values"] = matching.compute_inferred_values(profile["sector"], profile["deal_size_mm"]["value"], STATE["df"])
    df = STATE["df"]

    async def work(on_event):
        candidate_search = await matching.find_candidates(profile, df, on_event=on_event)
        return {"target_profile": profile, "candidate_search": candidate_search}

    return _stream_sse(work)

# Endpoint to generate the top-10 LLM rationales. Rebuilds the profile + candidates the same way
# /api/target-profile does (cheap, deterministic), then streams report_agent's live progress as
# SSE. The finished batch is also stashed in STATE so /api/reports/latest can serve it even if
# this connection drops -- run_job() below keeps running server-side regardless of the client.
@app.post("/api/reports")
async def generate_reports(payload: TargetProfileRequest):
    if STATE["metadata"] is None:
        raise HTTPException(503, "Dataset not loaded.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY is not set. Add it to .env and restart the server.")
    try:
        profile = build_target_profile(payload, STATE["metadata"])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    profile["inferred_values"] = matching.compute_inferred_values(profile["sector"], profile["deal_size_mm"]["value"], STATE["df"])
    df = STATE["df"]
    candidate_search = await matching.find_candidates(profile, df)

    async def work(on_event):
        report_run = await report_agent.generate_reports(profile, candidate_search["candidates"], on_event=on_event)
        STATE["last_report_run"] = {"target_profile": profile, "candidate_search": candidate_search, "report_run": report_run}
        return {"target_profile": profile, "candidate_search": candidate_search, "report_run": report_run}

    return _stream_sse(work)

# Retrieves the most recently completed report batch, so results survive a dropped SSE connection
# or a page reload without recomputing/re-spending tokens. Returns 404 if none has completed yet.
@app.get("/api/reports/latest")
def get_latest_report_run():
    if STATE["last_report_run"] is None:
        raise HTTPException(404, "No completed report run yet.")
    return STATE["last_report_run"]

# Downloads the most recently completed report batch as a formatted Word doc (one page per acquirer).
# Builds straight off STATE["last_report_run"] -- no regeneration, no new tokens spent. 404 if none yet.
@app.get("/api/reports/download")
def download_report_docx():
    run = STATE["last_report_run"]
    if run is None:
        raise HTTPException(404, "No completed report run yet.")
    buffer = docx_export.build_report_docx(run["target_profile"], run["report_run"])
    filename = f"MA_acquirer_rationales.docx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
