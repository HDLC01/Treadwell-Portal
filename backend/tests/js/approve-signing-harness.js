"use strict";
/* Run the REAL signing half of the approve card out of app.js and report what a customer sees.
 *
 * EXECUTED, NOT GREPPED, for the same reason view-signal-harness.js is: the claims here are
 * about what the page WON'T do. It won't let anybody sign a document they never opened, it
 * won't carry its own copy of the consent sentence, and it won't send a consent flag for a
 * proposal that has no Terms and Conditions to consent to. A source assertion cannot prove an
 * absence, and the one that matters most -- "the sentence on screen is the one the server
 * sent" -- is only provable by feeding two different sentences in and reading two different
 * labels out.
 *
 * The backend half (api_approve refusing `consent` that is not the literal boolean, and
 * signing.signing_block deciding required/blocked) is tested in test_signed_contract.py.
 *
 * Usage: node approve-signing-harness.js <frontend-dir>   ->  one line of JSON
 */
const fs = require("fs");
const path = require("path");

const ROOT = process.argv[2];
const src = fs.readFileSync(path.join(ROOT, "app.js"), "utf8");
const indexHtml = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");

/** Lift a top-level `function name(...) {...}` by brace counting. */
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

/** Lift a `let NAME = ...;` DECLARATION from the real source rather than re-declaring it here.
 *  PDF_MOUNTED is the whole PDF gate; a copy in this file would keep passing after app.js
 *  started latching something else. */
function grabDecl(name) {
  const m = new RegExp("^let " + name + ".*$", "m").exec(src);
  if (!m) throw new Error("the " + name + " declaration is gone -- rewrite this harness");
  return m[0];
}

/** The class attribute this element REALLY ships with in index.html.
 *
 *  Read from the markup instead of assumed, because "starts hidden" is half of each claim: a
 *  consent row that ships visible would show an empty box on a Budget Pricing proposal, and this
 *  harness would never notice if it seeded its own classes. */
function initialClasses(id) {
  const tag = new RegExp('<[a-zA-Z]+[^>]*id="' + id + '"[^>]*>').exec(indexHtml);
  if (!tag) throw new Error("#" + id + " is gone from index.html -- rewrite this harness");
  const cls = /class="([^"]*)"/.exec(tag[0]);
  return cls ? cls[1].split(/\s+/).filter(Boolean) : [];
}

const IDS = ["approve-btn", "ap-gate-hint", "consent-row", "ap-consent", "consent-text",
             "signing-blocked", "approve-plain-note", "sig-ink", "sig-hint", "ap-name",
             "ap-title", "approve-alert"];

function makeNode(id) {
  const cls = new Set(initialClasses(id));
  const node = {
    id, textContent: "", innerHTML: "", value: "", checked: false, disabled: false,
    dataset: {}, focus() {},
    classList: {
      add: (c) => cls.add(c),
      remove: (c) => cls.delete(c),
      contains: (c) => cls.has(c),
      toggle: (c, on) => {
        const want = on === undefined ? !cls.has(c) : !!on;
        if (want) cls.add(c); else cls.delete(c);
        return want;
      },
    },
    _classes: () => Array.from(cls),
  };
  return node;
}

/** One approve card, wired to a given view model. */
function card(opts) {
  const o = opts || {};
  const nodes = {};
  for (const id of IDS) nodes[id] = makeNode(id);
  const $ = (id) => nodes[id] || null;
  const calls = [];
  const api = (method, p, body) => {
    calls.push({ method, path: p, body });
    return Promise.resolve({ ok: true, status: 200, data: { ok: true, view: {} } });
  };

  const scope = new Function(
    "STATE_IN", "SELECTED_IN", "$", "api", "alertBox", "clearAlert", "handleExpired",
    "renderPortal", "window",
    `let STATE = STATE_IN;
     let SELECTED = SELECTED_IN;
     ${grabDecl("PDF_MOUNTED")}
     ${grabDecl("PDF_OPENED_IN_TAB")}
     ${fn("proposalWasOpened")}
     ${fn("signingView")}
     ${fn("approveBlocker")}
     ${fn("updateApproveGate")}
     ${fn("renderSignaturePreview")}
     ${fn("renderSigning")}
     ${fn("submitApproval")}
     return {
       render: renderSigning,
       gate: updateApproveGate,
       submit: submitApproval,
       preview: renderSignaturePreview,
       // The two ways a customer opens the document. mountPdf() and the new-tab links set these
       // in the page; the flags themselves are lifted from app.js above.
       openFullViewer: () => { PDF_MOUNTED = true; updateApproveGate(); },
       openInNewTab: () => { PDF_OPENED_IN_TAB = true; updateApproveGate(); },
       setState: (s) => { STATE = s; },
     };`);

  const alerts = [];
  const handle = scope(
    o.state, new Set(o.selected === undefined ? ["Base Bid"] : o.selected), $, api,
    (el, kind, msg) => alerts.push({ id: el && el.id, kind, msg }),
    (el) => { if (el) el.textContent = ""; },
    () => false,
    () => {},
    { scrollTo() {} });

  const read = () => ({
    buttonText: nodes["approve-btn"].textContent,
    disabled: nodes["approve-btn"].disabled,
    hint: nodes["ap-gate-hint"].textContent,
    hintHidden: nodes["ap-gate-hint"].classList.contains("hidden"),
    hintIsError: nodes["ap-gate-hint"].classList.contains("is-error"),
    consentLabel: nodes["consent-text"].textContent,
    consentRowHidden: nodes["consent-row"].classList.contains("hidden"),
    consentAgreed: nodes["consent-row"].classList.contains("is-agreed"),
    blockedText: nodes["signing-blocked"].textContent,
    blockedHidden: nodes["signing-blocked"].classList.contains("hidden"),
    plainNoteHidden: nodes["approve-plain-note"].classList.contains("hidden"),
    signature: nodes["sig-ink"].textContent,
    signatureHintHidden: nodes["sig-hint"].classList.contains("hidden"),
  });

  return { handle, nodes, read, calls, alerts };
}

const SIGNABLE = (consent) => ({
  has_pdf: true, revision_no: 2,
  signing: {
    work_type: "epoxy", required: true, blocked_reason: null,
    consent_text: consent, consent_version: "2026-09-v1",
  },
});
const CONSENT_A = "I have reviewed the proposal for Nearman Creek, including its Terms and " +
                  "Conditions, and agree to be bound by them.";
const CONSENT_B = "Different wording entirely, for Ashwood Middle School, agreed under v2.";
const BLOCKED_REASON =
  "This proposal type doesn't carry Terms and Conditions for e-signature, so it can't be " +
  "signed here. Send us a message in this project's thread (or call us) and we'll get you a " +
  "signable contract.";
const BUDGET = {
  has_pdf: true, revision_no: 2,
  signing: { work_type: "budget", required: false, blocked_reason: BLOCKED_REASON,
             consent_text: null, consent_version: null },
};

const flush = () => new Promise((r) => setImmediate(r));

(async () => {
  const out = {};

  // -- the presentation gate --------------------------------------------------
  {
    const c = card({ state: SIGNABLE(CONSENT_A) });
    c.handle.render();
    const closed = c.read();
    c.handle.openFullViewer();
    const opened = c.read();
    c.nodes["ap-consent"].checked = true;
    c.handle.gate();
    out.pdfGate = { closed, opened, ticked: c.read() };
  }

  // The same walk with the new-tab link instead of the popup. Same document, same endpoint.
  {
    const c = card({ state: SIGNABLE(CONSENT_A) });
    c.handle.render();
    c.handle.openInNewTab();
    c.nodes["ap-consent"].checked = true;
    c.handle.gate();
    out.newTabOpensToo = c.read();
  }

  // NO DOCUMENT TO OPEN. mountPdf early-returns without latching when has_pdf is false, so a
  // gate that waited for the latch alone would lock these customers out of approving forever.
  {
    const st = SIGNABLE(CONSENT_A);
    st.has_pdf = false;
    const c = card({ state: st });
    c.handle.render();
    const before = c.read();
    c.nodes["ap-consent"].checked = true;
    c.handle.gate();
    out.noPdfIsNotALockout = { beforeTick: before, afterTick: c.read() };
  }

  // -- the consent sentence is SERVED, not stored here -------------------------
  {
    const a = card({ state: SIGNABLE(CONSENT_A) });
    a.handle.render();
    const b = card({ state: SIGNABLE(CONSENT_B) });
    b.handle.render();
    out.consentIsLive = { a: a.read().consentLabel, b: b.read().consentLabel };
  }

  // An empty sentence is not a signature. Refuse rather than fall back to wording of our own.
  {
    const c = card({ state: SIGNABLE("") });
    c.handle.render();
    c.handle.openFullViewer();
    c.nodes["ap-consent"].checked = true;
    c.handle.gate();
    out.missingConsentTextRefuses = c.read();
  }

  // -- what actually goes on the wire -----------------------------------------
  {
    const c = card({ state: SIGNABLE(CONSENT_A), selected: ["Base Bid", "ROOM 1"] });
    c.handle.render();
    c.handle.openFullViewer();
    c.nodes["ap-name"].value = "  Marguerite Oyelaran  ";
    c.nodes["ap-title"].value = "Director of Facilities";
    c.nodes["ap-consent"].checked = true;
    c.handle.gate();
    await c.handle.submit();
    await flush();
    out.signedPost = { calls: c.calls, alerts: c.alerts, after: c.read() };
  }

  // An untickable box cannot be walked around with the Enter key.
  {
    const c = card({ state: SIGNABLE(CONSENT_A) });
    c.handle.render();
    c.handle.openFullViewer();
    c.nodes["ap-name"].value = "Marguerite Oyelaran";
    await c.handle.submit();
    await flush();
    out.submitWithoutConsent = { calls: c.calls, alerts: c.alerts };
  }

  // -- Budget Pricing: exempt, not blocked ------------------------------------
  {
    const c = card({ state: BUDGET });
    c.handle.render();
    const rendered = c.read();
    c.nodes["ap-name"].value = "Marguerite Oyelaran";
    await c.handle.submit();
    await flush();
    out.budget = { rendered, calls: c.calls, alerts: c.alerts };
  }

  // -- the signature preview ---------------------------------------------------
  {
    const c = card({ state: SIGNABLE(CONSENT_A) });
    c.handle.render();
    const empty = c.read();
    c.nodes["ap-name"].value = "Marguerite Oyelaran";
    c.handle.preview();
    out.preview = { empty, typed: c.read() };
  }

  // -- a payload with no signing block at all ---------------------------------
  // Older view models, and anything served while the two halves are mid-deploy. The card has to
  // behave exactly as it did before signing existed rather than refuse every approval.
  {
    const c = card({ state: { has_pdf: true, revision_no: 2 } });
    c.handle.render();
    c.nodes["ap-name"].value = "Marguerite Oyelaran";
    await c.handle.submit();
    await flush();
    out.noSigningBlock = { rendered: c.read(), calls: c.calls };
  }

  console.log(JSON.stringify(out));
})();
