"use strict";
/* Run the REAL attHtml + attHydrate + attFailed out of app.js and report what they built.
 *
 * WHY IT HAS TO BE EXECUTED. The requirement has two halves that no source assertion can reach.
 * One is a negative: the filename must NOT be pre-truncated in JavaScript, because the ellipsis is
 * CSS's job and a name cut here could never be recovered from the title. The other is a wiring
 * fact: the failure state is now bound as a LISTENER rather than written into the markup as
 * `onerror="..."`, because this app sends `script-src 'self'` with no 'unsafe-inline' and an
 * event-handler attribute is refused by the browser outright. Whether a handler exists is a
 * property of the running page, not of its text.
 *
 * THE DOM HERE IS A SMALL REAL TREE, not a bag of markup: the failure path mutates one tile out of
 * several, so "which element changed" is the whole question and a stub that answered
 * querySelector globally would give the same answer for every tile. It parses the markup attHtml
 * produced into nodes with real parents and children. It is not jsdom and it says so: no layout,
 * no cascade, no default event behaviour.
 *
 * Usage: node att-render-harness.js <frontend-dir>   →  one line of JSON
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(process.argv[2]);
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

// ── a very small DOM ─────────────────────────────────────────────────────────
// Enough of one to answer the questions this feature raises and no more: parents, children,
// classes, attributes, one event type. Written out rather than pulled in so the limits are
// visible; anything it cannot answer is meant to fail loudly rather than plausibly.
function node(tag, attrs) {
  const el = {
    tagName: String(tag).toUpperCase(),
    attrs: Object.assign({}, attrs),
    children: [],
    parent: null,
    listeners: {},
    text: "",
    complete: false,
    naturalWidth: 0,
    dataset: {},
    get className() { return this.attrs.class || ""; },
    set className(v) { this.attrs.class = String(v); },
    get classes() { return String(this.attrs.class || "").split(/\s+/).filter(Boolean); },
    get textContent() {
      return this.children.length ? this.children.map((c) => c.textContent).join("") : this.text;
    },
    set textContent(v) { this.children = []; this.text = String(v); },
    classList: {
      add(...cs) { const s = new Set(el.classes); cs.forEach((c) => s.add(c));
                   el.attrs.class = Array.from(s).join(" "); },
      remove(...cs) { const s = new Set(el.classes); cs.forEach((c) => s.delete(c));
                      el.attrs.class = Array.from(s).join(" "); },
      contains: (c) => el.classes.indexOf(c) >= 0,
    },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { c.parent = this; this.children.push(c); return c; },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    // The one event this feature has: an image that failed. Fired explicitly by a case below,
    // because a harness that fired it automatically could not tell the two failure routes apart
    // (a listener that heard the error, and an image that had already failed before the listener
    // was bound — the cached-404 case, which is the one an attribute handler used to cover).
    fire(t) { (this.listeners[t] || []).forEach((f) => f({ target: this })); },
    descendants() {
      return this.children.reduce((acc, c) => acc.concat([c], c.descendants()), []);
    },
    matches(sel) {
      // `.class`, `tag`, or `tag .class` — the three shapes this feature's code uses.
      const parts = sel.trim().split(/\s+/);
      const last = parts[parts.length - 1];
      const hit = last.startsWith(".")
        ? this.classes.indexOf(last.slice(1)) >= 0
        : this.tagName === last.toUpperCase();
      if (!hit) return false;
      let up = this.parent;
      for (let i = parts.length - 2; i >= 0; i--) {
        const want = parts[i];
        while (up && !(want.startsWith(".") ? up.classes.indexOf(want.slice(1)) >= 0
                                            : up.tagName === want.toUpperCase())) up = up.parent;
        if (!up) return false;
        up = up.parent;
      }
      return true;
    },
    // SCOPED, unlike the staff drawer's stub: it searches this element's own subtree. The failure
    // path writes into one tile's caption and must leave its neighbour's alone, and a global
    // lookup would hand back the same span for both and report a pass either way.
    querySelectorAll(sel) { return this.descendants().filter((d) => d.matches(sel)); },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    closest(sel) {
      let up = this;
      while (up) { if (up.matches(sel)) return up; up = up.parent; }
      return null;
    },
  };
  // data-* attributes reach `dataset`, which attHydrate latches on.
  Object.keys(el.attrs).forEach((k) => {
    if (k.startsWith("data-")) {
      el.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = el.attrs[k];
    }
  });
  const raw = el.setAttribute;
  el.setAttribute = function (k, v) {
    raw.call(this, k, v);
    if (k.startsWith("data-")) {
      this.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(v);
    }
  };
  return el;
}

const VOID = new Set(["img", "line", "path", "polyline", "br", "input", "meta"]);

/** Parse the markup attHtml produced into a real tree under one root. */
function parse(html) {
  const root = node("div", { class: "chat-thread" });
  const stack = [root];
  const TOKEN = /<\/?([a-zA-Z0-9]+)((?:\s+[-a-zA-Z0-9:_]+(?:="[^"]*")?)*)\s*(\/?)>|([^<]+)/g;
  let m;
  while ((m = TOKEN.exec(html))) {
    if (m[4] !== undefined) {                       // text
      const t = m[4];
      if (t.trim()) stack[stack.length - 1].appendChild(Object.assign(node("#text", {}), { text: t }));
      continue;
    }
    const tag = m[1];
    if (m[0][1] === "/") {                          // closing
      if (stack.length > 1) stack.pop();
      continue;
    }
    const attrs = {};
    const ar = /([-a-zA-Z0-9:_]+)(?:="([^"]*)")?/g;
    let a;
    while ((a = ar.exec(m[2] || ""))) attrs[a[1]] = a[2] === undefined ? "" : a[2];
    const el = node(tag, attrs);
    stack[stack.length - 1].appendChild(el);
    if (!m[3] && !VOID.has(tag.toLowerCase())) stack.push(el);
  }
  return root;
}

// ── the page's own helpers, lifted rather than reimplemented ──────────────────
// A reimplemented `esc` would prove that this harness escapes, not that app.js does.
const escSrc = /^const esc = [\s\S]*?;$/m.exec(src);
if (!escSrc) throw new Error("esc is gone from app.js — rewrite this harness");

const page = new Function("TOKEN", "document",
  `"use strict";
   ${escSrc[0]}
   ${fn("fileSize")}
   ${fn("attHtml")}
   ${fn("attHydrate")}
   ${fn("attFailed")}
   return { attHtml, attHydrate, attFailed };`)(
  "tok", { });

const IMG = (id, name) => ({ id, name: name || "Slab-north-bay-before-grinding-2026-08-26.jpg",
                             size: 2411724, image: true });
const DOC = (id) => ({ id, name: "Ridgeline-Cold-Storage-schedule-rev-C.docx",
                       size: 41231, image: false });

/** Render one message's attachments, arm them, and describe every tile and chip. */
function render(atts) {
  const html = page.attHtml({ meta: { attachments: atts } });
  const root = parse(html);
  return { html, root };
}

function describe(el) {
  if (!el) return null;
  const cap = el.querySelector(".att-cap");
  const nameEl = el.querySelector(".att-name");
  const sizeEl = el.querySelector(".att-size");
  return {
    classes: el.classes,
    hasHref: "href" in el.attrs,
    href: el.attrs.href || "",
    title: el.attrs.title || null,
    ariaLabel: el.attrs["aria-label"] || null,
    hasCaption: !!cap,
    name: nameEl ? nameEl.textContent : null,
    size: sizeEl ? sizeEl.textContent : null,
    imgs: el.querySelectorAll("img").length,
    imgAlt: (el.querySelector("img") || { attrs: {} }).attrs.alt,
    // The img must sit inside the WELL, not directly under the anchor: the anchor holds the
    // caption, and anything that rebuilds the anchor's contents takes the filename with it.
    imgParent: el.querySelector("img") ? el.querySelector("img").parent.classes : [],
    brokeGlyphs: el.querySelectorAll(".att-broke").length,
    errorListeners: (el.querySelector("img") || { listeners: {} }).listeners.error
      ? el.querySelector("img").listeners.error.length : 0,
  };
}

const out = { errors: {} };

try {
  // 1. AS RENDERED. One of each kind, which is the shape a real message has.
  const one = render([IMG("a1"), DOC("a2")]);
  out.rendered = {
    html: one.html,
    tile: describe(one.root.querySelector(".att-img")),
    chip: describe(one.root.querySelector(".att-file")),
    // No handler ATTRIBUTES anywhere: the CSP refuses them, so one in the markup is dead code
    // that reads as a working failure state.
    inlineHandlers: (one.html.match(/\son[a-z]+="/g) || []).length,
  };

  // 2. ARMED. attHydrate binds the listener the markup is not allowed to carry, and latches so a
  //    re-render of an unchanged thread cannot stack a second one on the same tile.
  const armed = render([IMG("b1"), DOC("b2")]);
  page.attHydrate(armed.root);
  page.attHydrate(armed.root);
  out.armed = {
    tile: describe(armed.root.querySelector(".att-img")),
    latched: armed.root.querySelector(".att-img").dataset.attArmed || null,
  };

  // 3. THE ERROR FIRES on one tile out of two. The other must be untouched — this is the case a
  //    globally-scoped querySelector cannot see, and the reason the tree above is a real tree.
  const two = render([IMG("c1", "first-photo.jpg"), IMG("c2", "second-photo.jpg")]);
  page.attHydrate(two.root);
  const tiles = two.root.querySelectorAll(".att-img");
  tiles[0].querySelector("img").fire("error");
  out.oneFailed = { failed: describe(tiles[0]), survivor: describe(tiles[1]) };

  // 4. ALREADY BROKEN before the listener was bound. A cached 404 can finish before the paint's
  //    own script runs, and a listener added afterwards never hears it — so a complete image with
  //    no intrinsic width is treated as the failure it is.
  const cached = render([IMG("d1")]);
  const cimg = cached.root.querySelector("img");
  cimg.complete = true;
  cimg.naturalWidth = 0;
  page.attHydrate(cached.root);
  out.alreadyBroken = { tile: describe(cached.root.querySelector(".att-img")) };

  // 5. A COMPLETE, GOOD image must not be mistaken for a broken one.
  const good = render([IMG("e1")]);
  const gimg = good.root.querySelector("img");
  gimg.complete = true;
  gimg.naturalWidth = 1600;
  page.attHydrate(good.root);
  out.completeAndFine = { tile: describe(good.root.querySelector(".att-img")) };

  // 6. A FAILED CHIP. Same function, no shape change at all.
  const chip = render([DOC("f1")]);
  page.attFailed(chip.root.querySelector(".att-file"));
  out.failedChip = { chip: describe(chip.root.querySelector(".att-file")) };

  // 7. A NAME LONGER THAN THE TILE, handed over whole. The ellipsis belongs to CSS; a name cut
  //    here could never be read back off the title.
  const LONG = "Ridgeline-Cold-Storage-north-bay-slab-moisture-test-2026-08-26-final-rev-C.jpeg";
  const long = render([IMG("g1", LONG)]);
  out.longName = { tile: describe(long.root.querySelector(".att-img")), given: LONG };
} catch (e) {
  out.errors.run = e.constructor.name + ": " + e.message + "\n" + (e.stack || "");
}

console.log(JSON.stringify(out));
