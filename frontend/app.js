const FIELD_TO_BAND_SELECT = {
  target_revenue_mm: "target_revenue_band",
  target_ebitda_mm: "target_ebitda_band",
  ebitda_margin_pct: "ebitda_margin_band",
  revenue_growth_pct: "revenue_growth_band",
};

const formatDollar = (v) => (v >= 1000 ? `$${(v / 1000).toFixed(1)}B` : `$${Math.round(v)}M`);

let metadata = null;

const $ = (id) => document.getElementById(id);

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
  const other = document.createElement("option");
  other.value = "Other";
  other.textContent = "Other";
  select.appendChild(other);
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
  rows.push(["Deal Size (EV, $mm)", profile.deal_size_mm.label]);
  for (const [key, select] of Object.entries(FIELD_TO_BAND_SELECT)) {
    const label = document.querySelector(`label[for="${select}"]`).textContent.replace(" *", "");
    const val = profile[key];
    rows.push([label, val ? val.label : "Not specified"]);
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

function renderCandidates(cs) {
  const warningBox = $("candidates-warning");
  if (cs.low_match_warning) {
    warningBox.textContent = "Data has low match rates. Proceeding with analysis.";
    warningBox.classList.remove("hidden");
  } else {
    warningBox.classList.add("hidden");
  }

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
      `${c.relevancy_score.score}%`,
    ];
    values.forEach((val, i) => {
      const td = document.createElement("td");
      if (i === 2) {
        const pill = document.createElement("span");
        pill.className = "tier-pill";
        pill.textContent = `Tier ${c.tier}`;
        td.appendChild(pill);
      } else {
        td.textContent = val;
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
  $("deal_size_hint").innerHTML = "";
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

async function onSubmit(e) {
  e.preventDefault();
  $("submit-error").classList.add("hidden");
  const formData = new FormData(e.target);
  const payload = Object.fromEntries(formData.entries());

  const res = await fetch("/api/target-profile", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await res.json();
  if (!res.ok) {
    $("submit-error").textContent = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    $("submit-error").classList.remove("hidden");
    return;
  }
  renderResult(body.target_profile);
  renderCandidates(body.candidate_search);
}

$("sector").addEventListener("change", onSectorChange);
$("csv-file-input").addEventListener("change", onUpload);
$("reset-btn").addEventListener("click", onResetToSample);
$("profile-form").addEventListener("submit", onSubmit);

fetchMetadata();
