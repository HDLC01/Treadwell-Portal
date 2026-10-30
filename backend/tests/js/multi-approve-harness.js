"use strict";
/* Run the REAL "approve several proposals at once" dialog out of app.js and report what a customer
 * sees and what the page sends.
 *
 * EXECUTED, NOT GREPPED, for the reason approve-signing-harness.js gives: the claims are about what
 * the dialog WON'T do -- sign before every selected document was opened, carry its own copy of the
 * consent sentence, keep a tick across a changed sentence, or merge several deposits into one
 * payment. Only feeding the real functions inputs and reading their outputs can show that.
 *
 * Usage: node multi-approve-harness.js <frontend-dir>   ->  one line of JSON
 */
const fs = require("fs");
const path = require("path");

const ROOT = process.argv[2];
const src = fs.readFileSync(path.join(ROOT, "app.js"), "utf8");
const indexHtml = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");

function fn(name) {
  const m = new RegExp("\\nfunction " + name + "\\s*\\(").exec(src)
         || new RegExp("\\nasync function " + name + "\\s*\\(").exec(src);
  if (!m) throw new Error(name + "() is gone from app.js -- rewrite this harness, don't delete it");
  const i = src.indexOf("{", m.index + m[0].length - 1);
  let depth = 0;
  for (let j = i; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}" && --depth === 0) return src.slice(m.index, j + 1);
  }
  throw new Error("unbalanced braces reading " + name);
}
function grabConst(name) {
  const m = new RegExp("\\nconst " + name + "\\s*=[^;]*;").exec(src);
  if (!m) throw new Error(name + " is gone from app.js -- rewrite this harness");
  return m[0];
}
/** `esc` carries ';' inside its entity strings, which grabConst's [^;]* cannot cross. */
function grabEsc() {
  const m = /\nconst esc = [\s\S]*?\}\[c\]\)\);/.exec(src);
  if (!m) throw new Error("esc is gone from app.js -- rewrite this harness");
  return m[0];
}
function initialClasses(id) {
  const tag = new RegExp('<[a-zA-Z]+[^>]*id="' + id + '"[^>]*>').exec(indexHtml);
  if (!tag) throw new Error("#" + id + " is gone from index.html -- rewrite this harness");
  const cls = /class="([^"]*)"/.exec(tag[0]);
  return cls ? cls[1].split(/\s+/).filter(Boolean) : [];
}

const IDS = ["ma-entry", "ma-modal", "ma-scrim", "ma-list", "ma-summary", "ma-consent-row",
             "ma-consent", "ma-consent-text", "ma-plain-note", "ma-submit", "ma-gate-hint",
             "ma-alert", "ma-form-wrap", "ma-done", "ma-name", "ma-title", "ma-deposits",
             "ap-name"];

function makeNode(id) {
  const cls = new Set(initialClasses(id));
  return {
    id, textContent: "", innerHTML: "", value: "", checked: false, disabled: false, dataset: {},
    focus() {}, querySelectorAll() { return []; },
    classList: {
      add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c),
      toggle: (c, on) => { const w = on === undefined ? !cls.has(c) : !!on; if (w) cls.add(c); else cls.delete(c); return w; },
    },
  };
}

function dialog(opts) {
  const o = opts || {};
  const nodes = {};
  for (const id of IDS) nodes[id] = makeNode(id);
  const $ = (id) => nodes[id] || null;
  const calls = [];
  const replies = o.replies || {};
  const api = (method, p, body) => {
    calls.push({ method, path: p, body: body === undefined ? undefined : JSON.parse(JSON.stringify(body)) });
    const r = replies[method + " " + p];
    const v = typeof r === "function" ? r(body) : r;
    return Promise.resolve(v || { ok: true, status: 200, data: { ok: true } });
  };
  const alerts = [];
  const scope = new Function(
    "STATE_IN", "$", "api", "alertBox", "clearAlert", "handleExpired", "renderPortal", "window",
    "TOKEN", "document", "show", "hide", "fetch", "setTimeout", "clearTimeout",
    `let STATE = STATE_IN;
     ${grabEsc()}
     ${grabConst("money")}
     ${grabConst("isVE")}
     ${grabConst("veLabel")}
     ${grabConst("TERMS_HASH")}
     ${grabConst("MA")}
     ${fn("multiApproveOffer")}
     ${fn("renderMultiApproveEntry")}
     ${fn("isMultiApproveOpen")}
     ${fn("maDefaultLabels")}
     ${fn("openMultiApprove")}
     ${fn("maNewBatchId")}
     ${fn("maRowHtml")}
     ${fn("maItems")}
     ${fn("multiApproveBlocker")}
     ${fn("maUpdateGate")}
     ${fn("maSummaryHtml")}
     ${fn("maApplyPreview")}
     ${fn("maRefreshPreview")}
     ${fn("renderMultiApprove")}
     ${fn("maOnListChange")}
     ${fn("maDoneHtml")}
     ${fn("renderMultiApproveDone")}
     let MA_POLL = null;
     ${fn("maPollContracts")}
     ${fn("maApplyStatus")}
     ${fn("submitMultiApprove")}
     ${fn("maReloadRows")}
     ${fn("renderProjectDeposits")}
     return { MA, offer: multiApproveOffer, entry: renderMultiApproveEntry, open: openMultiApprove,
              rowHtml: maRowHtml, blocker: multiApproveBlocker, gate: maUpdateGate,
              applyPreview: maApplyPreview, refresh: maRefreshPreview, change: maOnListChange,
              submit: submitMultiApprove, doneHtml: maDoneHtml, deposits: renderProjectDeposits,
              done: renderMultiApproveDone, applyStatus: maApplyStatus, poll: maPollContracts,
              setRows: (rows) => { MA.rows = rows; MA.available = true; } };`);
  const handle = scope(
    o.state || { customer_name: "Dana Reed", scope: { siblings: [{}, {}] } }, $, api,
    (el, kind, msg) => alerts.push({ id: el && el.id, kind, msg }),
    (el) => { if (el) el.textContent = ""; },
    () => false, () => {},
    { crypto: { randomUUID: () => "11111111-2222-3333-4444-555555555555" } },
    o.token || "tok-a", { body: { style: {} } },
    (x) => x && x.classList.remove("hidden"), (x) => x && x.classList.add("hidden"),
    () => Promise.reject(new Error("no network")),
    o.setTimeout || setTimeout, o.clearTimeout || clearTimeout);
  return { handle, nodes, calls, alerts };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

const ROW = (tok, label, over) => Object.assign({
  token: tok, label, display_name: "Acme HQ — " + label, revision_no: 2, status: "viewed",
  options: [{ label, total: 12000, is_base: true }, { label: "Sealer", total: 1000, is_base: false }],
  addons: [], signing: { required: true, blocked_reason: null }, has_pdf: true,
  document_required: true, document_opened: false, deposit_required: true, approvable: true,
  reasons: [{ code: "not_opened", message: `Open ${label}'s proposal before signing.` }],
}, over || {});

const SENTENCE_1 = "I have reviewed each of the following 2 proposals ... Lab 1 ($12,000.00); Lab 2 ($8,000.00).";
const SENTENCE_2 = "I have reviewed each of the following 2 proposals ... Lab 1 ($13,000.00); Lab 2 ($8,000.00).";
const PREVIEW = (text, reasons) => ({
  ok: true, status: 200, data: {
    ok: true, consent_text: text, consent_digest: text ? "digest-of:" + text.slice(-30) : null,
    proposals: [
      { token: "tok-a", label: "Lab 1", chosen: ["Lab 1"], total: 12000, deposit_required: true,
        deposit_amount: 3000, signing: { required: true }, reasons: reasons || [] },
      { token: "tok-b", label: "Lab 2", chosen: ["Lab 2"], total: 8000, deposit_required: false,
        deposit_amount: null, signing: { required: true }, reasons: [] },
    ],
  },
});

(async () => {
  const out = {};
  const read = (d) => ({
    disabled: d.nodes["ma-submit"].disabled, button: d.nodes["ma-submit"].textContent,
    hint: d.nodes["ma-gate-hint"].textContent,
    hintHidden: d.nodes["ma-gate-hint"].classList.contains("hidden"),
    consent: d.nodes["ma-consent-text"].textContent,
    consentHidden: d.nodes["ma-consent-row"].classList.contains("hidden"),
    ticked: d.nodes["ma-consent"].checked,
  });

  // -- when the dialog is offered ---------------------------------------------
  {
    const d = dialog();
    d.handle.setRows([ROW("tok-a", "Lab 1"), ROW("tok-b", "Lab 2")]);
    d.handle.entry();
    const two = !d.nodes["ma-entry"].classList.contains("hidden");
    d.handle.setRows([ROW("tok-a", "Lab 1"),
                      ROW("tok-b", "Lab 2", { approvable: false, status: "approved",
                                              reasons: [{ code: "already_approved", message: "Lab 2 is already approved." }] })]);
    d.handle.entry();
    const one = !d.nodes["ma-entry"].classList.contains("hidden");
    out.offer = { twoApprovable: two, oneApprovable: one,
                  shipsHidden: initialClasses("ma-entry").includes("hidden") };
  }

  // -- the rows ------------------------------------------------------------------
  {
    const d = dialog();
    const closed = ROW("tok-c", "Breakroom", { approvable: false,
      reasons: [{ code: "closed", message: "Breakroom was closed. To go ahead with it, open its tab and approve it there on its own." }] });
    const gyp = ROW("tok-g", "Gyp <b>Floor</b>", { signing: { required: false }, document_required: false, reasons: [] });
    d.handle.setRows([ROW("tok-a", "Lab 1"), closed, gyp]);
    d.handle.MA.picked.add("tok-a");
    d.handle.MA.labels["tok-a"] = new Set(["Lab 1"]);
    out.rows = {
      picked: d.handle.rowHtml(d.handle.MA.rows[0]),
      closed: d.handle.rowHtml(closed),
      gyp: d.handle.rowHtml(gyp),
    };
  }

  // -- the gate, step by step ---------------------------------------------------
  {
    let previewText = SENTENCE_1;
    let previewReasons = [];
    const d = dialog({ replies: {
      "POST /approve-several/preview": () => PREVIEW(previewText, previewReasons),
      "POST /approve-several": { ok: true, status: 200, data: { ok: true, approved: [] } },
    } });
    d.handle.setRows([ROW("tok-a", "Lab 1"), ROW("tok-b", "Lab 2"), ROW("tok-c", "Lab 3")]);
    d.handle.open();
    await flush();
    const steps = { none: read(d) };
    d.handle.change({ target: { dataset: { pick: "tok-a" }, checked: true } });
    await flush();
    steps.one = read(d);
    d.handle.change({ target: { dataset: { pick: "tok-b" }, checked: true } });
    await flush();
    steps.unopened = read(d);
    d.handle.MA.rows[0].document_opened = true;
    d.handle.MA.rows[1].document_opened = true;
    previewReasons = [{ code: "revised", message: "Lab 1 was revised since you opened it. Please open the new version before signing." }];
    await d.handle.refresh();
    steps.serverSaysNo = read(d);
    previewReasons = [];
    await d.handle.refresh();
    steps.untickedConsent = read(d);
    d.nodes["ma-consent"].checked = true;
    d.handle.gate();
    steps.ready = read(d);
    // An option on one proposal changes its price, so the server's sentence changes: the tick
    // given to the old sentence must not carry over.
    previewText = SENTENCE_2;
    d.handle.change({ target: { dataset: { opt: "tok-a", label: "Sealer" }, checked: true } });
    await flush();
    steps.sentenceChanged = read(d);
    // No option at all on one of them.
    d.handle.change({ target: { dataset: { opt: "tok-a", label: "Lab 1" }, checked: false } });
    d.handle.change({ target: { dataset: { opt: "tok-a", label: "Sealer" }, checked: false } });
    await flush();
    steps.noOption = read(d);
    d.handle.change({ target: { dataset: { opt: "tok-a", label: "Lab 1" }, checked: true } });
    await flush();
    d.nodes["ma-consent"].checked = true;
    d.handle.gate();
    d.nodes["ma-name"].value = "Dana Reed";
    d.nodes["ma-title"].value = "Owner";
    await d.handle.submit();
    await flush();
    out.gate = steps;
    out.sent = d.calls.filter((c) => c.path === "/approve-several").map((c) => c.body);
    out.previewCalls = d.calls.filter((c) => c.path === "/approve-several/preview").map((c) => c.body);
    out.summary = d.nodes["ma-summary"].innerHTML;
  }

  // -- nothing to sign: no consent sent ------------------------------------------
  {
    const d = dialog({ replies: {
      "POST /approve-several/preview": PREVIEW(null),
      "POST /approve-several": { ok: true, status: 200, data: { ok: true, approved: [] } },
    } });
    const g = (t, l) => ROW(t, l, { signing: { required: false }, document_required: false, reasons: [] });
    d.handle.setRows([g("tok-a", "Gyp 1"), g("tok-b", "Gyp 2")]);
    d.handle.open();
    d.handle.change({ target: { dataset: { pick: "tok-a" }, checked: true } });
    d.handle.change({ target: { dataset: { pick: "tok-b" }, checked: true } });
    await flush();
    const before = read(d);
    d.nodes["ma-name"].value = "Dana Reed";
    await d.handle.submit();
    await flush();
    out.unsigned = { before, sent: d.calls.filter((c) => c.path === "/approve-several").map((c) => c.body) };
  }

  // -- a refusal is shown and nothing claims success -----------------------------
  {
    const d = dialog({ replies: {
      "POST /approve-several/preview": PREVIEW(SENTENCE_1),
      "POST /approve-several": { ok: false, status: 409, data: { ok: false, error: "batch_refused",
        message: "Nothing was approved. Please fix the items below and try again.",
        reasons: [{ token: "tok-b", label: "Lab 2", code: "already_approved", message: "Lab 2 is already approved." }] } },
      "GET /approve-several": { ok: true, status: 200, data: { ok: true, available: true, proposals: [
        ROW("tok-a", "Lab 1", { document_opened: true, reasons: [] }),
        ROW("tok-b", "Lab 2", { approvable: false, status: "approved", approved: { total: 8000 },
                                reasons: [{ code: "already_approved", message: "Lab 2 is already approved." }] })] } },
    } });
    d.handle.setRows([ROW("tok-a", "Lab 1", { document_opened: true, reasons: [] }),
                      ROW("tok-b", "Lab 2", { document_opened: true, reasons: [] })]);
    d.handle.open();
    d.handle.change({ target: { dataset: { pick: "tok-a" }, checked: true } });
    d.handle.change({ target: { dataset: { pick: "tok-b" }, checked: true } });
    await flush();
    d.nodes["ma-consent"].checked = true;
    d.nodes["ma-name"].value = "Dana Reed";
    await d.handle.submit();
    await flush();
    out.refused = { alerts: d.alerts, doneShown: !d.nodes["ma-done"].classList.contains("hidden"),
                    stillPicked: [...d.handle.MA.picked], after: read(d) };
  }

  // -- after: several invoices, each paid on its own ------------------------------
  {
    const d = dialog();
    out.done = d.handle.doneHtml([
      { token: "tok-a", label: "Lab 1", total: 12000, options: ["Lab 1"], deposit_required: true,
        deposit_amount: 3000, invoice_no: "INV-A", signed: true, contract_ready: true },
      { token: "tok-b", label: "Lab 2", total: 8000, options: ["Lab 2"], deposit_required: true,
        deposit_amount: 2000, invoice_no: null, signed: true, contract_ready: false },
      { token: "tok-c", label: "Gyp", total: 5000, options: ["Gyp"], deposit_required: false,
        deposit_amount: null, invoice_no: null, signed: false, contract_ready: false },
    ]);
    d.handle.setRows([
      ROW("tok-a", "Lab 1", { approvable: false, status: "approved", approved: { invoice_no: "INV-A", deposit_amount: 3000, deposit_status: "pending" } }),
      ROW("tok-b", "Lab 2", { approvable: false, status: "approved", approved: { invoice_no: "INV-B", deposit_amount: 2000, deposit_status: "received" } }),
      ROW("tok-c", "Lab 3", { approvable: false, status: "approved", deposit_required: false, approved: { invoice_no: null } }),
    ]);
    d.handle.deposits();
    out.deposits = { html: d.nodes["ma-deposits"].innerHTML,
                     hidden: d.nodes["ma-deposits"].classList.contains("hidden") };
  }

  // -- after, while the contracts are still being built in the background -----------
  {
    const timers = [];
    let ready = false;
    const d = dialog({
      setTimeout: (cb, ms) => { timers.push({ cb, ms }); return timers.length; },
      clearTimeout: () => {},
      replies: {
        "GET /approve-several/status?batch_id=batch-0000-0042": () => ({ ok: true, status: 200, data: {
          ok: true, paperwork: ready ? "done" : "pending", approved: [
            { token: "tok-a", signed: true, contract_ready: ready, invoice_no: "INV-A" },
            { token: "tok-b", signed: true, contract_ready: ready, invoice_no: "INV-B" }] } }),
      },
    });
    d.handle.setRows([ROW("tok-a", "Lab 1"), ROW("tok-b", "Lab 2")]);
    d.handle.open();
    d.handle.MA.batchId = "batch-0000-0042";
    d.handle.done([
      { token: "tok-a", label: "Lab 1", total: 12000, options: ["Lab 1"], deposit_required: true,
        deposit_amount: 3000, invoice_no: "INV-A", signed: true, contract_ready: false },
      { token: "tok-b", label: "Lab 2", total: 8000, options: ["Lab 2"], deposit_required: true,
        deposit_amount: 2000, invoice_no: null, signed: true, contract_ready: false },
    ]);
    const first = d.nodes["ma-done"].innerHTML;
    const run = async () => { const t = timers.shift(); await t.cb(); await flush(); return t.ms; };
    const firstDelay = await run();                 // asked once: still being prepared
    const stillPending = d.nodes["ma-done"].innerHTML;
    ready = true;
    await run();                                    // asked again: both stored
    out.background = {
      first, stillPending, after: d.nodes["ma-done"].innerHTML, firstDelay,
      asked: d.calls.filter((c) => c.path.startsWith("/approve-several/status")).length,
      pollingAfterDone: timers.length,
    };
  }

  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
