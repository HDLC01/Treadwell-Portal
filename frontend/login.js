"use strict";
// Login page (/) controller. Uses TWLogin (auth.js). Kept external (not inline)
// so the Content-Security-Policy can forbid inline scripts.
(function () {
  const $ = (id) => document.getElementById(id);

  function handleProposals(proposals) {
    $("login-target").classList.add("hidden");
    if (!proposals || !proposals.length) { showEmpty(); return; }
    if (proposals.length === 1) { location.href = "/p/" + encodeURIComponent(proposals[0].token); return; }
    const list = $("project-list");
    list.classList.remove("hidden");
    list.innerHTML = '<div class="card"><h1>Your projects</h1>' +
      '<p class="muted">Choose a project to view.</p>' +
      TWProjects.rowsHtml(proposals) + "</div>";   // shared with the in-portal switcher
  }

  // Signed in, but nothing to open: every proposal on this email was deleted (Hanz, 2026-10-28).
  // A plain card rather than the login form again, which read as "sign-in failed".
  function showEmpty() {
    $("login-target").classList.add("hidden");
    const list = $("project-list");
    list.classList.remove("hidden");
    list.innerHTML = '<div class="card login-card" role="status"><h1>No proposals to show</h1>' +
      '<p class="muted">There are no proposals for this email right now. If you have questions, ' +
      "reply to the email or contact Treadwell.</p>" +
      '<button class="btn btn-secondary btn-block" type="button" id="empty-switch">Use a different email</button></div>';
    $("empty-switch").addEventListener("click", async () => {
      try { await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" }); } catch {}
      showLogin();
    });
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
      if (d && d.ok && d.authed && Array.isArray(d.proposals)) handleProposals(d.proposals);
      else showLogin();
    }).catch(() => { $("loading").classList.add("hidden"); showLogin(); });
})();
