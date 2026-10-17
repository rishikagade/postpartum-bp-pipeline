// Phase 7 viewer. Every value is inserted as text (never HTML). Every call goes to the read-only
// API, which checks the role and logs the read. Roles are simulated.
"use strict";

const state = { role: "nurse", actor: "nurse.1", tab: null, alertId: null };
const $ = (sel) => document.querySelector(sel);
const SVGNS = "http://www.w3.org/2000/svg";

function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const kid of kids.flat()) node.append(kid instanceof Node ? kid : document.createTextNode(String(kid ?? "")));
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

async function api(path) {
  const res = await fetch(path, { headers: { "X-PPBP-Role": state.role, "X-PPBP-Actor": state.actor } });
  const body = await res.json();
  if (!res.ok) throw new Error(body.reason ? `${body.error}: ${body.reason}` : body.error);
  return body.data;
}

function table(headers, rows, numeric = []) {
  return el("table", {},
    el("thead", {}, el("tr", {}, headers.map((h, i) => el("th", { class: numeric.includes(i) ? "num" : "" }, h)))),
    el("tbody", {}, rows.map((r) => el("tr", {}, r.map((c, i) => el("td", { class: numeric.includes(i) ? "num" : "" }, c))))));
}

const when = (s) => (s || "").slice(0, 16).replace("T", " ");
const toDate = (s) => new Date(s.replace(" ", "T").replace(/\+00$/, "Z"));
const fail = (err) => el("p", { class: "error" }, err.message);

// --- Tabs per role ----------------------------------------------------------

const TABS = {
  nurse: [["alerts", "Alerts"], ["patients", "Patients"]],
  engineer: [["loads", "Loads"], ["quality", "Data quality"], ["lineage", "Alert lineage"]],
  privacy_officer: [["holds", "Held alerts"], ["log", "Access log"], ["leaks", "Leak check"], ["quality", "Data quality"]],
};

function renderTabs() {
  const nav = $("#tabs");
  nav.replaceChildren(...TABS[state.role].map(([id, label]) =>
    el("button", { type: "button", class: id === state.tab ? "on" : "", onclick: () => show(id) }, label)));
}

async function show(tab) {
  state.tab = tab;
  renderTabs();
  const view = $("#view");
  view.replaceChildren(el("p", { class: "muted" }, "Loading..."));
  try {
    view.replaceChildren(await VIEWS[tab]());
  } catch (err) {
    view.replaceChildren(fail(err));
  }
}

// --- Nurse: alerts, alert detail, timeline, contact -------------------------

async function alertsView() {
  const alerts = await api("/api/alerts");
  const detail = el("section", { class: "panel" }, el("p", { class: "muted" }, "Select an alert."));
  const open = alerts.filter((a) => a.status === "open");
  const closed = alerts.filter((a) => a.status === "closed");
  const item = (a) => el("li", {
    class: a.alert_id === state.alertId ? "on" : "",
    onclick: (e) => { state.alertId = a.alert_id; e.currentTarget.parentNode.querySelectorAll("li").forEach((n) => n.classList.remove("on")); e.currentTarget.classList.add("on"); loadAlert(a.alert_id, detail); },
  },
    el("div", { class: "row" }, el("span", { class: `badge sev-${a.severity}` }, a.severity), el("strong", {}, a.label),
      el("span", { class: "muted small" }, `day ${a.postpartum_day}`)),
    el("div", { class: "small muted" }, `${a.patient_id} · ${when(a.triggered_at)} UTC`));
  const list = el("section", { class: "panel" },
    el("h2", {}, `Open alerts (${open.length})`),
    el("p", { class: "small muted" }, "Most urgent first. Alerts held back for lack of consent are not shown."),
    el("ul", { class: "list" }, open.map(item)),
    el("h3", {}, `Closed (${closed.length})`),
    el("ul", { class: "list" }, closed.map(item)));
  if (state.alertId) loadAlert(state.alertId, detail);
  return el("div", { class: "split" }, list, detail);
}

async function loadAlert(alertId, box) {
  box.replaceChildren(el("p", { class: "muted" }, "Loading..."));
  try {
    const d = await api(`/api/alerts/${encodeURIComponent(alertId)}`);
    const tl = await api(`/api/patients/${encodeURIComponent(d.alert.patient_id)}/timeline`);
    const a = d.alert;
    const silence = d.rule.rule_id === "DATA_STALE";
    box.replaceChildren(
      el("div", { class: "row" }, el("span", { class: `badge sev-${a.severity}` }, a.severity), el("h2", {}, a.label),
        el("span", { class: `small st-${a.status}` }, a.status)),
      el("p", {}, a.summary),
      el("div", { class: "kv small" },
        el("span", { class: "muted" }, "Patient"), el("span", {}, a.patient_id),
        el("span", { class: "muted" }, "Days since discharge"), el("span", {}, `${a.days_since_discharge} (postpartum day ${a.postpartum_day})`),
        el("span", { class: "muted" }, "Threshold"), el("span", {}, a.threshold),
        el("span", { class: "muted" }, "Rule"), el("span", {}, `${d.rule.rule_id} v${d.rule.rule_version}`)),
      silence
        ? el("div", { class: "notice" }, "No readings arrived for this period. This is not the same as normal readings: blood pressure during the gap is unknown.")
        : el("div", {}, el("h3", {}, "Readings behind this alert"),
            table(["Measured (UTC)", "Day", "BP (mm Hg)", "Flags"],
              d.readings.map((r) => [when(r.measured_at), r.postpartum_day, `${r.systolic}/${r.diastolic}`, (r.flags || []).join(", ")]), [1])),
      el("h3", {}, "Timeline"),
      chart(tl),
      legend(),
      tl.home_readings_withheld ? el("p", { class: "notice" }, "Home readings withheld: no home-monitoring consent.") : "",
      el("details", {}, el("summary", {}, `All readings (${tl.readings.length})`),
        table(["Measured (UTC)", "Day", "Source", "BP (mm Hg)", "What the alert rules made of it"],
          tl.readings.map((r) => [when(r.measured_at), r.postpartum_day, r.source, `${r.systolic}/${r.diastolic}`,
            el("span", { class: `mark-${r.mark}` }, r.mark_label)]), [1])),
      contactBox(a));
  } catch (err) {
    box.replaceChildren(fail(err));
  }
}

const MARKS = [["severe", "Severe: own alert"], ["elevated_in_alert", "Elevated: in an alert"],
  ["elevated_unconfirmed", "Elevated, unconfirmed: no alert"], ["normal", "Below thresholds"], ["clinical", "Clinic reading"]];

function legend() {
  return el("div", { class: "legend" }, MARKS.map(([m, label]) =>
    el("span", { class: `mark-${m}` }, el("span", { class: `dot${m === "clinical" || m === "elevated_unconfirmed" ? " hollow" : ""}` }), label)),
    el("span", { class: "muted" }, "● systolic   ▲ diastolic   dashed: 140/90 and 160/110"));
}

function chart(tl) {
  const W = 680, H = 250, L = 40, R = 10, T = 10, B = 34;
  const start = toDate(tl.patient.discharge_at), end = toDate(tl.patient.monitoring_end);
  const t0 = start.getTime() - 86400000, t1 = end.getTime();
  const pts = tl.readings.filter((r) => { const t = toDate(r.measured_at).getTime(); return t >= t0 && t <= t1; });
  const x = (t) => L + ((t - t0) / (t1 - t0)) * (W - L - R);
  const y = (v) => T + (1 - (Math.min(Math.max(v, 50), 200) - 50) / 150) * (H - T - B);
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": "Blood pressure readings over the monitoring window" });
  s.append(svg("line", { x1: L, y1: H - B, x2: W - R, y2: H - B, class: "axis" }), svg("line", { x1: L, y1: T, x2: L, y2: H - B, class: "axis" }));
  for (const v of [90, 110, 140, 160]) {
    s.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "thr" }));
    const t = svg("text", { x: 4, y: y(v) + 3 }); t.textContent = v; s.append(t);
  }
  for (let d = 0; d * 86400000 <= t1 - start.getTime(); d += 2) {
    const tx = Math.min(x(start.getTime() + d * 86400000) - 8, W - R - 22);
    const t = svg("text", { x: tx, y: H - 12 }); t.textContent = `+${d}d`; s.append(t);
  }
  const cap = svg("text", { x: L, y: H - 1 }); cap.textContent = "days since discharge"; s.append(cap);
  for (const r of pts) {
    const cx = x(toDate(r.measured_at).getTime()), hollow = r.mark === "clinical" || r.mark === "elevated_unconfirmed";
    const g = svg("g", { class: `mark-${r.mark}` });
    const title = svg("title"); title.textContent = `${when(r.measured_at)}  ${r.systolic}/${r.diastolic}  ${r.mark_label}`;
    g.append(title,
      svg("circle", { cx, cy: y(r.systolic), r: 3.6, fill: hollow ? "none" : "currentColor", stroke: "currentColor", "stroke-width": 1.6 }),
      svg("path", { d: `M${cx - 3.6},${y(r.diastolic) + 3} L${cx},${y(r.diastolic) - 3.6} L${cx + 3.6},${y(r.diastolic) + 3} Z`, fill: hollow ? "none" : "currentColor", stroke: "currentColor", "stroke-width": 1.4 }));
    s.append(g);
  }
  return s;
}

function contactBox(alert) {
  const box = el("div", { class: "contact" });
  const button = el("button", { type: "button", class: "ghost", onclick: async () => {
    try {
      const c = await api(`/api/patients/${encodeURIComponent(alert.patient_id)}/contact?alert_id=${encodeURIComponent(alert.alert_id)}`);
      box.replaceChildren(el("div", { class: "notice" }, el("strong", {}, c.display_name), ` · ${c.phone || "no phone on record"}`,
        el("div", { class: "small muted" }, `This lookup was logged as acting on ${alert.alert_id}.`)));
    } catch (err) { box.replaceChildren(fail(err)); }
  } }, "Look up contact details (logged)");
  box.append(button);
  return box;
}

async function patientsView() {
  const patients = await api("/api/patients");
  const box = el("section", { class: "panel" }, el("p", { class: "muted" }, "Select a patient to see every reading, including those that raised no alert."));
  const rows = patients.map((p) => el("li", { onclick: async (e) => {
    e.currentTarget.parentNode.querySelectorAll("li").forEach((n) => n.classList.remove("on")); e.currentTarget.classList.add("on");
    box.replaceChildren(el("p", { class: "muted" }, "Loading..."));
    try { box.replaceChildren(timelinePanel(p, await api(`/api/patients/${encodeURIComponent(p.patient_id)}/timeline`))); }
    catch (err) { box.replaceChildren(fail(err)); }
  } },
    el("div", { class: "row" }, el("strong", {}, `#${p.enrollment_no}`), el("span", {}, p.patient_id),
      p.open_alerts ? el("span", { class: "badge sev-High" }, `${p.open_alerts} open`) : "",
      p.unconfirmed_elevated ? el("span", { class: "badge mark-elevated_unconfirmed" }, `${p.unconfirmed_elevated} unconfirmed`) : ""),
    el("div", { class: "small muted" }, p.home_monitoring_consent ? `${p.home_readings} home readings` : "No home-monitoring consent: home readings withheld")));
  return el("div", { class: "split" }, el("section", { class: "panel" }, el("h2", {}, `Patients (${patients.length})`),
    el("p", { class: "small muted" }, "In enrollment order (#1 = earliest discharge)."), el("ul", { class: "list" }, rows)), box);
}

function timelinePanel(p, tl) {
  return el("div", {}, el("h2", {}, `#${p.enrollment_no} · ${p.patient_id}`),
    el("p", { class: "small muted" }, `Discharged ${when(tl.patient.discharge_at)} UTC; monitoring until ${when(tl.patient.monitoring_end)} UTC.`),
    tl.home_readings_withheld ? el("p", { class: "notice" }, "Home readings withheld: no home-monitoring consent.") : "",
    chart(tl), legend(),
    table(["Measured (UTC)", "Day", "Source", "BP (mm Hg)", "What the alert rules made of it"],
      tl.readings.map((r) => [when(r.measured_at), r.postpartum_day, r.source, `${r.systolic}/${r.diastolic}`,
        el("span", { class: `mark-${r.mark}` }, r.mark_label)]), [1]));
}

// --- Engineer ----------------------------------------------------------------

async function loadsView() {
  const d = await api("/api/loads");
  const reasons = (m) => (m ? Object.entries(m).map(([k, v]) => `${k} ${v}`).join(", ") || "none" : "—");
  return el("section", { class: "panel" }, el("h2", {}, "Recent loads"), el("p", { class: "small muted" }, d.note),
    table(["Load", "Source", "Started (UTC)", "Status", "Files", "Records in", "New", "Changed", "Unchanged", "Rejected", "Rejected by reason"],
      d.loads.map((l) => [l.load_id, l.kind, when(l.started_at), l.status, l.files_in, l.records_in, l.new, l.changed, l.unchanged, l.rejected, reasons(l.rejected_by_reason)]),
      [0, 4, 5, 6, 7, 8, 9]),
    d.latest_home_cleaning ? el("p", { class: "small" },
      `Latest home cleaning: ${d.latest_home_cleaning.lines_in} lines, ${d.latest_home_cleaning.accepted} accepted, ` +
      `${d.latest_home_cleaning.rejected} rejected (${d.latest_home_cleaning.duplicates} duplicates removed).`) : "");
}

async function qualityView() {
  const q = await api("/api/quality");
  return el("section", { class: "panel" },
    el("h2", {}, q.title), el("p", { class: "notice" }, q.label),
    el("div", { class: "score" }, `${q.score.overall_score} / 100`), el("p", {}, q.score.what_this_means),
    table(["Component", "Weight", "Points deducted", "Checks behind it"],
      Object.entries(q.score.components).map(([k, c]) => [k.replaceAll("_", " "), c.weight, c.deduction.toFixed(2),
        c.rule_results.filter((r) => r.counted).map((r) => `${r.rule_name}: ${r.affected}/${r.evaluated}`).join("; ") || "nothing found"]), [1, 2]),
    q.sections.map((s) => el("div", {}, el("h3", {}, s.title), el("p", {}, s.what_this_means))),
    q.policy_review.map((p) => el("div", { class: "flag" }, `POLICY REVIEW: ${p.alerts} ${p.issue}.`)));
}

async function lineageView() {
  const alerts = await api("/api/alerts");
  const out = el("pre", {}, "Select an alert to see its full lineage: rule version, rule results, cleaned readings, redacted raw lines with their hashes, and the loads that brought them in.");
  const pick = el("select", { onchange: async (e) => {
    try { out.textContent = JSON.stringify(await api(`/api/alerts/${encodeURIComponent(e.target.value)}/explain`), null, 2); }
    catch (err) { out.textContent = err.message; }
  } }, el("option", { value: "" }, "Choose an alert..."), alerts.map((a) => el("option", { value: a.alert_id }, `${a.alert_id} · ${a.severity} · ${a.label}`)));
  return el("section", { class: "panel" }, el("h2", {}, "Alert lineage (explain_alert)"), pick, out);
}

// --- Privacy officer ---------------------------------------------------------

async function holdsView() {
  const h = await api("/api/holds");
  return el("section", { class: "panel" }, el("h2", {}, `Alerts held back for consent (${h.summary.held_back_total})`),
    h.summary.policy_review.map((p) => el("div", { class: "flag" }, `POLICY REVIEW: ${p.alerts} ${p.issue}. ${p.why[0].toUpperCase()}${p.why.slice(1)}.`)),
    table(["Alert", "Severity", "Held back"], h.summary.by_rule.map((r) => [r.label, r.severity, r.alerts]), [2]),
    el("h3", {}, "Each held alert"),
    table(["Alert", "Rule", "Patient", "Would have been", "Reason"],
      h.alerts.map((a) => [a.alert_id, a.rule_id, a.patient_id, a.status_if_held, a.reason])));
}

async function logView() {
  const log = await api("/api/access-log");
  return el("section", { class: "panel" }, el("h2", {}, "Access log (latest first)"),
    table(["#", "When (UTC)", "Actor", "Role", "Action", "Resource", "Patient", "Outcome", "Reason"],
      log.map((l) => [l.access_id, when(l.accessed_at), l.actor, l.role, l.action, [l.resource_type, l.resource_id].filter(Boolean).join(" "),
        l.patient_id || "", l.outcome, l.reason || ""]), [0]));
}

async function leaksView() {
  const r = await api("/api/leak-check");
  return el("section", { class: "panel" }, el("h2", {}, `Leak check: ${r.passed ? "passed" : "FAILED"}`),
    el("p", {}, `Scanned ${r.scanned.table_rows} table rows, ${r.scanned.access_responses} access-layer responses, ` +
      `${r.scanned.output_files} output files and ${r.scanned.tracked_files} repository files.`),
    el("p", { class: "small muted" }, `Restricted, not scanned: ${r.restricted_not_scanned.join(", ")}.`),
    r.findings.length ? table(["Where", "What kind"], r.findings.map((f) => [`${f.target} ${f.location}`, JSON.stringify(f.kinds)]))
      : el("p", {}, "No identifying values found outside the restricted zones."));
}

const VIEWS = { alerts: alertsView, patients: patientsView, loads: loadsView, quality: qualityView, lineage: lineageView, holds: holdsView, log: logView, leaks: leaksView };

$("#who").addEventListener("submit", (e) => {
  e.preventDefault();
  state.role = $("#role").value;
  state.actor = $("#actor").value.trim() || `${state.role}.1`;
  state.alertId = null;
  show(TABS[state.role][0][0]);
});
$("#role").addEventListener("change", () => { $("#actor").value = `${$("#role").value.split("_")[0]}.1`; });
show("alerts");
