"""
Tier 3 relaxation-planning agent: a single agent task (not per-acquirer, runs once per search)
that decides which filters to relax when Tier 1 + Tier 2 fall short of MIN_CANDIDATES.

It never touches the dataframe itself -- it only proposes a plan (which sector(s) to widen to, how
far to expand the deal size window, and/or whether to match on shared strategic tags), and justifies
every relaxation in plain language tied to the user's actual input or the inferred values it's given.
matching.py executes that plan deterministically via the `execute_plan` callback passed in below, and
is the only thing that ever touches a row. Every optional field the user filled in on the form stays
a hard filter no matter what the agent proposes -- it's only choosing sector/size/tag levers.

Gets up to MAX_PLANNING_TURNS tries, revising its plan against the actual resulting candidate count
each time. If the API key isn't set, or any call fails, this degrades to "no plan" rather than
raising -- Tier 3 just doesn't widen further, matching.py's own low_match_warning handles the rest.
"""

from __future__ import annotations

import os
from typing import Callable

import anthropic

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

# Only iterate twice to avoid endless loops or the agent just widening the pool to the entire universe
MAX_PLANNING_TURNS = 2

# Tool to propose a relaxation plan
# Returns which sectors to widen to, how far to expand the deal size window, and/or whether to match on shared strategic tags. The agent never sees or touches the data itself, it only proposes a plan and justifies it in plain language
PROPOSE_PLAN_TOOL = {
    "name": "propose_relaxation_plan",
    "description": "Propose which sector(s) and/or strategic-tag overlap to widen the acquirer search to, and how far to expand the deal size window. You never see or touch the data directly -- after you propose a plan, you'll be told how many total candidate acquirers it produced.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sectors": {
                "type": "array", "items": {"type": "string"},
                "description": "Sector(s) to search beyond the target's own, e.g. the inferred likely_sectors. Omit or leave empty if relying only on tag matching.",
            },
            "deal_size_tolerance_pct": {
                "type": "number",
                "description": "Deal size window as +/- this fraction of the target deal size (e.g. 0.4 for +/-40%). Must be wider than the 0.2 (+/-20%) Tier 1/2 already used.",
            },
            "use_tag_matching": {
                "type": "boolean",
                "description": "If true, also include acquirers whose past deals share at least one of the target's top strategic tags, in any sector, within the deal size window.",
            },
            "justification": {
                "type": "string",
                "description": "Plain-language explanation of EACH relaxation you're applying (sector, deal size, and/or tag matching), tied to the user's actual input or the inferred values you were given. This is shown directly to a banker.",
            },
        },
        "required": ["deal_size_tolerance_pct", "justification"],
    },
}

# System prompt which explains the context (Tier 1 + Tier 2 fell short of min_candidates), the rules (which filters are hard, which are optional), and the goal (propose a plan that reaches min_candidates)
def _system_prompt(min_candidates: int) -> str:
    return f"""
You are helping a banker widen an acquirer search that came up short. Tier 1 (the \
target's own sector) and Tier 2 (the single most common adjacent sector, at the same deal size) have \
already run and didn't reach {min_candidates} candidate acquirers between them. Propose a Tier 3 relaxation plan.

Focus on strategic tag matching first and foremost, especially if relaxing sector constraints.
Aggressive sector widening and deal size expansion are secondary resorts, \
but you may propose them if litle to no candidates are found with tag matching alone.

Rules:
- Every optional field the user filled in on the form (geography, ownership, revenue, EBITDA, margin, \
growth) is enforced as a hard filter automatically no matter what you propose -- you cannot relax \
those, so don't try to. You are only choosing: which sector(s) to add, how far to widen the deal size, \
and whether to also match on shared strategic tags.
- Justify every relaxation you propose in plain language, tied to the user's actual input or the \
inferred values you're given (the likely adjacent sectors, the top strategic tags for this sector and \
size). This justification is shown directly to the banker, so make it concrete, not generic.
- You get up to {MAX_PLANNING_TURNS} tries. If your first plan doesn't reach {min_candidates} \
candidates, you'll be told the actual resulting count and can propose a broader plan.
- Call propose_relaxation_plan with your plan.
"""

# Starting message to the agent 
def _initial_message(target_profile: dict, current_count: int, min_candidates: int) -> str:
    inferred = target_profile["inferred_values"]
    return (
        f"Sector: {target_profile['sector']}. Deal size: {target_profile['deal_size_mm']['label']}.\n"
        f"Inferred likely adjacent sectors (ranked, most common first): {inferred['likely_sectors']}.\n"
        f"Top strategic tags for this sector/size: {inferred['top_strategic_tags']}.\n"
        f"Tier 1 + Tier 2 found {current_count} candidate acquirer(s) so far; the target is {min_candidates}-20.\n"
        "Propose a relaxation plan to close the gap."
    )

# Tier 3 agent flow 
async def plan_and_execute_tier3(target_profile: dict, current_count: int, min_candidates: int, execute_plan: Callable[[dict], tuple],) -> dict:
    """
    Drives up to MAX_PLANNING_TURNS of propose -> execute -> (maybe revise). `execute_plan(plan)`
    is matching.py's deterministic executor -- it returns (subset_df, resulting_total_count); this
    function only talks to the model and decides when to stop, it never touches a dataframe itself.
    Returns {"attempts": [...], "used_plan": dict|None, "used_subset": df|None}.
    """
    
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"attempts": [], "used_plan": None, "used_subset": None, "skipped_reason": "ANTHROPIC_API_KEY not set"}

    # Set up the client and initial messages
    client = anthropic.AsyncAnthropic()
    system = _system_prompt(min_candidates)
    messages = [{"role": "user", "content": _initial_message(target_profile, current_count, min_candidates)}]
    attempts: list[dict] = []
    best: tuple[dict, object, int] | None = None

    # Loop through agent turns
    for turn in range(MAX_PLANNING_TURNS):
        try:
            response = await client.messages.create(
                model=MODEL, max_tokens=1000, system=system, tools=[PROPOSE_PLAN_TOOL],
                tool_choice={"type": "tool", "name": "propose_relaxation_plan"}, messages=messages,
            )
        except Exception as e:
            attempts.append({"error": str(e)})
            break
        
        # Find the tool_use message in the response
        tool_use = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_use is None:
            break

        # Execute the proposed plan and record the result
        plan = tool_use.input
        subset, total_count = execute_plan(plan)
        attempts.append({"plan": plan, "resulting_total_count": total_count})
        if best is None or total_count > best[2]:
            best = (plan, subset, total_count)

        # Break if we reached the target or if it's the last turn
        if total_count >= min_candidates or turn == MAX_PLANNING_TURNS - 1:
            break

        # Feed the real resulting count back so turn 2 can propose something broader, not just different.
        messages.append({"role": "assistant", "content": response.content})
        messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use.id, "content":
                f"That plan produced {total_count} total candidate acquirers (target: {min_candidates}-20). "
                "Still short, please propose a broader plan."}],
        })

    # Finalize the best plan
    if best is None:
        return {"attempts": attempts, "used_plan": None, "used_subset": None}
    plan, subset, total_count = best
    return {"attempts": attempts, "used_plan": plan, "used_subset": subset, "used_total_count": total_count}
