"use strict";
// Login page (/) controller. Uses TWLogin (auth.js). Kept external (not inline)
// so the Content-Security-Policy can forbid inline scripts.
(function () {
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const $ = (id) => document.getElementById(id);

  function handleProposals(proposals) {
    $("login-target").classList.add("hidden");
    if (!proposals || !proposals.length) { showLogin(); return; }
    if (proposals.length === 1) { location.href = "/p/" + encodeURIComponent(proposals[0].token); return; }
    const list = $("project-list");
    list.classList.remove("hidden");
    list.innerHTML = '<div class="card"><h1>Your projects</h1>' +
      '<p class="muted">Choose a project to view.</p>' +
      proposals.map((p) => {
        const done = p.proposal_status === "approved";
        return `<a class="proj-row" href="/p/${encodeURIComponent(p.token)}">` +
          `<span class="pname">${esc(p.project_name)}</span>` +
          `<span class="badge ${done ? "done" : "warn"}">${done ? "Approved" : "Awaiting approval"}</span></a>`;
      }).join("") + "</div>";
  }

  function showLogin() {
    $("project-list").classList.add("hidden");
    const t = $("login-target");
    t.classList.remove("hidden");
    TWLogin.renderLogin(t, { onSuccess: handleProposals });
  }

  fetch("/api/me/proposals", { credentials: "same-origin" })
    .then((r) => r.json()).then((d) => {
      $("loading").classList.add("hidden");
      if (d && d.ok && d.proposals && d.proposals.length) handleProposals(d.proposals);
      else showLogin();
    }).catch(() => { $("loading").classList.add("hidden"); showLogin(); });
})();
