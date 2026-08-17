"""
Per-acquirer LLM rationale agent which runs one agentic
conversation per shortlisted acquirer (top 10, concurrently, capped by MAX_CONCURRENCY),
producing a validated one-page report structured into the six required sections.

Everything the model definitely needs (target profile, inferred values, this acquirer's
own tier/scores/matching deals/tier3 justification) is assembled up front by _build_context
and handed over in the first message -- no tool call needed for it. Beyond that, the model
has exactly one optional tool (pull this acquirer's full transaction history, at most once)
and one mandatory tool (submit the finished rationale). At most 3 model turns before it must
submit; one chance to self-correct if the submission fails schema validation; any unhandled
failure marks just that acquirer as dropped without taking down the rest of the batch.
"""

from __future__ import annotations
import asyncio
import json
import os
import statistics
import time
from typing import Callable, Literal
import anthropic
from pydantic import BaseModel, Field, ValidationError

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
MAX_TURNS = 3  # Exploration turns before submit_rationale must be called
MAX_CONCURRENCY = 5  # How many acquirer agent loops run at once

# Structured output schema for the six required sections -- validated after the model submits, not trusted blindly
class AcquirerReport(BaseModel):
    internal_reasoning: str = Field(description="Brief, honest reasoning on whether this acquirer is a strong or weak fit and why. Logged for review, not shown to the banker.")
    acquirer_overview: str = Field(description="Who they are: size, strategic priorities, recent M&A activity implied by the dataset.")
    strategic_fit_thesis: str = Field(description="Why this target makes sense for this acquirer specifically, grounded in the data.")
    precedent_activity: str = Field(description="Relevant prior transactions from the dataset: sector, size, deal type, multiples.")
    valuation_context: str = Field(description="Relevant EV/EBITDA and EV/Revenue comps from comparable closed deals in the dataset.")
    risk_flags: list[str] = Field(min_length=2, description="At least 2 concrete risks (e.g. antitrust, integration complexity, financing capacity, competitive process).")
    conviction_level: Literal["High", "Medium", "Low"]
    conviction_rationale: str = Field(description="1-2 sentences tying the conviction level to a specific data signal (a score, deal count, or multiple).")

# Tool definitions for agent
LOOKUP_HISTORY_TOOL = {
    "name": "lookup_transaction_history",
    "description": "Return this acquirer's full historical deal list from the dataset, beyond the qualifying deals already given. Only call this if the context you already have isn't enough to write a specific, confident rationale -- do not call it out of habit. You may call it at most once.",
    "input_schema": {"type": "object", "properties": {}},
}

SUBMIT_RATIONALE_TOOL = {
    "name": "submit_rationale",
    "description": "Submit the completed one-page acquirer rationale, structured into the six required sections. Call this only when you're ready to finalize.",
    "input_schema": AcquirerReport.model_json_schema(),
}

# System prompt detailing the 6 required sections
def _system_prompt() -> str:
    return f"""
You are a senior M&A associate writing a one-page acquirer rationale that a VP could send to
an MD without editing. You are told which acquirer to write about and given its full match
record up front.

Produce exactly six sections, in this order:
1. Acquirer Overview -- who they are: size, strategic priorities, recent M&A activity implied
   by the dataset.
2. Strategic Fit Thesis -- why this target makes sense for this acquirer specifically,
   grounded in the data.
3. Precedent Activity -- relevant prior transactions from the dataset: sector, size, deal
   type, multiples.
4. Valuation Context -- relevant EV/EBITDA and EV/Revenue comps from comparable closed deals
   in the dataset.
5. Risk Flags -- at least 2 risks (e.g. antitrust, integration complexity, financing
   capacity, competitive process).
6. Conviction Level -- High/Medium/Low with a 1-2 sentence rationale tied to data signals.

Grounding rule: every claim in Strategic Fit Thesis, Precedent Activity, and Valuation
Context must be backed by a specific number or named deal from the data you were given --
no unsupported claims. This is the bar to hit: "This acquirer has completed 6 Healthcare
Services deals in the $150-300M range with a median EV/EBITDA of 13.2x." Never write
anything that sounds like this, word for word from a real weak submission on this exact
assessment: "This acquirer is a leading healthcare company with a track record of strategic
acquisitions..." -- that is generic boilerplate that could describe any acquirer, and
writing anything like it is an automatic failure.

Conviction level cannot be asserted on its own, please tie it explicitly to a specific data
signal you were given. You are given match_signals: structured facts about how this
acquirer's own deal history compares to the target profile --
sector_fit/matched_sector (which sector actually matched: the exact target sector, the 1st or
2nd most common adjacent sector, or none), deal_size_fit (within/wider-than/outside the
target's typical size range), optional_criteria_fit (per optional field the banker specified,
whether this acquirer's own typical value matches/is adjacent to/doesn't match it, plus the
acquirer's actual value and the banker's target value), strategic_tag/strategic_tag_rank
(this acquirer's most common deal rationale, and whether it's the 1st/2nd/3rd most common
rationale among deals in this sector/size, or not in the top 3), and
years_since_last_closed_deal. Describe these in your own words, in plain business English --
you were not given, and must not invent, a numeric match score or percentage anywhere (e.g.
never write "primary score of 100%" or "an 83% match" or "a perfect sector match" implying a
number). Never quote match_signals' field names or enum values verbatim either (e.g. don't
write "deal_size_fit is within_range" or "match_signals: sector_fit = exact_target") -- those
are internal labels, not banker-facing language; translate them into a normal sentence
instead. Cite match_signals' actual values, plus the real numbers in matching_deals/
transaction history (deal sizes, multiples, dates, tag names), instead.

Weight match_signals in the order they're given -- sector_fit and deal_size_fit matter most,
then optional_criteria_fit, then strategic_tag_rank, then recency -- and be nuanced: e.g. if
sector/deal-size fit is strong but recency is weak, still write a rationale grounded
in the strong signals while noting the recency gap as a caveat, rather than letting one weak
signal drag down an otherwise strong case. Similarly, if given multiple medium signals with 
1-2 weak signals, let this build a case for moving the conviction level down. 
Essentially, be nuanced in your analysis and take all factors into consideration. Build an
arguement, don't just make isolated claims.  

Before calling submit_rationale, use its internal_reasoning field to briefly think through
whether this acquirer is genuinely a strong fit or a weak one and why. This is logged for
later review, not shown to the banker -- be honest even if your conclusion is "weak fit."

You have one optional tool, lookup_transaction_history, returning this acquirer's full deal
history beyond what's already in front of you. Only call it if the context you already have
isn't enough to write a specific, confident rationale -- do not call it out of habit. You
may call it at most once, and you have at most 3 exchanges total before you must call
submit_rationale. Reasons to call this tool may include wanting to cite a specific deal from 
the acquirer's history or referencing other data signals like deal_type. 
"""

def _initial_message(acquirer: str, context: dict) -> str:
    return f"Write the rationale for {acquirer}. Here is everything known so far:\n{json.dumps(context, default=str)}"

# Deterministic tool to aggregate stats so multiples the model cites are real numbers, so its nto hallucinated arithmetic over a raw table
# Shared by the upfront context and the lookup tool
def _format_deals(deals: list[dict]) -> dict:
    closed = [d for d in deals if d.get("outcome") == "Closed"]
    ev_ebitda = [d["ev_ebitda_multiple"] for d in closed if d.get("ev_ebitda_multiple") is not None]
    ev_revenue = [d["ev_revenue_multiple"] for d in closed if d.get("ev_revenue_multiple") is not None]
    return {
        "deals": deals,
        "count": len(deals),
        "closed_count": len(closed),
        "median_ev_ebitda_multiple": round(statistics.median(ev_ebitda), 1) if ev_ebitda else None,
        "median_ev_revenue_multiple": round(statistics.median(ev_revenue), 1) if ev_revenue else None,
    }

# Reshapes the candidate's own scoring sub-components (already computed once by matching.py's _primary_score/_secondary_score/_strategic_match_score/_relevancy_score) into the values the model needs, with the numeric 0-100 score fields left out
def _match_signals(candidate: dict, optional_criteria: dict) -> dict:
    primary, secondary = candidate["primary_score"], candidate["secondary_score"]
    strategic, relevancy = candidate["strategic_score"], candidate["relevancy_score"]
    return {
        "sector_fit": primary["sector_fit"],  # exact_target / adjacent_1 / adjacent_2 / none
        "matched_sector": primary["matched_sector"],
        "deal_size_fit": primary["deal_size_fit"],  # within_range / wider_range / outside_range
        "optional_criteria_fit": {
            field: {"fit": fs["fit"], "acquirer_value": fs["acquirer_value"], "target_value": optional_criteria.get(field)}
            for field, fs in secondary["field_scores"].items()
        },
        "strategic_tag": strategic["mode_tag"],
        "strategic_tag_rank": strategic["rank"],  # 1/2/3/None ... rank of strategic_tag among the sector's top 3 implied tags
        "years_since_last_closed_deal": relevancy["years_since_most_recent_closed_deal"],
    }

# Everything every run needs (target profile, inferred values, and the candidate's own match record, reshaped into match_signals rather than raw scores
def _build_context(profile: dict, candidate: dict) -> dict:
    optional_fields = ["target_revenue_mm", "target_ebitda_mm", "ebitda_margin_pct", "revenue_growth_pct", "geography", "target_ownership_pre"]
    optional_criteria = {f: profile[f].get("label", profile[f].get("value")) for f in optional_fields if profile.get(f)}
    return {
        "target_profile": {"sector": profile["sector"], "deal_size": profile["deal_size_mm"]["label"], "optional_criteria": optional_criteria},
        "inferred_values": profile["inferred_values"],
        "acquirer": candidate["acquirer"],
        "acquirer_type": candidate["acquirer_type"],
        "tier": candidate["tier"],
        "tier_deal_counts": candidate["tier_deal_counts"],
        "match_signals": _match_signals(candidate, optional_criteria),
        "tier3_relaxation": candidate.get("tier3_relaxation"),
        "matching_deals": _format_deals(candidate["matching_deals"]),
    }

# Validate the model's submission against the schema, returning either a valid report or an error string which will result in rerty
def _validate_report(raw_input: dict) -> tuple[AcquirerReport | None, str | None]:
    try:
        return AcquirerReport.model_validate(raw_input), None
    except ValidationError as e:
        return None, str(e)

# Runs one acquirer's whole conversation up to MAX_TURNS exploration turns (auto tool choice, so the model can reason in free text between calls), forcing submit_rationale on the last turn
async def _generate_one(client: anthropic.AsyncAnthropic, candidate: dict, context: dict, semaphore: asyncio.Semaphore, on_event: Callable[[dict], None] | None) -> dict:
    acquirer = candidate["acquirer"]

    # Event emitter for this acquirer, if the caller wants to receive progress updates
    def emit(event: dict) -> None:
        if on_event:
            on_event({**event, "acquirer": acquirer})

    emit({"type": "report_started"})

    # Build the system prompt and initial message, set up the conversation state, and run the agent loop
    system = _system_prompt()
    messages = [{"role": "user", "content": _initial_message(acquirer, context)}]
    tools = [LOOKUP_HISTORY_TOOL, SUBMIT_RATIONALE_TOOL]
    usage = {"input_tokens": 0, "output_tokens": 0}
    submit_call = None

    # Run the agent loop with a semaphore to limit concurrency (5 right now)
    async with semaphore:
        try:
            # Iterate up to MAX_TURNS letting the model choose between free text reasoning and tool calls but forcing submit_rationale on the last turn
            for turn in range(1, MAX_TURNS + 1):
                forced = turn == MAX_TURNS
                tool_choice = {"type": "tool", "name": "submit_rationale"} if forced else {"type": "auto"}
                response = await client.messages.create(model=MODEL, max_tokens=4096, system=system, tools=tools, tool_choice=tool_choice, messages=messages)
                usage["input_tokens"] += response.usage.input_tokens # Token tracking
                usage["output_tokens"] += response.usage.output_tokens # Token trakcing
                messages.append({"role": "assistant", "content": response.content}) # Append the model's response to the conversation history

                submit_call = next((b for b in response.content if b.type == "tool_use" and b.name == "submit_rationale"), None)
                if submit_call:
                    # End agent loop if time to submit report! 
                    break 

                lookup_call = next((b for b in response.content if b.type == "tool_use" and b.name == "lookup_transaction_history"), None)
                if lookup_call:
                    emit({"type": "report_tool_called", "tool": "lookup_transaction_history"})
                    tools = [SUBMIT_RATIONALE_TOOL]  # At most one lookup, structurally can't call again
                    history = _format_deals(candidate["all_deals"])
                    messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": lookup_call.id, "content": json.dumps(history, default=str)}]})
                    continue
                messages.append({"role": "user", "content": "Continue, or call submit_rationale now."})

            # Validate the submission, retry once if it fails and return final result or failure reason
            report, error = _validate_report(submit_call.input if submit_call else {})
            if report is None:
                emit({"type": "report_retry", "reason": error})
                messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": submit_call.id, "content": f"Invalid submission: {error}. Fix and resubmit.", "is_error": True}]})
                response = await client.messages.create(model=MODEL, max_tokens=4096, system=system, tools=[SUBMIT_RATIONALE_TOOL], tool_choice={"type": "tool", "name": "submit_rationale"}, messages=messages)
                usage["input_tokens"] += response.usage.input_tokens
                usage["output_tokens"] += response.usage.output_tokens
                retry_call = next((b for b in response.content if b.type == "tool_use"), None)
                report, error = _validate_report(retry_call.input if retry_call else {})
            
            # Return the final result or failure reason
            if report is None:
                emit({"type": "report_failed", "reason": error})
                return {"failed": True, "acquirer": acquirer, "reason": error, "token_usage": usage}

            # Results include the acquirer, tier, scores, the validated report, and token usage
            result = {
                "failed": False, "acquirer": acquirer, "tier": candidate["tier"],
                "primary_score": candidate["primary_score"]["score"],
                "secondary_score": candidate["secondary_score"]["score"],
                "strategic_score": candidate["strategic_score"]["score"],
                "relevancy_score": candidate["relevancy_score"]["score"],
                "report": report.model_dump(), "token_usage": usage,
            }

            # Emit success event
            emit({"type": "report_succeeded"})
            return result
        
        except Exception as e:
            emit({"type": "report_failed", "reason": str(e)})
            return {"failed": True, "acquirer": acquirer, "reason": str(e), "token_usage": usage}

# Top 10 candidates run concurrently (5 at a time)
async def generate_reports(profile: dict, candidates: list[dict], on_event: Callable[[dict], None] | None = None) -> dict:
    # Event emitter
    def emit(event: dict) -> None:
        if on_event:
            on_event(event)

    # Pulll top 10 candidates 
    top10 = candidates[:10]
    emit({"type": "report_batch_started", "acquirers": [c["acquirer"] for c in top10]})

    # Instantiate the async Anthropics client, set up a semaphore for concurrency, and track start time
    client = anthropic.AsyncAnthropic()
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    started = time.monotonic()

    # Call generation for each acquirer concurrenlty
    results = await asyncio.gather(*[
        _generate_one(client, c, _build_context(profile, c), semaphore, emit) for c in top10
    ])

    # Separate delivered and dropped reports, aggregate token usage, and return a summary
    delivered = [r for r in results if not r["failed"]]
    dropped = [r for r in results if r["failed"]]
    total_usage = {
        "input_tokens": sum(r["token_usage"]["input_tokens"] for r in results),
        "output_tokens": sum(r["token_usage"]["output_tokens"] for r in results),
    }
    total_usage["total_tokens"] = total_usage["input_tokens"] + total_usage["output_tokens"]

    # Return a summary of the run, including counts of delivered and dropped reports, total token usage, and elapsed time
    return {
        "delivered": delivered,
        "dropped": dropped,
        "requested_count": len(top10),
        "delivered_count": len(delivered),
        "token_usage": total_usage,
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }
