"use strict";
/* Run the REAL api() + renderGone() out of app.js against a 410 proposal_gone and report what the
 * page drew. A deleted proposal must show one card with one sentence, escaped, and nothing else;
 * a proposal deleted while the customer has it open must reload (the boot path then draws the card
 * on a clean page) and stop polling.
 *
 * Usage: node gone-state-harness.js <frontend-dir>   ->  one line of JSON
 */
const fs = require("fs");
const path = require("path");

// Line endings normalised: the working copy is CRLF on Windows checkouts and LF in CI.
const src = fs.readFileSync(path.join(process.argv[2], "app.js"), "utf8").replace(/\r\n/g, "\n");

function fn(name, kw) {
  const m = new RegExp("\\n" + (kw || "function") + " " + name + "\\s*\\(").exec(src);
  if (!m) throw new Error(name + "() is gone from app.js -- rewrite this harness, don't delete it");
  const i = src.indexOf("{", m.index + m[0].length - 1);
  let depth = 0;
  for (let j = i; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}" && --depth === 0) return src.slice(m.index, j + 1);
  }
  throw new Error("unbalanced braces reading " + name);
}

function line(re, what) {
  const m = re.exec(src);
  if (!m) throw new Error(what + " is gone from app.js -- rewrite this harness");
  return m[0];
}

function escLine() {
  const m = /^const esc = [\s\S]*?\);\n/m.exec(src);
  if (!m) throw new Error("esc is gone from app.js -- rewrite this harness");
  return m[0];
}

async function drive(opts) {
  const nodes = {};
  const el = (id) => (nodes[id] = {
    id, innerHTML: "", hidden: id === "gate",
    classList: { add(c) { if (c === "hidden") nodes[id].hidden = true; },
                 remove(c) { if (c === "hidden") nodes[id].hidden = false; } },
  });
  ["portal", "gate", "loading"].forEach(el);
  const loc = { reloads: 0, reload() { this.reloads++; } };
  const cleared = [];
  const fetchImpl = () => Promise.resolve({
    ok: opts.status < 400, status: opts.status, json: () => Promise.resolve(opts.body),
  });
  const body =
    `const $ = (id) => NODES[id];
     ${line(/^const show = .*$/m, "show")}
     ${line(/^const hide = .*$/m, "hide")}
     ${escLine()}
     ${line(/^const GONE_TEXT = .*$/m, "GONE_TEXT")}
     ${line(/^let GONE = .*$/m, "GONE")}
     let STATE = STATE_IN;
     let POLL_TIMER = STATE_IN ? 7 : null;
     ${fn("renderGone")}
     ${fn("api", "async function")}
     return { api, gone: () => GONE, timer: () => POLL_TIMER };`;
  const make = new Function("fetch", "location", "clearInterval", "STATE_IN", "TOKEN", "NODES", body);
  const h = make(fetchImpl, loc, (t) => cleared.push(t), opts.state || null, "tok", nodes);
  const res = await h.api("GET", "");
  await h.api("GET", "/messages");          // a second 410 must not draw or reload twice
  return {
    res, gone: h.gone(), timer: h.timer(), cleared, reloads: loc.reloads,
    gate: nodes.gate.innerHTML, gateHidden: nodes.gate.hidden,
    portalHidden: nodes.portal.hidden, loadingHidden: nodes.loading.hidden,
  };
}

(async () => {
  const out = {};
  out.fresh = await drive({ status: 410, body: { ok: false, error: "proposal_gone",
    message: "This proposal is no longer available. <b>x</b>" } });
  out.noMessage = await drive({ status: 410, body: { ok: false, error: "proposal_gone" } });
  out.midSession = await drive({ status: 410, state: { project_name: "Acme" },
    body: { ok: false, error: "proposal_gone", message: "gone" } });
  out.otherGone = await drive({ status: 410, body: { ok: false, error: "something_else" } });
  out.live = await drive({ status: 200, body: { ok: true, authed: true } });
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
