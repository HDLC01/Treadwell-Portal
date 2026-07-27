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

  // Deposit stays reachable at every stage so a customer can open the step from
  // the tracker and skip around. setupDeposit's "recorded" banner already covers
  // the post-submission state, and the server has no approval precondition on
  // POST /deposit — only a valid session.
  show($("deposit-card"));
  renderSchedule(vm.status);

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
  mountSwitcher();        // once per session; no-op on poll re-renders
  startPolling();
}

// ── "My projects" switcher ────────────────────────────────────────────────────
// A customer can hold several proposals. Sessions are email-scoped, so every
// project on this email is already authorized — switching needs no new login.
// Fetched once and only revealed when there are 2+ projects, so a single-project
// customer sees no clutter.
let SWITCHER_MOUNTED = false;

async function mountSwitcher() {
  if (SWITCHER_MOUNTED || !window.TWProjects) return;
  SWITCHER_MOUNTED = true;
  const wrap = $("proj-switcher");
  const btn = $("proj-switch-btn");
  const menu = $("proj-switch-menu");
  if (!wrap || !btn || !menu) return;

  const proposals = await TWProjects.load();
  if (proposals.length < 2) return;   // nothing to switch between
  menu.innerHTML = TWProjects.rowsHtml(proposals, TOKEN);
  wrap.classList.remove("hidden");

  const close = () => { menu.classList.add("hidden"); btn.setAttribute("aria-expanded", "false"); };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = menu.classList.toggle("hidden") === false;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });
  document.addEventListener("click", (e) => { if (!wrap.contains(e.target)) close(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
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
    { key: "proposal", label: "Proposal", done: st.proposal === "approved", val: st.proposal === "approved" ? "Approved" : "Pending" },
    { key: "deposit", label: "Deposit", done: st.deposit === "received", val: st.deposit === "received" ? "Received" : "Pending" },
    { key: "contacts", label: "Contact info", done: st.contacts === "received", val: st.contacts === "received" ? "Received" : "Pending" },
    { key: "schedule", label: "Schedule", done: st.schedule === "scheduled", val: st.schedule === "scheduled" ? "Scheduled" : "Pending" },
  ];
  // Buttons, not divs: each tile navigates to that step. Customers can move back
  // and forth and skip ahead — nothing here gates on the previous step.
  $("tracker").innerHTML = steps.map((s) => `
    <button type="button" class="step ${s.done ? "is-done" : ""}${ACTIVE_STEP === s.key ? " is-active" : ""}"
            data-step="${s.key}"${ACTIVE_STEP === s.key ? ' aria-current="step"' : ""}>
      <span class="lbl">${s.label}</span>
      <span class="val" style="color:${s.done ? "var(--success)" : "var(--secondary)"}">${s.done ? ICON_CHECK : ICON_DOT}${s.val}</span>
    </button>`).join("");
}

// Which step the customer last opened. Module-level because renderPortal (and so
// renderTracker) re-runs on every status poll, which would otherwise wipe it.
let ACTIVE_STEP = null;

// Exposed so the sidebar (shell.js) drives the SAME navigation as the tiles.
window.focusStep = (k) => focusStep(k);

const STEP_TARGETS = {
  proposal: ["pdf-card", "options-card", "approve-card"],
  deposit: ["deposit-card"],
  contacts: ["contacts-card"],
  schedule: ["schedule-card"],
};

/** Open a step: switch to the proposal view, then scroll its card into sight.
 *  The rAF matters — hashchange is async, so #proposal-view is still display:none
 *  at call time and scrollIntoView would silently do nothing. */
function focusStep(step) {
  ACTIVE_STEP = step;
  if (location.hash !== "#proposal") location.hash = "proposal"; else applyHashView(false);
  requestAnimationFrame(() => {
    const el = (STEP_TARGETS[step] || []).map((id) => $(id))
      .find((n) => n && !n.classList.contains("hidden"));
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
      el.classList.add("step-flash");
      setTimeout(() => el.classList.remove("step-flash"), 1400);
    }
    const t = $("tracker");
    if (t) t.querySelectorAll(".step").forEach((b) =>
      b.classList.toggle("is-active", b.dataset.step === step));
  });
}

// Selected pricing option labels (multi-select). Persists across re-renders.
let SELECTED = new Set();
let CUR_OPTIONS = [];
const DEPOSIT_PCT = 0.25;   // mirrors backend proposals.DEPOSIT_PCT — live deposit preview

/** A value-engineering row: an alternative priced AGAINST the base bid, not an
 *  extra job. Its `total` is what the job costs if you take it, so it must never
 *  be summed as a lump sum. `delta` is the signed difference vs base. */
const isVE = (o) => o && o.price_mode === "deduct" && !o.is_base && o.delta != null;
const veLabel = (o) => (o.delta < 0 ? `Deduct (${money(Math.abs(o.delta))})` : `Add ${money(o.delta)}`);

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
  // Pricing exists, so the approve card belongs on screen. It had no show()
  // anywhere: once hidden (empty pricing, or an approval) it stayed hidden for
  // the rest of the session even after pricing landed on a later poll.
  if (!approved) show($("approve-card"));
  // Default selection (pre-approval only): the base option, else the first.
  if (!approved && !SELECTED.size) {
    const base = options.find((o) => o.is_base) || options[0];
    SELECTED = new Set([base.label]);
  }
  $("options-help").textContent = approved
    ? "" : (options.length > 1 ? "Select every option you'd like to approve — your total updates below." : "");
  wrap.innerHTML = options.map((o) => {
    const on = SELECTED.has(o.label);
    const ve = isVE(o);
    // A value-engineering row prices the job differently rather than adding a
    // second job — show it the way the document does ("Add $X" / "Deduct ($X)"),
    // never as a standalone lump sum.
    const price = ve ? veLabel(o) : money(o.total);
    const sub = ve
      ? `instead of the base bid — ${money(o.total)} total`
      : (o.diff != null && o.diff !== 0 ? `${o.diff > 0 ? "+" : ""}${money(o.diff)} vs base bid` : "");
    return `<label class="option opt-check ${on ? "selected" : ""}">
      <input type="checkbox" ${on ? "checked" : ""} ${approved ? "disabled" : ""} data-label="${esc(o.label)}">
      <span class="opt-main">
        <span class="top"><span class="name">${esc(o.label)}</span><span class="price">${esc(price)}</span></span>
        ${o.system_desc ? `<span class="meta">${esc(o.system_desc)}</span>` : ""}
        ${sub ? `<span class="meta">${esc(sub)}</span>` : ""}
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
  // Must mirror proposals.resolve_selection on the server exactly: lump-sum rows
  // contribute their total, VE rows contribute their delta against the base, and
  // a VE row selected alone still implies the base bid.
  const picked = CUR_OPTIONS.filter((o) => SELECTED.has(o.label));
  const ve = picked.filter(isVE);
  let total = picked.filter((o) => !isVE(o)).reduce((s, o) => s + o.total, 0);
  if (ve.length) {
    if (!picked.some((o) => o.is_base)) {
      const base = CUR_OPTIONS.find((o) => o.is_base);
      if (base) total += base.total;
    }
    total += ve.reduce((s, o) => s + (o.delta || 0), 0);
  }
  total = Math.round(total * 100) / 100;
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
  // Once the invoice has been issued, offer it right here — the customer lands on
  // this view after approving, so they shouldn't have to find the chat card.
  const inv = STATE && STATE.deposit && STATE.deposit.invoice_no;
  const lead = $("thankyou-lead");
  const actions = $("thankyou-actions");
  if (inv) {
    lead.textContent = `Invoice ${inv} has been emailed to you and is attached below.`;
    $("thankyou-invoice").href = `/api/portal/${TOKEN}/deposit-invoice.pdf`;
    actions.classList.remove("hidden");
  } else {
    lead.textContent = "Your deposit invoice is on its way — expect it within about 24 hours.";
    actions.classList.add("hidden");
  }
  show($("thankyou-card"));
}

// ── project contacts (visible after approval; emphasized once deposit received) ─
let CONTACT_ROWS = [];

function renderContacts(vm) {
  const card = $("contacts-card");
  // Always open, per Hanz — the customer can add contacts whenever they like
  // instead of waiting for approval.
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
  // "Same as primary" — copy the primary's details in and lock the row. A full
  // redraw is fine here (explicit click, no typing in flight).
  list.querySelectorAll("[data-same]").forEach((cb) =>
    cb.addEventListener("change", () => {
      const row = CONTACT_ROWS[+cb.dataset.same];
      row.same_as_primary = cb.checked;
      if (cb.checked) mirrorPrimary(row);
      drawContacts();
    }));
  list.querySelectorAll("[data-field]").forEach((el) => {
    const upd = () => {
      const i = +el.dataset.i;
      CONTACT_ROWS[i][el.dataset.field] = el.value;
      // Editing the primary keeps every mirrored row in step — patch their inputs
      // directly rather than redrawing, so the field being typed in keeps focus.
      if (i === 0) {
        CONTACT_ROWS.forEach((r, j) => {
          if (!j || !r.same_as_primary) return;
          mirrorPrimary(r);
          const inp = list.querySelector(`[data-field="${el.dataset.field}"][data-i="${j}"]`);
          if (inp) inp.value = el.value;
        });
      }
    };
    el.addEventListener("input", upd); el.addEventListener("change", upd);
  });
}

/** The Schedule step's destination. Treadwell books the date, so this explains
 *  where the customer stands rather than offering a self-service picker. */
function renderSchedule(st) {
  const el = $("schedule-help");
  if (!el) return;
  el.textContent = st.schedule === "scheduled"
    ? "Your project is scheduled — your Treadwell contact will confirm the details with you."
    : st.deposit === "received"
      ? "We've received your deposit. We'll be in touch shortly to book your dates."
      : "We book your dates once the deposit is received. Your Treadwell contact will confirm them with you.";
}

/** Copy the primary contact's details onto a mirrored row (role is preserved). */
function mirrorPrimary(row) {
  const p = CONTACT_ROWS[0] || {};
  row.name = p.name || "";
  row.email = p.email || "";
  row.phone = p.phone || "";
}

function contactRow(c, i) {
  const isPrimary = i === 0;
  // "accounts_payable" is the billing contact — the role vocabulary is fixed by a
  // CHECK constraint on portal_contacts, so this is a label, not a new role.
  const same = !isPrimary && !!c.same_as_primary;
  const head = isPrimary
    ? '<span class="contact-role">Primary contact</span>'
    : `<select data-field="role" data-i="${i}" class="contact-role-sel">
         <option value="accounts_payable" ${c.role !== "other" ? "selected" : ""}>Billing contact</option>
         <option value="other" ${c.role === "other" ? "selected" : ""}>Other</option>
       </select>
       <label class="contact-same"><input type="checkbox" data-same="${i}" ${same ? "checked" : ""}>
         Same as primary contact</label>
       <button class="linkbtn contact-remove" type="button" data-remove="${i}">Remove</button>`;
  const ro = same ? "disabled" : "";
  return `<div class="contact-row${same ? " is-mirrored" : ""}">
    <div class="contact-row-head">${head}</div>
    <div class="contact-grid">
      <input data-field="name" data-i="${i}" type="text" placeholder="Name *" value="${esc(c.name || "")}" ${ro}>
      <input data-field="email" data-i="${i}" type="email" placeholder="Email" value="${esc(c.email || "")}" ${ro}>
      <input data-field="phone" data-i="${i}" type="tel" placeholder="Phone" value="${esc(c.phone || "")}" ${ro}>
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
  t.querySelectorAll("[data-pay-deposit]").forEach((el) => el.addEventListener("click", openDeposit));
  if (atBottom) t.scrollTop = t.scrollHeight;   // keep pinned to newest unless the user scrolled up
}

/** System lines read "Heading — detail". Split them so they render as a card
 *  (matching the deposit card) instead of a cramped grey pill. The length guard
 *  stops a long sentence that happens to contain a dash becoming a giant title. */
function splitSystem(body) {
  const s = String(body == null ? "" : body);
  const i = s.indexOf(" — ");
  if (i > 0 && i <= 60) return { title: s.slice(0, i), body: s.slice(i + 3) };
  return { title: "Update", body: s };
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
    const meta = m.meta || {};
    const amt = meta.amount != null ? money(meta.amount) : "";
    // Only the CURRENT invoice number is stored, so a superseded card's document
    // can't be re-rendered — label it and drop the download rather than linking
    // to a PDF that would show a different number.
    const dead = !!meta.superseded;
    const no = meta.invoice_no ? `<div class="cc-meta">Invoice ${esc(meta.invoice_no)}${
      meta.reference ? ` · Reference ${esc(meta.reference)}` : ""}${
      dead && meta.superseded_by ? ` · replaced by ${esc(meta.superseded_by)}` : ""}</div>` : "";
    if (dead) {
      return `<div class="chat-card deposit is-superseded">
        <div class="cc-title">Deposit invoice${amt ? ` — ${amt}` : ""} <span class="cc-tag">Superseded</span></div>
        ${no}
        <div class="cc-body">${esc(m.body || "")}</div>
      </div>`;
    }
    // The invoice PDF is served per-proposal, so the link works for any recipient
    // with a valid session (same gate as the proposal PDF).
    const dl = meta.invoice_no
      ? `<a class="btn btn-secondary" href="/api/portal/${TOKEN}/deposit-invoice.pdf">Download invoice (PDF)</a>`
      : "";
    return `<div class="chat-card deposit">
      <div class="cc-title">Deposit invoice${amt ? ` — <span class="cc-amt">${amt}</span>` : ""}</div>
      ${no}
      <div class="cc-body">${esc(m.body || "")}</div>
      <div class="cc-actions">${dl}
        <button class="btn btn-primary" type="button" data-pay-deposit>Pay deposit</button>
      </div>
    </div>`;
  }
  if (m.msg_type === "system") {
    const s = splitSystem(m.body);
    return `<div class="chat-card system">
      <div class="cc-title">${esc(s.title)}</div>
      <div class="cc-body">${esc(s.body)}</div>
    </div>`;
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

/** Jump from the chat's invoice card straight to the deposit form. */
function openDeposit() {
  location.hash = "proposal";
  requestAnimationFrame(() => {
    const card = $("deposit-card");
    if (card && !card.classList.contains("hidden")) card.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

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
  // Clean, full-width page teaser: hide the viewer toolbar, side panes AND the
  // scrollbars — the frame is pointer-events:none so clicks reach the enlarge
  // button, which meant the viewer's own scrollbars rendered but could never be
  // used. Reading happens in the full-size popup.
  ifr.src = `/api/portal/${TOKEN}/pdf#toolbar=0&navpanes=0&scrollbar=0&view=FitH`;
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
$("thankyou-pay").addEventListener("click", openDeposit);
// Delegated on the static container: renderTracker replaces its innerHTML on
// every poll, so per-tile listeners would not survive.
$("tracker").addEventListener("click", (e) => {
  const b = e.target.closest(".step");
  if (b && b.dataset.step) focusStep(b.dataset.step);
});

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
  // Billing is the common second contact, so it's the default (switchable to Other).
  CONTACT_ROWS.push({ role: "accounts_payable", name: "", email: "", phone: "" });
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
  const account_type = $("ach-account-type").value;
  if (!account_type) return fail("Please choose an account type (checking or savings).", "ach-account-type");
  const btn = $("ach-btn"); btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Submitting…';
  const res = await api("POST", "/deposit", {
    method: "ach", account_name, routing_number: routing, account_number: account,
    account_type, note: $("ach-note").value.trim(),
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
  // Green/red border on the input itself (only once there's something typed).
  const setField = (id, ok, active) => {
    const el = F(id); if (!el) return;
    el.classList.toggle("ach-ok", !!active && ok);
    el.classList.toggle("ach-bad", !!active && !ok);
  };
  function refresh() {
    const r = digitsOf("ach-routing"), rc = digitsOf("ach-routing-confirm");
    const a = digitsOf("ach-account"), ac = digitsOf("ach-account-confirm");
    const rOk = /^\d{9}$/.test(r), aOk = /^\d{4,}$/.test(a);
    const rcOk = rc.length > 0 && rc === r, acOk = ac.length > 0 && ac === a;
    setInd("ind-routing", rOk, !r ? "" : (rOk ? "✓ Looks good" : "✗ Must be 9 digits"));
    setInd("ind-routing-confirm", rcOk, !rc ? "" : (rcOk ? "✓ Matches" : "✗ Doesn't match"));
    setInd("ind-account", aOk, !a ? "" : (aOk ? "✓ Looks good" : "✗ At least 4 digits"));
    setInd("ind-account-confirm", acOk, !ac ? "" : (acOk ? "✓ Matches" : "✗ Doesn't match"));
    setField("ach-routing", rOk, !!r);
    setField("ach-routing-confirm", rcOk, !!rc);
    setField("ach-account", aOk, !!a);
    setField("ach-account-confirm", acOk, !!ac);
    const name = (F("ach-acct-name").value || "").trim();
    const type = F("ach-account-type") ? F("ach-account-type").value : "";
    const btn = F("ach-btn");
    if (btn) btn.disabled = !(name && rOk && rcOk && aOk && acOk && type);
  }
  ["ach-acct-name", "ach-routing", "ach-routing-confirm", "ach-account", "ach-account-confirm"]
    .forEach((id) => { const el = F(id); if (el) el.addEventListener("input", refresh); });
  const typeSel = F("ach-account-type");
  if (typeSel) typeSel.addEventListener("change", refresh);
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
