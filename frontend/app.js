const FIELD_TO_BAND_SELECT = {
  target_revenue_mm: "target_revenue_band",
  target_ebitda_mm: "target_ebitda_band",
  ebitda_margin_pct: "ebitda_margin_band",
  revenue_growth_pct: "revenue_growth_band",
};

const formatDollar = (v) => (v >= 1000 ? `$${(v / 1000).toFixed(1)}B` : `$${Math.round(v)}M`);

const SECONDARY_FIELD_LABELS = {
  target_revenue_mm: "Target Revenue",
  target_ebitda_mm: "Target EBITDA",
  ebitda_margin_pct: "EBITDA Margin",
  revenue_growth_pct: "Revenue Growth",
  geography: "Geography",
  target_ownership_pre: "Ownership (pre-deal)",
};

// Plain-English notes for matching.py's own categorical fit labels (sector_fit/deal_size_fit/rank),
// rather than re-deriving them here from the numeric score
const SECTOR_FIT_NOTES = {
  exact_target: (s) => `target sector: ${s.matched_sector}`,
  adjacent_1: (s) => `1st implied sector: ${s.matched_sector}`,
  adjacent_2: (s) => `2nd implied sector: ${s.matched_sector}`,
  none: () => "no sector match",
};
const DEAL_SIZE_FIT_NOTES = {
  within_range: "within ±20% of target size",
  wider_range: "within ±20-40% of target size",
  outside_range: "outside ±40% of target size",
};
const STRATEGIC_RANK_NOTES = {
  1: "the sector's #1 implied tag",
  2: "the sector's #2 implied tag",
  3: "the sector's #3 implied tag",
};

let metadata = null;
let lastSubmittedPayload = null;

const $ = (id) => document.getElementById(id);

// Reads a fetch Response's SSE body, calling onEvent(event) for each `data: {...}` chunk as it arrives
async function readSSEStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop(); // last chunk may be incomplete, keep it for next read
    for (const chunk of chunks) {
      if (!chunk.startsWith("data: ")) continue;
      onEvent(JSON.parse(chunk.slice(6)));
    }
  }
}

async function fetchMetadata() {
  const res = await fetch("/api/metadata");
  metadata = await res.json();
  renderDatasetStatus();
  renderSectorOptions();
  renderStaticOptionalFields();
}

function renderDatasetStatus() {
  const label = metadata.source === "default" ? "Sample dataset (ma_transactions_500.csv)" : metadata.source;
  $("dataset-status").innerHTML = `<strong>${label}</strong> — ${metadata.row_count} transactions, ${metadata.sectors.length} sectors`;
}

function renderSectorOptions() {
  const select = $("sector");
  const current = select.value;
  select.innerHTML = '<option value="" disabled selected>Select a sector…</option>';
  for (const sector of metadata.sectors) {
    const opt = document.createElement("option");
    opt.value = sector;
    opt.textContent = sector;
    select.appendChild(opt);
  }
  if (metadata.sectors.includes(current)) select.value = current;
}

function renderStaticOptionalFields() {
  fillCategoricalSelect("geography", metadata.geography_options, "Not specified");
  fillCategoricalSelect("target_ownership_pre", metadata.ownership_options, "Not specified");
  $("geography").disabled = false;
  $("target_ownership_pre").disabled = false;
}

function fillCategoricalSelect(id, options, blankLabel) {
  const select = $(id);
  select.innerHTML = `<option value="">${blankLabel}</option>`;
  for (const opt of options) {
    const el = document.createElement("option");
    el.value = opt;
    el.textContent = opt;
    select.appendChild(el);
  }
}

function fillBandSelect(id, fieldKey, required) {
  const select = $(id);
  const sector = $("sector").value;
  const fieldBands = sector ? metadata.bands[sector][fieldKey] : null;

  select.innerHTML = "";
  if (!fieldBands) {
    select.innerHTML = '<option value="" disabled selected>Select a sector first…</option>';
    select.disabled = true;
    return;
  }

  if (!required) {
    select.appendChild(new Option("Not specified", ""));
  } else {
    select.appendChild(new Option("Select a range…", "", true, true));
    select.firstChild.disabled = true;
  }
  select.appendChild(new Option(fieldBands.lower_label, "lower"));
  for (const band of fieldBands.bands) {
    select.appendChild(new Option(band.label, band.key));
  }
  select.appendChild(new Option(fieldBands.higher_label, "higher"));
  select.disabled = false;
}

function updateDealSizeHint(sector) {
  const hint = $("deal_size_hint");
  const fieldBands = sector ? metadata.bands[sector].deal_size_mm : null;
  if (!fieldBands) {
    hint.innerHTML = "";
    return;
  }
  const lo = fieldBands.bands[0].min;
  const hi = fieldBands.bands[fieldBands.bands.length - 1].max;
  hint.innerHTML = `${sector} deals range ${formatDollar(lo)}–${formatDollar(hi)} · median <strong>${formatDollar(fieldBands.median)}</strong>`;
}

function onSectorChange() {
  const sector = $("sector").value;
  updateDealSizeHint(sector);
  for (const fieldKey of ["target_revenue_mm", "target_ebitda_mm", "ebitda_margin_pct", "revenue_growth_pct"]) {
    fillBandSelect(FIELD_TO_BAND_SELECT[fieldKey], fieldKey, false);
  }
}

function renderResult(profile) {
  const rows = [];
  rows.push(["Sector", profile.sector]);
  rows.push(["Deal Size (EV, $mm)", formatDollar(profile.deal_size_mm.value)]);
  for (const key of Object.keys(FIELD_TO_BAND_SELECT)) {
    const val = profile[key];
    rows.push([SECONDARY_FIELD_LABELS[key], val ? val.label : "Not specified"]);
  }
  rows.push(["Geography", profile.geography ? profile.geography.value : "Not specified"]);
  rows.push(["Ownership (pre-deal)", profile.target_ownership_pre ? profile.target_ownership_pre.value : "Not specified"]);

  const dl = document.createElement("dl");
  for (const [term, desc] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = term;
    const dd = document.createElement("dd");
    dd.textContent = desc;
    dl.appendChild(dt);
    dl.appendChild(dd);
  }
  const summary = $("result-summary");
  summary.innerHTML = "";
  summary.appendChild(dl);
  $("result-json").textContent = JSON.stringify(profile, null, 2);
  $("result-panel").classList.remove("hidden");
}

function renderTier3Justification(tr) {
  const banner = $("tier3-justification");
  banner.innerHTML = "";
  if (!tr) {
    banner.classList.add("hidden");
    return;
  }

  const plan = tr.used_plan;
  if (!plan) {
    const p = document.createElement("p");
    p.style.margin = "0";
    p.textContent = tr.skipped_reason
      ? `Tier 3 widening was skipped: ${tr.skipped_reason}.`
      : "Tier 3 widening was attempted but produced no usable plan.";
    banner.appendChild(p);
    banner.classList.remove("hidden");
    return;
  }

  const summaryParts = [];
  if (plan.sectors && plan.sectors.length) summaryParts.push(`sectors: ${plan.sectors.join(", ")}`);
  if (plan.deal_size_tolerance_pct) summaryParts.push(`deal size ±${Math.round(plan.deal_size_tolerance_pct * 100)}%`);
  if (plan.use_tag_matching) summaryParts.push("shared strategic tags");

  const header = document.createElement("div");
  const label = document.createElement("strong");
  label.textContent = "Pool widened (Tier 3): ";
  const summary = document.createElement("span");
  summary.textContent = summaryParts.join(" · ");
  header.appendChild(label);
  header.appendChild(summary);
  banner.appendChild(header);

  const justification = document.createElement("p");
  justification.style.margin = "6px 0 0";
  justification.textContent = plan.justification;
  banner.appendChild(justification);

  if (tr.attempts && tr.attempts.length > 1) {
    const note = document.createElement("div");
    note.className = "tier3-attempt";
    note.textContent = `${tr.attempts.length} relaxation attempts tried; using the one that found the most candidates (${tr.used_total_count}).`;
    banner.appendChild(note);
  }

  banner.classList.remove("hidden");
}

// Plain-language breakdown of where each score is coming from, shown on hover in the matches table
function primaryBreakdown(c) {
  const s = c.primary_score;
  const sectorNote = SECTOR_FIT_NOTES[s.sector_fit](s);
  const sizeNote = DEAL_SIZE_FIT_NOTES[s.deal_size_fit];
  return `Sector: ${s.sector_score}% (${sectorNote})\nDeal size: ${s.deal_size_score}% (${sizeNote})\nAverage: ${s.score}%`;
}

function secondaryBreakdown(c) {
  const s = c.secondary_score;
  if (s.score === null) return null;
  const lines = Object.entries(s.field_scores).map(([field, fs]) => {
    const value = fs.acquirer_value === null ? "no data" : fs.acquirer_value;
    return `${SECONDARY_FIELD_LABELS[field] || field}: ${fs.score}% (their typical: ${value})`;
  });
  lines.push(`Average: ${s.score}%`);
  return lines.join("\n");
}

function relevancyBreakdown(c) {
  const s = c.relevancy_score;
  if (s.years_since_most_recent_closed_deal === null) return "No closed deals on record — 0%.";
  return `Most recent closed deal: ${s.years_since_most_recent_closed_deal} years ago\n100% × e^(-0.35 × years) = ${s.score}%`;
}

function strategicBreakdown(c) {
  const s = c.strategic_score;
  if (s.mode_tag === null) return "No strategic_rationale_tags on record — 0%.";
  const rankNote = STRATEGIC_RANK_NOTES[s.rank] || "not among the sector's top 3 implied tags";
  return `Acquirer's most common tag: "${s.mode_tag}"\n${rankNote} → ${s.score}%`;
}

function renderCandidates(cs) {
  const warningBox = $("candidates-warning");
  if (cs.low_match_warning) {
    warningBox.textContent = "Data has low match rates. Proceeding with analysis.";
    warningBox.classList.remove("hidden");
  } else {
    warningBox.classList.add("hidden");
  }

  renderTier3Justification(cs.tier3_relaxation);

  const tierLine = [1, 2, 3].map((t) => cs.tier_sectors[`tier_${t}`]).filter(Boolean).join(" → ");
  $("candidates-summary").textContent =
    `${cs.candidate_count} candidates found — sectors searched: ${tierLine} — ` +
    `deal size range $${cs.deal_size_range_mm[0]}M–$${cs.deal_size_range_mm[1]}M`;

  const tbody = $("candidates-tbody");
  tbody.innerHTML = "";
  for (const c of cs.candidates) {
    const tr = document.createElement("tr");
    const values = [
      c.acquirer,
      c.acquirer_type || "—",
      null,
      `${c.primary_score.score}%`,
      c.secondary_score.score === null ? "—" : `${c.secondary_score.score}%`,
      `${c.strategic_score.score}%`,
      `${c.relevancy_score.score}%`,
    ];
    const breakdowns = { 3: primaryBreakdown(c), 4: secondaryBreakdown(c), 5: strategicBreakdown(c), 6: relevancyBreakdown(c) };
    values.forEach((val, i) => {
      const td = document.createElement("td");
      if (i === 2) {
        const pill = document.createElement("span");
        pill.className = "tier-pill";
        pill.textContent = `Tier ${c.tier}`;
        td.appendChild(pill);
      } else {
        td.textContent = val;
        if (breakdowns[i]) td.dataset.tooltip = breakdowns[i];
      }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }

  $("candidates-json").textContent = JSON.stringify(cs, null, 2);
  $("candidates-panel").classList.remove("hidden");
  $("candidates-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function onUpload(e) {
  const file = e.target.files[0];
  if (!file) return;
  const formData = new FormData();
  formData.append("file", file);
  $("upload-error").classList.add("hidden");
  try {
    const res = await fetch("/api/upload", { method: "POST", body: formData });
    const body = await res.json();
    if (!res.ok) {
      showUploadError(body.detail);
      return;
    }
    metadata = body;
    renderDatasetStatus();
    renderSectorOptions();
    renderStaticOptionalFields();
    resetForm();
  } catch (err) {
    showUploadError({ message: `Upload failed: ${err}`, details: [] });
  } finally {
    e.target.value = "";
  }
}

function showUploadError(detail) {
  const box = $("upload-error");
  const lines = [detail.message, ...(detail.details || [])];
  box.textContent = lines.join("\n");
  box.classList.remove("hidden");
}

function resetForm() {
  $("profile-form").reset();
  $("result-panel").classList.add("hidden");
  $("candidates-panel").classList.add("hidden");
  $("reports-panel").classList.add("hidden");
  $("deal_size_hint").innerHTML = "";
  resetProfileProgress();
  resetReportsProgress();
  currentReports = [];
  currentReportIndex = 0;
  renderCurrentReportCard();
  $("download-reports-btn").classList.add("hidden");
  lastSubmittedPayload = null;
  for (const id of ["target_revenue_band", "target_ebitda_band", "ebitda_margin_band", "revenue_growth_band"]) {
    $(id).innerHTML = '<option value="" disabled selected>Select a sector first…</option>';
    $(id).disabled = true;
  }
}

async function onResetToSample() {
  const res = await fetch("/api/reset", { method: "POST" });
  metadata = await res.json();
  $("upload-error").classList.add("hidden");
  renderDatasetStatus();
  renderSectorOptions();
  renderStaticOptionalFields();
  resetForm();
}

// --- Live progress for "Build Target Profile" (Server-Sent Events) ---------------

function resetProfileProgress() {
  $("profile-progress-list").innerHTML = "";
}

// Turns a matching.find_candidates() progress event into the plain-language line a banker would want to see
function profileProgressText(event) {
  switch (event.type) {
    case "tier1_started":
      return `Finding Tier 1 matches in ${event.sector}, $${event.deal_size_range_mm[0]}M–$${event.deal_size_range_mm[1]}M…`;
    case "tier1_done":
      return `Tier 1: found ${event.candidate_count} candidate${event.candidate_count === 1 ? "" : "s"}.`;
    case "tier2_started":
      return `Finding Tier 2 matches in the next most common sector, ${event.sector}…`;
    case "tier2_done":
      return `Tier 2: pool now at ${event.candidate_count} candidates.`;
    case "tier2_skipped":
      return `Tier 1 already found ${event.candidate_count} candidates — skipping Tier 2.`;
    case "tier3_started":
      return `Finding Tier 3 matches using agent based on ${event.likely_sectors.join(", ") || "shared strategic tags"}…`;
    case "tier3_done":
      return event.used_plan
        ? `Tier 3 agent widened the pool to ${event.candidate_count} candidates.`
        : `Tier 3 agent could not widen the pool further (${event.candidate_count} candidates).`;
    case "tier3_skipped":
      return `Tier 1 + 2 already found ${event.candidate_count} candidates — skipping Tier 3.`;
    default:
      return null;
  }
}

function addProfileProgressRow(text, status) {
  const list = $("profile-progress-list");
  const prev = list.lastElementChild;
  if (prev && prev.classList.contains("running")) prev.className = "progress-row succeeded";

  const li = document.createElement("li");
  li.className = `progress-row ${status}`;
  const dot = document.createElement("span");
  dot.className = "progress-dot";
  const detail = document.createElement("span");
  detail.className = "progress-detail";
  detail.textContent = text;
  li.appendChild(dot);
  li.appendChild(detail);
  list.appendChild(li);
}

function handleProfileProgressEvent(event) {
  const text = profileProgressText(event);
  if (!text) return;
  const status = event.type.endsWith("_started") ? "running" : "succeeded";
  addProfileProgressRow(text, status);
}

async function onSubmit(e) {
  e.preventDefault();
  $("submit-error").classList.add("hidden");
  resetProfileProgress();
  const formData = new FormData(e.target);
  const payload = Object.fromEntries(formData.entries());
  const btn = e.target.querySelector('button[type="submit"]');
  btn.disabled = true;

  try {
    const res = await fetch("/api/target-profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const body = await res.json();
      $("submit-error").textContent = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      $("submit-error").classList.remove("hidden");
      return;
    }

    await readSSEStream(res, (event) => {
      if (event.type === "done") {
        lastSubmittedPayload = payload;
        $("reports-panel").classList.add("hidden");
        renderResult(event.target_profile);
        renderCandidates(event.candidate_search);
      } else if (event.type === "error") {
        $("submit-error").textContent = event.message;
        $("submit-error").classList.remove("hidden");
      } else {
        handleProfileProgressEvent(event);
      }
    });
  } catch (err) {
    $("submit-error").textContent = `Request failed: ${err}`;
    $("submit-error").classList.remove("hidden");
  } finally {
    btn.disabled = false;
  }
}

// --- Live progress for "Generate Reports" (Server-Sent Events) -------------------

let reportProgressRows = {}; // acquirer -> <li> element, for the current run

function resetReportsProgress() {
  $("reports-progress-list").innerHTML = "";
  reportProgressRows = {};
}

function reportProgressRow(acquirer) {
  if (reportProgressRows[acquirer]) return reportProgressRows[acquirer];
  const li = document.createElement("li");
  li.className = "progress-row queued";
  const dot = document.createElement("span");
  dot.className = "progress-dot";
  const name = document.createElement("span");
  name.className = "progress-acquirer";
  name.textContent = acquirer;
  const detail = document.createElement("span");
  detail.className = "progress-detail";
  li.appendChild(dot);
  li.appendChild(name);
  li.appendChild(detail);
  $("reports-progress-list").appendChild(li);
  reportProgressRows[acquirer] = li;
  return li;
}

function setReportProgress(acquirer, status, detailText) {
  const row = reportProgressRow(acquirer);
  row.className = `progress-row ${status}`;
  row.querySelector(".progress-detail").textContent = detailText;
}

function handleReportProgressEvent(event) {
  switch (event.type) {
    case "report_batch_started":
      for (const acquirer of event.acquirers) setReportProgress(acquirer, "queued", "queued");
      break;
    case "report_started":
      setReportProgress(event.acquirer, "running", "starting…");
      break;
    case "report_tool_called":
      setReportProgress(event.acquirer, "running", `calling ${event.tool}…`);
      break;
    case "report_retry":
      setReportProgress(event.acquirer, "retrying", "submission failed validation, retrying…");
      break;
    case "report_succeeded":
      setReportProgress(event.acquirer, "succeeded", "done");
      break;
    case "report_failed":
      setReportProgress(event.acquirer, "failed", `failed: ${event.reason}`);
      break;
  }
}

// --- Rendering the finished report run --------------------------------------------

function conviction(text) {
  const span = document.createElement("span");
  span.className = `conviction-badge conviction-${text}`;
  span.textContent = text;
  return span;
}

function reportSection(title, bodyText) {
  const section = document.createElement("div");
  section.className = "rationale-section";
  const h4 = document.createElement("h4");
  h4.textContent = title;
  const p = document.createElement("p");
  p.textContent = bodyText;
  section.appendChild(h4);
  section.appendChild(p);
  return section;
}

function renderReportCard(entry) {
  const r = entry.report;
  const card = document.createElement("div");
  card.className = "rationale-card";

  const header = document.createElement("div");
  header.className = "rationale-card-header";
  const h3 = document.createElement("h3");
  h3.textContent = entry.acquirer;
  const tierPill = document.createElement("span");
  tierPill.className = "tier-pill";
  tierPill.textContent = `Tier ${entry.tier}`;
  header.appendChild(h3);
  header.appendChild(tierPill);
  header.appendChild(conviction(r.conviction_level));
  card.appendChild(header);

  card.appendChild(reportSection("Acquirer Overview", r.acquirer_overview));
  card.appendChild(reportSection("Strategic Fit Thesis", r.strategic_fit_thesis));
  card.appendChild(reportSection("Precedent Activity", r.precedent_activity));
  card.appendChild(reportSection("Valuation Context", r.valuation_context));

  const riskSection = document.createElement("div");
  riskSection.className = "rationale-section";
  const riskH4 = document.createElement("h4");
  riskH4.textContent = "Risk Flags";
  const ul = document.createElement("ul");
  for (const flag of r.risk_flags) {
    const li = document.createElement("li");
    li.textContent = flag;
    ul.appendChild(li);
  }
  riskSection.appendChild(riskH4);
  riskSection.appendChild(ul);
  card.appendChild(riskSection);

  card.appendChild(reportSection("Conviction Rationale", r.conviction_rationale));

  const meta = document.createElement("div");
  meta.className = "rationale-meta";
  meta.textContent =
    `Primary ${entry.primary_score}% · Secondary ${entry.secondary_score ?? "—"}% · Strategic ${entry.strategic_score}% · ` +
    `Relevancy ${entry.relevancy_score}% · ${entry.token_usage.input_tokens + entry.token_usage.output_tokens} tokens`;
  card.appendChild(meta);

  const details = document.createElement("details");
  details.className = "raw-json";
  const summary = document.createElement("summary");
  summary.textContent = "Reasoning + raw JSON";
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(entry, null, 2);
  details.appendChild(summary);
  details.appendChild(pre);
  card.appendChild(details);

  return card;
}

function renderDroppedCard(entry) {
  const card = document.createElement("div");
  card.className = "dropped-card";
  const header = document.createElement("div");
  header.className = "dropped-card-header";
  const strong = document.createElement("strong");
  strong.textContent = entry.acquirer;
  const reason = document.createElement("span");
  reason.className = "dropped-reason";
  reason.textContent = entry.reason;
  header.appendChild(strong);
  header.appendChild(reason);
  card.appendChild(header);
  return card;
}

// Carousel state for the delivered report cards -- one card shown at a time, arrows flip between them
let currentReports = [];
let currentReportIndex = 0;

function renderCurrentReportCard() {
  const cardsContainer = $("reports-cards");
  cardsContainer.innerHTML = "";

  if (!currentReports.length) {
    $("reports-carousel").classList.add("hidden");
    $("reports-position").textContent = "";
    return;
  }

  cardsContainer.appendChild(renderReportCard(currentReports[currentReportIndex]));
  $("reports-carousel").classList.remove("hidden");
  $("reports-position").textContent = `${currentReportIndex + 1} of ${currentReports.length}`;
  $("reports-prev").disabled = currentReportIndex === 0;
  $("reports-next").disabled = currentReportIndex === currentReports.length - 1;
}

function showReportOffset(delta) {
  const next = currentReportIndex + delta;
  if (next < 0 || next >= currentReports.length) return;
  currentReportIndex = next;
  renderCurrentReportCard();
}

function renderReportRun(data) {
  const run = data.report_run;
  $("reports-summary").textContent =
    `${run.delivered_count} of ${run.requested_count} rationales generated in ${run.elapsed_seconds}s — ` +
    `${run.token_usage.total_tokens} tokens (${run.token_usage.input_tokens} in / ${run.token_usage.output_tokens} out)` +
    (run.dropped.length ? ` — ${run.dropped.length} acquirer(s) failed.` : ".");

  const droppedContainer = $("reports-dropped");
  droppedContainer.innerHTML = "";
  for (const entry of run.dropped) droppedContainer.appendChild(renderDroppedCard(entry));

  currentReports = run.delivered;
  currentReportIndex = 0;
  renderCurrentReportCard();

  $("download-reports-btn").classList.toggle("hidden", run.delivered.length === 0);
  $("reports-panel").classList.remove("hidden");
}

async function onGenerateReports() {
  if (!lastSubmittedPayload) return;
  const btn = $("generate-reports-btn");
  $("reports-error").classList.add("hidden");
  $("reports-summary").textContent = "";
  $("reports-dropped").innerHTML = "";
  currentReports = [];
  currentReportIndex = 0;
  renderCurrentReportCard();
  resetReportsProgress();
  $("download-reports-btn").classList.add("hidden");
  btn.disabled = true;
  $("reports-panel").classList.remove("hidden");
  $("reports-panel").scrollIntoView({ behavior: "smooth", block: "start" });

  try {
    const res = await fetch("/api/reports", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(lastSubmittedPayload),
    });
    if (!res.ok) {
      const body = await res.json();
      $("reports-error").textContent = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      $("reports-error").classList.remove("hidden");
      return;
    }

    await readSSEStream(res, (event) => {
      if (event.type === "done") {
        renderReportRun(event);
      } else if (event.type === "error") {
        $("reports-error").textContent = event.message;
        $("reports-error").classList.remove("hidden");
      } else {
        handleReportProgressEvent(event);
      }
    });
  } catch (err) {
    $("reports-error").textContent = `Request failed: ${err}`;
    $("reports-error").classList.remove("hidden");
  } finally {
    btn.disabled = false;
  }
}

$("sector").addEventListener("change", onSectorChange);
$("csv-file-input").addEventListener("change", onUpload);
$("reset-btn").addEventListener("click", onResetToSample);
$("profile-form").addEventListener("submit", onSubmit);
$("generate-reports-btn").addEventListener("click", onGenerateReports);
$("reports-prev").addEventListener("click", () => showReportOffset(-1));
$("reports-next").addEventListener("click", () => showReportOffset(1));

fetchMetadata();
