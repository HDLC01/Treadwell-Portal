"use strict";
// Portal shell: left sidebar, notification bell, bottom-right toasts.
//
// Mirrors the staff tool's chrome, but deliberately NOT a copy of its auth.js:
// customers have no roles, no Supabase, and an opaque same-origin session cookie
// rather than a bearer token. Named shell.js because the portal already has its
// own /auth.js (TWLogin) — and it must be added to main.py's asset allowlist.
//
// z-index sits in the PORTAL's band (its whole stack is 5/50/60/900/901). The
// staff values (9990-10001) would paint over this app's PDF popup.
(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const TOKEN = (location.pathname.match(/\/p\/([^/]+)/) || [])[1] || "";

  function injectCss() {
    if ($("tw-shell-css")) return;
    const s = document.createElement("style");
    s.id = "tw-shell-css";
    s.textContent = `
:root{--shell-w:240px}
body{transition:margin-left .2s ease}
#shell-side{position:fixed;top:0;left:0;height:100vh;width:var(--shell-w);background:var(--bg);
 border-right:1px solid var(--surface-highest);display:flex;flex-direction:column;padding:18px 14px;
 z-index:800;transform:translateX(-100%);transition:transform .2s ease;box-sizing:border-box;
 font-size:.9rem;overflow-y:auto}
html.shell-open #shell-side{transform:translateX(0)}
#shell-back{display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:790}
html.shell-open #shell-back{display:block}
@media (min-width:900px){
  html.shell-open body{margin-left:var(--shell-w)}
  html.shell-open #shell-back{display:none}
}
.shell-brand{display:flex;align-items:center;gap:10px;margin-bottom:20px;font-weight:900;
 color:var(--primary);letter-spacing:-.01em}
/* flex:none so the mark holds its size and the close button keeps its margin-left:auto slot */
.shell-bison{flex:none;display:block}
.shell-x{margin-left:auto;border:none;background:none;font-size:20px;line-height:1;cursor:pointer;
 color:var(--secondary);padding:2px 6px;border-radius:6px}
.shell-x:hover{background:var(--surface-low)}
.shell-sec{font-size:.62rem;font-weight:800;letter-spacing:.06em;text-transform:uppercase;
 color:var(--secondary);margin:16px 0 6px;padding:0 8px}
.shell-item{display:flex;align-items:center;gap:9px;min-height:40px;padding:0 9px;border-radius:8px;
 text-decoration:none;color:var(--fg);border:none;background:none;font:inherit;width:100%;
 text-align:left;cursor:pointer}
.shell-item:hover{background:var(--surface-low)}
.shell-item.active{background:var(--primary-tint);color:var(--primary);font-weight:700}
.shell-ico{width:18px;text-align:center;color:var(--secondary)}
.shell-item.active .shell-ico{color:var(--primary)}
.shell-foot{margin-top:auto;padding-top:12px;border-top:1px solid var(--surface-highest);
 font-size:.72rem;color:var(--secondary);display:flex;align-items:center;gap:8px}
.shell-foot button{margin-left:auto;border:none;background:none;cursor:pointer;color:var(--secondary);
 font-size:15px;padding:4px;border-radius:6px}
.shell-foot button:hover{background:var(--surface-low);color:var(--primary)}
#shell-burger{border:1px solid var(--outline);background:var(--bg);color:var(--fg);width:54px;height:34px;
 border-radius:8px;cursor:pointer;font-size:16px;line-height:1;flex:none}
#shell-burger:hover{background:var(--surface-low)}
/* Feedback dialog. z-index above the sidebar (800) and its scrim (790), below the PDF popup
   (900/901) — the portal's own band, never the staff tool's 9990+. */
#fb-back{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:870;display:flex;
 align-items:center;justify-content:center;padding:18px}
.fb-card{background:var(--surface);border:1px solid var(--surface-highest);
 border-radius:var(--radius-lg);padding:20px;width:100%;max-width:460px;box-shadow:var(--shadow-sm);
 max-height:90vh;overflow-y:auto}
.fb-head{display:flex;align-items:center;gap:10px;margin-bottom:8px;font-size:1.05rem}
.fb-head .shell-x{margin-left:auto}
.fb-lede{color:var(--secondary);font-size:.85rem;margin:0 0 14px}
.fb-lbl{display:block;font-size:.68rem;font-weight:800;letter-spacing:.05em;text-transform:uppercase;
 color:var(--secondary);margin:0 0 5px}
.fb-in{width:100%;box-sizing:border-box;background:var(--bg);color:var(--fg);
 border:1px solid var(--outline);border-radius:var(--radius);padding:10px 12px;font:inherit;
 margin-bottom:14px}
.fb-in:focus{outline:none;border-color:var(--primary)}
textarea.fb-in{resize:vertical}
.fb-err{color:var(--primary);font-size:.82rem;font-weight:600;margin:0 0 10px}
.fb-foot{display:flex;gap:10px;justify-content:flex-end}
.bell{position:relative;border:none;background:none;color:var(--secondary);font-size:17px;cursor:pointer;
 padding:5px 6px;border-radius:8px;line-height:1;margin-left:8px}
.bell:hover{background:var(--surface-low);color:var(--primary)}
.bell-badge{position:absolute;top:-1px;right:-1px;min-width:25px;height:16px;padding:0 3px;border-radius:8px;
 background:var(--primary);color:#fff;font:700 9px/16px system-ui;text-align:center;box-sizing:border-box}
#bell-back{position:fixed;inset:0;z-index:949;background:transparent}
#bell-panel{position:fixed;top:58px;right:16px;width:min(360px,calc(100vw - 28px));max-height:70vh;
 overflow-y:auto;background:var(--bg);border:1px solid var(--outline);border-radius:12px;
 box-shadow:0 16px 44px rgba(0,0,0,.22);z-index:950;font-size:.82rem}
.bell-h{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;font-weight:800;
 border-bottom:1px solid var(--surface-highest);position:sticky;top:0;background:var(--bg)}
.bell-list{padding:6px}
.bell-empty{padding:24px 14px;text-align:center;color:var(--secondary)}
.bell-item{display:flex;gap:10px;align-items:flex-start;padding:10px;border-radius:9px;
 text-decoration:none;color:var(--fg)}
.bell-item:hover{background:var(--surface-low)}
.bell-item.unread{background:var(--primary-tint)}
.bell-main{flex:1;min-width:0;display:flex;flex-direction:column;gap:1px}
.bell-title{font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bell-body{color:var(--secondary);font-size:.76rem}
.bell-time{color:var(--secondary);font-size:.7rem;flex:none}
#shell-toasts{position:fixed;right:16px;bottom:16px;z-index:700;display:flex;flex-direction:column;
 gap:10px;width:min(340px,calc(100vw - 28px));pointer-events:none}
.shell-toast{pointer-events:auto;display:flex;gap:10px;align-items:flex-start;background:var(--bg);
 border:1px solid var(--outline);border-left:3px solid var(--primary);border-radius:11px;
 padding:12px;box-shadow:0 12px 34px rgba(0,0,0,.2);cursor:pointer;transform:translateX(120%);opacity:0;
 transition:transform .32s cubic-bezier(.22,1,.36,1),opacity .32s ease;font-size:.82rem}
.shell-toast.in{transform:translateX(0);opacity:1}
.shell-toast .t{font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.shell-toast .b{color:var(--secondary);font-size:.76rem;display:-webkit-box;-webkit-line-clamp:3;
 -webkit-box-orient:vertical;overflow:hidden}
.shell-toast .x{border:none;background:none;color:var(--secondary);font-size:16px;line-height:1;
 cursor:pointer;padding:0 3px;border-radius:6px;flex:none}
/* Clear the sticky composer on small screens — it sits at bottom:0. */
@media (max-width:767px){#shell-toasts{left:12px;right:12px;bottom:84px;width:auto}}`;
    document.head.appendChild(s);
  }

  function buildSidebar() {
    if ($("shell-side")) return;
    const side = document.createElement("aside");
    side.id = "shell-side";
    const onChat = !!TOKEN;
    side.innerHTML =
      `<div class="shell-brand"><img class="shell-bison" src="/static/img/treadwell-bison.svg" alt="" width="28" height="18">TREADWELL<button class="shell-x" id="shell-close" title="Hide menu">‹</button></div>
       <div class="shell-sec">Your account</div>
       <a class="shell-item${onChat ? " active" : ""}" href="${onChat ? `/p/${encodeURIComponent(TOKEN)}` : "/"}">
         <span class="shell-ico">💬</span><span>Chat</span></a>
       <a class="shell-item" href="/"><span class="shell-ico">▤</span><span>My projects</span></a>
       <div class="shell-sec" id="shell-steps-h" hidden>This project</div>
       <div id="shell-steps"></div>
       <div class="shell-sec">Help us improve</div>
       <button class="shell-item" id="shell-fb" type="button">
         <span class="shell-ico">✎</span><span>Send feedback</span></button>
       <div class="shell-foot"><span id="shell-email"></span>
         <button id="shell-out" title="Sign out">⏻</button></div>`;
    document.body.appendChild(side);
    const back = document.createElement("div");
    back.id = "shell-back";
    document.body.appendChild(back);

    const setOpen = (open) => {
      document.documentElement.classList.toggle("shell-open", open);
      try { localStorage.setItem("tw_portal_nav", open ? "1" : "0"); } catch {}
    };
    // Default CLOSED: customers are mobile-heavy and the reading column is only
    // 760px, so an open drawer squeezes it.
    let saved = null;
    try { saved = localStorage.getItem("tw_portal_nav"); } catch {}
    setOpen(saved === "1");
    back.addEventListener("click", () => setOpen(false));
    $("shell-close").addEventListener("click", () => setOpen(false));
    $("shell-out").addEventListener("click", async () => {
      try { await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" }); } catch {}
      location.href = "/";
    });
    $("shell-fb").addEventListener("click", () => { setOpen(false); openFeedback(); });

    // Burger + bell go INSIDE the existing sticky header — adding a second bar
    // would fight the sticky chat header already anchored at top:0.
    const header = document.querySelector(".site-header");
    if (header) {
      const burger = document.createElement("button");
      burger.id = "shell-burger"; burger.title = "Menu"; burger.textContent = "☰";
      burger.addEventListener("click", () => setOpen(true));
      header.insertBefore(burger, header.firstChild);
      const bell = document.createElement("button");
      bell.className = "bell"; bell.id = "bell"; bell.title = "Notifications";
      bell.setAttribute("aria-label", "Notifications");
      bell.innerHTML = '🔔<span class="bell-badge" id="bell-badge" hidden></span>';
      const tag = header.querySelector(".tag");
      if (tag) header.insertBefore(bell, tag); else header.appendChild(bell);
    }
    // The 4 project steps reuse the tracker's focusStep, so the sidebar and the
    // tiles share one navigation path.
    if (onChat && typeof window.focusStep === "function") {
      $("shell-steps-h").hidden = false;
      renderSteps();
      $("shell-steps").addEventListener("click", (e) => {
        const b = e.target.closest("[data-step]");
        if (b) { window.focusStep(b.dataset.step); if (innerWidth < 900) setOpen(false); }
      });
    }
  }

  /** Paint the step list. Separate from buildSidebar (which runs once) because the
   *  sidebar is built before the first portal load, so whether this project has a
   *  Deposit step isn't known yet — app.js calls back here once it is.
   *
   *  app.js owns the deposit rule (required, or an invoice already issued) and
   *  publishes it on window; shell.js has no STATE of its own. Undefined keeps the
   *  item, matching the default-true column. The click handler is delegated on the
   *  container, so repainting the buttons never loses it. */
  function renderSteps() {
    const box = $("shell-steps");
    if (!box) return;
    const showDeposit = window.TW_DEPOSIT_APPLIES !== false;
    box.innerHTML = [
      ["proposal", "📄", "Proposal"], ["deposit", "💳", "Deposit"],
      ["contacts", "👤", "Contact info"], ["schedule", "📅", "Schedule"],
    ].filter(([k]) => k !== "deposit" || showDeposit).map(([k, i, l]) =>
      `<button class="shell-item" data-step="${k}"><span class="shell-ico">${i}</span><span>${l}</span></button>`
    ).join("");
  }
  window.TW_refreshShellSteps = renderSteps;

  // ── bell + toasts ──────────────────────────────────────────────────────────
  let ITEMS = [], OPEN = false;
  let toasted = load();
  function load() {
    try { return new Set(JSON.parse(localStorage.getItem("tw_portal_toasted") || "[]")); }
    catch { return new Set(); }
  }
  function save() {
    try { localStorage.setItem("tw_portal_toasted", JSON.stringify([...toasted].slice(-100))); } catch {}
  }
  const relTime = (iso) => {
    const t = Date.parse(iso); if (isNaN(t)) return "";
    let s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s < 60) return "just now";
    const m = Math.floor(s / 60); if (m < 60) return m + "m ago";
    const h = Math.floor(m / 60); if (h < 24) return h + "h ago";
    return Math.floor(h / 24) + "d ago";
  };

  function buildBell() {
    if ($("bell-panel")) return;
    const panel = document.createElement("div");
    panel.id = "bell-panel"; panel.hidden = true;
    panel.innerHTML = '<div class="bell-h"><span>Notifications</span>' +
      '<button class="shell-x" id="bell-close" title="Close">×</button></div>' +
      '<div class="bell-list" id="bell-list"><div class="bell-empty">Loading…</div></div>';
    document.body.appendChild(panel);
    const back = document.createElement("div");
    back.id = "bell-back"; back.hidden = true;
    document.body.appendChild(back);
    const toasts = document.createElement("div");
    toasts.id = "shell-toasts";
    document.body.appendChild(toasts);

    const close = () => { OPEN = false; panel.hidden = true; back.hidden = true; };
    $("bell").addEventListener("click", (e) => {
      e.stopPropagation();
      if (OPEN) return close();
      OPEN = true; panel.hidden = false; back.hidden = false;
      render();
      markSeen();
    });
    $("bell-close").addEventListener("click", close);
    back.addEventListener("click", close);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && OPEN) close(); });
  }

  function render() {
    const list = $("bell-list");
    if (!list) return;
    list.innerHTML = ITEMS.length
      ? ITEMS.map((n) =>
          `<a class="bell-item${n.unread ? " unread" : ""}" href="${esc(n.link || "#")}">
             <span>${esc(n.icon || "•")}</span>
             <span class="bell-main"><span class="bell-title">${esc(n.title || "")}</span>
             <span class="bell-body">${esc(n.body || "")}</span></span>
             <span class="bell-time">${esc(relTime(n.ts))}</span></a>`).join("")
      : '<div class="bell-empty">Nothing new right now.</div>';
  }

  function setBadge(n) {
    const b = $("bell-badge");
    if (!b) return;
    if (n > 0) { b.textContent = n > 99 ? "99+" : String(n); b.hidden = false; } else b.hidden = true;
  }

  function toast(n) {
    const wrap = $("shell-toasts");
    if (!wrap) return;
    const el = document.createElement("div");
    el.className = "shell-toast";
    el.innerHTML = `<span>${esc(n.icon || "🔔")}</span>
      <span class="bell-main"><span class="t">${esc(n.title || "")}</span>
      <span class="b">${esc(n.body || "")}</span></span>
      <button class="x" title="Dismiss">×</button>`;
    wrap.appendChild(el);
    requestAnimationFrame(() => el.classList.add("in"));
    let gone = false;
    const bye = () => { if (gone) return; gone = true; el.classList.remove("in"); setTimeout(() => el.remove(), 400); };
    el.querySelector(".x").addEventListener("click", (e) => { e.stopPropagation(); bye(); });
    el.addEventListener("click", () => { if (n.link) location.href = n.link; });
    setTimeout(bye, 10000);
  }

  async function markSeen() {
    try { await fetch("/api/me/notifications/seen", { method: "POST", credentials: "same-origin" }); } catch {}
    ITEMS = ITEMS.map((i) => ({ ...i, unread: false }));
    setBadge(0);
  }

  async function poll() {
    let j;
    try {
      const r = await fetch("/api/me/notifications", { credentials: "same-origin" });
      j = await r.json();
    } catch { return; }                       // offline — keep the last view
    if (!j || !j.ok || !j.authed) return;
    ITEMS = j.items || [];
    if (!OPEN) setBadge(j.unread || 0); else render();
    // Preview the newest unread ones. Capped and deduped so a backlog (or simply
    // navigating between pages) can't produce a storm.
    if (!OPEN) {
      const fresh = ITEMS.filter((i) => i.unread && !toasted.has(i.id));
      fresh.slice(0, 3).forEach(toast);
      fresh.forEach((i) => toasted.add(i.id));
      if (fresh.length) save();
    }
  }

  /** "Tell us what you need from this portal."
   *
   *  Hanz, 2026-08-13: "Here create a Feedback form for the customer of what queries or update
   *  they want from this system", in the sidebar.
   *
   *  A dialog rather than a page: this is a thing you do while looking at something else, and a
   *  navigation that loses the customer's place in their proposal to type two sentences is a
   *  navigation they abandon. The category is asked for because "how do I pay by check" and "the
   *  invoice button is broken" need different people to read them.
   *
   *  It does NOT post to the project chat — that would reach the estimator as if it were a
   *  question about their job. See the note on the portal_feedback table. */
  function openFeedback() {
    if ($("fb-back")) return;
    const back = document.createElement("div");
    back.id = "fb-back";
    back.innerHTML =
      `<div class="fb-card" role="dialog" aria-modal="true" aria-labelledby="fb-h">
         <div class="fb-head"><strong id="fb-h">Send feedback</strong>
           <button class="shell-x" id="fb-x" title="Close">✕</button></div>
         <p class="fb-lede">What would you like from this portal? A question about how something
            works, something you wish it did, or something that looks wrong — it all reaches the
            Treadwell team.</p>
         <label class="fb-lbl" for="fb-cat">This is…</label>
         <select id="fb-cat" class="fb-in">
           <option value="question">A question about how something works</option>
           <option value="request">Something I wish it did</option>
           <option value="problem">Something looks wrong or broken</option>
           <option value="other">Something else</option>
         </select>
         <label class="fb-lbl" for="fb-body">Your message</label>
         <textarea id="fb-body" class="fb-in" rows="5"
                   placeholder="Tell us in your own words…"></textarea>
         <p class="fb-err" id="fb-err" hidden></p>
         <div class="fb-foot">
           <button class="btn btn-secondary" id="fb-cancel" type="button">Cancel</button>
           <button class="btn btn-primary" id="fb-send" type="button">Send feedback</button>
         </div>
       </div>`;
    document.body.appendChild(back);
    const close = () => { back.remove(); document.removeEventListener("keydown", onKey); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    $("fb-x").addEventListener("click", close);
    $("fb-cancel").addEventListener("click", close);
    back.addEventListener("click", (e) => { if (e.target === back) close(); });
    setTimeout(() => { const t = $("fb-body"); if (t) t.focus(); }, 0);

    $("fb-send").addEventListener("click", async () => {
      const btn = $("fb-send"), err = $("fb-err");
      const text = ($("fb-body").value || "").trim();
      err.hidden = true;
      if (!text) { err.textContent = "Add a message first."; err.hidden = false; return; }
      btn.disabled = true; btn.textContent = "Sending…";
      try {
        const r = await fetch("/api/me/feedback", {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          // The TOKEN, not a proposal id — this page never learns the id. The server resolves
          // it through the same access check every other per-project route uses, so feedback
          // cannot be filed against a project this session may not see.
          body: JSON.stringify({ category: $("fb-cat").value, body: text,
                                 token: TOKEN || undefined }),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok || j.ok === false) throw new Error(j.error || ("HTTP " + r.status));
        back.querySelector(".fb-card").innerHTML =
          `<div class="fb-head"><strong>Thank you</strong>
             <button class="shell-x" id="fb-x2" title="Close">✕</button></div>
           <p class="fb-lede">That has reached the Treadwell team. If it needs a reply, somebody
              will come back to you by email.</p>
           <div class="fb-foot"><button class="btn btn-primary" id="fb-done" type="button">Close</button></div>`;
        $("fb-x2").addEventListener("click", close);
        $("fb-done").addEventListener("click", close);
      } catch (e) {
        btn.disabled = false; btn.textContent = "Send feedback";
        err.textContent = "That didn't send — check your connection and try again.";
        err.hidden = false;
      }
    });
  }

  function boot() {
    injectCss();
    buildSidebar();
    buildBell();
    fetch("/api/me/proposals", { credentials: "same-origin" })
      .then((r) => r.json())
      .then((d) => { if (d && d.email && $("shell-email")) $("shell-email").textContent = d.email; })
      .catch(() => {});
    poll();
    setInterval(poll, 60000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  }

  // NOTHING is built until the customer is signed in.
  //
  // Hanz, 2026-08-13: "For example a customer hasnt log in yet or doesnt get the OTP. it should
  // not show the sidebar this is for safety purposes."
  //
  // This file used to boot straight off DOMContentLoaded, with no notion of auth — and it loads
  // BEFORE app.js, which is the script that actually learns whether the session is valid. So
  // somebody sitting on the one-time-code screen was shown the whole project navigation (Chat,
  // My projects, Proposal, Deposit, Contact info, Schedule), the notification bell, and had
  // `/api/me/proposals` called on their behalf. The routes behind those links refuse an
  // unauthenticated caller, so nothing could be opened — but "My projects" on a login screen
  // still tells a stranger this address has projects, and a navigation you cannot use is a
  // promise the page has no business making before it knows who is reading it.
  //
  // app.js signals exactly once, from the one place that has the answer (`data.authed` on the
  // portal payload). The flag is checked as well as the event because shell.js loads first
  // today but load order is not something this file should depend on.
  let booted = false;
  function bootOnce() {
    if (booted) return;
    booted = true;
    boot();
  }
  window.TWShell = Object.assign(window.TWShell || {}, { mount: bootOnce });
  if (window.TW_PORTAL_AUTHED) bootOnce();
  else document.addEventListener("tw-portal-authed", bootOnce, { once: true });
})();
