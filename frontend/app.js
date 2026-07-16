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
  let res;
  try { res = await fetch(`/api/portal/${TOKEN}${path}`, opts); }
  catch { return { ok: false, status: 0, data: {} }; }   // network failure
  let data = {};
  try { data = await res.json(); } catch {}
  return { ok: res.ok && data.ok !== false, status: res.status, data };
}

// If a mid-session action 401s, the session expired — tell the user and reload
// to the login gate. Returns true if it handled an expiry.
function handleExpired(res, alertEl) {
  if (res.status === 401) {
    alertBox(alertEl, "info", "Your session expired — please sign in again.");
    setTimeout(() => location.reload(), 1400);
    return true;
  }
  return false;
}

function alertBox(el, kind, msg) { if (!el) return; el.className = `alert ${kind}`; el.textContent = msg; show(el); }
function clearAlert(el) { if (el) { el.textContent = ""; el.className = "hidden"; } }

let STATE = null;

// ── boot ──────────────────────────────────────────────────────────────────────
(async function boot() {
  if (!TOKEN) { renderNotFound(); return; }
  const res = await api("GET", "");
  hide($("loading"));
  if (res.status === 0 || res.status >= 500) { renderError(); return; }
  const { ok, data } = res;
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

function renderError() {
  const g = $("gate"); show(g);
  g.innerHTML = '<div class="card login-card"><h1>Something went wrong</h1><p class="muted">We couldn\'t load your proposal right now. Please try again in a moment.</p><button class="btn btn-primary" id="err-retry">Retry</button></div>';
  $("err-retry").addEventListener("click", () => location.reload());
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
  STATE.messages = vm.messages || [];
  show($("portal"));
  const approved = vm.status.proposal === "approved";

  setHeader(vm, approved);
  renderTracker(vm.status);

  if (approved && vm.approved && vm.approved.name) {
    const a = vm.approved;
    $("approved-banner").innerHTML = `Approved by <strong>${esc(a.name)}</strong>${a.title ? ", " + esc(a.title) : ""} on ${esc(a.date || "")} — <strong>${esc(a.option || "")}</strong> at <strong>${money(a.total)}</strong>.`;
    show($("approved-banner"));
    hide($("approve-card"));
    show($("deposit-card"));
  }

  renderOptions(vm.options, vm.addons, approved);
  renderPdf(vm.has_pdf);
  renderChat(STATE.messages);
  setupDeposit();

  LAST_STATUS = statusKey(vm.status);
  applyHashView();
  startPolling();
}

function setHeader(vm, approved) {
  const title = vm.project_name || "Your Proposal";
  const sub = vm.city_state || "";
  $("p-title").textContent = title; $("pv-title").textContent = title;
  $("p-sub").textContent = sub; $("pv-sub").textContent = sub;
  for (const id of ["p-status-badge", "pv-status-badge"]) {
    const b = $(id);
    if (approved) { b.className = "badge done"; b.textContent = "Approved"; }
    else { b.className = "badge warn"; b.textContent = "Awaiting your approval"; }
  }
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

function renderOptions(options, addons, approved) {
  const wrap = $("options");
  if (!options || !options.length) {
    $("options-help").textContent = "";
    wrap.innerHTML = '<p class="muted">Your pricing is being finalized — your Treadwell rep will follow up. You can still ask questions below.</p>';
    $("addons").innerHTML = "";
    if (!approved) hide($("approve-card"));
    return;
  }
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

// ── chat thread ──────────────────────────────────────────────────────────────
function renderChat(msgs) {
  const t = $("chat-thread");
  if (!msgs || !msgs.length) {
    t.innerHTML = '<p class="muted small chat-empty">Your conversation with Treadwell will appear here.</p>';
    return;
  }
  const atBottom = t.scrollHeight - t.scrollTop - t.clientHeight < 60;
  t.innerHTML = msgs.map(renderMsg).join("");
  t.querySelectorAll("[data-open-proposal]").forEach((el) => el.addEventListener("click", openProposal));
  if (atBottom) t.scrollTop = t.scrollHeight;   // keep pinned to newest unless the user scrolled up
}

function renderMsg(m) {
  const when = m.created_at ? new Date(m.created_at).toLocaleString() : "";
  if (m.msg_type === "proposal_card") {
    return `<div class="chat-card proposal">
      <div class="cc-title">Your proposal is ready</div>
      <div class="cc-body">${esc(m.body || "")}</div>
      <button class="btn btn-primary" type="button" data-open-proposal>View proposal</button>
    </div>`;
  }
  if (m.msg_type === "deposit_request") {
    const amt = m.meta && m.meta.amount != null ? money(m.meta.amount) : "";
    return `<div class="chat-card deposit">
      <div class="cc-title">Deposit requested${amt ? ` — <span class="cc-amt">${amt}</span>` : ""}</div>
      <div class="cc-body">${esc(m.body || "")}</div>
    </div>`;
  }
  if (m.msg_type === "system") {
    return `<div class="chat-system">${esc(m.body || "")}</div>`;
  }
  const mine = m.author_kind === "customer";
  return `<div class="msg ${mine ? "customer" : "staff"}">
    <div class="who">${mine ? "You" : "Treadwell"}</div>
    <div>${esc(m.body || "")}</div>
    <div class="when">${when}</div>
  </div>`;
}

// ── chat ⇄ proposal view toggle (hash-driven) ─────────────────────────────────
function openProposal() { location.hash = "proposal"; }

function applyHashView() {
  const wantProposal = location.hash.replace("#", "") === "proposal";
  if (wantProposal) {
    hide($("chat-view")); show($("proposal-view"));
    mountPdf();
    window.scrollTo({ top: 0, behavior: "smooth" });
  } else {
    show($("chat-view")); hide($("proposal-view"));
  }
}
window.addEventListener("hashchange", applyHashView);

// ── PDF iframe: mount lazily on first proposal-view entry (upstream render is a
// full docx + LibreOffice pass; never trigger it on the chat landing) ──────────
let PDF_MOUNTED = false;
function mountPdf() {
  if (PDF_MOUNTED || !STATE || !STATE.has_pdf) return;
  PDF_MOUNTED = true;
  const wrap = $("pdf-frame-wrap");
  const ifr = document.createElement("iframe");
  ifr.className = "pdf-frame";
  ifr.title = "Proposal PDF";
  ifr.setAttribute("loading", "lazy");
  ifr.addEventListener("load", () => { const l = $("pdf-loading"); if (l) l.remove(); });
  ifr.src = `/api/portal/${TOKEN}/pdf`;
  wrap.appendChild(ifr);
}

// ── polling: pull new chat messages + detect status changes ───────────────────
let POLL_TIMER = null;
let LAST_STATUS = "";
const statusKey = (st) => `${st.proposal}|${st.deposit}|${st.schedule}`;
const maxMsgId = () => (STATE && STATE.messages || []).reduce((m, x) => Math.max(m, x.id || 0), 0);

async function pollOnce() {
  if (document.hidden || !STATE) return;
  const res = await api("GET", `/messages?after=${maxMsgId()}`);
  if (!res.ok) return;
  const { messages, status } = res.data;
  if (messages && messages.length) {
    const have = new Set((STATE.messages || []).map((m) => m.id));
    const fresh = messages.filter((m) => !have.has(m.id));
    if (fresh.length) { STATE.messages = (STATE.messages || []).concat(fresh); renderChat(STATE.messages); }
  }
  if (status && statusKey(status) !== LAST_STATUS) {
    const full = await api("GET", "");   // status moved elsewhere — refresh tracker + cards
    if (full.ok && full.data.view) renderPortal(full.data.view);
  }
}

function startPolling() {
  if (POLL_TIMER) return;
  POLL_TIMER = setInterval(pollOnce, 12000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollOnce(); });
}

function setupDeposit() {
  $("check-address").textContent = (STATE && STATE.check_address) || "Your Treadwell representative will provide the mailing address.";
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
  const res = await api("POST", "/approve", { name, title: $("ap-title").value.trim(), option_label, date: new Date().toISOString().slice(0, 10) });
  btn.disabled = false; btn.textContent = "Approve proposal";
  if (handleExpired(res, $("approve-alert"))) return;
  const { ok, data } = res;
  if (!ok) { alertBox($("approve-alert"), "error", data.error || "Could not approve. Please try again."); return; }
  const fresh = await api("GET", "");
  renderPortal(fresh.data.view);
  window.scrollTo({ top: 0, behavior: "smooth" });
});

$("back-to-chat").addEventListener("click", () => { location.hash = "chat"; });

$("qa-form").addEventListener("submit", async (e) => {
  e.preventDefault(); clearAlert($("qa-alert"));
  const ta = $("qa-body");
  const body = ta.value.trim();
  if (!body) return;
  const btn = $("qa-btn"); btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
  const res = await api("POST", "/questions", { body });
  btn.disabled = false; btn.textContent = "Send";
  if (handleExpired(res, $("qa-alert"))) return;
  const { ok, data } = res;
  if (!ok) { alertBox($("qa-alert"), "error", data.error || "Could not send. Try again."); return; }
  ta.value = ""; ta.style.height = "";
  if (data.message) { STATE.messages = (STATE.messages || []).concat([data.message]); renderChat(STATE.messages); }
});

// Enter sends; Shift+Enter makes a newline. Auto-grow the composer up to a cap.
$("qa-body").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("qa-form").requestSubmit(); }
});
$("qa-body").addEventListener("input", (e) => {
  const ta = e.target; ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 140) + "px";
});

$("ach-form").addEventListener("submit", async (e) => {
  e.preventDefault(); clearAlert($("deposit-alert"));
  const account_name = $("ach-acct-name").value.trim();
  if (!account_name) { alertBox($("deposit-alert"), "error", "Please enter the name on the account."); return; }
  const btn = $("ach-btn"); btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Submitting…';
  const res = await api("POST", "/deposit", {
    method: "ach", account_name, bank_name: $("ach-bank").value.trim(),
    account_last4: $("ach-last4").value.trim(), note: $("ach-note").value.trim(),
  });
  btn.disabled = false; btn.textContent = "Submit ACH details";
  if (handleExpired(res, $("deposit-alert"))) return;
  const { ok, data } = res;
  if (!ok) { alertBox($("deposit-alert"), "error", data.error || "Could not submit."); return; }
  alertBox($("deposit-alert"), "success", "Thank you — your deposit details were sent securely. We'll mark it Received once confirmed.");
});

$("check-ack").addEventListener("click", async () => {
  clearAlert($("deposit-alert"));
  const res = await api("POST", "/deposit", { method: "check" });
  if (handleExpired(res, $("deposit-alert"))) return;
  const { ok, data } = res;
  if (!ok) { alertBox($("deposit-alert"), "error", data.error || "Could not submit."); return; }
  alertBox($("deposit-alert"), "success", "Thanks for letting us know — we'll mark your deposit Received once the check arrives.");
});
