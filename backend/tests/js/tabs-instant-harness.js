"use strict";
/* Run the real reveal.js, the real boot wrapper / jumpToDocument / rememberDocTop from app.js, the
 * real projects.js and the real shell.js boot, and report what they did. Lifted from the shipped
 * files (functions by brace counting, like scope-group-harness.js), never copied.
 *
 * Usage: node tabs-instant-harness.js <frontend-dir>   ->  one line of JSON
 */
const fs = require("fs");
const path = require("path");

const ROOT = process.argv[2];
const read = (f) => fs.readFileSync(path.join(ROOT, f), "utf8");
const appSrc = read("app.js");

function braces(src, from, what) {
  const i = src.indexOf("{", from);
  let depth = 0;
  for (let j = i; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}" && --depth === 0) return j + 1;
  }
  throw new Error("unbalanced braces reading " + what);
}
function fn(name) {
  const m = new RegExp("\\nfunction " + name + "\\s*\\(").exec(appSrc);
  if (!m) throw new Error(name + "() is gone from app.js — rewrite this harness, don't delete it");
  return appSrc.slice(m.index, braces(appSrc, m.index + m[0].length - 1, name));
}
function topConst(name) {
  const m = new RegExp("\\nconst " + name + " = [^\\n]*;").exec(appSrc);
  if (!m) throw new Error("const " + name + " is gone from app.js");
  return m[0];
}
const tick = () => new Promise((r) => setTimeout(r, 0));

// ── reveal.js ────────────────────────────────────────────────────────────────
function anim(pseudo) {
  return { effect: { pseudoElement: pseudo }, paused: 0, played: 0,
    pause() { this.paused++; }, play() { this.played++; } };
}
async function reveal(scenario) {
  const listeners = {};
  const timers = [];
  const anims = [anim("::view-transition-old(root)"), anim("::view-transition-group(tw-header)"),
                 anim(null)];                      // an ordinary page animation: never touched
  let resolveReady;
  const ready = new Promise((r) => { resolveReady = r; });
  const win = { addEventListener(n, f) { listeners[n] = f; } };
  const doc = { getAnimations: () => anims };
  new Function("window", "document", "setTimeout", "requestAnimationFrame", read("reveal.js"))(
    win, doc, (f, ms) => timers.push([f, ms]), (f) => f());
  const vt = scenario === "no-vt" ? null : { ready };
  if (scenario === "drawn-first") win.TWReveal.done();          // a back/forward restore
  listeners.pagereveal({ viewTransition: vt });
  if (scenario === "done-before-ready") win.TWReveal.done();
  resolveReady();
  await tick();
  const atReveal = anims.map((a) => [a.paused, a.played]);
  if (scenario === "timeout") { timers.forEach(([f]) => f()); }
  else if (scenario !== "never") win.TWReveal.done();
  const after = anims.map((a) => [a.paused, a.played]);
  win.TWReveal.done();                                           // a second call plays nothing twice
  return { listened: typeof listeners.pagereveal === "function", atReveal, after,
           afterSecondDone: anims.map((a) => [a.paused, a.played]), timers: timers.map((t) => t[1]) };
}

// ── app.js: the boot wrapper always releases the fade ─────────────────────────
function bootWrapper() {
  const at = appSrc.indexOf("(async function boot() {");
  if (at < 0) throw new Error("the boot wrapper is gone from app.js");
  const end = braces(appSrc, at, "boot");
  return appSrc.slice(at, end) + ")()";
}
async function boot(kind) {
  let released = 0;
  const window = { TWReveal: { done() { released++; } } };
  const bootPage = async () => { await tick(); if (kind === "throws") throw new Error("boom"); };
  const p = new Function("window", "bootPage", "return " + bootWrapper() + ";")(window, bootPage);
  try { await p; } catch { /* the throw still propagates; what matters is the release */ }
  return released;
}

// ── app.js: landing on the document card at the height it was clicked at ──────
function store(initial) {
  const m = Object.assign({}, initial);
  return { m, getItem: (k) => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); },
           removeItem: (k) => { delete m[k]; } };
}
function landing(saved, hash) {
  const ss = store(saved === undefined ? {} : { tw_doc_top: saved });
  const card = { hidden: false, top: 640, scrolledIntoView: 0,
    classList: { contains: (c) => c === "hidden" && card.hidden },
    getBoundingClientRect: () => ({ top: card.top }), scrollIntoView() { card.scrolledIntoView++; } };
  const by = [];
  const window = { scrollBy: (x, y) => by.push([x, y]) };
  new Function("$", "location", "sessionStorage", "window",
    `${topConst("DOC_TOP_KEY")}
     let DOC_JUMPED = false;
     ${fn("jumpToDocument")}
     jumpToDocument();
     jumpToDocument();`)((id) => (id === "pdf-card" ? card : null),
    { hash: hash || "#proposal/proposal/document" }, ss, window);
  return { scrollBy: by, intoView: card.scrolledIntoView, left: ss.m };
}
function remember(ev, onTab) {
  const ss = store({});
  const card = { getBoundingClientRect: () => ({ top: 117.6 }) };
  const target = { closest: (s) => (onTab && s === "a.scope-tab" ? {} : null) };
  new Function("$", "sessionStorage", "e",
    `${topConst("DOC_TOP_KEY")}
     ${fn("rememberDocTop")}
     rememberDocTop(e);`)((id) => (id === "pdf-card" ? card : null), ss,
    Object.assign({ defaultPrevented: false, button: 0, target }, ev));
  return ss.m;
}

// ── projects.js: one /api/me/proposals per page ──────────────────────────────
async function projects(answers) {
  const calls = [];
  const win = {};
  const fetch = (u) => {
    calls.push(u);
    const a = answers[Math.min(calls.length - 1, answers.length - 1)];
    return a === "reject" ? Promise.reject(new Error("offline"))
      : Promise.resolve({ json: async () => a });
  };
  new Function("window", "fetch", read("projects.js"))(win, fetch);
  const one = await Promise.all([win.TWProjects.me(), win.TWProjects.load()]);
  const two = await win.TWProjects.load();
  return { calls: calls.length, emailFirst: one[0].email || null, list: two.length };
}

// ── shell.js: the remembered rail is drawn open, not slid in ────────────────
function shell() {
  const ops = [];
  const meCalls = [];
  const fetched = [];
  const el = () => ({ id: "", innerHTML: "", textContent: "", style: {}, dataset: {}, hidden: false,
    classList: { add() {}, remove() {}, toggle() {} }, setAttribute() {},
    appendChild() {}, insertBefore() {}, addEventListener() {}, querySelector: () => null,
    querySelectorAll: () => [], remove() {}, focus() {} });
  const all = [];
  const byId = (id) => all.find((n) => n.id === id)
    || (all.some((n) => (n.innerHTML || "").includes('id="' + id + '"')) ? el() : null);
  const body = { appendChild(n) { all.push(n); }, insertBefore(n) { all.push(n); }, firstChild: null };
  Object.defineProperty(body, "offsetWidth", { get() { ops.push("layout"); return 1000; } });
  const header = el();          // the bell and the burger go into the sticky .site-header
  header.insertBefore = (n) => all.push(n);
  header.appendChild = (n) => all.push(n);
  const doc = { getElementById: byId, createElement: el, addEventListener() {},
    querySelector: (s) => (s === ".site-header" ? header : null), head: { appendChild() {} }, body,
    documentElement: { classList: {
      add: (c) => ops.push("add:" + c), remove: (c) => ops.push("remove:" + c),
      toggle: (c, on) => ops.push("toggle:" + c + "=" + on), contains: () => false } } };
  const win = { addEventListener() {}, location: { pathname: "/p/tok123", href: "x" },
    setInterval() {}, setTimeout() {},
    matchMedia: () => ({ matches: true, addEventListener() {}, addListener() {} }),
    TWProjects: { me: () => { meCalls.push(1); return Promise.resolve({ ok: true, email: "c@x.com" }); } },
    fetch: (u) => { fetched.push(u); return Promise.resolve({ json: async () => ({}) }); },
    localStorage: { getItem: () => "1", setItem() {} } };
  new Function("window", "document", "localStorage", "fetch", "setInterval", "setTimeout", "location",
    read("shell.js"))(win, doc, win.localStorage, win.fetch, win.setInterval, win.setTimeout, win.location);
  win.TWShell.mount();
  return { ops, me: meCalls.length, fetchedMine: fetched.filter((u) => u === "/api/me/proposals").length };
}

(async () => {
  const out = {};
  for (const s of ["hold", "timeout", "no-vt", "drawn-first", "done-before-ready", "never"]) {
    out["reveal_" + s] = await reveal(s);
  }
  out.bootOk = await boot("ok");
  out.bootThrows = await boot("throws");
  out.landSaved = landing("212");
  out.landNone = landing(undefined);
  out.landJunk = landing("abc");
  out.landOtherHash = landing("212", "#proposal");
  out.rememberPlain = remember({}, true);
  out.rememberCtrl = remember({ ctrlKey: true }, true);
  out.rememberMiddle = remember({ button: 1 }, true);
  out.rememberOffTab = remember({}, false);
  out.projectsOnce = await projects([{ ok: true, authed: true, email: "c@x.com", proposals: [{ token: "a" }] }]);
  out.projectsFailRetry = await projects([{ ok: false }, { ok: true, authed: true, proposals: [{ token: "a" }] }]);
  out.projectsSignedOutRetry = await projects([{ ok: true, authed: false, proposals: [] },
                                               { ok: true, authed: true, proposals: [{ token: "a" }] }]);
  out.projectsRejectRetry = await projects(["reject", { ok: true, authed: true, proposals: [{ token: "a" }] }]);
  out.shell = shell();
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
