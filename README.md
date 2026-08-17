# M&A Acquirer Identification Engine

Given a target company profile, this finds likely acquirers from a historical M&A
transaction dataset, scores and ranks them, and writes a one-page rationale for the
top 10 using an LLM. Everything through the ranked candidate list is deterministic.
The LLM only shows up in two places: widening the search when the deterministic
tiers don't turn up enough candidates, and writing the final rationales, both of
which are genuinely judgment calls rather than lookups.

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY
python3 run.py
```

`run.py` starts the server and opens `http://127.0.0.1:8000` in your browser. The
sample dataset (`data/ma_transactions_500.csv`, 500 transactions across 10 sectors)
loads automatically, so there's nothing else to set up.

You don't need an API key to upload a CSV, fill out the target profile form, or see
Tier 1/2 matches. The key is used in two places: automatically inside "Build Target
Profile" if Tier 1 and 2 come up short and the Tier 3 relaxation agent kicks in (no
key just means Tier 3 is skipped and the app tells the user it found fewer candidates than
usual), and in "Generate Reports," which creates the final output. To use this part of
the app, just create a copy of te .env.example file, name it ".env", and fill with your 
Claude API key. 

## How it works

**1. CSV ingestion.** The dataset gets validated against a fixed 24 column schema on
load, whether it's the pre-stored sample or something teh user uploads. Wrong columns, wrong
types, or missing values in the two mandatory fields (`sector`, `deal_size_mm`) fail
immediately with a specific message rather than a stack trace or a quietly wrong
result. This portion is purely deterministic, and just validates that the user inputted 
data is a valid input. 

**2. Target profile form.** The user picks a sector and a deal size (the two required
fields), plus whatever optional fields the user wants to use when finding potenital 
acquirer candidates: revenue, EBITDA, margin, growth, geography, ownership. 
I choose to have the numeric optional fields banded into five ranges per sector, computed from that sector's 
own 20/40/60/80 percentiles, because the same dollar figure means something very different in Dental versus Pharma/Biotech.
Deal size itself is typed directly rather than banded, since a banker usually knows
the number, and it is banded using +/- percentages later during matching. 

**3. Tier 1 and Tier 2 matching, deterministic.** Tier 1 pulls every acquirer with a
deal in the target's own sector, within plus or minus 20% of the target deal size.
If that's fewer than 10 acquirers, Tier 2 adds acquirers from the single most common
other sector across acquirers who transact in the target deal size. The "most common other sector" isn't a
hardcoded table of related sectors, it's computed straight from the dataset by
counting which sector actually shows up most often near the target deal size, since I wanted the logic
to not be tailored any specific CSV and hold up on differing datasets.
However, on this dataset a handful of generalist PE acquirers transact across almost every deal size, so the top 2 
often collapse to the same pair regardless of the target, 
since those firms dominate the overlap count everywhere. This is a limitation for sure, 
and given more time, something that would be revised and tested. 

**4. Tier 3 matching, agentic.** If Tier 1 and 2 combined still haven't found 10
candidates, a single agent (`backend/tier3_agent.py`, one call per search, not per
acquirer) proposes a plan for widening the search by deciding which additional sector(s) to
include, how far to expand the deal size window, and whether to also pull in
acquirers who share the target sector's most common strategic rationale tags. It
gets up to two turns, sees the real resulting candidate count after each attempt,
and can propose something broader if the first plan falls short. The agent never
touches the data itself, it only proposes a plan, and `matching.py` executes it
deterministically. This is one of the places I identified an LLM or agent could be used for decision making, because
determining how far to widen a filter and in which direction isn't always a fixed rule, 
and in this case, can be dependant on the user inputs. 
In this stepm, if there's no API key set or both turns fall short, the code proceeds
anyway with a low match warning rather than blocking the flow. 

**5. Scoring, deterministic.** Every candidate acquirer gets four independent
scores, all computed in `matching.py` off the acquirer's own qualifying deals:

- *Primary*: sector fit plus deal size fit, averaged. Sector fit is 100 if any of
  the acquirer's qualifying deals were in the target's own sector, 67 if in the
  first implied sector, 33 if in the second implied sector, else 0. Deal size fit
  is 100 if any qualifying deal is within plus or minus 20% of the target deal
  size, 50 if within the wider 20 to 40% band, else 0. The two are averaged for
  the final primary score.
- *Secondary*: average fit across whichever optional fields the user actually filled
  in, each judged against the acquirer's own qualifying deals, not their full
  history. For numeric fields (revenue, EBITDA, margin, growth) it takes the
  acquirer's median value across those deals, bands it the same sector-relative
  way the target profile itself is banded, and scores 100 if that lands in the
  same band teh user selected, 50 if one band off, else 0. For categorical fields
  (geography, ownership) it takes the acquirer's most common value across those
  deals and scores 100 if it matches the user's selection, else 0. Any field with no
  data from the acquirer scores 0 for that field. If the user left every optional
  field blank, there's no secondary score at all rather than a misleading number.
- *Strategic*: takes the acquirer's own most common strategic rationale tag
  across its qualifying deals, then checks it against the target sector's top 3
  implied tags. 100 if it's the #1 tag, 67 if #2, 33 if #3, else 0, including
  when the acquirer has no tags on record at all.
- *Relevancy*: looks only at the acquirer's qualifying deals with an outcome of
  Closed, finds how many years ago the most recent one closed, and applies
  exponential decay, 100 times e to the power of negative 0.35 times years since.
  That decay rate gives roughly a two year half life, so a deal 2 years old
  scores about half of a deal closed today. Zero if there are no closed deals at
  all in the qualifying set.

None of this involves the model. The categorical explanation for each score
("exact sector match," "within the wider deal size range," and so on) is computed
in the same place as the numeric score itself, so nothing gets recalculated
downstream, the report agent and the UI's tooltips both just read off the same
fields. 

A genuine limitation here is this is a score mechanism that was derived via human judgement (aka me lol), 
not a tested, industry-standrd methodology. This is defintely something that would be researched 
and remedied before moving to a production grade system.  

**6. Candidate ranking, deterministic.** Candidates are stack ranked by tier first,
then the four scores descending. This is a plain sort, no judgment involved, which
is currently a gap in my opinion. I'd originally planned a second agent whose job was
to look specifically at Tier 2 and Tier 3 candidates and decide whether they
genuinely deserve a spot in the top 10, versus just having cleared a mechanically
widened bar. Unfortunately, I ran out of time to build it for this round. Right now the top 10 is
purely score sorted, tier included.

**7. Report generation, agentic.** `POST /api/reports` runs one agent conversation
per shortlisted acquirer, top 10, up to 5 running concurrently, one shared Anthropic
client for the whole batch. This piece uses an LLM to write
a rationale that weighs sector fit, deal size fit, several optional criteria,
strategic alignment, and recency against each other, and then lands on a
conviction level. Each agent receives:

- Everything it needs up front in one message: the target profile, the inferred
  values, and this acquirer's own tier, match signals, and qualifying deals with
  precomputed EV/EBITDA and EV/Revenue medians (real numbers, not the model doing
  arithmetic over a raw table).
- One optional tool, `lookup_transaction_history`, for this acquirer's full deal
  history beyond what's already in context. Callable at most once, it's removed
  from the tool list the moment it's used so a second call is structurally
  impossible, not just discouraged in the prompt. This is to be used only if the agent 
  needs more details to back its rationale. 
- One mandatory tool, `submit_rationale`, forced by the third turn if it hasn't
  been called yet, which pushes the output to validation but doesn't end the agent flow so a retry is possible in case of failuer.
- The submission gets validated against a Pydantic schema (all six required
  sections present, at least two risk flags, conviction level is High, Medium, or
  Low). If it fails, the model gets exactly one corrective turn with the real
  validation error fed back. If that also fails, that acquirer is marked failed
  and dropped, and the rest of the batch keeps going so that everything doesn't fail. 
  In a way, this is another constraint at the moment, which can result in less than 10 outputs. 

The model is deliberately never handed the raw 0-100 scores computed for tiering / matching. Early on I tried
prompt engineering to force it not to cite a score, and it
cited the score anyway. Instead, I moved to context engineering by removing the number from context entirely and giving it
categorical labels instead (`sector_fit`, `deal_size_fit`, `strategic_tag_rank`, and
so on, all computed once in `matching.py` alongside the numeric scores), which was much stronger. The system prompt also
spells out the six required sections in order, a grounding rule that every claim
needs a real number or named deal behind it, both a good and a bad example lifted
from the provided take-home document, and an explicit order to weight the signals in (sector
and deal size fit first, then the optional criteria, then strategic tag, then
recency), so the agent isn't just latching onto whichever single signal happens to
be strongest.

**8. Word doc export.** `build_report_docx()` builds a formatted `.docx` straight
from the same report run already sitting in memory, no regeneration is done and no new tokens are
spent. `internal_reasoning`, the model's private fit assessment before it commits to
sections, stays out of the doc the same way it stays off the main report card in the
UI. I was planning to use this to create an agent loop which picks up another candidate if 
one of them does not meet the bar here, but I ran out of time. 

## Architecture decisions

**No build step.** Backend is FastAPI, frontend is plain HTML/CSS/JS served as
static files by the same process (one port). For the scope of this project a JS framework would 
have been overhead for no real benefit.

**In-memory state.** The active dataset and the last completed report run both live
in a module-level dict in `backend/app.py`. If this application were to be scaled to rpoduction, 
it would need a real session or a datastore before that.

**CSV handling.** The CSV is read once at startup, or replaced on upload, into a
pandas DataFrame kept in memory for the life of the process. There's no database, and as mentioned 
above, to scale to production, data management would be necessary. There's also a lot we could do with 
a user's session history to personalize reports for them and we could cache analyses 
the agent has already conducted for them to save on futture LLM runs. 

**What I'd improve given more time.** The Tier 2/3 evaluator agent mentioned above
is the most obvious gap. Past that, the matching engine itself is currently a set
of rule based tiers and percentile bands, which is simple and explainable but not
particularly scientific. I've worked with more rigorous similarity approaches
before, Gower's distance and knowledge graph based matching, with an agent layered
on top for the explainability or reporting piece, and given more runway that's the
direction I'd be inclined take the matching itself. I'd also add real persistence instead of
in-memory state, and a programmatic check on the claim grounding rule instead of
relying on the prompt alone (more on that below).

## Assumptions

**The scoring is skewed toward a specific set of fields.** Results are skewed towards sector and deal size,
since they're required, whatever optional fields the user fills in, the strategic
rationale tags, and recency of the acquirer's last closed deal. There are other
columns in the dataset (financing type, number of bidders, synergy percent of deal,
sub sector, deal type, etc.) that aren't factored into scoring right now, which to be honest, 
is mostly due to the fact that I don't yet have a strong enough sense of how
an M&A professional would actually weight those signals to bake them into a scoring
formula responsibly. Especially given the time constraint, I'd opted to leave a signal out than 
assign a weight for it without knowing if it's truly valuable.

**"Regional" for geography is an assumption, not a value in the data.** The raw
dataset only has specific region names (Northeast, Southeast, and so on) plus
Multi-Regional and National. "Regional" isn't one of them, but it's listed as an
option in the target profile description in the assignment doc, so I assumed 
it means any single-region geography, anything that's not Multi-Regional or
National.

**The plus or minus 20% deal size tolerance (40% for Tier 3's wider search)** This is a
judgment call I haven't fully validated. I considered something more scientific,
a log scale or a percentile based window instead of a fixed percentage, but kept it
simple given the time I had for this. It's a single constant in `matching.py`
(`DEAL_SIZE_TOLERANCE`) if it needs to change.

**Sector adjacency is data driven, not a hardcoded table.** Which sector counts as
"the next most related one" to a given target sector is computed by counting which
other sectors actually show up most often at a similar deal size in the dataset
itself, rather than a table I wrote by hand of which sectors are supposedly related.
Wanted the tool to generalize past this one CSV rather than being quietly tuned to it.

## Known limitations (in addition to those already mentioned)

- Report generation only gets one retry. If a submission fails schema validation
  twice for a given acquirer, that acquirer is dropped from the batch with a reason
  rather than retried further.
- Generating all 10 acquirer rationales currently takes somewhere in the 70 to 90
  second range. That's slower than I'd like, but it's the direct tradeoff for
  letting each acquirer's agent optionally pull its full transaction history via
  tool call when the upfront context isn't enough, instead of forcing every
  acquirer's entire deal history whether it needs it or not. I think it's a worthy
  tradeoff for the report quality it could buy.
- No persistence. Restarting the server, or uploading a new CSV, resets everything
  back to the bundled sample dataset, including any completed report run.
- One dataset globally, not per session, so two people using this at the same time
  would step on each other.
- No promotion or backfill. If one of the top 10 fails validation twice, the batch
  just delivers fewer than 10 rather than pulling in the next ranked candidate to
  fill the slot.
- The Tier 2/3 evaluator agent described above is still on the list, not built.

## Handling non-determinism

The facts the report agent works from are always the same for a given target
profile and dataset, because `matching.py`'s scoring is fully deterministic and gets
handed to the model as one JSON blob (match signals plus qualifying deals), not
recomputed or reinterpreted by the model itself. What can actually vary between
runs is the model's own synthesis on top of those facts, mainly the conviction
level it lands on, since that's a real judgment call across several weighted
signals rather than a fact lookup. That's the main piece that still needs
work in my opinion, and given more time, I'd use a more scientific, 
mathematical method to ensure stability in this judgement.

There's no caching and no fixed seed anywhere in the report generation path. Every
"Generate Reports" click runs all 10 acquirers fresh against the live API on
purpose, so a run always reflects the current model rather than a stale cached
result from earlier in the session. Something I mentioned above as well, but
in a production grade version, there's also a lot we could do with 
a user's session history to personalize reports for them and we could cache analyses 
the agent has already conducted for them to save on futture LLM runs. 

Validation today is structural only, through the Pydantic schema: all six sections
present, at least two risk flags, conviction level is one of High, Medium, or Low.
There's no check yet that the specific numbers cited in the prose, a deal count or
a multiple, actually appear in the data the model was given. That grounding rule is
enforced by the prompt only, not verified in code. A future version could parse the
numbers the model cites back out of the generated text and cross check them against
the real matching deals, or sanity check the conviction level against the
underlying scores instead of trusting the model's own weighting entirely.
