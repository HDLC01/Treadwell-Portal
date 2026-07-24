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
    // Lock the selection to what was actually approved (jsonb list; fall back to
    // the denormalized single summary for pre-revamp approvals).
    SELECTED = new Set(a.options && a.options.length ? a.options : (a.option ? [a.option] : []));
    $("approved-banner").innerHTML = `Approved by <strong>${esc(a.name)}</strong>${a.title ? ", " + esc(a.title) : ""} on ${esc(a.date || "")} — <strong>${esc(a.option || "")}</strong> at <strong>${money(a.total)}</strong>.`;
    show($("approved-banner"));
    hide($("approve-card"));
    renderThankYou(a);
  }

  renderOptions(vm.options, vm.addons, approved);
  renderPdf(vm.has_pdf);
  renderContacts(vm);
  renderChat(STATE.messages);
  setupDeposit();

  // Deposit card: shown once approved, until the deposit is marked received.
  if (approved && vm.status.deposit !== "received") show($("deposit-card"));
  else hide($("deposit-card"));

  // Pre-fill the approver name from the contact we already have — editable, so a
  // different signer can overwrite. Only when empty, so a poll refetch (or the
  // customer's in-progress typing) is never clobbered. Blank when we truly have
  // no name on file.
  if (!approved) {
    const nm = $("ap-name");
    if (nm && !nm.value && vm.customer_name) nm.value = vm.customer_name;
  }

  LAST_STATUS = statusKey(vm.status);
  applyHashView(false);   // re-render (incl. poll-triggered) must not scroll the reader
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
    { label: "Contact info", done: st.contacts === "received", val: st.contacts === "received" ? "Received" : "Pending" },
    { label: "Schedule", done: st.schedule === "scheduled", val: st.schedule === "scheduled" ? "Scheduled" : "Pending" },
  ];
  $("tracker").innerHTML = steps.map((s) => `
    <div class="step ${s.done ? "is-done" : ""}">
      <div class="lbl">${s.label}</div>
      <div class="val" style="color:${s.done ? "var(--success)" : "var(--secondary)"}">${s.done ? ICON_CHECK : ICON_DOT}${s.val}</div>
    </div>`).join("");
}

// Selected pricing option labels (multi-select). Persists across re-renders.
let SELECTED = new Set();
let CUR_OPTIONS = [];
const DEPOSIT_PCT = 0.25;   // mirrors backend proposals.DEPOSIT_PCT — live deposit preview

function renderOptions(options, addons, approved) {
  CUR_OPTIONS = options || [];
  const wrap = $("options");
  if (!options || !options.length) {
    $("options-help").textContent = "";
    wrap.innerHTML = '<p class="muted">Your pricing is being finalized — your Treadwell rep will follow up. You can still message us below.</p>';
    $("addons").innerHTML = "";
    if (!approved) hide($("approve-card"));
    return;
  }
  // Default selection (pre-approval only): the base option, else the first.
  if (!approved && !SELECTED.size) {
    const base = options.find((o) => o.is_base) || options[0];
    SELECTED = new Set([base.label]);
  }
  $("options-help").textContent = approved
    ? "" : (options.length > 1 ? "Select every option you'd like to approve — your total updates below." : "");
  wrap.innerHTML = options.map((o) => {
    const on = SELECTED.has(o.label);
    return `<label class="option opt-check ${on ? "selected" : ""}">
      <input type="checkbox" ${on ? "checked" : ""} ${approved ? "disabled" : ""} data-label="${esc(o.label)}">
      <span class="opt-main">
        <span class="top"><span class="name">${esc(o.label)}</span><span class="price">${money(o.total)}</span></span>
        ${o.system_desc ? `<span class="meta">${esc(o.system_desc)}</span>` : ""}
        ${o.diff != null && o.diff !== 0 ? `<span class="meta">${o.diff > 0 ? "+" : ""}${money(o.diff)} vs base bid</span>` : ""}
      </span>
    </label>`;
  }).join("") + '<div class="selected-total" id="selected-total"></div>';
  wrap.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.addEventListener("change", () => {
      if (approved) return;
      if (cb.checked) SELECTED.add(cb.dataset.label); else SELECTED.delete(cb.dataset.label);
      cb.closest(".option").classList.toggle("selected", cb.checked);
      updateSelectedTotal();
    });
  });
  $("addons").innerHTML = (addons && addons.length)
    ? "Optional add-ons: " + addons.map((a) => `${esc(a.label)} (${money(a.amount)})`).join(" · ") : "";
  updateSelectedTotal();
}

function updateSelectedTotal() {
  const total = CUR_OPTIONS.filter((o) => SELECTED.has(o.label)).reduce((s, o) => s + o.total, 0);
  const el = $("selected-total");
  const approved = !!(STATE && STATE.status && STATE.status.proposal === "approved");
  if (el) {
    let html = `<div class="st-row"><span>Selected total</span><strong>${money(total)}</strong></div>`;
    if (!approved && total > 0) {   // live deposit preview while the customer is choosing
      html += `<div class="st-row st-dep"><span>25% deposit due on approval</span><strong>${money(total * DEPOSIT_PCT)}</strong></div>`;
    }
    el.innerHTML = html;
  }
  const btn = $("approve-btn");
  if (btn && !btn.dataset.locked) btn.disabled = SELECTED.size === 0;
}

function renderThankYou(a) {
  const dep = a.deposit_amount;
  $("thankyou-deposit").textContent = dep != null
    ? `Deposit due: ${money(dep)} (25% of ${money(a.total)}).`
    : "";
  show($("thankyou-card"));
}

// ── project contacts (visible after approval; emphasized once deposit received) ─
let CONTACT_ROWS = [];

function renderContacts(vm) {
  const card = $("contacts-card");
  if (vm.status.proposal !== "approved") { hide(card); return; }
  show(card);
  card.classList.toggle("emphasized", vm.status.deposit === "received" && vm.status.contacts !== "received");
  const submitted = vm.status.contacts === "received";
  $("contacts-help").textContent = submitted
    ? "We've got your contacts — you can update them any time before scheduling."
    : "Add the people we should coordinate with. A primary contact is required; add accounts-payable or billing contacts if they differ.";
  if (!CONTACT_ROWS.length) {
    CONTACT_ROWS = (vm.contacts && vm.contacts.length)
      ? vm.contacts.map((c) => ({ role: c.role, name: c.name || "", email: c.email || "", phone: c.phone || "" }))
      : [{ role: "primary", name: vm.customer_name || "", email: "", phone: "" }];
  }
  drawContacts();
}

function drawContacts() {
  const list = $("contacts-list");
  list.innerHTML = CONTACT_ROWS.map(contactRow).join("");
  list.querySelectorAll("[data-remove]").forEach((b) =>
    b.addEventListener("click", () => { CONTACT_ROWS.splice(+b.dataset.remove, 1); drawContacts(); }));
  list.querySelectorAll("[data-field]").forEach((el) => {
    const upd = () => { CONTACT_ROWS[+el.dataset.i][el.dataset.field] = el.value; };
    el.addEventListener("input", upd); el.addEventListener("change", upd);
  });
}

function contactRow(c, i) {
  const isPrimary = i === 0;
  const head = isPrimary
    ? '<span class="contact-role">Primary contact</span>'
    : `<select data-field="role" data-i="${i}" class="contact-role-sel">
         <option value="accounts_payable" ${c.role === "accounts_payable" ? "selected" : ""}>Accounts payable</option>
         <option value="other" ${c.role !== "accounts_payable" ? "selected" : ""}>Other</option>
       </select>
       <button class="linkbtn contact-remove" type="button" data-remove="${i}">Remove</button>`;
  return `<div class="contact-row">
    <div class="contact-row-head">${head}</div>
    <div class="contact-grid">
      <input data-field="name" data-i="${i}" type="text" placeholder="Name *" value="${esc(c.name || "")}">
      <input data-field="email" data-i="${i}" type="email" placeholder="Email" value="${esc(c.email || "")}">
      <input data-field="phone" data-i="${i}" type="tel" placeholder="Phone" value="${esc(c.phone || "")}">
    </div>
  </div>`;
}

function renderPdf(has) {
  if (!has) { hide($("pdf-card")); return; }
  show($("pdf-card"));
  const src = `/api/portal/${TOKEN}/pdf`;
  $("pdf-link").href = src;
  $("pdf-modal-link").href = src;
  $("pdf-modal-title").textContent = (STATE && STATE.project_name) || "Your proposal";
  mountInlinePdf();   // website-style preview in the card; clicking it opens the full view
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
  const viaEmail = m.meta && m.meta.source === "email";
  return `<div class="msg ${mine ? "customer" : "staff"}">
    <div class="who">${mine ? "You" : "Treadwell"}${viaEmail ? ' <span class="via-email">via email</span>' : ""}</div>
    <div>${esc(m.body || "")}</div>
    <div class="when">${when}</div>
  </div>`;
}

// ── chat ⇄ proposal view toggle (hash-driven) ─────────────────────────────────
function openProposal() { location.hash = "proposal"; }

function applyHashView(scroll) {
  const wantProposal = location.hash.replace("#", "") === "proposal";
  if (wantProposal) {
    hide($("chat-view")); show($("proposal-view"));
    if (scroll) window.scrollTo({ top: 0, behavior: "smooth" });   // only on a user nav, not a poll refetch
  } else {
    show($("chat-view")); hide($("proposal-view"));
  }
}
window.addEventListener("hashchange", () => applyHashView(true));

// ── PDF: opens in a full-screen popup (the native viewer needs room; a card is
// unusable). Mount the iframe lazily on first open — the upstream render is a
// full docx + LibreOffice pass, so only customers who open it trigger it. ──────
let PDF_MOUNTED = false;
function mountPdf() {
  if (PDF_MOUNTED || !STATE || !STATE.has_pdf) return;
  PDF_MOUNTED = true;
  const wrap = $("pdf-frame-wrap");
  const ifr = document.createElement("iframe");
  ifr.className = "pdf-frame";
  ifr.title = "Proposal PDF";
  ifr.addEventListener("load", () => { const l = $("pdf-loading"); if (l) l.remove(); });
  // #view=FitH opens the native viewer fit-to-width (readable) instead of its
  // tiny default zoom; keep the toolbar so the customer can zoom/print/download.
  ifr.src = `/api/portal/${TOKEN}/pdf#view=FitH`;
  wrap.appendChild(ifr);
}

// Inline website-style preview inside the card. Non-interactive (pointer-events
// are disabled in CSS) so a click anywhere on it falls through to the #pdf-preview
// button, which opens the full-size popup. Mounted once, on first render.
let INLINE_PDF_MOUNTED = false;
function mountInlinePdf() {
  if (INLINE_PDF_MOUNTED || !STATE || !STATE.has_pdf) return;
  const wrap = $("pdf-inline-wrap");
  if (!wrap) return;
  INLINE_PDF_MOUNTED = true;
  const ifr = document.createElement("iframe");
  ifr.className = "pdf-inline-frame";
  ifr.title = "Proposal document preview";
  ifr.setAttribute("tabindex", "-1");
  ifr.setAttribute("aria-hidden", "true");
  ifr.addEventListener("load", () => { const l = $("pdf-inline-loading"); if (l) l.remove(); });
  // Clean, full-width page teaser: hide the viewer toolbar, fit to width.
  ifr.src = `/api/portal/${TOKEN}/pdf#toolbar=0&view=FitH`;
  wrap.appendChild(ifr);
}
function openPdfModal() {
  if (!STATE || !STATE.has_pdf) return;
  show($("pdf-modal")); show($("pdf-scrim"));
  document.body.style.overflow = "hidden";   // lock the page behind the popup
  mountPdf();
}
function closePdfModal() {
  hide($("pdf-modal")); hide($("pdf-scrim"));
  document.body.style.overflow = "";   // iframe stays mounted → reopening is instant
}

// ── polling: pull new chat messages + detect status changes ───────────────────
let POLL_TIMER = null;
let LAST_STATUS = "";
const statusKey = (st) => `${st.proposal}|${st.deposit}|${st.contacts}|${st.schedule}`;
const maxMsgId = () => (STATE && STATE.messages || []).reduce((m, x) => Math.max(m, x.id || 0), 0);

async function pollOnce() {
  if (document.hidden || !STATE) return;
  const res = await api("GET", `/messages?after=${maxMsgId()}`);
  if (res.status === 401) {   // session expired — stop hammering + surface it
    if (POLL_TIMER) { clearInterval(POLL_TIMER); POLL_TIMER = null; }
    handleExpired(res, $("qa-alert"));
    return;
  }
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
  const dep = (STATE && STATE.deposit) || {};
  const due = dep.due != null ? money(dep.due) : null;
  // Customer-facing reference = the project name (the internal TW-… ref is staff-only).
  const ref = (STATE && STATE.project_name) || "";
  $("deposit-due-line").textContent = due
    ? `Deposit due: ${due} (25% of your total). Reference: ${ref}`
    : (ref ? `Reference: ${ref}` : "");
  if ($("check-ref")) $("check-ref").textContent = ref;
  if ($("check-payable")) $("check-payable").textContent = (STATE && STATE.payable_to) || "Treadwell";
  $("check-address").textContent = (STATE && STATE.check_address) || "Your Treadwell representative will provide the mailing address.";

  const tabs = $("deposit-tabs"), achPane = $("ach-pane"), checkPane = $("check-instructions");
  const tabAch = $("tab-ach"), tabCheck = $("tab-check");
  const showAch = () => { tabAch.setAttribute("aria-pressed", "true"); tabCheck.setAttribute("aria-pressed", "false"); show(achPane); hide(checkPane); };
  const showCheck = () => { tabAch.setAttribute("aria-pressed", "false"); tabCheck.setAttribute("aria-pressed", "true"); hide(achPane); show(checkPane); };
  tabAch.onclick = showAch; tabCheck.onclick = showCheck;

  // If a deposit was already submitted (and not yet marked received), show a shared
  // recorded banner and hide the tabs + BOTH panes — so a reload / second device
  // can't invite a duplicate submission (either method). "Update or resend" reopens
  // the form on whichever method was used (deposit_status only flips when staff confirm).
  const recorded = $("deposit-recorded");
  const reopen = () => { hide(recorded); show(tabs); (dep.submitted_method === "check" ? showCheck : showAch)(); };
  if (dep.submitted) {
    const isCheck = dep.submitted_method === "check";
    $("deposit-recorded-msg").textContent =
      `Thanks — we've recorded your ${isCheck ? "check" : "ACH transfer"}. We'll mark your deposit Received once it ${isCheck ? "arrives" : "clears"}.`;
    show(recorded); hide(tabs); hide(achPane); hide(checkPane);
  } else {
    hide(recorded); show(tabs); showAch();
  }
  $("deposit-resend").onclick = reopen;
}

// ── actions (handlers attach once; elements exist in the hidden #portal) ──────────
$("approve-form").addEventListener("submit", async (e) => {
  e.preventDefault(); clearAlert($("approve-alert"));
  const name = $("ap-name").value.trim();
  if (!name) { alertBox($("approve-alert"), "error", "Please enter your full name."); $("ap-name").focus(); return; }
  const option_labels = [...SELECTED];
  if (!option_labels.length) { alertBox($("approve-alert"), "error", "Please select at least one option to approve."); return; }
  const btn = $("approve-btn"); btn.dataset.locked = "1"; btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Submitting…';
  const res = await api("POST", "/approve", { name, title: $("ap-title").value.trim(), option_labels, date: new Date().toISOString().slice(0, 10) });
  delete btn.dataset.locked; btn.disabled = false; btn.textContent = "Approve proposal";
  if (handleExpired(res, $("approve-alert"))) return;
  const { ok, data } = res;
  if (!ok) { alertBox($("approve-alert"), "error", data.error || "Could not approve. Please try again."); return; }
  const fresh = await api("GET", "");
  renderPortal(fresh.data.view);
  window.scrollTo({ top: 0, behavior: "smooth" });
});

$("back-to-chat").addEventListener("click", () => { location.hash = "chat"; });

// PDF: click the inline preview to open the full-size popup; close via ×, scrim, or Esc.
$("pdf-preview").addEventListener("click", openPdfModal);
$("pdf-close").addEventListener("click", closePdfModal);
$("pdf-scrim").addEventListener("click", closePdfModal);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePdfModal(); });

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
  if (data.message) {   // dedup: a concurrent poll may have already appended this id
    const have = new Set((STATE.messages || []).map((m) => m.id));
    if (!have.has(data.message.id)) {
      STATE.messages = (STATE.messages || []).concat([data.message]);
      renderChat(STATE.messages);
    }
  }
});

$("contacts-add").addEventListener("click", () => {
  CONTACT_ROWS.push({ role: "other", name: "", email: "", phone: "" });
  drawContacts();
});

$("contacts-submit").addEventListener("click", async () => {
  clearAlert($("contacts-alert"));
  const primaryName = ((CONTACT_ROWS[0] && CONTACT_ROWS[0].name) || "").trim();
  if (!primaryName) { alertBox($("contacts-alert"), "error", "Please enter your primary contact's name."); return; }
  const contacts = CONTACT_ROWS
    .map((c, i) => ({
      role: i === 0 ? "primary" : (c.role === "accounts_payable" ? "accounts_payable" : "other"),
      name: (c.name || "").trim(), email: (c.email || "").trim(), phone: (c.phone || "").trim(),
    }))
    .filter((c, i) => i === 0 || c.name);   // keep primary; drop blank extras
  const btn = $("contacts-submit"); btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Submitting…';
  const res = await api("POST", "/contacts", { contacts });
  btn.disabled = false; btn.textContent = "Submit contacts";
  if (handleExpired(res, $("contacts-alert"))) return;
  const { ok, data } = res;
  if (!ok) { alertBox($("contacts-alert"), "error", data.error || "Could not submit your contacts."); return; }
  alertBox($("contacts-alert"), "success", "Thank you — your contacts were sent to our team.");
  const fresh = await api("GET", "");
  if (fresh.ok && fresh.data.view) { CONTACT_ROWS = []; renderPortal(fresh.data.view); }
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
  const digits = (id) => $(id).value.replace(/\D/g, "");
  const routing = digits("ach-routing"), routingConfirm = digits("ach-routing-confirm");
  const account = digits("ach-account"), accountConfirm = digits("ach-account-confirm");
  const fail = (msg, id) => { alertBox($("deposit-alert"), "error", msg); $(id).focus(); };
  if (!account_name) return fail("Please enter the account name.", "ach-acct-name");
  if (!/^\d{9}$/.test(routing)) return fail("Routing number must be exactly 9 digits.", "ach-routing");
  if (routing !== routingConfirm) return fail("Routing numbers don't match — please re-enter.", "ach-routing-confirm");
  if (!/^\d{4,}$/.test(account)) return fail("Account number must be at least 4 digits.", "ach-account");
  if (account !== accountConfirm) return fail("Account numbers don't match — please re-enter.", "ach-account-confirm");
  const btn = $("ach-btn"); btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Submitting…';
  const res = await api("POST", "/deposit", {
    method: "ach", account_name, routing_number: routing, account_number: account, note: $("ach-note").value.trim(),
  });
  btn.disabled = false; btn.textContent = "Pay The Deposit";
  if (handleExpired(res, $("deposit-alert"))) return;
  const { ok, data } = res;
  if (!ok) { alertBox($("deposit-alert"), "error", data.error || "Could not submit."); return; }
  const fresh = await api("GET", "");   // refetch → recorded state (prevents accidental re-submit)
  if (fresh.ok && fresh.data.view) renderPortal(fresh.data.view);
  alertBox($("deposit-alert"), "success", "Thank you — we've received your payment details. We'll initiate the transfer and mark your deposit Received once it clears.");
});

// Live ✓/✗ verifier on the ACH routing/account + confirm fields (like an OTP/password
// check). No character cap on the inputs — the indicator + a gated submit button
// signal correctness instead of truncating what you type.
(function achValidator() {
  const F = (id) => document.getElementById(id);
  if (!F("ach-routing")) return;
  const digitsOf = (id) => (F(id).value || "").replace(/\D/g, "");
  const setInd = (id, ok, msg) => {
    const el = F(id); if (!el) return;
    el.textContent = msg || "";
    el.className = "ach-ind" + (msg ? (ok ? " ok" : " bad") : "");
  };
  function refresh() {
    const r = digitsOf("ach-routing"), rc = digitsOf("ach-routing-confirm");
    const a = digitsOf("ach-account"), ac = digitsOf("ach-account-confirm");
    const rOk = /^\d{9}$/.test(r), aOk = /^\d{4,}$/.test(a);
    const rcOk = rc.length > 0 && rc === r, acOk = ac.length > 0 && ac === a;
    setInd("ind-routing", rOk, !r ? "" : (rOk ? "✓ Valid 9-digit routing number" : "✗ Routing number should be 9 digits"));
    setInd("ind-routing-confirm", rcOk, !rc ? "" : (rcOk ? "✓ Matches" : "✗ Doesn't match"));
    setInd("ind-account", aOk, !a ? "" : (aOk ? "✓ Looks good" : "✗ Enter at least 4 digits"));
    setInd("ind-account-confirm", acOk, !ac ? "" : (acOk ? "✓ Matches" : "✗ Doesn't match"));
    const name = (F("ach-acct-name").value || "").trim();
    const btn = F("ach-btn");
    if (btn) btn.disabled = !(name && rOk && rcOk && aOk && acOk);
  }
  ["ach-acct-name", "ach-routing", "ach-routing-confirm", "ach-account", "ach-account-confirm"]
    .forEach((id) => { const el = F(id); if (el) el.addEventListener("input", refresh); });
  refresh();
})();

$("check-form").addEventListener("submit", async (e) => {
  e.preventDefault(); clearAlert($("deposit-alert"));
  const btn = $("check-btn"); btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Submitting…';
  const res = await api("POST", "/deposit", { method: "check", note: $("check-note").value.trim() });
  btn.disabled = false; btn.textContent = "I've mailed the check";
  if (handleExpired(res, $("deposit-alert"))) return;
  const { ok, data } = res;
  if (!ok) { alertBox($("deposit-alert"), "error", data.error || "Could not submit."); return; }
  const fresh = await api("GET", "");   // refetch → recorded state (prevents accidental re-submit)
  if (fresh.ok && fresh.data.view) renderPortal(fresh.data.view);
  alertBox($("deposit-alert"), "success", "Thanks for letting us know — we'll mark your deposit Received once the check arrives.");
});
