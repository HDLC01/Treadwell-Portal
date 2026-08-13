"use strict";
/* Build the customer's PDF URLs by running the REAL app.js functions.
 *
 * The claim under test is "a re-send changes the bytes the customer sees". Half of that is the
 * URL: the frames are mounted once and the browser caches the response, so as long as every
 * revision shares one URL the customer can keep reading the superseded document while the prices
 * beside it are new. A source grep for "rev=" would pass while one of the three mount sites still
 * built its own literal URL — so this executes each site and reports the src it actually set.
 *
 * Usage: node pdf-url-harness.js <frontend-dir>   →  one line of JSON
 */
const fs = require("fs");
const path = require("path");

const ROOT = process.argv[2];
const src = fs.readFileSync(path.join(ROOT, "app.js"), "utf8");

/** Lift a top-level `function name(...) {...}` by brace counting. */
function fn(name) {
  const m = new RegExp("\\nfunction " + name + "\\s*\\(").exec(src);
  if (!m) throw new Error(name + "() is gone from app.js — rewrite this harness, don't delete it");
  const i = src.indexOf("{", m.index + m[0].length - 1);
  let depth = 0;
  for (let j = i; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}" && --depth === 0) return src.slice(m.index, j + 1);
  }
  throw new Error("unbalanced braces reading " + name);
}

/** Run the three mount sites against one STATE and collect every src they set.
 *
 *  `mounted` seeds the once-only latches, so the harness can also ask the question that matters
 *  after a revision lands: does a REMOUNT produce the new URL? */
function mount(state, mounted) {
  const frames = [];          // every iframe the page created, in order
  const nodes = {};
  const el = (id) => (nodes[id] = nodes[id] || {
    id, innerHTML: "", textContent: "", href: "",
    querySelectorAll: () => [], remove() {}, appendChild() {},
    addEventListener() {}, setAttribute() {}, classList: { add() {}, remove() {}, toggle() {} },
  });
  const doc = {
    createElement: () => {
      const f = { className: "", title: "", src: "", setAttribute() {}, addEventListener() {} };
      frames.push(f);
      return f;
    },
    body: { style: {} },
  };
  const scope = new Function(
    "STATE", "TOKEN", "$", "document", "setEligible", "PDF_MOUNTED_IN", "INLINE_MOUNTED_IN",
    `let PDF_MOUNTED = PDF_MOUNTED_IN, INLINE_PDF_MOUNTED = INLINE_MOUNTED_IN;
     ${fn("pdfUrl")}
     ${fn("renderPdf")}
     ${fn("mountPdf")}
     ${fn("mountInlinePdf")}
     ${fn("resetPdfMounts")}
     renderPdf(STATE.has_pdf);      // sets the download + popup links, mounts the inline preview
     mountPdf();                    // the full-size popup frame
     return { link: $("pdf-link").href, modalLink: $("pdf-modal-link").href,
              helper: pdfUrl(), helperHash: pdfUrl("#view=FitH"),
              afterReset: (function () { resetPdfMounts(); mountPdf(); mountInlinePdf();
                                         return true; })() };`);
  const out = scope(state, "tok-123", el, doc, () => {},
                    !!(mounted && mounted.popup), !!(mounted && mounted.inline));
  out.frameSrcs = frames.map((f) => f.src);
  return out;
}

const out = {};

// A pinned revision — the normal case once staff have sent anything.
out.rev2 = mount({ has_pdf: true, revision_no: 2 });

// The same project after a re-send. Every URL must differ from rev 2's, or the browser serves
// what it already has.
out.rev3 = mount({ has_pdf: true, revision_no: 3 });

// A legacy row that was never revisioned. Still a well-formed URL (the server sends no-store
// for these, so correctness doesn't depend on the value).
out.legacy = mount({ has_pdf: true, revision_no: null });

// A frame that was already mounted before the revision landed: the reset + remount has to
// produce the NEW url, which is the whole point of pairing resetPdfMounts with a versioned URL.
out.remount = mount({ has_pdf: true, revision_no: 5 }, { popup: true, inline: true });

// The server ignores ?rev= when choosing a document; assert the client never asks it to select
// one (no revision_no / revision param), only to bust the cache.
out.paramNames = Object.keys(out.rev2)
  .filter((k) => typeof out.rev2[k] === "string")
  .map((k) => (out.rev2[k].split("?")[1] || "").split("#")[0]);

console.log(JSON.stringify(out));
