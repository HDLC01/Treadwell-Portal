"use strict";
// Shared "your projects" logic, used by BOTH the login page (full list) and the
// in-portal header switcher. One implementation so the two can never disagree
// about statuses or ordering. External file (not inline) for the CSP.
(function () {
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /** The single most useful thing to say about where a project stands.
   *  Checked latest-stage-first so a scheduled job doesn't read "Approved". */
  function status(p) {
    if (p.schedule_status === "scheduled") return { label: "Scheduled", cls: "done" };
    if (p.deposit_status === "received") return { label: "Deposit received", cls: "done" };
    // Between paying and staff confirming it landed — telling them "Deposit due"
    // here reads as though their payment never arrived.
    if (p.deposit_status === "submitted") return { label: "Deposit sent", cls: "warn" };
    if (p.proposal_status === "approved") return { label: "Deposit due", cls: "warn" };
    if (p.proposal_status === "viewed") return { label: "Awaiting approval", cls: "warn" };
    return { label: "New proposal", cls: "pending" };
  }

  /** `.proj-row` anchors for a list of proposal cards. `currentToken` marks the
   *  one you're already looking at so the switcher can show it as current. */
  function rowsHtml(proposals, currentToken) {
    return (proposals || []).map((p) => {
      const st = status(p);
      const here = currentToken && p.token === currentToken;
      return `<a class="proj-row${here ? " current" : ""}" href="/p/${encodeURIComponent(p.token)}"` +
        `${here ? ' aria-current="page"' : ""}>` +
        `<span class="pname">${esc(p.project_name)}</span>` +
        `<span class="badge ${st.cls}">${st.label}</span></a>`;
    }).join("");
  }

  /** Resolves to the caller's project list ([] when signed out or on error). */
  function load() {
    return fetch("/api/me/proposals", { credentials: "same-origin" })
      .then((r) => r.json())
      .then((d) => (d && d.ok && Array.isArray(d.proposals) ? d.proposals : []))
      .catch(() => []);
  }

  window.TWProjects = { status, rowsHtml, load, esc };
})();
