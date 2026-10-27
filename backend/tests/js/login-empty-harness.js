"use strict";
/* Run the REAL frontend/login.js against /api/me/proposals answers and report what it drew.
 * A signed-in customer whose only proposal was deleted gets an empty-state card, not the login
 * form again (which read as "sign-in failed").
 *
 * Usage: node login-empty-harness.js <frontend-dir>   ->  one line of JSON
 */
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(process.argv[2], "login.js"), "utf8");

function page(answer) {
  const nodes = {};
  const el = (id) => (nodes[id] = nodes[id] || {
    id, innerHTML: "", hidden: id !== "loading", listeners: {},
    classList: { add(c) { if (c === "hidden") nodes[id].hidden = true; },
                 remove(c) { if (c === "hidden") nodes[id].hidden = false; } },
    addEventListener(ev, f) { this.listeners[ev] = f; },
  });
  const calls = { login: 0, logout: 0, redirect: null };
  const document = { getElementById: el };
  const fetch = (url) => {
    if (url === "/api/auth/logout") { calls.logout++; return Promise.resolve({}); }
    return Promise.resolve({ json: () => Promise.resolve(answer) });
  };
  const TWLogin = { renderLogin: () => { calls.login++; } };
  const TWProjects = { rowsHtml: (ps) => ps.map((p) => "<row>" + p.token + "</row>").join("") };
  const location = { set href(v) { calls.redirect = v; } };
  new Function("document", "fetch", "TWLogin", "TWProjects", "location", src)(
    document, fetch, TWLogin, TWProjects, location);
  return { nodes, calls };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

(async () => {
  const out = {};
  const empty = page({ ok: true, authed: true, proposals: [] });
  await tick(); await tick();
  out.empty = { list: empty.nodes["project-list"].innerHTML, listHidden: empty.nodes["project-list"].hidden,
                loginCalls: empty.calls.login };
  await empty.nodes["empty-switch"].listeners.click();
  out.emptySwitch = { logout: empty.calls.logout, loginCalls: empty.calls.login };

  const anon = page({ ok: true, authed: false, proposals: [] });
  await tick(); await tick();
  out.anon = { loginCalls: anon.calls.login, list: anon.nodes["project-list"].innerHTML };

  const one = page({ ok: true, authed: true, proposals: [{ token: "tok-a" }] });
  await tick(); await tick();
  out.one = { redirect: one.calls.redirect };
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
