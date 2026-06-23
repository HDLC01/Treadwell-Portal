"use strict";
// Treadwell Customer Proposal Portal — frontend controller (vanilla JS).

const TOKEN = (location.pathname.match(/\/p\/([^/]+)/) || [])[1] || "";
const $ = (id) => document.getElementById(id);
const show = (el) => el && el.classList.remove("hidden");
const hide = (el) => el && el.classList.add("hidden");
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => (n == null ? "" : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(n));

async function api(method, path, body) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch(`/api/portal/${TOKEN}${path}`, opts);
  let data = {};
  try { data = await res.json(); } catch {}
  return { ok: res.ok && data.ok !== false, status: res.status, data };
}

function alertBox(el, kind, msg) {
  if (!el) return;
  el.className = `alert ${kind}`;
  el.textContent = msg;
  show(el);
}
function clearAlert(el) { if (el) { el.textContent = ""; el.className = ""; hide(el); } }

let STATE = null; // last view model

// ── boot ──────────────────────────────────────────────────────────────────────
(async function boot() {
  if (!TOKEN) { renderNotFound(); return; }
  const { ok, data } = await api("GET", "");
  hide($("loading"));
  if (!ok && data.error === "not_found") { renderNotFound(); return; }
  $("email-hint").textContent = data.email_hint || "your email";
  if (data.authed && data.view) { renderPortal(data.view); }
  else { show($("verify")); }
})();

function renderNotFound() {
  hide($("loading"));
  const c = document.createElement("section");
  c.className = "card";
  c.innerHTML = '<h1>Link not found</h1><p class="muted">This proposal link is invalid or has expired. Please contact your Treadwell representative for a new link.</p>';
  $("app").appendChild(c);
}

// ── verify flow ─────────────────────────────────────────────────────────────────
$("email-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  clearAlert($("verify-alert"));
  const email = $("email").value.trim();
  if (!email) { alertBox($("verify-alert"), "error", "Please enter your email."); return; }
  const btn = $("send-code-btn");
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Sending…';
  const { data } = await api("POST", "/request-code", { email });
  btn.disabled = false; btn.textContent = "Send my code";
  if (data && data.dev_code) {
    alertBox($("verify-alert"), "info", `Staging — your code is ${data.dev_code}. (In production this is emailed.)`);
    $("code").value = data.dev_code;
  } else {
    alertBox($("verify-alert"), "info", "If that email is on file, a 6-digit code is on its way. Enter it below.");
  }
  hide($("email-form")); show($("code-form")); $("code").focus();
  $("_lastEmail") || (window._lastEmail = email);
});

$("resend").addEventListener("click", async () => {
  await api("POST", "/request-code", { email: window._lastEmail || $("email").value.trim() });
  alertBox($("verify-alert"), "info", "A new code has been sent.");
});

$("code-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  clearAlert($("verify-alert"));
  const code = $("code").value.trim();
  if (code.length < 6) { alertBox($("verify-alert"), "error", "Enter the 6-digit code."); return; }
  const btn = $("verify-code-btn");
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Verifying…';
  const { ok, data } = await api("POST", "/verify-code", { email: window._lastEmail || "", code });
  btn.disabled = false; btn.textContent = "View proposal";
  if (!ok) { alertBox($("verify-alert"), "error", data.error || "Incorrect code."); return; }
  const fresh = await api("GET", "");
  hide($("verify"));
  renderPortal(fresh.data.view);
});

// ── portal ───────────────────────────────────────────────────────────────────────
const ICON_CHECK = '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
const ICON_DOT = '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/></svg>';

function renderPortal(vm) {
  STATE = vm;
  show($("portal"));
  $("p-title").textContent = vm.project_name || "Your Proposal";
  $("p-sub").textContent = vm.city_state || "";

  const approved = vm.status.proposal === "approved";

  // top badge
  const badge = $("p-status-badge");
  if (approved) { badge.className = "badge done"; badge.textContent = "Approved"; }
  else { badge.className = "badge warn"; badge.textContent = "Awaiting your approval"; }

  renderTracker(vm.status);

  // approved banner
  if (approved && vm.approved && vm.approved.name) {
    const a = vm.approved;
    $("approved-banner").innerHTML = `Approved by <strong>${esc(a.name)}</strong>${a.title ? ", " + esc(a.title) : ""} on ${esc(a.date || "")} — <strong>${esc(a.option || "")}</strong> at <strong>${money(a.total)}</strong>.`;
    show($("approved-banner"));
    hide($("approve-card"));
    show($("deposit-card"));
  }

  renderSummary(vm.summary);
  renderOptions(vm.options, vm.addons, approved);
  renderPdf(vm.has_pdf);
  renderThread(vm.questions || []);
  setupDeposit();
}

function renderTracker(st) {
  const steps = [
    { key: "proposal", label: "Proposal", done: st.proposal === "approved", val: st.proposal === "approved" ? "Approved" : "Pending" },
    { key: "deposit", label: "Deposit", done: st.deposit === "received", val: st.deposit === "received" ? "Received" : "Pending" },
    { key: "schedule", label: "Schedule", done: st.schedule === "scheduled", val: st.schedule === "scheduled" ? "Scheduled" : "Pending" },
  ];
  $("tracker").innerHTML = steps.map((s) => `
    <div class="step ${s.done ? "is-done" : ""}">
      <div class="lbl">${s.label}</div>
      <div class="val" style="color:${s.done ? "var(--success)" : "var(--muted-fg)"}">${s.done ? ICON_CHECK : ICON_DOT}${s.val}</div>
    </div>`).join("");
}

function renderSummary(s) {
  const rows = [];
  if (s.system_name) rows.push(["System", s.system_name + (s.texture ? ` · ${s.texture}` : "")]);
  if (s.scope_notes) rows.push(["Scope", s.scope_notes]);
  if (s.exclusions) rows.push(["Exclusions", s.exclusions]);
  if (s.proposal_date) rows.push(["Proposal date", s.proposal_date]);
  if (s.site_visit_date) rows.push(["Site visit", s.site_visit_date]);
  const band = '<div class="doc-band"><div class="doc-brand">TREADWELL</div><div class="doc-sub">Industrial Flooring Solutions</div></div>';
  $("summary-body").innerHTML = band + (rows.length
    ? rows.map(([k, v]) => `<div><div class="label-caps">${esc(k)}</div><div>${esc(v)}</div></div>`).join("")
    : '<p class="muted">See the official PDF below for full details.</p>');
}

function renderOptions(options, addons, approved) {
  const wrap = $("options");
  if (!options || !options.length) { hide($("options-card")); return; }
  $("options-help").textContent = options.length > 1 ? "Choose the option you'd like when you approve." : "";
  wrap.innerHTML = options.map((o, i) => `
    <div class="option ${i === 0 ? "selected" : ""}" data-i="${i}">
      <div class="top"><span class="name">${esc(o.label)}</span><span class="price">${money(o.total)}</span></div>
      ${o.system_desc ? `<div class="meta">${esc(o.system_desc)}</div>` : ""}
      ${o.diff != null && o.diff !== 0 ? `<div class="meta">${o.diff > 0 ? "+" : ""}${money(o.diff)} vs base bid</div>` : ""}
    </div>`).join("");
  // selectable (only matters for approval)
  let selected = 0;
  wrap.querySelectorAll(".option").forEach((el) => {
    el.addEventListener("click", () => {
      if (approved) return;
      selected = +el.dataset.i;
      wrap.querySelectorAll(".option").forEach((x) => x.classList.toggle("selected", +x.dataset.i === selected));
      const sel = $("ap-option"); if (sel) sel.value = options[selected].label;
    });
  });
  // add-ons
  $("addons").innerHTML = (addons && addons.length)
    ? "Optional add-ons: " + addons.map((a) => `${esc(a.label)} (${money(a.amount)})`).join(" · ")
    : "";
  // populate approve option select
  const sel = $("ap-option");
  sel.innerHTML = options.map((o) => `<option value="${esc(o.label)}">${esc(o.label)} — ${money(o.total)}</option>`).join("");
  if (options.length === 1) hide($("ap-option-field"));
}

function renderPdf(has) {
  if (!has) { hide($("pdf-card")); return; }
  show($("pdf-card"));
  $("pdf-link").href = `/api/portal/${TOKEN}/pdf`;
}

// ── approve ────────────────────────────────────────────────────────────────────
$("approve-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  clearAlert($("approve-alert"));
  const name = $("ap-name").value.trim();
  if (!name) { alertBox($("approve-alert"), "error", "Please enter your full name."); $("ap-name").focus(); return; }
  const option_label = $("ap-option").value || (STATE.options[0] && STATE.options[0].label);
  const btn = $("approve-btn");
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Submitting…';
  const { ok, data } = await api("POST", "/approve", {
    name, title: $("ap-title").value.trim(), option_label, date: new Date().toISOString().slice(0, 10),
  });
  btn.disabled = false; btn.textContent = "Approve proposal";
  if (!ok) { alertBox($("approve-alert"), "error", data.error || "Could not approve. Please try again."); return; }
  const fresh = await api("GET", "");
  renderPortal(fresh.data.view);
  window.scrollTo({ top: 0, behavior: "smooth" });
});

// ── Q&A ─────────────────────────────────────────────────────────────────────────
function renderThread(qs) {
  const t = $("thread");
  if (!qs.length) { t.innerHTML = '<p class="muted small">No questions yet.</p>'; return; }
  t.innerHTML = qs.map((q) => `
    <div class="msg ${q.author_kind === "customer" ? "customer" : "staff"}">
      <div class="who">${q.author_kind === "customer" ? "You" : "Treadwell"}</div>
      <div>${esc(q.body)}</div>
      <div class="when">${q.created_at ? new Date(q.created_at).toLocaleString() : ""}</div>
    </div>`).join("");
}

$("qa-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  clearAlert($("qa-alert"));
  const body = $("qa-body").value.trim();
  if (!body) return;
  const btn = $("qa-btn");
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Sending…';
  const { ok, data } = await api("POST", "/questions", { body });
  btn.disabled = false; btn.textContent = "Send question";
  if (!ok) { alertBox($("qa-alert"), "error", data.error || "Could not send. Try again."); return; }
  $("qa-body").value = "";
  STATE.questions = (STATE.questions || []).concat([data.question]);
  renderThread(STATE.questions);
  alertBox($("qa-alert"), "success", "Sent — our team has been notified and will reply here.");
});

// ── deposit ───────────────────────────────────────────────────────────────────────
function setupDeposit() {
  $("check-address").textContent = "Treadwell — Attn: Accounts Receivable (mailing address provided by your representative)";
  const tabAch = $("tab-ach"), tabCheck = $("tab-check");
  const showAch = () => { tabAch.setAttribute("aria-pressed", "true"); tabCheck.setAttribute("aria-pressed", "false"); show($("ach-form")); hide($("check-instructions")); };
  const showCheck = () => { tabAch.setAttribute("aria-pressed", "false"); tabCheck.setAttribute("aria-pressed", "true"); hide($("ach-form")); show($("check-instructions")); };
  tabAch.onclick = showAch; tabCheck.onclick = showCheck;
}

$("ach-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  clearAlert($("deposit-alert"));
  const account_name = $("ach-acct-name").value.trim();
  if (!account_name) { alertBox($("deposit-alert"), "error", "Please enter the name on the account."); return; }
  const btn = $("ach-btn"); btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Submitting…';
  const { ok, data } = await api("POST", "/deposit", {
    method: "ach", account_name, bank_name: $("ach-bank").value.trim(),
    account_last4: $("ach-last4").value.trim(), note: $("ach-note").value.trim(),
  });
  btn.disabled = false; btn.textContent = "Submit ACH details";
  if (!ok) { alertBox($("deposit-alert"), "error", data.error || "Could not submit."); return; }
  alertBox($("deposit-alert"), "success", "Thank you — your deposit details were sent securely. We'll mark it Received once confirmed.");
});

$("check-ack").addEventListener("click", async () => {
  clearAlert($("deposit-alert"));
  const { ok, data } = await api("POST", "/deposit", { method: "check" });
  if (!ok) { alertBox($("deposit-alert"), "error", data.error || "Could not submit."); return; }
  alertBox($("deposit-alert"), "success", "Thanks for letting us know — we'll mark your deposit Received once the check arrives.");
});
