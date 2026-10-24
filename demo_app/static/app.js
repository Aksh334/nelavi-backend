"use strict";

// All text is inserted with textContent (never as HTML strings), so no data can inject markup.
const SEND = { "X-Nelavi-Demo": "1" };
const $ = (id) => document.getElementById(id);
let maxImageBytes = 1048576;

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function cell(row, text) {
  const td = document.createElement("td");
  td.textContent = text === null || text === undefined ? "" : String(text);
  row.appendChild(td);
  return td;
}

function setStatus(node, text, kind) {
  node.textContent = text;
  node.className = "status" + (kind ? " " + kind : "");
}

// Small DOM helpers. Text always goes in through textContent, never as markup.
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

// A text badge. The meaning is always in the words; the colour is only a second cue.
function badge(text, kind) {
  return el("span", "badge " + (kind || "muted"), text);
}

function kindOf(status) {
  if (status === "completed" || status === "ok" || status === "valid") return "good";
  if (status === "failed" || status === "error" || status === "fault" || status === "invalid_record") return "bad";
  if (status === "partial" || status === "running") return "warn";
  return "muted"; // skipped, not_run, unknown, missing
}

function setText(id, text) {
  const node = $(id);
  if (node) node.textContent = text;
}

async function api(path, options) {
  const response = await fetch(path, options);
  const contentType = (response.headers && response.headers.get("content-type")) || "";
  if (contentType.includes("audio/")) {
    if (!response.ok) {
      let errData = null;
      try {
        errData = await response.json();
      } catch (e) {
        errData = null;
      }
      const error = new Error(errData && (errData.error || errData.message) ? (errData.error || errData.message) : "Request failed (HTTP " + response.status + ").");
      error.data = errData;
      throw error;
    }
    return response;
  }
  let data = null;
  try {
    data = await response.json();
  } catch (e) {
    data = null;
  }
  if (!response.ok) {
    const error = new Error(data && (data.error || data.message) ? (data.error || data.message) : "Request failed (HTTP " + response.status + ").");
    error.data = data;
    throw error;
  }
  return data;
}

function showValue(value) {
  return value === null || value === undefined ? "no reading (null)" : value;
}

// One card per reading, grouped by the part of the name before the dot (soil, air, battery). The value is shown
// exactly as the Record holds it; the unit is only what the application defines; a missing reading says so.
function renderReadingCards(rows) {
  const holder = $("reading-cards");
  clear(holder);
  const groups = [];
  rows.forEach((r) => {
    const name = String(r.name).split(".")[0];
    let group = groups.find((g) => g.name === name);
    if (!group) {
      group = { name: name, rows: [] };
      groups.push(group);
    }
    group.rows.push(r);
  });
  groups.forEach((group) => {
    const box = el("div", "reading-group");
    box.appendChild(el("h4", null, group.name.charAt(0).toUpperCase() + group.name.slice(1) + " (simulated)"));
    const grid = el("div", "cards");
    group.rows.forEach((r) => {
      const card = el("div", "card");
      card.appendChild(el("div", "card-label", r.name));
      const value = el("div", "card-value");
      const missing = r.value === null || r.value === undefined;
      value.appendChild(el("span", missing ? "missing" : "number", showValue(r.value)));
      card.appendChild(value);
      card.appendChild(el("div", "card-unit", r.unit));
      const meta = el("div", "card-meta");
      meta.appendChild(el("span", "muted", "sensor " + (r.sensor === null || r.sensor === undefined ? "unknown" : r.sensor)));
      const unknown = r.status === null || r.status === undefined;
      meta.appendChild(badge(unknown ? "status unknown" : "status " + r.status, kindOf(r.status)));
      card.appendChild(meta);
      grid.appendChild(card);
    });
    box.appendChild(grid);
    holder.appendChild(box);
  });
}

function renderRun(data) {
  $("record-empty").hidden = true;
  $("record-body").hidden = false;

  const validation = $("validation");
  const errors = $("validation-errors");
  clear(errors);
  if (data.validation.valid) {
    const stored = data.stored_id === null ? "not stored" : "saved to history as storage id " + data.stored_id;
    setStatus(validation, "Record Core validation: VALID - " + stored + ".", "ok");
  } else {
    setStatus(validation, "Record Core validation: INVALID - the Record was not saved.", "bad");
    data.validation.errors.forEach((e) => {
      const li = document.createElement("li");
      li.textContent = e.path + ": " + e.message + " (" + e.code + ")";
      errors.appendChild(li);
    });
  }

  renderReadingCards(data.readings);

  const photo = $("photo");
  if (data.photo) {
    photo.textContent = "photo id " + data.photo.photo_id + "; leaf label " + data.photo.leaf_label +
      "; confidence " + data.photo.confidence + ". " + data.photo.note;
  } else {
    photo.textContent = "This Record has no photo reference (the photo fields are null).";
  }
  $("record-json").textContent = JSON.stringify(data.record, null, 2);
  renderAdvice(data.advice);
  renderReport(data.report);
}

// ---- farmer report: every part comes from the server's pipeline and is inserted as text only ----
function addSection(root, title, lines) {
  const heading = document.createElement("h4");
  heading.textContent = title;
  root.appendChild(heading);
  const list = document.createElement("ul");
  (lines.length ? lines : ["Nothing to report."]).forEach((line) => {
    const li = document.createElement("li");
    li.textContent = line;
    list.appendChild(li);
  });
  root.appendChild(list);
}

function describeObservation(o) {
  const unit = o.unit === null || o.unit === undefined ? "unit not defined" : o.unit;
  const value = o.value === null || o.value === undefined ? "no reading" : o.value;
  return o.parameter + ": " + value + " (" + unit + "; sensor " + o.quality + "; calibration " + o.calibration + ")";
}

function describeFindings(image) {
  const lines = [image.note];
  const f = image.findings;
  if (f) {
    f.crop.candidates.forEach((c) => lines.push("Possible crop: " + c.name + " (uncertainty " + c.uncertainty +
      (c.evidence.length ? "; seen: " + c.evidence.join(", ") : "") + ")"));
    if (f.growth_stage.candidate) lines.push("Possible growth stage: " + f.growth_stage.candidate +
      " (uncertainty " + f.growth_stage.uncertainty + ")");
    f.symptoms.forEach((s) => lines.push("Observed: " + s.observation + (s.possible_interpretations.length
      ? " - possible explanations, none confirmed: " + s.possible_interpretations.join(", ") : "")));
  }
  return lines;
}

function renderReport(report) {
  const origin = $("report-origin");
  const root = $("report-body");
  clear(root);
  if (!report || report.status !== "ok" || !report.sections) {
    origin.textContent = "The report is unavailable for this run.";
    return;
  }
  origin.textContent = report.origin_note;
  const s = report.sections;
  const rover = s.rover_observed;
  addSection(root, "What the rover observed", [
    "Time: " + rover.time + "; photo reference: " + (rover.photo_reference === null ? "none" : rover.photo_reference) +
      "; leaf label: " + (rover.leaf_label === null ? "none" : rover.leaf_label),
    rover.note,
  ]);
  addSection(root, "What the AI inferred from images", describeFindings(s.image_inference));
  addSection(root, "What the sensors measured", [s.sensors_measured.note].concat(s.sensors_measured.observations.map(describeObservation)));
  const entries = s.retrieved_sources.entries;
  addSection(root, "What was retrieved from agricultural sources", [s.retrieved_sources.note].concat(
    entries.length ? entries.map((e) => e.title + " - " + e.url + " (" + e.applicability.status +
      (e.applicability.unmet.length ? "; unmet: " + e.applicability.unmet.join(", ") : "") + ")")
      : [researchConfig
        ? "No sources are included in this report. Section 6 can search for sources separately."
        : "No sources were retrieved. Research is not configured on this server."]));
  addSection(root, "Recommendations that are supported", s.recommendations.supported.length
    ? s.recommendations.supported.map((r) => r.text + " [source: " + r.source + "]")
    : ["None. No rule or applicable source supports a recommendation for this Record."]);
  addSection(root, "Uncertain or unavailable", s.uncertain_or_unavailable.map((u) => u.text));
  addSection(root, "What would help next", s.next_steps);
}

// Deterministic advice from the rules engine. Fixed wording for every outcome; a recommendation
// is only ever shown as the rule's own message with its source and the readings that triggered it.
const ADVICE_OUTCOMES = {
  insufficient_evidence: {
    no_rules: "No advice is available: no agronomic rules have been loaded. The project has no " +
      "source-approved thresholds yet, so nothing is recommended. This does not mean the crop is " +
      "healthy or that no action is needed.",
    nothing_could_be_assessed: "No advice is available: no loaded rule could be assessed with this " +
      "Record (see the list below).",
  },
  no_rule_fired: "The loaded rules were assessed and none applies. This is not a statement that " +
    "the soil or crop is fine.",
  advice: "Advice from the loaded rules (not a diagnosis, not a dose):",
  invalid_record: "No advice: the Record did not pass validation.",
  invalid_ruleset: "No advice: the loaded ruleset is not valid.",
};

function renderAdvice(advice) {
  const summary = $("advice");
  const list = $("advice-items");
  clear(list);
  if (!advice || typeof advice.outcome !== "string") {
    summary.textContent = "Advice is unavailable: the server sent no advice result.";
    return;
  }
  let text = ADVICE_OUTCOMES[advice.outcome];
  if (advice.outcome === "insufficient_evidence" && text) text = text[advice.reason];
  summary.textContent = typeof text === "string" ? text : "Advice is unavailable: the result was not understood.";
  (Array.isArray(advice.items) ? advice.items : []).forEach((item) => {
    const li = document.createElement("li");
    li.textContent = item.message + " [rule " + item.rule_id + "; source: " + item.source + "; based on " +
      (Array.isArray(item.evidence) ? item.evidence.map((e) =>
        e.reading + (e.calibrated ? " (calibrated)" : "") + " = " + e.observed + " " + e.op + " " + e.threshold
      ).join(", ") : "no evidence listed") + "]";
    list.appendChild(li);
  });
  (Array.isArray(advice.missing_information) ? advice.missing_information : []).forEach((m) => {
    const li = document.createElement("li");
    li.textContent = "Needed: " + m.needs;
    list.appendChild(li);
  });
  (Array.isArray(advice.not_assessed) ? advice.not_assessed : []).forEach((n) => {
    const li = document.createElement("li");
    li.textContent = "Not assessed: rule " + n.rule_id + ", " + (n.reading === null ? "crop or growth stage" : n.reading) +
      " (" + n.code + ").";
    list.appendChild(li);
  });
  (Array.isArray(advice.not_applicable) ? advice.not_applicable : []).forEach((n) => {
    const li = document.createElement("li");
    li.textContent = "Not applicable: rule " + n.rule_id + " (" + n.code + ").";
    list.appendChild(li);
  });
  (Array.isArray(advice.caveats) ? advice.caveats : []).forEach((c) => {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "Note: " + c.message;
    list.appendChild(li);
  });
}

function renderAnalysis(result) {
  $("analysis-body").hidden = false;
  $("analysis-label").textContent = result.label;
  $("analysis-text").textContent = result.text === null ? "No analysis text (error code: " + result.error_code + ")." : result.text;
  $("analysis-model").textContent = "Analyzer: " + result.model;
  const list = $("analysis-caveats");
  clear(list);
  result.caveats.forEach((c) => {
    const li = document.createElement("li");
    li.textContent = c.code + ": " + c.message;
    list.appendChild(li);
  });
}

function renderHistory(data) {
  const body = $("history-table").querySelector("tbody");
  clear(body);
  data.records.forEach((item) => {
    const r = item.record;
    const faults = Object.keys(r.sensor_status).filter((k) => r.sensor_status[k] === "fault");
    const tr = document.createElement("tr");
    cell(tr, item.id);
    cell(tr, r.time);
    cell(tr, r.sequence_number);
    cell(tr, showValue(r.battery.voltage_v));
    cell(tr, faults.length ? faults.join(", ") : "none");
    cell(tr, r.photo_id === null ? "none" : r.photo_id);
    body.appendChild(tr);
  });
  $("history-status").textContent = "Showing " + data.records.length + " of " + data.total + " saved Records (newest first).";
  renderTrend(data.records);
  updateOverviewFromHistory(data);
}

// Battery voltage of the listed Records, oldest first. Each bar is a native <meter> scaled between the smallest and
// largest listed value, so it shows only how the listed values compare. It is not a limit or a health range.
function renderTrend(records) {
  const list = $("trend-list");
  if (!list) return;
  clear(list);
  const points = records.slice().reverse().map((item) => ({
    id: item.id,
    sequence: item.record.sequence_number,
    voltage: item.record.battery ? item.record.battery.voltage_v : null,
  }));
  setHidden("trend-empty", points.length > 0);
  const numbers = points.map((p) => p.voltage).filter((v) => typeof v === "number" && Number.isFinite(v));
  const low = Math.min.apply(null, numbers);
  const high = Math.max.apply(null, numbers);
  points.forEach((p) => {
    const li = el("li");
    li.appendChild(el("span", null, "Storage id " + p.id + ", sequence " + p.sequence));
    const known = typeof p.voltage === "number" && Number.isFinite(p.voltage);
    li.appendChild(el("span", "number", known ? p.voltage.toFixed(3) + " V (rounded)" : "no reading (null)"));
    if (known && high > low) {
      const meter = document.createElement("meter");
      meter.min = low;
      meter.max = high;
      meter.value = p.voltage;
      meter.setAttribute("aria-label", "Battery voltage of storage id " + p.id + " between the listed minimum and maximum");
      li.appendChild(meter);
    } else {
      li.appendChild(el("span", "muted", known ? "same value in every listed Record" : ""));
    }
    list.appendChild(li);
  });
}

function setHidden(id, hidden) {
  const node = $(id);
  if (node) node.hidden = hidden;
}

// ---- overview tiles: only facts the server or the saved history actually supplies ----
function updateOverviewFromHistory(data) {
  setText("ov-records", String(data.total));
  const latest = data.records.length ? data.records[0] : null;
  if (!latest) {
    setText("ov-latest", "None yet");
    setText("ov-faults", "No Record yet");
    return;
  }
  const r = latest.record;
  setText("ov-latest", "Storage id " + latest.id + ", sequence " + r.sequence_number + ", time " + r.time);
  const faults = Object.keys(r.sensor_status || {}).filter((k) => r.sensor_status[k] === "fault");
  setText("ov-faults", faults.length ? faults.join(", ") : "none reported");
}

function configureOverview(state) {
  if (!state) {
    ["ov-so", "ov-research", "ov-orch", "ov-integrated", "ov-va"].forEach((id) => setText(id, "Unavailable (state not loaded)"));
    return;
  }
  const so = state.second_opinion;
  setText("ov-so", !so || typeof so.simulated !== "boolean" ? "Unavailable"
    : (so.simulated ? "Simulated provider" : "Cloud provider (real model)"));
  const search = state.research && state.research.configured;
  const pro = state.research_pro && state.research_pro.configured;
  setText("ov-research", pro ? "Research Pro configured" : (search ? "Search configured" : "Off (not configured)"));
  const orch = state.orchestrated;
  setText("ov-orch", orch && orch.available === true && orch.fake_providers === true
    ? "Available (fake providers only)" : "Unavailable");
  const iw = state.integrated;
  const realNames = iw && iw.real_available && typeof iw.real_available === "object"
    ? Object.keys(iw.real_available).filter((k) => iw.real_available[k] === true) : [];
  setText("ov-integrated", iw && iw.available === true
    ? "Available (real: " + (realNames.length ? realNames.join(", ") : "none") + ")" : "Unavailable");
  const va = state.voice_assistant;
  const vaOnline = va && (va.speech_to_text_configured === true || va.text_to_speech_configured === true || va.elevenlabs_configured === true);
  setText("ov-va", va && (va.available === true || va.enabled === true)
    ? (vaOnline ? "Active (ElevenLabs Online)" : "Active (Text / QA / Lessons)")
    : "Unavailable");
}

async function refreshHistory() {
  try {
    renderHistory(await api("/api/history?limit=20"));
  } catch (e) {
    $("history-status").textContent = "Could not load history: " + e.message;
    ["ov-records", "ov-latest", "ov-faults"].forEach((id) => setText(id, "Unavailable (history not loaded)"));
  }
}

async function runScenario() {
  const status = $("run-status");
  setStatus(status, "Running...", "");
  try {
    const data = await api("/api/run", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, SEND),
      body: JSON.stringify({ scenario: $("scenario").value, crop: $("user-crop").value,
        region: $("user-region").value, growth_stage: $("user-stage").value }),
    });
    setStatus(status, "Ran '" + data.scenario + "' (" + data.steps.length + " simulated steps).", "ok");
    renderRun(data);
    await refreshHistory();
  } catch (e) {
    setStatus(status, "Error: " + e.message, "bad");
  }
}

async function analyze() {
  const status = $("analysis-status");
  const input = $("file");
  $("analysis-body").hidden = true;
  const file = input.files && input.files[0];
  if (!file) {
    setStatus(status, "Choose a PNG file first.", "bad");
    return;
  }
  if (file.type !== "image/png") {
    setStatus(status, "That file is not a PNG (its type is '" + (file.type || "unknown") + "').", "bad");
    return;
  }
  if (file.size > maxImageBytes) {
    setStatus(status, "That file is larger than the " + maxImageBytes + "-byte limit.", "bad");
    return;
  }
  setStatus(status, "Analysing...", "");
  try {
    const result = await api("/api/analyze", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "image/png" }, SEND),
      body: file,
    });
    setStatus(status, "Done (demo-only result below).", "ok");
    renderAnalysis(result);
  } catch (e) {
    setStatus(status, "Error: " + e.message, "bad");
    if (e.data && e.data.caveats) renderAnalysis(e.data);
  }
}

// ---- Second Opinion: mode, limits and reading names come from /api/state ----
let soConfig = null;
let soBusy = false;
let soInputs = [];
const NUMBER_PATTERN = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;
const DATA_URL_PREFIX = /^data:[^,]*;base64,/;

function isCount(value) {
  return Number.isInteger(value) && value > 0;
}

function isName(value) {
  return typeof value === "string" && value !== "";
}

// Returns a validated copy of the server configuration, or null when anything is missing.
function parseSecondOpinionConfig(raw) {
  if (!raw || typeof raw !== "object" || typeof raw.simulated !== "boolean") return null;
  if (!isCount(raw.max_image_bytes) || !isCount(raw.crop_max_chars) || !isCount(raw.language_max_chars)) return null;
  if (!Array.isArray(raw.reading_groups) || raw.reading_groups.length === 0) return null;
  const names = new Set();
  const groups = [];
  for (const group of raw.reading_groups) {
    if (!group || !isName(group.name) || !Array.isArray(group.keys) || group.keys.length === 0) return null;
    if (!group.keys.every(isName) || new Set(group.keys).size !== group.keys.length) return null;
    if (names.has(group.name)) return null;
    names.add(group.name);
    groups.push({ name: group.name, keys: group.keys.slice() });
  }
  return {
    simulated: raw.simulated,
    maxImageBytes: raw.max_image_bytes,
    cropMax: raw.crop_max_chars,
    languageMax: raw.language_max_chars,
    groups: groups,
  };
}

function disableSecondOpinion(message) {
  soConfig = null;
  $("so-submit").disabled = true;
  showModeUnavailable();
  setStatus($("so-status"), message, "bad");
}

function buildReadingFields(groups) {
  const holder = $("so-readings");
  clear(holder);
  soInputs = [];
  groups.forEach((group, g) => {
    const box = document.createElement("fieldset");
    const legend = document.createElement("legend");
    legend.textContent = "Readings: " + group.name + " (optional, no units are defined)";
    box.appendChild(legend);
    const grid = document.createElement("div");
    grid.className = "reading-grid";
    group.keys.forEach((key, k) => {
      const id = "so-reading-" + g + "-" + k;
      const label = document.createElement("label");
      label.htmlFor = id;
      label.textContent = group.name + "." + key;
      const input = document.createElement("input");
      input.id = id;
      input.type = "text";
      input.setAttribute("inputmode", "decimal");
      input.autocomplete = "off";
      const field = document.createElement("div");
      field.className = "reading-field";  // keeps each label directly above its own input
      field.appendChild(label);
      field.appendChild(input);
      grid.appendChild(field);
      soInputs.push({ group: group.name, key: key, input: input });
    });
    box.appendChild(grid);
    holder.appendChild(box);
  });
}

function configureSecondOpinion(raw) {
  const config = parseSecondOpinionConfig(raw);
  if (!config) {
    disableSecondOpinion("Second Opinion is unavailable: the server configuration is missing or invalid.");
    return;
  }
  soConfig = config;
  $("so-crop").maxLength = config.cropMax;
  $("so-language").maxLength = config.languageMax;
  buildReadingFields(config.groups);
  $("so-submit").disabled = false;
  if (config.simulated) showSimulatedDisclosures();
  else showRealModeDisclosures();
  setStatus($("so-status"), "Ready (" + modeName() + "). Images up to " + config.maxImageBytes + " bytes.", "");
}

// The page ships with neutral "checking" wording. Mode claims (simulated, real, or unavailable)
// are written only here, and only after the server has answered, so the page never states a
// mode it has not confirmed. All text goes in through textContent / text nodes.
function setBanner(lead, rest) {
  const banner = $("banner");
  clear(banner);
  const strong = document.createElement("strong");
  strong.textContent = lead;
  banner.appendChild(strong);
  banner.appendChild(document.createTextNode(" " + rest));
}

function showSimulatedDisclosures() {  // only after the server confirmed simulated: true
  setBanner("SIMULATED DEMONSTRATION.", "The rover, its readings, the photo reference and the " +
    "image analysis on this page are all produced by local code. No AI model, no NVIDIA / " +
    "Nebius / Tavily service, no network and no hardware is used.");
  $("so-notice").textContent = "SIMULATED. This feature does not run an AI model and does not " +
    "look at your image. It returns fixed demonstration text, which is not a plant diagnosis " +
    "and not advice.";
  $("so-help").textContent = "Choose a PNG and describe the crop. The request goes only to this " +
    "local server. Nothing is saved and nothing is added to the Record history.";
}

function showRealModeDisclosures() {  // only after the server confirmed simulated: false
  setBanner("MIXED MODE.", "The rover, its readings and the demo image analysis (sections 1 to " +
    "4) are produced by local simulation code. Section 5 (Second Opinion) is different: it " +
    "sends the PNG you choose, plus the crop, language and readings you enter, over the " +
    "internet to a cloud AI provider and shows its answer.");
  $("so-notice").textContent = "REAL MODEL. Submitting sends your image, crop, language and " +
    "readings to the configured cloud AI provider. The answer is AI-generated text, not a " +
    "diagnosis and not advice. Confirm with your agriculture officer.";
  $("so-help").textContent = "Choose a PNG and describe the crop. This page does not save the " +
    "request or add anything to the Record history, but the provider receives the data above. " +
    "How the provider stores or uses submitted data has not been verified by this project.";
}

function showModeUnavailable() {  // the mode was not confirmed: claim nothing about it
  setBanner("APPLICATION MODE UNAVAILABLE.", "The server did not confirm whether Second Opinion " +
    "is simulated or uses a cloud AI provider, so Second Opinion is disabled. Reload the page " +
    "or check that the demo server is running.");
  $("so-notice").textContent = "Second Opinion is disabled because the application mode could " +
    "not be confirmed.";
  $("so-help").textContent = "Nothing can be submitted until the server confirms the mode.";
}

function modeName() {
  return soConfig && soConfig.simulated === false ? "real model" : "simulated";
}

function inputError(message) {
  const error = new Error(message);
  error.userFacing = true;
  return error;
}

function characterCount(text) {
  return Array.from(text).length;
}

function readText(id, label, max) {
  const value = $(id).value;
  if (value.trim() === "") throw inputError(label + " is required.");
  if (characterCount(value) > max) throw inputError(label + " is longer than " + max + " characters.");
  return value;
}

function readReadings() {
  const readings = Object.create(null);
  soConfig.groups.forEach((group) => {
    readings[group.name] = Object.create(null);
  });
  soInputs.forEach((entry) => {
    const text = entry.input.value.trim();
    let value = null;
    if (text !== "") {
      const number = NUMBER_PATTERN.test(text) ? Number(text) : NaN;
      if (!Number.isFinite(number)) {
        throw inputError("The reading " + entry.group + "." + entry.key + " must be a finite number, or left blank.");
      }
      value = number;
    }
    readings[entry.group][entry.key] = value;
  });
  return readings;
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(inputError("The file could not be read."));
    reader.onabort = () => reject(inputError("Reading the file was cancelled."));
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string" || !DATA_URL_PREFIX.test(result)) {
        reject(inputError("The file could not be read."));
        return;
      }
      const encoded = result.replace(DATA_URL_PREFIX, "");
      if (encoded === "") {
        reject(inputError("The file is empty."));
        return;
      }
      resolve(encoded);
    };
    reader.readAsDataURL(file);
  });
}

async function buildSecondOpinionPayload() {
  const input = $("so-file");
  const file = input.files && input.files[0];
  if (!file) throw inputError("Choose a PNG file first.");
  if (file.type !== "image/png") {
    throw inputError("That file is not a PNG (its type is '" + (file.type || "unknown") + "').");
  }
  if (file.size === 0) throw inputError("The file is empty.");
  if (file.size > soConfig.maxImageBytes) {
    throw inputError("That file is larger than the " + soConfig.maxImageBytes + "-byte limit.");
  }
  const crop = readText("so-crop", "Crop", soConfig.cropMax);
  const language = readText("so-language", "Language", soConfig.languageMax);
  const readings = readReadings();
  const image = await readFileAsBase64(file);
  return { image_png_base64: image, readings: readings, crop: crop, language: language };
}

function safeCode(value) {
  return typeof value === "string" && /^[a-z_]{1,40}$/.test(value) ? value : null;
}

// Shows an opinion only when the response states the same mode that /api/state announced
// (both must be real booleans). A mismatch or a missing value is never shown. Returns true if shown.
function renderSecondOpinion(result) {
  const body = $("so-result");
  body.hidden = true;
  if (!soConfig || !result || typeof result.simulated !== "boolean") return false;
  if (result.simulated !== soConfig.simulated) return false;
  if (typeof result.opinion !== "string" || result.opinion === "" || result.error_code !== null) return false;
  $("so-label").textContent = typeof result.label === "string" ? result.label : "";
  $("so-opinion").textContent = result.opinion;
  $("so-model").textContent = typeof result.model === "string" ? "Provider: " + result.model : "";
  body.hidden = false;
  return true;
}

// Fixed wording for the closed set of error codes. No provider text is ever shown. No answer is
// displayed when there is an error, and nothing is substituted for it.
const SECOND_OPINION_ERRORS = {
  credential_missing: "The server has no API credential configured for the provider.",
  configuration_invalid: "The provider settings or the request prompt are invalid.",
  image_unsupported: "The provider could not accept this image (it must be a PNG within the size limit).",
  provider_rejected: "The provider rejected the request.",
  provider_unavailable: "The provider reported a server-side problem. Try again later.",
  provider_unreachable: "The provider could not be reached or did not answer in time.",
  provider_timeout: "The provider did not answer before the time limit. No answer was received.",
  provider_connection_failed: "The connection to the provider failed before an answer arrived (network, DNS or TLS).",
  transport_failed: "The request could not be completed because of a local transport problem; no answer was received.",
  response_malformed: "The provider's answer was missing, malformed or did not meet the answer rules, so it was not shown.",
  response_empty: "The provider returned an empty answer.",
  response_too_large: "The provider's response was too large and was discarded.",
};

function describeSecondOpinionError(e) {
  if (e && e.userFacing) return e.message;
  if (e && e.data && safeCode(e.data.error_code)) {
    const code = safeCode(e.data.error_code);
    const known = Object.prototype.hasOwnProperty.call(SECOND_OPINION_ERRORS, code);
    return (known ? SECOND_OPINION_ERRORS[code] : "The " + modeName() + " service reported an error.") +
      " (code: " + code + "). No answer was produced.";
  }
  if (e && typeof e.message === "string" && e.data !== undefined) return e.message;  // sanitised by the server
  return "The request could not be completed.";
}

async function submitSecondOpinion(event) {
  if (event) event.preventDefault();
  if (soBusy || !soConfig) return;
  soBusy = true;
  const button = $("so-submit");
  const status = $("so-status");
  button.disabled = true;
  $("so-result").hidden = true;
  try {
    const payload = await buildSecondOpinionPayload();
    setStatus(status, soConfig.simulated === false
      ? "Requesting (real model)... this can take up to a minute and the page cannot be used meanwhile."
      : "Requesting (simulated)...", "");
    const result = await api("/api/second-opinion", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, SEND),
      body: JSON.stringify(payload),
    });
    if (renderSecondOpinion(result)) {
      setStatus(status, "Done (" + modeName() + " result below).", "ok");
    } else {
      setStatus(status, "The response did not match the announced mode (" + modeName() +
        "), so it was not displayed.", "bad");
    }
  } catch (e) {
    setStatus(status, "Error: " + describeSecondOpinionError(e), "bad");
  } finally {
    soBusy = false;
    button.disabled = soConfig === null;
  }
}

// ---- research (advisory): off unless /api/state says the server configured it ----
let researchConfig = null;
let rsBusy = false;

// Fixed wording for the closed set of research error codes. No provider text is ever shown.
const RESEARCH_ERRORS = {
  credential_missing: "The server has no research credential configured.",
  configuration_invalid: "The research settings are invalid.",
  query_invalid: "The question is empty, too long or contains characters that are not allowed.",
  rate_limited: "The search service is rate limiting requests. Try again later.",
  usage_limit_exceeded: "The search service reports that a usage limit was reached.",
  provider_rejected: "The search service rejected the request.",
  provider_unavailable: "The search service reported a server-side problem. Try again later.",
  provider_unreachable: "The search service could not be reached or did not answer in time.",
  provider_timeout: "The search service did not answer within the time allowed. Try again later.",
  provider_connection_failed: "A connection to the search service could not be made. Check the network and try again.",
  transport_failed: "The request to the search service failed for another reason, and no answer was received.",
  response_malformed: "The search service's answer could not be read, so no sources are shown.",
  response_too_large: "The search service's response was too large and was discarded.",
  no_results: "No usable source from the configured sites was found for that question.",
};

function parseResearchConfig(raw) {
  if (!raw || typeof raw !== "object" || typeof raw.configured !== "boolean") return null;
  if (!raw.configured) return { configured: false, domains: [], maxChars: null };
  if (!Array.isArray(raw.domains) || raw.domains.length === 0 || !raw.domains.every(isName)) return null;
  if (!isCount(raw.query_max_chars)) return null;
  return { configured: true, domains: raw.domains.slice(), maxChars: raw.query_max_chars };
}

// The page ships neutral wording; it claims nothing about research until the server has answered.
function configureResearch(raw) {
  const config = parseResearchConfig(raw);
  researchConfig = config && config.configured ? config : null;
  $("rs-submit").disabled = researchConfig === null;
  if (!config) {
    $("rs-notice").textContent = "Research is disabled because its configuration could not be confirmed.";
    $("rs-help").textContent = "Nothing can be searched until the server confirms the configuration.";
    setStatus($("rs-status"), "Research is unavailable: the server configuration is missing or invalid.", "bad");
    return;
  }
  if (!config.configured) {
    $("rs-notice").textContent = "Research is not configured on this server, so nothing is searched and " +
      "nothing is sent anywhere from this section.";
    $("rs-help").textContent = "To enable it, start the demo with --research-provider tavily and " +
      "--research-domains agreed, and set the research key in the terminal. Research is limited to the agreed " +
      "allowlist (icar.org.in, agritech.tnau.ac.in and fao.org) and their true subdomains. Matching a domain does " +
      "not verify the content.";
    setStatus($("rs-status"), "Research is off.", "");
    return;
  }
  $("rs-question").maxLength = config.maxChars;
  $("rs-notice").textContent = "REAL SERVICE. Submitting sends the question you type over the internet to the " +
    "Tavily search service, limited to these sites: " + config.domains.join(", ") + ". Results are evidence " +
    "to read, not recommendations, and a search may use credits.";
  $("rs-help").textContent = "Type a question. The crop, region and growth stage you entered in Section 1 " +
    "(if any) are used to judge whether a source applies. Nothing here is saved or added to the history.";
  setStatus($("rs-status"), "Ready. Questions up to " + config.maxChars + " characters.", "");
  if (soConfig) showResearchBanner();
}

// Replaces the banner when research is configured, so it never says that no search service is used.
function showResearchBanner() {
  const second = soConfig.simulated === false
    ? "Section 5 (Second Opinion) sends the PNG you choose, plus the crop, language and readings you enter, " +
      "to a cloud AI provider."
    : "Section 5 (Second Opinion) is a simulation.";
  setBanner("MIXED MODE.", "The rover, its readings and the demo image analysis (sections 1 to 4) are " +
    "produced by local simulation code. " + second + " Section 6 (Research) is different: it sends the " +
    "question you type over the internet to the Tavily search service and shows the sources it returns.");
}

function describeResearchError(e) {
  if (e && e.userFacing) return e.message;
  const code = e && e.data ? safeCode(e.data.error_code) : null;
  if (code) {
    const known = Object.prototype.hasOwnProperty.call(RESEARCH_ERRORS, code);
    return (known ? RESEARCH_ERRORS[code] : "The research service reported an error.") +
      " (code: " + code + "). No sources were produced.";
  }
  if (e && typeof e.message === "string" && e.data !== undefined) return e.message;  // sanitised by the server
  return "The request could not be completed.";
}

function renderAdvisory(report) {
  const box = $("rs-result");
  const root = $("rs-sources");
  clear(root);
  box.hidden = true;
  if (!report || report.kind !== "research_evidence_only" || !Array.isArray(report.sources)) return false;
  $("rs-note").textContent = report.note + (typeof report.retrieved_at === "string"
    ? " Retrieved at " + report.retrieved_at + "." : "");
  addSection(root, "Sources found (" + report.sources.length + ")", report.sources.map((s) => {
    const when = s.publication_date
      ? "published " + s.publication_date + " (" + (s.date_source === "provider_estimate" ? "the search service's estimate" : "stated") + ")"
      : "no publication date";
    const unmet = s.applicability && s.applicability.unmet && s.applicability.unmet.length
      ? "; not established because: " + s.applicability.unmet.join(", ") : "";
    return s.title + " - " + s.url + " - " + when + " - applicability " +
      (s.applicability ? s.applicability.status : "unknown") + unmet + ". Excerpt: " + s.snippet;
  }));
  addSection(root, "Recommendations that are supported", [report.recommendations.note]);
  addSection(root, "What would help next", Array.isArray(report.next_steps) ? report.next_steps : []);
  addSection(root, "Limitations", Array.isArray(report.caveats) ? report.caveats.map((c) => c.message) : []);
  box.hidden = false;
  return true;
}

async function submitAdvisory(event) {
  if (researchProConfig) return submitResearchPro(event);  // the server configured Research Pro instead of Search
  if (event) event.preventDefault();
  if (rsBusy || !researchConfig) return;
  rsBusy = true;
  const button = $("rs-submit");
  const status = $("rs-status");
  button.disabled = true;
  $("rs-result").hidden = true;
  try {
    const question = $("rs-question").value;
    if (question.trim() === "") throw inputError("The question is required.");
    if (characterCount(question) > researchConfig.maxChars) {
      throw inputError("The question is longer than " + researchConfig.maxChars + " characters.");
    }
    setStatus(status, "Searching... this can take up to half a minute and the page cannot be used meanwhile.", "");
    const report = await api("/api/advisory", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, SEND),
      body: JSON.stringify({ question: question, crop: $("user-crop").value, region: $("user-region").value,
        growth_stage: $("user-stage").value }),
    });
    if (renderAdvisory(report)) {
      setStatus(status, "Done. These are sources to read, not recommendations.", "ok");
    } else {
      setStatus(status, "The response was not understood, so it was not displayed.", "bad");
    }
  } catch (e) {
    setStatus(status, "Error: " + describeResearchError(e), "bad");
  } finally {
    rsBusy = false;
    button.disabled = researchConfig === null;
  }
}

// ---- research pro (start and poll): off unless /api/state says the server configured it ----
let researchProConfig = null;
let rpRunId = 0;
const RP_POLL_MS = 5000;
const RP_PAGE_MARGIN_MS = 20000;  // the page gives up this long after the server's own deadline
const RP_MAX_POLL_FAILURES = 3;
const RP_LABEL = "UNREVIEWED AI-GENERATED RESEARCH — NOT VERIFIED AGRICULTURAL ADVICE.";
const RP_STATES = ["running", "completed", "failed", "timed_out", "error"];

// Fixed wording for the closed set of Research Pro error codes. No provider text is ever shown.
const RESEARCH_PRO_ERRORS = {
  credential_missing: "The server has no research credential configured.",
  configuration_invalid: "The research settings are invalid.",
  query_invalid: "The question is empty, too long or contains characters that are not allowed.",
  rate_limited: "The research service is rate limiting requests. Try again later.",
  usage_limit_exceeded: "The research service reports that a usage limit was reached.",
  provider_rejected: "The research service rejected the request.",
  provider_unavailable: "The research service reported a server-side problem. Try again later.",
  provider_unreachable: "The research service could not be reached or did not answer in time.",
  response_malformed: "The research service's answer could not be read, so nothing is shown.",
  response_too_large: "The research service's response was too large and was discarded.",
  task_not_found: "The research service no longer knows this task.",
  task_failed: "The research service reported that the task failed.",
  deadline_exceeded: "The task did not finish before the time limit and was abandoned.",
  too_many_tasks: "Too many research tasks are still running. Wait for one to finish.",
};

function parseResearchProConfig(raw) {
  if (!raw || typeof raw !== "object" || typeof raw.configured !== "boolean") return null;
  if (!raw.configured) return { configured: false };
  if (!isName(raw.domain) || !isCount(raw.query_max_chars) || !isCount(raw.deadline_seconds)) return null;
  return { configured: true, domain: raw.domain, maxChars: raw.query_max_chars, deadline: raw.deadline_seconds };
}

// Leaves the Section 6 wording alone unless the server confirms Research Pro.
function configureResearchPro(raw) {
  const config = parseResearchProConfig(raw);
  researchProConfig = config && config.configured ? config : null;
  if (!researchProConfig) return;
  $("rs-submit").disabled = false;
  $("rs-submit").textContent = "Start research task";
  $("rs-question").maxLength = config.maxChars;
  $("rs-notice").textContent = "REAL SERVICE. Starting a task sends only the question you type over the internet " +
    "to Tavily Research (pro model). A task can take up to " + config.deadline + " seconds and is abandoned after " +
    "that. It may use 15 to 250 credits. The answer is unreviewed AI-generated text, not agricultural advice, and " +
    "only source links from " + config.domain + " are shown.";
  $("rs-help").textContent = "Type a general question. The crop, region and growth stage in Section 1 are not " +
    "sent. Nothing here is saved or added to the history.";
  setStatus($("rs-status"), "Ready. Questions up to " + config.maxChars + " characters.", "");
  if (soConfig) showResearchProBanner();
}

function showResearchProBanner() {
  const second = soConfig.simulated === false
    ? "Section 5 (Second Opinion) sends the PNG you choose, plus the crop, language and readings you enter, " +
      "to a cloud AI provider."
    : "Section 5 (Second Opinion) is a simulation.";
  setBanner("MIXED MODE.", "The rover, its readings and the demo image analysis (sections 1 to 4) are " +
    "produced by local simulation code. " + second + " Section 6 (Research) is different: it sends the " +
    "question you type over the internet to Tavily Research and shows AI-generated text that nobody has reviewed.");
}

function describeResearchProCode(code) {
  const known = Object.prototype.hasOwnProperty.call(RESEARCH_PRO_ERRORS, code);
  return (known ? RESEARCH_PRO_ERRORS[code] : "The research service reported an error.") +
    " (code: " + code + "). No result was produced.";
}

function describeResearchProError(e) {
  if (e && e.userFacing) return e.message;
  const code = e && e.data ? safeCode(e.data.error_code) : null;
  if (code) return describeResearchProCode(code);
  if (e && typeof e.message === "string" && e.data !== undefined && e.data !== null) return e.message;  // sanitised by the server
  return "The request could not be completed.";
}

function validResearchProView(view) {
  return !!view && typeof view === "object" && view.kind === "research_pro_unreviewed" &&
    RP_STATES.indexOf(view.status) !== -1 && Array.isArray(view.sources) && Array.isArray(view.caveats);
}

// Defence in depth: the server already filters sources, and the page checks again before it makes a link.
function isApprovedLink(url, domain) {
  if (typeof url !== "string") return false;
  let parsed;
  try {
    parsed = new URL(url);
  } catch (e) {
    return false;
  }
  if (parsed.protocol !== "https:" || parsed.username !== "" || parsed.password !== "") return false;
  const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
  return host === domain || host.endsWith("." + domain);
}

function proStatusText(view) {
  if (view.status === "running") {
    const wait = Number.isInteger(view.retry_after_seconds) && view.retry_after_seconds > 0
      ? " The service asked us to wait " + view.retry_after_seconds + " seconds before checking again." : "";
    return "Research task running... " + (Number.isInteger(view.elapsed_seconds) ? view.elapsed_seconds : 0) +
      " s of up to " + researchProConfig.deadline + " s." + wait;
  }
  if (view.status === "completed") return "Done. This is unreviewed AI-generated text, not agricultural advice.";
  if (view.status === "failed") return "The research service reported that the task failed. No result was produced.";
  if (view.status === "timed_out") {
    return "The task did not finish within " + researchProConfig.deadline + " seconds and was abandoned. No result was produced.";
  }
  const code = safeCode(view.error_code);
  return code ? describeResearchProCode(code) : "The research task ended with an error. No result was produced.";
}

function renderResearchPro(view) {
  const box = $("rp-result");
  box.hidden = true;
  if (!validResearchProView(view)) return false;
  if (view.status !== "completed") return true;  // the status line carries the message; no text, no links
  if (typeof view.content !== "string") return false;
  const domain = researchProConfig.domain;
  $("rp-label").textContent = RP_LABEL;
  $("rp-meta").textContent = "Question: " + (typeof view.query === "string" ? view.query : "") +
    (typeof view.retrieved_at === "string" ? " - retrieved at " + view.retrieved_at : "");
  $("rp-text").textContent = view.content;
  $("rp-sources-heading").textContent = "Approved source links (" + domain + " only)";
  const links = view.sources.filter((s) => s && isApprovedLink(s.url, domain));
  const list = $("rp-sources");
  clear(list);
  links.forEach((s) => {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = s.url;
    a.rel = "noopener noreferrer nofollow";
    a.target = "_blank";
    a.textContent = typeof s.title === "string" && s.title !== "" ? s.title : s.url;
    li.appendChild(a);
    li.appendChild(document.createTextNode(" - " + s.url));
    list.appendChild(li);
  });
  const dropped = Number.isInteger(view.sources_dropped) ? view.sources_dropped : 0;
  $("rp-sources-note").textContent = (links.length === 0
    ? "No approved sources available. No source links are shown."
    : "Only the links are limited to the approved domain; the text above may use or mention other sources.") +
    (dropped > 0 ? " " + dropped + " returned source(s) were not on the approved domain and are not shown." : "");
  const caveats = $("rp-caveats");
  clear(caveats);
  view.caveats.forEach((c) => {
    if (!c || typeof c.message !== "string") return;
    const li = document.createElement("li");
    li.textContent = c.message;
    caveats.appendChild(li);
  });
  box.hidden = false;
  return true;
}

function showResearchProView(view) {
  setStatus($("rs-status"), proStatusText(view), view.status === "running" ? "" : (view.status === "completed" ? "ok" : "bad"));
  if (!renderResearchPro(view)) {
    setStatus($("rs-status"), "The response was not understood, so it was not displayed.", "bad");
  }
}

function finishResearchPro(runId) {
  if (runId !== rpRunId) return;
  rsBusy = false;
  $("rs-submit").disabled = researchProConfig === null;
}

async function pollResearchPro(runId, taskId, startedAt, failures) {
  if (runId !== rpRunId) return;
  const status = $("rs-status");
  if (Date.now() - startedAt > researchProConfig.deadline * 1000 + RP_PAGE_MARGIN_MS) {
    setStatus(status, "The page stopped waiting for this task. No result was produced.", "bad");
    finishResearchPro(runId);
    return;
  }
  let view;
  try {
    view = await api("/api/research-pro/status?task=" + encodeURIComponent(taskId));
    if (!validResearchProView(view)) throw new Error("The response was not understood.");
  } catch (e) {
    if (runId !== rpRunId) return;
    if (e && e.data !== undefined && e.data !== null) {  // the server answered with an error: stop following
      setStatus(status, "Error: " + describeResearchProError(e), "bad");
      finishResearchPro(runId);
      return;
    }
    if (failures + 1 >= RP_MAX_POLL_FAILURES) {
      setStatus(status, "The page could not read the task state, so it stopped waiting. No result was produced.", "bad");
      finishResearchPro(runId);
      return;
    }
    setTimeout(() => pollResearchPro(runId, taskId, startedAt, failures + 1), RP_POLL_MS);
    return;
  }
  if (runId !== rpRunId) return;
  showResearchProView(view);
  if (view.status === "running") {
    const wait = Number.isInteger(view.retry_after_seconds) && view.retry_after_seconds > 0 ? view.retry_after_seconds * 1000 : 0;
    setTimeout(() => pollResearchPro(runId, taskId, startedAt, 0), Math.max(RP_POLL_MS, wait));
  } else {
    finishResearchPro(runId);
  }
}

async function submitResearchPro(event) {
  if (event) event.preventDefault();
  if (rsBusy || !researchProConfig) return;
  rsBusy = true;
  const runId = ++rpRunId;
  const status = $("rs-status");
  $("rs-submit").disabled = true;
  $("rp-result").hidden = true;
  try {
    const question = $("rs-question").value;
    if (question.trim() === "") throw inputError("The question is required.");
    if (characterCount(question) > researchProConfig.maxChars) {
      throw inputError("The question is longer than " + researchProConfig.maxChars + " characters.");
    }
    setStatus(status, "Starting the research task...", "");
    const view = await api("/api/research-pro/start", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, SEND),
      body: JSON.stringify({ question: question }),
    });
    if (!validResearchProView(view) || typeof view.task_id !== "string" || view.status !== "running") {
      throw inputError("The response was not understood, so no research task is being followed.");
    }
    showResearchProView(view);
    setTimeout(() => pollResearchPro(runId, view.task_id, Date.now(), 0), RP_POLL_MS);
  } catch (e) {
    setStatus(status, "Error: " + describeResearchProError(e), "bad");
    finishResearchPro(runId);
  }
}

// ---- orchestrated report (fake providers only): off unless /api/state announces it ----
let omConfig = null;
let omBusy = false;
const OM_MODE_TEXT = {
  skip: "Skip research",
  fake: "Fake research results (made up)",
  fake_error: "Fake research failure (made up)",
};

function configureOrchestrated(raw) {
  const usable = !!raw && typeof raw === "object" && raw.available === true && raw.fake_providers === true &&
    Array.isArray(raw.research_modes) && raw.research_modes.length > 0 && raw.research_modes.every(isName) &&
    isCount(raw.query_max_chars);
  omConfig = usable ? { modes: raw.research_modes.slice(), maxChars: raw.query_max_chars } : null;
  $("om-run").disabled = omConfig === null;
  if (!omConfig) {
    setStatus($("om-status"), "This section is unavailable: the server configuration is missing or invalid.", "bad");
    return;
  }
  const select = $("om-research");
  clear(select);
  omConfig.modes.forEach((mode) => {
    const option = document.createElement("option");
    option.value = mode;
    option.textContent = Object.prototype.hasOwnProperty.call(OM_MODE_TEXT, mode) ? OM_MODE_TEXT[mode] : mode;
    select.appendChild(option);
  });
  $("om-question").maxLength = omConfig.maxChars;
  setStatus($("om-status"), "Ready (fake providers). Questions up to " + omConfig.maxChars + " characters.", "");
}

// A read-only table with column headers. A cell is text or a ready-made node.
function tableOf(headers, rows) {
  const wrap = el("div", "table-wrap");
  const table = el("table");
  const head = el("thead");
  const headRow = el("tr");
  headers.forEach((h) => {
    const th = el("th", null, h);
    th.scope = "col";
    headRow.appendChild(th);
  });
  head.appendChild(headRow);
  table.appendChild(head);
  const body = el("tbody");
  rows.forEach((cells) => {
    const tr = el("tr");
    cells.forEach((c) => {
      const td = el("td");
      if (c !== null && typeof c === "object") td.appendChild(c);
      else td.textContent = c === null || c === undefined ? "" : String(c);
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
  table.appendChild(body);
  wrap.appendChild(table);
  return wrap;
}

function statTile(label, valueNode) {
  const tile = el("div", "stat");
  tile.appendChild(el("span", "stat-label", label));
  const value = el("span", "stat-value");
  value.appendChild(valueNode);
  tile.appendChild(value);
  return tile;
}

function callout(kind, lead, text) {
  const box = el("div", "callout " + kind);
  box.appendChild(el("strong", null, lead + " "));
  box.appendChild(document.createTextNode(text));
  return box;
}

// One retrieved source. When the response says the research stage is fake, the card says so in words and the
// address is shown as plain text (never a link) with a note that it is not a real address.
// `reference` (optional) is the label a synthesis uses to cite this source, such as "E1". The model is never given the
// address, so this label is how a reader matches a citation to its source.
function sourceCard(entry, fake, reference) {
  const card = el("div", "source-card" + (fake ? " fake" : ""));
  const title = el("div", "source-title");
  title.appendChild(el("span", null, entry.title));
  if (reference) title.appendChild(badge("cited as " + reference, "muted"));
  if (fake) title.appendChild(badge("FAKE", "fake"));
  card.appendChild(title);
  card.appendChild(el("code", null, entry.url));
  if (fake) card.appendChild(el("div", "muted", "Made-up address. It is not a real web page."));
  card.appendChild(el("p", null, entry.snippet));
  const applicability = entry.applicability || {};
  const line = el("div", "card-meta");
  line.appendChild(badge("applicability " + (applicability.status || "unknown"), "muted"));
  if (Array.isArray(applicability.unmet) && applicability.unmet.length) {
    line.appendChild(el("span", "muted", "not established because: " + applicability.unmet.join(", ")));
  }
  card.appendChild(line);
  return card;
}

function describeProvenance(provenance) {
  const lines = Object.keys(provenance || {}).map((key) => {
    const value = provenance[key];
    return key + ": " + (value !== null && typeof value === "object" ? JSON.stringify(value) : value);
  });
  return lines.length ? lines : ["Nothing recorded."];
}

// Returns true when the response is the fake orchestrated workflow and was shown. Text only.
function renderOrchestrated(data) {
  const box = $("om-result");
  box.hidden = true;
  if (!data || data.workflow !== "orchestrated_fake" || data.fake_providers !== true || !data.orchestration) return false;
  const run = data.orchestration;
  const report = run.report;
  if (!report || report.status !== "ok" || !report.sections || !run.run || !Array.isArray(run.run.steps)) return false;
  const s = report.sections;
  $("om-result-label").textContent = data.label;
  const root = $("om-body");
  clear(root);
  const fake = !!data.research_stage && data.research_stage.fake === true;

  const tiles = el("div", "stats");
  tiles.appendChild(statTile("Run status", badge(run.run_status, kindOf(run.run_status))));
  tiles.appendChild(statTile("Record", el("span", null, run.record_id === null ? "not stored"
    : "id " + run.record_id + " (" + run.record_id_scope + ")")));
  tiles.appendChild(statTile("Report origin", badge(report.origin, "warn")));
  tiles.appendChild(statTile("Run id", el("code", null, run.run_id)));
  root.appendChild(tiles);
  root.appendChild(callout("sim", "Where the data comes from.", data.data_source + " " + report.origin_note));

  root.appendChild(el("h4", null, "Orchestration steps"));
  root.appendChild(tableOf(["Step", "Provider", "Status", "Detail"], run.run.steps.map((e) => [
    e.name, e.provider, badge(e.status, kindOf(e.status)),
    e.error_code ? "error code " + e.error_code : (e.skip_reason ? "skipped because " + e.skip_reason : ""),
  ])));

  root.appendChild(callout("warn", "Image stage: " + data.image_stage.status + ".",
    data.image_stage.note + " " + s.image_inference.note));

  root.appendChild(el("h4", null, "What the sensors measured (simulated)"));
  root.appendChild(el("p", "muted", s.sensors_measured.note));
  root.appendChild(tableOf(["Parameter", "Value", "Unit", "Sensor quality", "Calibration"],
    s.sensors_measured.observations.map((o) => [
      o.parameter, showValue(o.value), o.unit === null || o.unit === undefined ? "unit not defined" : o.unit,
      badge(o.quality === null || o.quality === undefined ? "unknown" : o.quality, kindOf(o.quality)),
      o.calibration,
    ])));

  const sources = s.retrieved_sources;
  root.appendChild(el("h4", null, fake ? "What was retrieved (FAKE results)" : "What was retrieved"));
  root.appendChild(callout(fake ? "fake" : "warn", fake ? "FAKE." : "Research stage.", data.research_stage.note));
  if (sources.error_code) {
    root.appendChild(el("p", "bad", "Research stage error code: " + sources.error_code + ". No sources were produced."));
  }
  sources.entries.forEach((entry) => root.appendChild(sourceCard(entry, fake)));
  if (!sources.entries.length && !sources.error_code) root.appendChild(el("p", "muted", "No sources in this report."));
  const sourceNotes = (sources.caveats || []).map((c) => "Note: " + c.message);
  if (sourceNotes.length) addSection(root, "Notes on these sources", sourceNotes);

  addSection(root, "Recommendations that are supported", s.recommendations.supported.length
    ? s.recommendations.supported.map((r) => r.text + " [source: " + r.source + "]")
    : ["None. No rule or applicable source supports a recommendation for this Record."]);
  addSection(root, "Uncertain or unavailable", s.uncertain_or_unavailable.map((u) => u.text));
  addSection(root, "Provenance", describeProvenance(run.provenance));
  box.hidden = false;
  return true;
}

async function runOrchestrated() {
  if (omBusy || !omConfig) return;
  omBusy = true;
  const button = $("om-run");
  const status = $("om-status");
  button.disabled = true;
  $("om-result").hidden = true;
  try {
    const mode = $("om-research").value;
    const question = $("om-question").value;
    if (mode !== "skip") {
      if (question.trim() === "") throw inputError("The question is required for the fake research choices.");
      if (characterCount(question) > omConfig.maxChars) {
        throw inputError("The question is longer than " + omConfig.maxChars + " characters.");
      }
    }
    setStatus(status, "Running (fake providers)...", "");
    const payload = { scenario: $("scenario").value, crop: $("user-crop").value, region: $("user-region").value,
      growth_stage: $("user-stage").value, research: mode };
    if (mode !== "skip") payload.question = question;
    const data = await api("/api/run-orchestrated", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, SEND),
      body: JSON.stringify(payload),
    });
    if (renderOrchestrated(data)) {
      setStatus(status, "Done (fake providers). Run status: " + data.orchestration.run_status + ".", "ok");
      await refreshHistory();
    } else {
      setStatus(status, "The response was not understood, so it was not displayed.", "bad");
    }
  } catch (e) {
    const known = e && (e.userFacing || e.data !== undefined) && typeof e.message === "string";
    setStatus(status, "Error: " + (known ? e.message : "The request could not be completed."), "bad");
  } finally {
    omBusy = false;
    button.disabled = omConfig === null;
  }
}

// ---- integrated workflow: stages, modes and limits come from /api/state; a real stage is offered only when the
// server says it is configured, and nothing falls back to anything else ----
let iwConfig = null;
let iwBusy = false;
const IW_STAGES = [["image", "iw-image-mode"], ["research", "iw-research-mode"], ["compute", "iw-compute-mode"],
  ["synthesis", "iw-synthesis-mode"]];
const IW_MODE_TEXT = { skip: "Skip this stage", fake: "Fake stand-in (made up)", real: "Real provider" };
const IW_STAGE_KEY = { image_findings: "image", research: "research", sensor_summary: "compute", synthesis: "synthesis" };
const IW_STAGE_LABEL = { image: "Image findings", research: "Research", compute: "Compute summary", synthesis: "Synthesis" };
// What a stage sends when it is set to Real. The research text is added from the question box at display time.
const IW_SEND = {
  image: "the PNG image you choose with a fixed findings prompt (no crop, region, sensor values or identifiers)",
  compute: "the sensor_status, soil, air and battery sections of the simulated Record",
  synthesis: "the validated image findings, the retrieved evidence items and the question text",
};
// A starting question so a first run works without typing. It is only sent if the research stage is set to Real.
const IW_DEFAULT_QUESTION = "how should soil moisture readings be read";
// A 1 by 1 PNG used only by the FAKE image stage when no file is chosen; the fake stand-in never looks at an image.
const IW_PLACEHOLDER_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

function configureIntegrated(raw) {
  const usable = !!raw && typeof raw === "object" && raw.available === true && Array.isArray(raw.stages) &&
    raw.stages.length === IW_STAGES.length && raw.stages.every(isName) && Array.isArray(raw.modes) &&
    raw.modes.length > 0 && raw.modes.every(isName) && !!raw.real_available && typeof raw.real_available === "object" &&
    isCount(raw.query_max_chars) && isCount(raw.max_image_bytes);
  iwConfig = usable ? { modes: raw.modes.slice(), real: Object.assign({}, raw.real_available),
    maxChars: raw.query_max_chars, maxImageBytes: raw.max_image_bytes } : null;
  $("iw-run").disabled = iwConfig === null;
  if (!iwConfig) {
    setStatus($("iw-status"), "This section is unavailable: the server configuration is missing or invalid.", "bad");
    return;
  }
  const realNames = [];
  IW_STAGES.forEach((pair) => {
    const stage = pair[0];
    const select = $(pair[1]);
    clear(select);
    iwConfig.modes.forEach((mode) => {
      if (mode === "real" && iwConfig.real[stage] !== true) return;   // never offered unless the server has it
      if (mode === "real") realNames.push(stage);
      const option = document.createElement("option");
      option.value = mode;
      option.textContent = Object.prototype.hasOwnProperty.call(IW_MODE_TEXT, mode) ? IW_MODE_TEXT[mode] : mode;
      select.appendChild(option);
    });
    // Local defaults: a fake stand-in, never a real provider, whatever is configured.
    select.value = iwConfig.modes.indexOf("fake") >= 0 ? "fake" : "skip";
  });
  $("iw-question").maxLength = iwConfig.maxChars;
  if ($("iw-question").value === "") $("iw-question").value = IW_DEFAULT_QUESTION;
  updateRunSummary();
  setStatus($("iw-status"), "Ready. Real providers configured on this server (not called unless you choose them): " +
    (realNames.length ? realNames.join(", ") : "none") + ". Questions up to " + iwConfig.maxChars +
    " characters; images up to " + iwConfig.maxImageBytes + " bytes.", "");
}

// The current choices in words: what each stage will do and what a Real stage would send. Configured is not
// invoked: nothing is sent until Run is pressed.
function iwSendText(stage) {
  if (stage === "research") {
    return 'the question text "' + $("iw-question").value + '" (plus the crop, region and growth stage, only if you typed ' +
      'them above) and the host names this server limits the search to';
  }
  return IW_SEND[stage];
}

function updateRunSummary() {
  const list = $("iw-run-summary");
  clear(list);
  if (!iwConfig) return;
  IW_STAGES.forEach((pair) => {
    const stage = pair[0];
    const mode = $(pair[1]).value;
    const label = IW_STAGE_LABEL[stage];
    if (mode === "real") list.appendChild(el("li", null, label + ": REAL provider. Sends " + iwSendText(stage) + "."));
    else if (mode === "fake") list.appendChild(el("li", null, label + ": FAKE stand-in on this machine. Nothing is sent to any provider."));
    else list.appendChild(el("li", null, label + ": skipped."));
  });
}

// Providers that are configured on this server and what each would receive if chosen. Built only from /api/state;
// "available" never means a request has been made. The server offers NVIDIA for images, Tavily for search and
// Nodus (through its launcher) for the compute summary; the state itself carries only true or false per stage.
function availableRealProviders(state) {
  const items = [];
  const iw = state && state.integrated;
  const real = iw && iw.real_available && typeof iw.real_available === "object" ? iw.real_available : {};
  if (real.image === true) {
    items.push("NVIDIA image findings (Section 8, image stage) would receive the PNG image you choose with a fixed " +
      "findings prompt, and no crop, region, sensor values or identifiers");
  }
  if (real.research === true) {
    items.push("Tavily search (Section 8 research stage and Section 6) would receive the question text you type and " +
      "the host names this server limits the search to");
  }
  if (real.compute === true) {
    items.push("Nodus (Section 8, compute stage) would receive the sensor_status, soil, air and battery sections of the simulated Record");
  }
  if (real.synthesis === true) {
    items.push("the configured synthesis provider (Section 8, synthesis stage) would receive the validated image findings, " +
      "the retrieved evidence items and the question text");
  }
  if (state && state.research_pro && state.research_pro.configured === true) {
    items.push("Tavily Research Pro (Section 6) would receive the question text you type and may use many credits per task");
  }
  if (state && state.second_opinion && state.second_opinion.simulated === false) {
    items.push("a cloud AI provider (Section 5) would receive the PNG you choose plus the crop, language and readings you enter");
  }
  return items;
}

// Replaces the banner only when the server confirmed at least one real provider, so the page never says that no
// outside service is used while one is available. With none configured, the existing mode wording stays.
function refreshBanner(state) {
  const items = availableRealProviders(state);
  if (items.length === 0) return;
  setBanner("REAL PROVIDERS ARE AVAILABLE.", "These are configured on this server but have NOT been called: " +
    items.join("; ") + ". Nothing is sent until you set a stage to Real (or use the Section 5 or Section 6 form) and " +
    "press its run button. Everything else on this page, including the rover and its readings, the fake stand-ins " +
    "and the demo image analysis, is produced by local code.");
}

function buildScenarioOptions(select, scenarios) {
  clear(select);
  scenarios.forEach((s) => {
    const option = document.createElement("option");
    option.value = s.name;
    option.textContent = s.name;
    option.dataset.description = s.description;
    select.appendChild(option);
  });
}

function findingsLines(findings) {
  const lines = [];
  findings.crop.candidates.forEach((c) => lines.push("Crop candidate: " + c.name + " (uncertainty " + c.uncertainty + "; evidence: " +
    c.evidence.join(", ") + ")"));
  const stage = findings.growth_stage;
  lines.push(stage.candidate === null ? "Growth stage: no candidate" : "Growth stage candidate: " + stage.candidate +
    " (uncertainty " + stage.uncertainty + ")");
  findings.symptoms.forEach((s) => lines.push("Symptom observed: " + s.observation + " (possible interpretations: " +
    s.possible_interpretations.join(", ") + ")"));
  findings.needs_more.forEach((n) => lines.push("More information needed: " + n));
  return lines;
}

// What "provider invoked" means depends on the mode the visitor chose: a fake stand-in runs locally and contacts
// nothing, a real provider's adapter was invoked and a request was attempted.
// Closed codes an adapter returns BEFORE it sends anything (missing key, unusable settings, query or image).
const IW_LOCAL_REFUSALS = ["credential_missing", "configuration_invalid", "query_invalid", "image_unsupported"];

function invokedText(mode, called, code) {
  if (called !== true) return "no";
  if (mode === "real") {
    if (IW_LOCAL_REFUSALS.indexOf(code) >= 0) {
      return "yes - the real provider's adapter was invoked but refused locally (" + code + "); no request was sent";
    }
    return "yes - the real provider's adapter was invoked and a request was sent or attempted";
  }
  return "yes - a fake stand-in ran locally (no service was contacted)";
}

// Fixed plain-language meaning and next step for the closed error codes a stage or step can show.
// Written by this page, never taken from a provider. A code outside the table gets a neutral line.
const IW_CODE_HELP = {
  credential_missing: "The server has no usable API key for this provider, so nothing was sent. Set the key before starting the server.",
  configuration_invalid: "The provider settings or the request were unusable, so nothing was sent.",
  image_unsupported: "The image was refused locally (a PNG within the size limit is required), so nothing was sent.",
  query_invalid: "The question was refused locally, so nothing was sent.",
  provider_rejected: "The provider answered with a client error (for example a bad key, no access to the model or a quota). Check the account.",
  provider_unavailable: "The provider answered with a server-side error. Try again later.",
  provider_timeout: "No answer arrived before the time limit. The wait may have been too short for this request; try a smaller image or one more attempt.",
  provider_connection_failed: "The connection failed before any answer arrived (network, DNS, proxy or TLS). Check the connection.",
  transport_failed: "The request could not be completed because of a local problem (for example a key with characters that cannot be sent). No answer was received.",
  provider_unreachable: "The provider could not be reached or did not answer in time (older, less specific code).",
  response_malformed: "An answer arrived but did not match the expected shape, so it was not used.",
  response_empty: "The provider returned an empty answer.",
  response_too_large: "The reply was larger than the local limit and was discarded.",
  rate_limited: "The provider says too many requests were made. Wait before trying again.",
  usage_limit_exceeded: "The provider says the usage or credit limit is used up. Check the account.",
  no_results: "The search returned nothing from the approved sites.",
};

function codeHelp(code) {
  if (typeof code !== "string") return "";
  if (Object.prototype.hasOwnProperty.call(IW_CODE_HELP, code)) return IW_CODE_HELP[code];
  return "No further explanation is available for this code.";
}

function errorDetail(code) {
  return "error code " + code + " - " + codeHelp(code);
}

// Diagnostics for the real NVIDIA image request: a numeric HTTP status or a closed failure category, plus timing. The
// server sends only allowlisted values (never text from NVIDIA, the network or an exception).
const IW_OBS_FAILURE = {
  timeout: "no HTTP response arrived before the time limit",
  connection_failed: "no HTTP response arrived because the connection failed (network, DNS, proxy or TLS)",
  response_too_large: "a reply arrived but was larger than the local size limit and was discarded",
  other_transport_error: "the request could not be completed because of a local transport problem",
};

// What an HTTP 5xx reply's body looked like, as a closed class chosen by the server from fixed word fragments. These are
// descriptions of the reply's wording, not a diagnosis; the text of the reply is never sent to the page.
const IW_ERROR_CLASS = {
  empty: "the reply body was empty",
  not_text: "the reply body was not readable text",
  resource_exhausted: "the reply body mentions an exhausted resource (wording only; the account and the model were not checked)",
  capacity_or_rate_limit: "the reply body mentions a capacity, queue or rate limit (wording only)",
  gateway_or_unavailable: "the reply body mentions a gateway, upstream or unavailable service (wording only)",
  unclassified: "the reply body matched none of the known wordings",
};

// The HTTP error status by its standard meaning only. It says what kind of answer the code is, not why it was given.
const IW_STATUS_KIND = {
  rate_limited: "the status is 429, which means 'too many requests' (a rate limit was signalled; it does not say whose or which)",
  authentication_or_permission: "the status is 401 or 403, which means the credential or its permission was refused",
  payment_required: "the status is 402, which means the service says payment or credit is required",
  request_timeout: "the status is 408, which means the service says the request timed out",
  client_error: "the status is a 4xx client error: the service refused the request itself",
  server_unavailable: "the status is 503, which means 'service unavailable' (temporarily unable to handle the request)",
  gateway_error: "the status is 502 or 504, which means a gateway between you and the model failed",
  server_error: "the status is a 5xx server error",
};
const IW_RETRY_AFTER = {
  absent: "no Retry-After header came with the reply",
  date: "a Retry-After header with a date came with the reply (the date is not shown or compared with the clock)",
  unusable: "a Retry-After header came with the reply but its value was not usable",
  not_captured: "this server's transport does not expose reply headers, so Retry-After could not be checked",
};

function errorClassLines(c) {
  if (!c || typeof c !== "object" || !Object.prototype.hasOwnProperty.call(IW_ERROR_CLASS, c.body_class)) return [];
  const has = (map, key) => typeof key === "string" && Object.prototype.hasOwnProperty.call(map, key);
  const size = typeof c.body_bytes === "number" ? c.body_bytes + " bytes" : "an unknown size";
  const lines = [];
  if (has(IW_STATUS_KIND, c.status_kind)) lines.push("Error reply: " + IW_STATUS_KIND[c.status_kind] + ".");
  lines.push("Error reply body (" + size + "): " + IW_ERROR_CLASS[c.body_class] +
    ". The text of the reply is not kept or shown, and a class does not prove the cause.");
  if (c.retry_after === "seconds" && typeof c.retry_after_seconds === "number") {
    lines.push("Retry-After: the service asked for a wait of " + c.retry_after_seconds + " seconds. Nothing here retries; any retry is yours to make.");
  } else if (has(IW_RETRY_AFTER, c.retry_after)) {
    lines.push("Retry-After: " + IW_RETRY_AFTER[c.retry_after] + ".");
  }
  return lines;
}

// Where the real synthesis request stopped, and the shape of what came back. The server sends only closed words and
// bounded numbers (never the prompt, the reply or any model text), and this page shows nothing else.
const IW_SYN_FAILED_AT = {
  configuration_invalid: "the synthesis configuration was not usable, so nothing was sent",
  credential_missing: "the key variable was empty, so nothing was sent",
  request_not_built: "the request could not be built from the inputs, so nothing was sent",
  request_too_large: "the request was larger than the local limit, so nothing was sent",
  transport_failed: "the request failed before a usable reply arrived (timeout, connection or oversize reply)",
  http_status: "the service answered with an error status",
  status_invalid: "the service answered with an unusable status",
  reply_not_bytes: "the reply was not readable bytes",
  document_error: "the service reported an error inside a 200 reply",
  reply_not_json: "the reply was not valid JSON",
  reply_shape: "the reply had no first choice or no text field",
  empty_content: "the model returned empty text",
  content_too_long: "the model text was longer than the local limit",
  content_not_json_object: "the model text was not a single JSON object",
  output_truncated: "the model ran out of its token limit before it finished, so its JSON was cut off and was refused (nothing is repaired or completed)",
  output_invalid: "the model text was a JSON object but did not meet the synthesis rules",
};
const IW_SYN_SHAPE = {
  json_object: "a bare JSON object", fenced_json: "a JSON object inside a code fence",
  prose_or_other: "something other than a JSON object (prose, or text cut off mid-way)", empty: "empty",
};

function synthesisDiagnosticLines(d) {
  const lines = [];
  const known = (map, key) => typeof key === "string" && Object.prototype.hasOwnProperty.call(map, key);
  lines.push(d.failed_at === null ? "The request reached the end of the synthesis checks."
    : known(IW_SYN_FAILED_AT, d.failed_at) ? "Stopped at: " + IW_SYN_FAILED_AT[d.failed_at] + "." : "Stopped at an unknown step.");
  if (typeof d.http_status === "number") lines.push("HTTP status: " + d.http_status + ".");
  if (typeof d.reply_bytes === "number") lines.push("Reply size: " + d.reply_bytes + " bytes.");
  if (typeof d.finish_reason === "string" && /^[a-z_]{1,20}$/.test(d.finish_reason)) {
    lines.push("Finish reason: " + d.finish_reason + (d.finish_reason === "length"
      ? " (the model hit the token limit, so its JSON may be cut off)." : "."));
  }
  if (typeof d.completion_tokens === "number") {
    lines.push("Completion tokens: " + d.completion_tokens + (typeof d.max_tokens === "number"
      ? " of a limit of " + d.max_tokens : "") + ".");
  }
  if (typeof d.reasoning_tokens === "number") {
    lines.push("Of these, " + d.reasoning_tokens + " were reported as hidden reasoning tokens (not part of the visible text).");
  }
  if (typeof d.content_chars === "number") lines.push("Model text length: " + d.content_chars + " characters.");
  if (known(IW_SYN_SHAPE, d.content_shape)) lines.push("Model text was " + IW_SYN_SHAPE[d.content_shape] + ".");
  if (Array.isArray(d.output_problems) && d.output_problems.length) {
    const safe = d.output_problems.filter((p) => typeof p === "string" && /^[a-z_]{1,48}$/.test(p));
    if (safe.length) lines.push("Rules broken: " + safe.map((p) => p.replace(/_/g, " ")).join("; ") + ".");
  }
  lines.push("No prompt or model text is kept or shown, and these facts do not prove why the model answered this way.");
  return lines;
}

// Closed facts about an HTTP success reply to the image request, chosen by the server from the raw reply and never
// containing any of its text. The finish reason is the service's own report. Nothing here infers why a reply ended.
const IW_REPLY_ENVELOPE = {
  not_json: "the reply was not valid JSON, so it is not a chat completion",
  not_an_object: "the reply was JSON but not an object",
  no_choices: "the reply had no usable first choice",
  no_message: "the first choice had no message object",
  content_absent: "the message had no content field",
  content_null: "the message content was null (no text)",
  content_not_text: "the message content was not text",
  content_blank: "the message content was blank",
  content_text: "the message had text content",
};
const IW_REPLY_SHAPE = { json_object: "a bare JSON object", fenced_json: "a JSON object inside a code fence",
  other: "something other than a JSON object (prose, cut-off or invalid JSON, or a list)" };
const IW_REPLY_CHECK = {
  too_long: "the text was longer than the findings limit, so it was not parsed",
  not_json: "the text was not valid JSON, so the findings rules were never reached",
  not_an_object: "the text was JSON but not an object, so the findings rules were never reached",
  contract_failed: "the text was a JSON object that did not meet the findings rules",
  accepted: "the text met the findings rules",
};
// The closed rule codes the server may report for a refused JSON object, in plain words. A path is only ever our own
// contract's field names and list positions; the page shows one only if it has exactly that form.
const IW_RULE_CODE = {
  WRONG_TYPE: "a value of the wrong type", MISSING_REQUIRED_KEY: "a required key is missing",
  EXTRA_FIELD: "an extra key that the contract does not allow", INVALID_ENUM_VALUE: "uncertainty is not one of the allowed words",
  TOO_MANY_ITEMS: "a list has more items than allowed", EMPTY_TEXT: "a text is empty",
  TEXT_TOO_LONG: "a text is longer than allowed", CONTROL_CHARACTER: "a text contains a control or format character",
  DISALLOWED_CHARACTER: "a name contains a character that names may not contain",
  EVIDENCE_WITHOUT_CANDIDATE: "evidence is given although the growth stage is null",
  UNDIAGNOSED_REJECTION: "the validator refused it and the diagnostic could not say why",
  DIAGNOSTIC_ERROR: "the diagnostic itself failed",
};
const IW_RULE_PATH = /^[a-z_]{0,16}(\.[a-z_]{1,32}|\[\d{1,2}\])*$/;

function replyFactsLines(f) {
  if (!f || typeof f !== "object") return [];
  const has = (map, key) => typeof key === "string" && Object.prototype.hasOwnProperty.call(map, key);
  const count = (v) => typeof v === "number" && Number.isInteger(v) && v >= 0;
  const lines = [];
  if (has(IW_REPLY_ENVELOPE, f.envelope)) lines.push("Reply envelope: " + IW_REPLY_ENVELOPE[f.envelope] + ".");
  if (typeof f.finish_reason === "string" && /^[a-z_]{1,20}$/.test(f.finish_reason)) {
    lines.push("Finish reason reported by the service: " + f.finish_reason + (f.finish_reason === "length"
      ? " (the service reports that the reply reached its token limit; this is its report, not a finding about the text)"
      : "") + ".");
  }
  if (count(f.completion_tokens)) {
    lines.push("Completion tokens reported: " + f.completion_tokens + (count(f.reasoning_tokens)
      ? ", of which " + f.reasoning_tokens + " were reported as hidden reasoning tokens" : "") + ".");
  }
  if (count(f.content_chars)) lines.push("Reply text length: " + f.content_chars + " characters (the text is not kept or shown).");
  if (has(IW_REPLY_SHAPE, f.content_shape)) lines.push("Reply text was " + IW_REPLY_SHAPE[f.content_shape] + ".");
  if (has(IW_REPLY_CHECK, f.findings_check)) lines.push("Findings check: " + IW_REPLY_CHECK[f.findings_check] + ".");
  if (f.findings_check === "contract_failed" && Array.isArray(f.findings_rules)) {
    const shown = f.findings_rules.slice(0, 12).filter((r) => r && typeof r === "object" && has(IW_RULE_CODE, r.code)
      && typeof r.path === "string" && r.path.length <= 80 && IW_RULE_PATH.test(r.path));
    shown.forEach((r) => lines.push("Rule broken: " + IW_RULE_CODE[r.code] + " (" + r.code + ") at "
      + (r.path === "" ? "the top level" : r.path) + "."));
    if (count(f.findings_rules_total) && f.findings_rules_total > shown.length) {
      lines.push("Rules broken in all: " + f.findings_rules_total + " (the first " + shown.length + " are listed).");
    }
  }
  if (lines.length) lines.push("These are closed categories and numbers only; they do not show why the service answered as it did.");
  return lines;
}

// A length and a short SHA-256 prefix of the request body that was handed to the transport and of the image this run
// used. They carry no content: they exist so that two runs, or a run and a standalone test of the same file, can be
// compared. The same bytes give the same prefix; a different prefix means different bytes.
function fingerprintLines(f) {
  if (!f || typeof f !== "object") return [];
  const count = (v) => typeof v === "number" && Number.isInteger(v) && v >= 0;
  const hex = (v) => typeof v === "string" && /^[0-9a-f]{16}$/.test(v);
  const lines = [];
  if (count(f.image_bytes) && hex(f.image_sha256_prefix)) {
    lines.push("Image used: " + f.image_bytes + " bytes, SHA-256 starts " + f.image_sha256_prefix + ".");
  }
  if (count(f.body_bytes) && hex(f.body_sha256_prefix)) {
    lines.push("Request body handed to the transport: " + f.body_bytes + " bytes, SHA-256 starts " +
      f.body_sha256_prefix + ".");
  } else if (lines.length) {
    lines.push("No request body was handed to the transport (the request was refused locally).");
  }
  if (lines.length) lines.push("These are a length and a short digest only; no image data or prompt is shown.");
  return lines;
}

function observationLines(o) {
  const secs = (v) => (typeof v === "number" ? v.toFixed(1) + " s" : "an unknown time");
  const lines = [];
  if (o.request_made !== true) {
    lines.push("No request reached NVIDIA: it was refused locally before anything was sent (see the stage's error code).");
  } else if (o.outcome === "http_response" && typeof o.http_status === "number") {
    lines.push("An HTTP response was received: status " + o.http_status + ", after " + secs(o.elapsed_seconds) +
      ". The status alone does not say why (a 5xx can be a capacity or server problem; a 4xx means the request or the account was refused).");
  } else if (o.outcome === "no_http_response") {
    const what = Object.prototype.hasOwnProperty.call(IW_OBS_FAILURE, o.failure) ? IW_OBS_FAILURE[o.failure] : "no usable HTTP response";
    lines.push("No usable HTTP response: " + what + ", after " + secs(o.elapsed_seconds) +
      ". This is a transport failure, not an HTTP status. A timeout or connection error while the reply was being read is shown the same way.");
  } else {
    lines.push("A response arrived without a valid HTTP status, after " + secs(o.elapsed_seconds) + ".");
  }
  if (o.request_made === true && typeof o.timeout_seconds === "number") {
    lines.push("Time limit for this request: " + o.timeout_seconds.toFixed(1) + " s.");
  }
  lines.push("Reasoning-off field: " + (o.thinking_off === true
    ? (o.thinking_off_applied === true ? "added to the request (--findings-thinking-off)" : "requested but not added")
    : "not added (default)"));
  return lines;
}

// Returns true when the response is the integrated workflow and was shown. Text only; no value becomes a link.
function renderIntegrated(data) {
  const box = $("iw-result");
  box.hidden = true;
  if (!data || data.workflow !== "integrated" || !data.integrated || !data.providers) return false;
  const result = data.integrated;
  const report = result.report;
  if (!report || report.status !== "ok" || !report.sections || !result.run || !Array.isArray(result.run.steps) ||
      !result.integration || !Array.isArray(result.integration.stages)) return false;
  const s = report.sections;
  $("iw-result-label").textContent = data.label;
  const root = $("iw-body");
  clear(root);
  const realStages = Array.isArray(data.real_stages) ? data.real_stages : [];

  const tiles = el("div", "stats");
  tiles.appendChild(statTile("Run status", badge(result.run_status, kindOf(result.run_status))));
  tiles.appendChild(statTile("Record", el("span", null, result.record_id === null ? "not stored"
    : "id " + result.record_id + " (" + result.record_id_scope + ")")));
  tiles.appendChild(statTile("Real stages", el("span", null, realStages.length ? realStages.join(", ") : "none")));
  tiles.appendChild(statTile("Run id", el("code", null, result.run_id)));
  root.appendChild(tiles);
  root.appendChild(callout("sim", "Where the data comes from.", data.data_source + " " + report.origin_note));
  const sent = data.data_sent && typeof data.data_sent === "object" ? data.data_sent : {};
  if (realStages.length) {
    root.appendChild(callout("warn", "Sent to real providers.", realStages.map((k) => k + ": " + sent[k]).join("; ")));
  }

  root.appendChild(el("h4", null, "Stages"));
  const stageRows = result.integration.stages.map((st) => [
    st.stage, data.providers[IW_STAGE_KEY[st.stage]], badge(st.status, kindOf(st.status)),
    invokedText(data.providers[IW_STAGE_KEY[st.stage]], st.provider_called, st.error_code),
    st.error_code ? errorDetail(st.error_code) : (st.skip_reason ? "skipped because " + st.skip_reason : ""),
  ]);
  const specialist = result.integration.specialist;
  stageRows.push([specialist.stage, "not available", badge(specialist.status, kindOf(specialist.status)), "no",
    "skipped because " + specialist.skip_reason]);
  root.appendChild(tableOf(["Stage", "Mode chosen", "Status", "Provider invoked", "Detail"], stageRows));
  if (data.findings_observation && typeof data.findings_observation === "object") {
    addSection(root, "NVIDIA image request diagnostics",
      observationLines(data.findings_observation).concat(errorClassLines(data.findings_error_class),
        fingerprintLines(data.findings_request_fingerprint), replyFactsLines(data.findings_reply_facts)));
  }
  if (data.synthesis_diagnostic && typeof data.synthesis_diagnostic === "object") {
    addSection(root, "Synthesis request diagnostics", synthesisDiagnosticLines(data.synthesis_diagnostic));
  }

  root.appendChild(el("h4", null, "Orchestration steps"));
  root.appendChild(tableOf(["Step", "Provider", "Status", "Detail"], result.run.steps.map((e) => [
    e.name, e.provider, badge(e.status, kindOf(e.status)),
    e.error_code ? errorDetail(e.error_code) : (e.skip_reason ? "skipped because " + e.skip_reason : ""),
  ])));

  const imageStep = result.run.steps.find((e) => e.name === "image_findings");
  if (imageStep && imageStep.status === "completed" && imageStep.result && imageStep.result.findings) {
    root.appendChild(callout(data.providers.image === "fake" ? "fake" : "warn", "Image findings.",
      "Model: " + imageStep.result.model + ". Unverified model output: candidates and observations from the image, " +
      "not a diagnosis and not verified evidence."));
    addSection(root, "Image findings", findingsLines(imageStep.result.findings));
  }

  if (result.sensor_summary) {
    const sum = result.sensor_summary.summary;
    addSection(root, "Compute summary of the Record (counts only)", [
      "Readings present: " + sum.readings_present, "Readings missing: " + sum.readings_missing,
      "Sensors ok: " + sum.sensors_ok, "Sensors in fault: " + sum.sensors_fault,
      "Faulty sensors: " + (sum.faulty_sensors.length ? sum.faulty_sensors.join(", ") : "none"),
      "Produced by: " + result.sensor_summary.runner + (result.sensor_summary.fake ? " (FAKE stand-in)" : ""),
    ]);
  }

  const sources = s.retrieved_sources;
  const fakeSources = data.providers.research === "fake";
  root.appendChild(el("h4", null, fakeSources ? "What was retrieved (FAKE results)" : "What was retrieved"));
  if (sources.error_code) {
    root.appendChild(el("p", "bad", "Research stage " + errorDetail(sources.error_code) + " No sources were produced."));
  }
  if (sources.entries.length) {
    root.appendChild(callout(fakeSources ? "fake" : "warn", "Unreviewed search results.",
      "These are search results kept as evidence to read, not verified evidence, and nobody has reviewed them for this crop."));
  }
  sources.entries.forEach((entry, index) => root.appendChild(sourceCard(entry, fakeSources, "E" + (index + 1))));
  if (!sources.entries.length && !sources.error_code) root.appendChild(el("p", "muted", "No sources in this report."));
  const sourceNotes = (sources.caveats || []).map((c) => "Note: " + c.message);
  if (sourceNotes.length) addSection(root, "Notes on these sources", sourceNotes);

  const syn = result.synthesis;
  root.appendChild(el("h4", null, "Synthesis"));
  if (syn) {
    root.appendChild(callout(syn.fake ? "fake" : "warn", "Model-generated and unverified.",
      "Model: " + syn.model + ". This text is not evidence and does not change the findings or the sources it cites."));
    root.appendChild(el("p", "muted", "References: E1, E2 and so on are the retrieved sources above, in the order " +
      "shown. F:crop and F:growth_stage are the image findings' crop and growth stage, and F:symptom:0, F:symptom:1 " +
      "and so on are its symptoms in order. A citation shows which supplied item a statement draws on; it does not " +
      "check that the statement is true."));
    addSection(root, "Statements (each cites findings or sources by reference)", syn.statements.map((st) =>
      st.text + " [cites: " + st.cites.join(", ") + "]"));
    addSection(root, "Uncertainties stated by the synthesis", syn.uncertainties);
  } else {
    root.appendChild(el("p", "muted", "No synthesis in this run. See the stage table for why."));
  }

  addSection(root, "Recommendations that are supported", s.recommendations.supported.length
    ? s.recommendations.supported.map((r) => r.text + " [source: " + r.source + "]")
    : ["None. No rule or applicable source supports a recommendation for this Record."]);
  addSection(root, "Uncertain or unavailable", s.uncertain_or_unavailable.map((u) => u.text));
  addSection(root, "Provenance", describeProvenance(result.provenance));
  box.hidden = false;
  return true;
}

async function runIntegrated() {
  if (iwBusy || !iwConfig) return;
  iwBusy = true;
  const button = $("iw-run");
  const status = $("iw-status");
  button.disabled = true;
  $("iw-result").hidden = true;
  try {
    const providers = {};
    IW_STAGES.forEach((pair) => {
      providers[pair[0]] = $(pair[1]).value;
    });
    const payload = { scenario: $("iw-scenario").value, crop: $("iw-crop").value, region: $("iw-region").value,
      growth_stage: $("iw-stage").value, providers: providers };
    if (providers.research !== "skip") {
      const question = $("iw-question").value;
      if (question.trim() === "") throw inputError("The question is required when the research stage is not skipped.");
      if (characterCount(question) > iwConfig.maxChars) {
        throw inputError("The question is longer than " + iwConfig.maxChars + " characters.");
      }
      payload.question = question;
    }
    if (providers.image !== "skip") {
      const input = $("iw-file");
      const file = input.files && input.files[0];
      if (!file) {
        // Only the FAKE image stage may run without a chosen file; a real provider is never given a made-up image.
        if (providers.image === "real") throw inputError("Choose a PNG file: the real image stage needs one.");
        payload.image_png_base64 = IW_PLACEHOLDER_PNG;
      } else {
        if (file.type !== "image/png") {
          throw inputError("That file is not a PNG (its type is '" + (file.type || "unknown") + "').");
        }
        if (file.size === 0) throw inputError("The file is empty.");
        if (file.size > iwConfig.maxImageBytes) {
          throw inputError("That file is larger than the " + iwConfig.maxImageBytes + "-byte limit.");
        }
        payload.image_png_base64 = await readFileAsBase64(file);
      }
    }
    const real = IW_STAGES.filter((pair) => providers[pair[0]] === "real").map((pair) => pair[0]);
    // The server handles one request at a time and reports nothing until the whole run ends, so the page can only wait.
    setStatus(status, "Running. This page waits for the whole run and shows every stage when it finishes; it cannot show " +
      "progress while the server works. " + (real.length ? "Real providers were chosen (" + real.join(", ") +
      "): a real call can take up to about a minute, and the rest of this demo will not respond meanwhile."
      : "Only local code and fake stand-ins were chosen, so this should take a moment."), "");
    const data = await api("/api/run-integrated", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, SEND),
      body: JSON.stringify(payload),
    });
    if (renderIntegrated(data)) {
      setStatus(status, "Done. Run status: " + data.integrated.run_status + ".", "ok");
      await refreshHistory();
    } else {
      setStatus(status, "The response was not understood, so it was not displayed.", "bad");
    }
  } catch (e) {
    const known = e && (e.userFacing || e.data !== undefined) && typeof e.message === "string";
    setStatus(status, "Error: " + (known ? e.message : "The request could not be completed."), "bad");
  } finally {
    iwBusy = false;
    button.disabled = iwConfig === null;
  }
}

// ---- Section 8: Voice & Multilingual Learning Assistant ----
let mediaRecorder = null;
let audioChunks = [];
let recordingTimer = null;
let recordingSeconds = 0;
let isRecording = false;
let currentAnswerText = "";
let currentAnswerLang = "en";
let currentAudioBlobUrl = null;
let currentLessonId = null;

async function toggleVoiceRecording() {
  const micBtn = $("va-mic-btn");
  const micLabel = $("va-mic-label");
  const timer = $("va-rec-timer");
  const status = $("va-voice-status");

  if (isRecording) {
    if (mediaRecorder && mediaRecorder.state !== "inactive") {
      mediaRecorder.stop();
    }
    return;
  }

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    setStatus(status, "Microphone recording is not supported in this browser. You can type your question directly below.", "warn");
    return;
  }

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioChunks = [];
    recordingSeconds = 0;

    let mimeType = "";
    if (typeof MediaRecorder !== "undefined" && typeof MediaRecorder.isTypeSupported === "function") {
      if (MediaRecorder.isTypeSupported("audio/webm")) {
        mimeType = "audio/webm";
      } else if (MediaRecorder.isTypeSupported("audio/ogg")) {
        mimeType = "audio/ogg";
      } else if (MediaRecorder.isTypeSupported("audio/mp4")) {
        mimeType = "audio/mp4";
      }
    }

    mediaRecorder = mimeType ? new MediaRecorder(stream, { mimeType: mimeType }) : new MediaRecorder(stream);

    mediaRecorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) {
        audioChunks.push(event.data);
      }
    };

    mediaRecorder.onstart = () => {
      isRecording = true;
      if (micBtn) micBtn.classList.add("recording");
      if (micLabel) micLabel.textContent = "Stop Recording";
      if (timer) {
        timer.hidden = false;
        timer.textContent = "00:00";
      }
      setStatus(status, "Listening... Speak your agricultural question now.", "good");
      recordingTimer = setInterval(() => {
        recordingSeconds++;
        const mins = String(Math.floor(recordingSeconds / 60)).padStart(2, "0");
        const secs = String(recordingSeconds % 60).padStart(2, "0");
        if (timer) timer.textContent = mins + ":" + secs;
      }, 1000);
    };

    mediaRecorder.onstop = async () => {
      isRecording = false;
      if (micBtn) micBtn.classList.remove("recording");
      if (micLabel) micLabel.textContent = "Record Voice Question";
      if (timer) timer.hidden = true;
      if (recordingTimer) {
        clearInterval(recordingTimer);
        recordingTimer = null;
      }
      stream.getTracks().forEach((track) => track.stop());

      if (audioChunks.length === 0) {
        setStatus(status, "No audio was captured. Please try speaking again.", "warn");
        return;
      }

      const audioBlob = new Blob(audioChunks, { type: mediaRecorder.mimeType || "audio/webm" });
      setStatus(status, "Transcribing speech via ElevenLabs...", "warn");

      try {
        const lang = $("va-language") ? $("va-language").value : "en";
        const data = await api("/api/voice/transcribe?lang=" + encodeURIComponent(lang), {
          method: "POST",
          headers: Object.assign({ "Content-Type": audioBlob.type || "audio/webm" }, SEND),
          body: audioBlob,
        });

        if (data && data.status === "ok") {
          $("va-question").value = data.text;
          setStatus(status, "Transcribed (" + (data.words_count || 0) + " words in " + data.language + "). Review question and click 'Ask Assistant'.", "good");
        } else {
          setStatus(status, "Transcription completed but returned no text.", "warn");
        }
      } catch (err) {
        const errCode = err.data && err.data.error_code ? err.data.error_code : "unknown";
        if (errCode === "credential_missing") {
          setStatus(status, "Voice transcription requires ElevenLabs credentials. Type your question directly below.", "warn");
        } else {
          setStatus(status, "Transcription error: " + (err.message || "could not transcribe audio"), "bad");
        }
      }
    };

    mediaRecorder.start();
  } catch (err) {
    if (err.name === "NotAllowedError" || err.name === "PermissionDeniedError") {
      setStatus(status, "Microphone access was denied. Allow microphone permission in your browser or type your question.", "bad");
    } else {
      setStatus(status, "Microphone error: " + err.message, "bad");
    }
  }
}

async function askAssistant() {
  const questionInput = $("va-question");
  const question = questionInput.value.trim();
  const status = $("va-voice-status");
  const submitBtn = $("va-submit-btn");
  const answerCard = $("va-answer-card");

  if (!question) {
    setStatus(status, "Please enter or record an agricultural question first.", "warn");
    questionInput.focus();
    return;
  }

  const lang = $("va-language") ? $("va-language").value : "en";
  const includeRover = $("va-include-telemetry") ? $("va-include-telemetry").value === "true" : true;

  submitBtn.disabled = true;
  setStatus(status, "Consulting CropSync assistant with telemetry fusion...", "warn");
  answerCard.hidden = true;

  if (currentAudioBlobUrl) {
    URL.revokeObjectURL(currentAudioBlobUrl);
    currentAudioBlobUrl = null;
  }
  const audioPlayer = $("va-audio-player");
  if (audioPlayer) {
    audioPlayer.pause();
    audioPlayer.removeAttribute("src");
    audioPlayer.hidden = true;
  }
  $("va-play-audio-btn").disabled = false;
  $("va-audio-btn-label").textContent = "Listen to Answer (ElevenLabs Voice)";
  $("va-audio-status").textContent = "Audio ready to synthesize";

  try {
    const payload = {
      question: question,
      language: lang,
      include_rover_data: includeRover,
    };

    const data = await api("/api/assistant/ask", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, SEND),
      body: JSON.stringify(payload),
    });

    currentAnswerText = (data && (data.text || data.answer)) || "";
    currentAnswerLang = (data && data.language) || lang;

    const langNames = { en: "English", hi: "हिन्दी (Hindi)", te: "తెలుగు (Telugu)" };
    $("va-answer-lang-badge").textContent = langNames[currentAnswerLang] || currentAnswerLang;
    $("va-answer-body").textContent = currentAnswerText;

    const sourcesList = $("va-sources-list");
    clear(sourcesList);
    if (data && Array.isArray(data.sources) && data.sources.length > 0) {
      data.sources.forEach((src) => {
        const li = document.createElement("li");
        li.textContent = src;
        sourcesList.appendChild(li);
      });
    } else {
      const li = document.createElement("li");
      li.textContent = "Agronomic general principles";
      sourcesList.appendChild(li);
    }

    $("va-disclaimer").textContent = (data && (data.advisory_warning || data.disclaimer)) || "";
    answerCard.hidden = false;
    setStatus(status, "Answer ready. You can listen via ElevenLabs TTS or read below.", "good");
  } catch (err) {
    setStatus(status, "Failed to get answer: " + err.message, "bad");
  } finally {
    submitBtn.disabled = false;
  }
}

async function toggleAudioPlayback() {
  const audioPlayer = $("va-audio-player");
  const playBtn = $("va-play-audio-btn");
  const btnLabel = $("va-audio-btn-label");
  const audioStatus = $("va-audio-status");

  if (!currentAnswerText) {
    audioStatus.textContent = "No answer text to speak.";
    return;
  }

  if (audioPlayer && !audioPlayer.paused && !audioPlayer.ended && audioPlayer.currentTime > 0) {
    audioPlayer.pause();
    btnLabel.textContent = "Resume Answer Audio";
    audioStatus.textContent = "Audio paused.";
    return;
  }

  if (currentAudioBlobUrl && audioPlayer && audioPlayer.paused && audioPlayer.currentTime > 0) {
    audioPlayer.play();
    btnLabel.textContent = "Pause Audio";
    audioStatus.textContent = "Playing synthesized audio...";
    return;
  }

  if (currentAudioBlobUrl && audioPlayer) {
    audioPlayer.currentTime = 0;
    audioPlayer.play();
    btnLabel.textContent = "Pause Audio";
    audioStatus.textContent = "Playing synthesized audio...";
    return;
  }

  playBtn.disabled = true;
  btnLabel.textContent = "Synthesizing voice...";
  audioStatus.textContent = "Requesting ElevenLabs multilingual neural synthesis...";

  try {
    const response = await api("/api/voice/synthesize", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, SEND),
      body: JSON.stringify({
        text: currentAnswerText,
        language: currentAnswerLang,
      }),
    });

    const blob = await response.blob();
    currentAudioBlobUrl = URL.createObjectURL(blob);
    if (audioPlayer) {
      audioPlayer.src = currentAudioBlobUrl;
      audioPlayer.hidden = false;
      audioPlayer.onended = () => {
        btnLabel.textContent = "Replay Answer Audio";
        audioStatus.textContent = "Playback completed.";
      };
      audioPlayer.onerror = () => {
        btnLabel.textContent = "Listen to Answer (ElevenLabs Voice)";
        audioStatus.textContent = "Audio playback error.";
      };
      await audioPlayer.play();
    }
    btnLabel.textContent = "Pause Audio";
    audioStatus.textContent = "Playing synthesized audio (ElevenLabs multilingual v2)...";
  } catch (err) {
    const errCode = err.data && err.data.error_code ? err.data.error_code : "unknown";
    if (errCode === "credential_missing") {
      audioStatus.textContent = "Audio synthesis requires ElevenLabs credentials. Written answer is shown above.";
      btnLabel.textContent = "Audio Unavailable (Key Missing)";
    } else {
      audioStatus.textContent = "TTS error: " + (err.message || "playback error");
      btnLabel.textContent = "Listen to Answer (ElevenLabs Voice)";
    }
  } finally {
    playBtn.disabled = false;
  }
}

async function loadLessons(lang) {
  const language = lang || ($("va-language") ? $("va-language").value : "en");
  const grid = $("va-lessons-grid");
  try {
    const res = await api("/api/learning/lessons?lang=" + encodeURIComponent(language));
    if (res && (res.lessons || res.status === "ok")) {
      const lessons = res.lessons || [];
      updateLessonProgress(res);
      renderLessons(lessons, res, language);
    }
  } catch (err) {
    clear(grid);
    grid.appendChild(el("p", "muted", "Could not load learning lessons: " + err.message));
  }
}

function updateLessonProgress(progress) {
  if (!progress) return;
  const completedCount = progress.completed_count !== undefined
    ? progress.completed_count
    : (Array.isArray(progress.completed) ? progress.completed.length : 0);
  const total = progress.total || 5;
  const pct = total > 0 ? Math.round((completedCount / total) * 100) : 0;

  const textNode = $("va-progress-text");
  if (textNode) {
    textNode.textContent = "Progress: " + completedCount + " of " + total + " Lessons Completed (" + pct + "%)";
  }
  const fillNode = $("va-progress-fill");
  if (fillNode) {
    fillNode.style.width = pct + "%";
  }
}

function renderLessons(lessons, progress, lang) {
  const grid = $("va-lessons-grid");
  clear(grid);
  if (!Array.isArray(lessons)) return;

  const completedSet = new Set(
    progress && Array.isArray(progress.completed) ? progress.completed : []
  );

  lessons.forEach((les) => {
    const isCompleted = les.completed === true || completedSet.has(les.id);
    const card = el("div", "lesson-card" + (isCompleted ? " completed" : ""));

    const head = el("div", "lesson-card-header");
    head.appendChild(el("span", "lesson-icon", les.icon || "🌱"));
    head.appendChild(badge(isCompleted ? "Completed ✓" : les.category, isCompleted ? "good" : "muted"));
    card.appendChild(head);

    card.appendChild(el("h4", null, les.title));
    card.appendChild(el("p", "lesson-desc", les.summary || les.description));

    const meta = el("div", "lesson-card-meta");
    meta.appendChild(el("span", "muted", "⏱ " + les.duration_minutes + " mins"));
    meta.appendChild(el("span", "muted", "🎯 " + (les.objectives_count || 1) + " objective"));
    card.appendChild(meta);

    const btn = el("button", "btn-lesson-open", isCompleted ? "Review Lesson" : "Start Lesson");
    btn.type = "button";
    btn.addEventListener("click", () => openLesson(les.id, lang));
    card.appendChild(btn);

    grid.appendChild(card);
  });
}

async function openLesson(lessonId, lang) {
  const language = lang || ($("va-language") ? $("va-language").value : "en");
  try {
    const res = await api("/api/learning/lesson?id=" + encodeURIComponent(lessonId) + "&lang=" + encodeURIComponent(language));
    const les = (res && res.lesson) || res;
    if (les && les.id) {
      currentLessonId = les.id;

      $("va-lesson-icon").textContent = les.icon || "🌱";
      $("va-lesson-title").textContent = les.title;
      $("va-lesson-meta").textContent = "Duration: " + les.duration_minutes + " mins | Category: " + les.category;
      $("va-lesson-objective").textContent = les.objective;

      const secContainer = $("va-lesson-sections");
      clear(secContainer);
      if (Array.isArray(les.sections)) {
        les.sections.forEach((sec) => {
          const block = el("div", "lesson-section-block");
          block.appendChild(el("h5", null, sec.heading));
          block.appendChild(el("p", null, sec.body || sec.content));
          if (Array.isArray(sec.key_takeaways) && sec.key_takeaways.length > 0) {
            const list = el("ul", "takeaways-list");
            sec.key_takeaways.forEach((t) => {
              const li = document.createElement("li");
              li.textContent = t;
              list.appendChild(li);
            });
            block.appendChild(list);
          }
          secContainer.appendChild(block);
        });
      }

      const tipsList = $("va-lesson-tips");
      clear(tipsList);
      if (Array.isArray(les.farmer_tips)) {
        les.farmer_tips.forEach((tip) => {
          const li = document.createElement("li");
          li.textContent = tip;
          tipsList.appendChild(li);
        });
      }

      const completeBtn = $("va-complete-lesson-btn");
      completeBtn.disabled = false;
      completeBtn.textContent = les.completed ? "Completed ✓" : "Mark as Completed";

      const viewer = $("va-lesson-viewer");
      viewer.hidden = false;
      viewer.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  } catch (err) {
    alert("Could not load lesson: " + err.message);
  }
}

async function completeCurrentLesson() {
  if (!currentLessonId) return;
  const completeBtn = $("va-complete-lesson-btn");
  completeBtn.disabled = true;

  try {
    const res = await api("/api/learning/progress", {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, SEND),
      body: JSON.stringify({
        lesson_id: currentLessonId,
        completed: true,
      }),
    });

    if (res && (res.status === "ok" || res.completed)) {
      updateLessonProgress(res);
      completeBtn.textContent = "Completed ✓";
      const lang = $("va-language") ? $("va-language").value : "en";
      loadLessons(lang);
    }
  } catch (err) {
    alert("Could not update lesson progress: " + err.message);
    completeBtn.disabled = false;
  }
}

function clearVoiceAssistant() {
  $("va-question").value = "";
  $("va-answer-card").hidden = true;
  setStatus($("va-voice-status"), "Ready. Tap to speak or type below.");
  if (currentAudioBlobUrl) {
    URL.revokeObjectURL(currentAudioBlobUrl);
    currentAudioBlobUrl = null;
  }
  const audioPlayer = $("va-audio-player");
  if (audioPlayer) {
    audioPlayer.pause();
    audioPlayer.removeAttribute("src");
    audioPlayer.hidden = true;
  }
  currentAnswerText = "";
}

function configureVoiceAssistant(vaConfig) {
  const badgeEl = $("va-badge");
  const isOnline = vaConfig && (vaConfig.speech_to_text_configured || vaConfig.text_to_speech_configured || vaConfig.elevenlabs_configured);
  if (isOnline) {
    if (badgeEl) {
      badgeEl.className = "tag good";
      badgeEl.textContent = "ElevenLabs Voice Online";
    }
  } else {
    if (badgeEl) {
      badgeEl.className = "tag sim";
      badgeEl.textContent = "Voice Assistant Active (Key Unset: Text Mode)";
    }
  }

  const micBtn = $("va-mic-btn");
  if (micBtn) micBtn.addEventListener("click", toggleVoiceRecording);

  const submitBtn = $("va-submit-btn");
  if (submitBtn) submitBtn.addEventListener("click", askAssistant);

  const clearBtn = $("va-clear-btn");
  if (clearBtn) clearBtn.addEventListener("click", clearVoiceAssistant);

  const playAudioBtn = $("va-play-audio-btn");
  if (playAudioBtn) playAudioBtn.addEventListener("click", toggleAudioPlayback);

  const langSelect = $("va-language");
  if (langSelect) {
    langSelect.addEventListener("change", () => {
      const chosenLang = langSelect.value;
      const placeholders = {
        en: "e.g. What is my soil moisture and how should I irrigate? or When to apply urea?",
        hi: "उदा. मिट्टी में नमी कितनी है और सिंचाई कब करनी चाहिए? या यूरिया का प्रयोग कब करें?",
        te: "ఉదా. నేలలో తేమ ఎంత ఉంది, నీటిపారుదల ఎలా చేయాలి? లేదా యూరియా ఎప్పుడు వాడాలి?",
      };
      const qInput = $("va-question");
      if (qInput) qInput.placeholder = placeholders[chosenLang] || placeholders.en;
      loadLessons(chosenLang);
      if (currentLessonId && !$("va-lesson-viewer").hidden) {
        openLesson(currentLessonId, chosenLang);
      }
    });
  }

  const closeLessonBtn = $("va-close-lesson-btn");
  if (closeLessonBtn) {
    closeLessonBtn.addEventListener("click", () => {
      $("va-lesson-viewer").hidden = true;
    });
  }

  const completeLessonBtn = $("va-complete-lesson-btn");
  if (completeLessonBtn) completeLessonBtn.addEventListener("click", completeCurrentLesson);

  loadLessons("en");
}

// ---- theme: follows the system setting until the visitor chooses, then the choice applies to every section.
// The choice lasts for this page visit only; nothing is stored in the browser. ----
function systemPrefersDark() {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function currentTheme() {
  const chosen = document.documentElement.getAttribute("data-theme");
  if (chosen === "dark" || chosen === "light") return chosen;
  return systemPrefersDark() ? "dark" : "light";
}

function refreshThemeButton() {
  setText("theme-toggle", currentTheme() === "dark" ? "Switch to light theme" : "Switch to dark theme");
}

function toggleTheme() {
  document.documentElement.setAttribute("data-theme", currentTheme() === "dark" ? "light" : "dark");
  refreshThemeButton();
}

function initTheme() {
  const button = $("theme-toggle");
  if (!button) return;
  button.addEventListener("click", toggleTheme);
  refreshThemeButton();
  if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    if (typeof query.addEventListener === "function") query.addEventListener("change", refreshThemeButton);
  }
}

async function start() {
  initTheme();
  // Attached before the mode is known: the handler does nothing without a confirmed configuration.
  $("so-form").addEventListener("submit", submitSecondOpinion);
  $("rs-form").addEventListener("submit", submitAdvisory);
  try {
    const state = await api("/api/state");
    maxImageBytes = state.max_image_bytes;
    configureSecondOpinion(state.second_opinion);
    configureResearch(state.research);
    configureResearchPro(state.research_pro);
    configureOrchestrated(state.orchestrated);
    configureIntegrated(state.integrated);
    configureOverview(state);
    configureVoiceAssistant(state.voice_assistant);
    refreshBanner(state);
    buildScenarioOptions($("iw-scenario"), state.scenarios);
    const iwDescribe = () => {
      const chosen = $("iw-scenario").options[$("iw-scenario").selectedIndex];
      $("iw-scenario-description").textContent = chosen ? chosen.dataset.description : "";
    };
    $("iw-scenario").addEventListener("change", iwDescribe);
    iwDescribe();
    const select = $("scenario");
    clear(select);
    state.scenarios.forEach((s) => {
      const option = document.createElement("option");
      option.value = s.name;
      option.textContent = s.name;
      option.dataset.description = s.description;
      select.appendChild(option);
    });
    const describe = () => {
      const chosen = select.options[select.selectedIndex];
      $("scenario-description").textContent = chosen ? chosen.dataset.description : "";
    };
    select.addEventListener("change", describe);
    describe();
  } catch (e) {
    setStatus($("run-status"), "Could not load the demo: " + e.message, "bad");
    disableSecondOpinion("Second Opinion is unavailable: the configuration could not be loaded.");
    configureResearch(null);
    configureOrchestrated(null);
    configureIntegrated(null);
    configureOverview(null);
    configureVoiceAssistant(null);
  }
  $("run").addEventListener("click", runScenario);
  $("analyze").addEventListener("click", analyze);
  $("om-run").addEventListener("click", runOrchestrated);
  $("iw-run").addEventListener("click", runIntegrated);
  IW_STAGES.forEach((pair) => $(pair[1]).addEventListener("change", updateRunSummary));
  $("iw-question").addEventListener("input", updateRunSummary);
  $("refresh").addEventListener("click", refreshHistory);
  await refreshHistory();
}

start();
