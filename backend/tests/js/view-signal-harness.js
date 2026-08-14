"use strict";
/* Run the REAL applyHashView + signalProposalViewed out of app.js and report every POST they made.
 *
 * The requirement is a negative — "landing in the chat must NOT mark the proposal viewed" — and a
 * negative about what code DOESN'T do cannot be grepped. It also has to survive the two shapes
 * that bit us before: a legacy row whose revision is genuinely `null` (so `null` can't be the
 * never-signalled sentinel), and a revision landing while the customer is already sitting on the
 * proposal step (the poll re-renders, and the latch must notice the revision moved).
 *
 * Usage: node view-signal-harness.js <frontend-dir>   →  one line of JSON
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

/** Lift the latch DECLARATION from the real source, not a copy.
 *
 *  The sentinel is load-bearing: it must be `undefined`, because `null` is a real revision on rows
 *  that predate revisioning. Re-declaring it here would let app.js switch to `= null` while this
 *  harness kept testing the old shape — the legacy case below is what catches that, and it can
 *  only catch it if the declaration under test is the one that runs. */
function grabLatchDecl() {
  const m = /^let VIEW_SIGNALED_REV.*$/m.exec(src);
  if (!m) throw new Error("the VIEW_SIGNALED_REV latch is gone — rewrite this harness");
  return m[0];
}

/** A page scope holding one STATE, driven by a script of hash navigations. */
function run(opts) {
  const o = opts || {};
  const posts = [];
  const shown = { chat: true, proposal: false };
  const nodes = {};
  const el = (id) => (nodes[id] = nodes[id] || {
    id, classList: { _h: false, add() { this._h = true; }, remove() { this._h = false; },
                     contains: (c) => c === "hidden" && nodes[id].classList._h },
    scrollIntoView() {},
  });
  // `api` is the seam: record the call and answer per the case under test.
  const api = (method, p, body) => {
    posts.push({ method, path: p, body });
    return Promise.resolve({ ok: o.apiFails ? false : true, status: o.apiFails ? 500 : 200, data: {} });
  };

  const scope = new Function(
    "STATE_IN", "api", "$", "hide", "show", "applyStepPanel", "STEP_CARDS", "location", "window",
    `let STATE = STATE_IN;
     let ACTIVE_STEP = "proposal";
     ${grabLatchDecl()}
     ${fn("signalProposalViewed")}
     ${fn("applyHashView")}
     return {
       go: (hash) => { location.hash = hash; applyHashView(false); },
       setState: (s) => { STATE = s; },
       bumpRevision: (n) => { STATE.revision_no = n; },
       latch: () => VIEW_SIGNALED_REV,
     };`);

  const loc = { hash: "" };
  const api2 = api;
  const handle = scope(
    o.state === undefined ? { revision_no: 2 } : o.state,
    api2, el,
    (x) => x && x.classList.add("hidden"),
    (x) => x && x.classList.remove("hidden"),
    () => {}, { deposit: 1, contacts: 1 }, loc, { scrollTo() {} });

  return { handle, posts, shown, loc };
}

/** Let the api() promise's .then (the latch revert) settle before reading anything. */
const flush = () => new Promise((r) => setImmediate(r));

(async () => {
  const out = {};

  // ── the headline requirement ───────────────────────────────────────────────
  {
    const r = run({});
    r.handle.go("#chat");
    r.handle.go("#status");
    await flush();
    out.chatAndStatusNeverMark = r.posts;
  }

  // Opening the proposal step marks it, exactly once, with the revision on screen.
  {
    const r = run({});
    r.handle.go("#proposal");
    await flush();
    out.proposalMarks = r.posts;
  }

  // Repeated navigation to the same revision is one POST, not one per click.
  {
    const r = run({});
    r.handle.go("#proposal");
    r.handle.go("#chat");
    r.handle.go("#proposal");
    r.handle.go("#proposal/deposit");
    await flush();
    out.repeatIsIdempotent = r.posts;
  }

  // The deposit and contacts TABS live inside the proposal step, so they count too.
  {
    const r = run({});
    r.handle.go("#proposal/deposit");
    await flush();
    out.depositTabMarks = r.posts;
  }

  // ── a revision lands while the customer is on the proposal step ────────────
  // The poll refetches and re-renders, which re-runs applyHashView with the hash unchanged.
  // The latch has to notice the revision moved, or a customer watching the document swap under
  // them is never recorded as having seen the new one.
  {
    const r = run({});
    r.handle.go("#proposal");
    await flush();
    r.handle.bumpRevision(3);
    r.handle.go("#proposal");
    await flush();
    out.newRevisionReSignals = r.posts;
  }

  // ── failure handling ───────────────────────────────────────────────────────
  // A failed POST must not consume the latch, or one dropped packet means the proposal is never
  // marked viewed for that revision.
  {
    const r = run({ apiFails: true });
    r.handle.go("#proposal");
    await flush();
    // `undefined` does not survive JSON, so report the type — the point is that the latch went
    // back to the never-signalled sentinel rather than to null (a real revision) or a number.
    const afterFailure = typeof r.handle.latch();
    r.handle.go("#chat");
    r.handle.go("#proposal");
    await flush();
    out.failureRetries = { posts: r.posts, latchTypeAfterFailure: afterFailure };
  }

  // ── the shapes that bit us ─────────────────────────────────────────────────
  // A legacy row's revision is genuinely null. If null were the sentinel, these customers would
  // never be recorded at all.
  {
    const r = run({ state: { revision_no: null } });
    r.handle.go("#proposal");
    r.handle.go("#chat");
    r.handle.go("#proposal");
    await flush();
    out.legacyNullRevision = r.posts;
  }

  // Before the first load resolves there is no STATE — navigating must not throw.
  {
    const r = run({ state: null });
    let threw = false;
    try { r.handle.go("#proposal"); } catch { threw = true; }
    await flush();
    out.noStateIsSafe = { posts: r.posts, threw };
  }

  console.log(JSON.stringify(out));
})();
