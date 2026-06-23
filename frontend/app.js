"use strict";
// Treadwell Customer Proposal Portal — proposal page (/p/<token>).
// Account model: access requires a session whose email matches this proposal.
// If not signed in -> show the shared login (auth.js). If signed in as a
// different email -> "wrong account" message.

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

function alertBox(el, kind, msg) { if (!el) return; el.className = `alert ${kind}`; el.textContent = msg; show(el); }
function clearAlert(el) { if (el) { el.textContent = ""; el.className = "hidden"; } }

let STATE = null;

// ── boot ──────────────────────────────────────────────────────────────────────
(async function boot() {
  if (!TOKEN) { renderNotFound(); return; }
  const { ok, data } = await api("GET", "");
  hide($("loading"));
  if (!ok && data.error === "not_found") { renderNotFound(); return; }
  if (data.authed && data.view) { renderPortal(data.view); }
  else if (data.wrong_account) { renderWrongAccount(); }
  else { renderGate(); }
})();

function renderNotFound() {
  hide($("loading"));
  const g = $("gate"); show(g);
  g.innerHTML = '<div class="card login-card"><h1>Link not found</h1><p class="muted">This proposal link is invalid or has expired. Please contact your Treadwell representative.</p></div>';
}

function renderGate() {
  const g = $("gate"); show(g);
  TWLogin.renderLogin(g, {
    onSuccess: async () => {
      const fresh = await api("GET", "");
      if (fresh.data.authed && fresh.data.view) { hide(g); renderPortal(fresh.data.view); }
      else { renderWrongAccount(); }
    },
  });
}

function renderWrongAccount() {
  const g = $("gate"); show(g); hide($("portal"));
  g.innerHTML =
    '<div class="card login-card"><h1>Different account</h1>' +
    '<p class="muted">You\'re signed in with an email that isn\'t on this proposal. View your own projects, or sign in with the email this proposal was sent to.</p>' +
    '<div class="stack">' +
    '<a class="btn btn-primary btn-block" href="/">View your projects</a>' +
    '<button class="btn btn-secondary btn-block" id="wa-logout">Use a different account</button>' +
    '</div></div>';
  $("wa-logout").addEventListener("click", async () => {
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
    location.reload();
  });
}

// ── portal render ───────────────────────────────────────────────────────────────
const ICON_CHECK = '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
const ICON_DOT = '<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/></svg>';

function renderPortal(vm) {
  STATE = vm;
  show($("portal"));
  $("p-title").textContent = vm.project_name || "Your Proposal";
  $("p-sub").textContent = vm.city_state || "";
  const approved = vm.status.proposal === "approved";

  const badge = $("p-status-badge");
  if (approved) { badge.className = "badge done"; badge.textContent = "Approved"; }
  else { badge.className = "badge warn"; badge.textContent = "Awaiting your approval"; }

  renderTracker(vm.status);

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
    { label: "Proposal", done: st.proposal === "approved", val: st.proposal === "approved" ? "Approved" : "Pending" },
    { label: "Deposit", done: st.deposit === "received", val: st.deposit === "received" ? "Received" : "Pending" },
    { label: "Schedule", done: st.schedule === "scheduled", val: st.schedule === "scheduled" ? "Scheduled" : "Pending" },
  ];
  $("tracker").innerHTML = steps.map((s) => `
    <div class="step ${s.done ? "is-done" : ""}">
      <div class="lbl">${s.label}</div>
      <div class="val" style="color:${s.done ? "var(--success)" : "var(--secondary)"}">${s.done ? ICON_CHECK : ICON_DOT}${s.val}</div>
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
  let selected = 0;
  wrap.querySelectorAll(".option").forEach((el) => {
    el.addEventListener("click", () => {
      if (approved) return;
      selected = +el.dataset.i;
      wrap.querySelectorAll(".option").forEach((x) => x.classList.toggle("selected", +x.dataset.i === selected));
      const sel = $("ap-option"); if (sel) sel.value = options[selected].label;
    });
  });
  $("addons").innerHTML = (addons && addons.length)
    ? "Optional add-ons: " + addons.map((a) => `${esc(a.label)} (${money(a.amount)})`).join(" · ") : "";
  const sel = $("ap-option");
  sel.innerHTML = options.map((o) => `<option value="${esc(o.label)}">${esc(o.label)} — ${money(o.total)}</option>`).join("");
  if (options.length === 1) hide($("ap-option-field"));
}

function renderPdf(has) {
  if (!has) { hide($("pdf-card")); return; }
  show($("pdf-card"));
  $("pdf-link").href = `/api/portal/${TOKEN}/pdf`;
}

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

function setupDeposit() {
  $("check-address").textContent = "Treadwell — Attn: Accounts Receivable (mailing address provided by your representative)";
  const tabAch = $("tab-ach"), tabCheck = $("tab-check");
  const showAch = () => { tabAch.setAttribute("aria-pressed", "true"); tabCheck.setAttribute("aria-pressed", "false"); show($("ach-form")); hide($("check-instructions")); };
  const showCheck = () => { tabAch.setAttribute("aria-pressed", "false"); tabCheck.setAttribute("aria-pressed", "true"); hide($("ach-form")); show($("check-instructions")); };
  tabAch.onclick = showAch; tabCheck.onclick = showCheck;
}

// ── actions (handlers attach once; elements exist in the hidden #portal) ──────────
$("approve-form").addEventListener("submit", async (e) => {
  e.preventDefault(); clearAlert($("approve-alert"));
  const name = $("ap-name").value.trim();
  if (!name) { alertBox($("approve-alert"), "error", "Please enter your full name."); $("ap-name").focus(); return; }
  const option_label = $("ap-option").value || (STATE.options[0] && STATE.options[0].label);
  const btn = $("approve-btn"); btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Submitting…';
  const { ok, data } = await api("POST", "/approve", { name, title: $("ap-title").value.trim(), option_label, date: new Date().toISOString().slice(0, 10) });
  btn.disabled = false; btn.textContent = "Approve proposal";
  if (!ok) { alertBox($("approve-alert"), "error", data.error || "Could not approve. Please try again."); return; }
  const fresh = await api("GET", "");
  renderPortal(fresh.data.view);
  window.scrollTo({ top: 0, behavior: "smooth" });
});

$("qa-form").addEventListener("submit", async (e) => {
  e.preventDefault(); clearAlert($("qa-alert"));
  const body = $("qa-body").value.trim();
  if (!body) return;
  const btn = $("qa-btn"); btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Sending…';
  const { ok, data } = await api("POST", "/questions", { body });
  btn.disabled = false; btn.textContent = "Send question";
  if (!ok) { alertBox($("qa-alert"), "error", data.error || "Could not send. Try again."); return; }
  $("qa-body").value = "";
  STATE.questions = (STATE.questions || []).concat([data.question]);
  renderThread(STATE.questions);
  alertBox($("qa-alert"), "success", "Sent — our team has been notified and will reply here.");
});

$("ach-form").addEventListener("submit", async (e) => {
  e.preventDefault(); clearAlert($("deposit-alert"));
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
