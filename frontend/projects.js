"use strict";
// Shared "your projects" logic, used by BOTH the login page (full list) and the
// in-portal header switcher. One implementation so the two can never disagree
// about statuses or ordering. External file (not inline) for the CSP.
(function () {
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /** The single most useful thing to say about where ONE proposal stands.
   *  Checked latest-stage-first so a scheduled job doesn't read "Approved". */
  function proposalStatus(p) {
    if (p.schedule_status === "scheduled") return { label: "Scheduled", cls: "done" };
    if (p.deposit_status === "received") return { label: "Deposit received", cls: "done" };
    // Between paying and staff confirming it landed — telling them "Deposit due"
    // here reads as though their payment never arrived.
    if (p.deposit_status === "submitted") return { label: "Deposit sent", cls: "warn" };
    // "Deposit due" is wrong on a job that doesn't collect one. An issued invoice
    // still means due, even when the flag says otherwise — staff changed their mind.
    if (p.proposal_status === "approved" && p.deposit_required === false && !p.deposit_requested)
      return { label: "Approved", cls: "done" };
    if (p.proposal_status === "approved") return { label: "Deposit due", cls: "warn" };
    if (p.proposal_status === "viewed") return { label: "Awaiting approval", cls: "warn" };
    return { label: "New proposal", cls: "pending" };
  }

  /** Where a PROJECT stands. One row of the list is one project, and a project priced as several
   *  proposals (the server folds them into one card, `scopes` listing each) is approved one
   *  proposal at a time, so a single proposal's badge would misstate it. When they all agree, that
   *  is the answer; once some are approved, "1 of 3 approved"; when all are approved, whatever is
   *  still outstanding on one of them. */
  function status(p) {
    const scopes = (p && p.scopes) || [];
    if (scopes.length < 2) return proposalStatus(p);
    const each = scopes.map(proposalStatus);
    if (each.every((s) => s.label === each[0].label)) return each[0];
    const approved = scopes.filter((s) => s.proposal_status === "approved").length;
    if (approved === scopes.length) return each.find((s) => s.cls !== "done") || each[0];
    if (approved) return { label: `${approved} of ${scopes.length} approved`, cls: "warn" };
    return each.find((s) => s.label === "Awaiting approval") || each[0];
  }

  /** Is `token` this project (any of its proposals)? */
  function holds(p, token) {
    if (!p || !token) return false;
    return p.token === token || ((p.scopes || []).some((s) => s.token === token));
  }

  /** The project row for the page at `token`, or undefined. */
  function find(proposals, token) {
    return (proposals || []).find((p) => holds(p, token));
  }

  /** `.proj-row` anchors for a list of project cards. `currentToken` marks the
   *  one you're already looking at so the switcher can show it as current — on
   *  any proposal of a project priced as several. */
  function rowsHtml(proposals, currentToken) {
    return (proposals || []).map((p) => {
      const st = status(p);
      const here = holds(p, currentToken);
      // `display_name` is the project name: a project priced as several proposals is ONE row
      // (the server folds it), opening its first proposal, whose page has a tab for each.
      return `<a class="proj-row${here ? " current" : ""}"` +
        ` href="/p/${encodeURIComponent(p.token)}"` +
        `${here ? ' aria-current="page"' : ""}>` +
        `<span class="pname">${esc(p.display_name || p.project_name)}</span>` +
        `<span class="badge ${st.cls}">${esc(st.label)}</span></a>`;
    }).join("");
  }

  /** The raw `/api/me/proposals` answer, fetched ONCE per page. The proposal page asked for it
   *  twice on every load (the header switcher here, the sidebar's email in shell.js), one after the
   *  other in the same tick. Only a signed-in, successful answer is kept, so a failure or a
   *  signed-out answer is asked again by the next caller exactly as before. */
  let ME = null;
  function me() {
    if (!ME) {
      ME = fetch("/api/me/proposals", { credentials: "same-origin" })
        .then((r) => r.json())
        .then((d) => { if (!(d && d.ok && d.authed)) ME = null; return d || {}; })
        .catch(() => { ME = null; return {}; });
    }
    return ME;
  }

  /** Resolves to the caller's project list ([] when signed out or on error). */
  function load() {
    return me().then((d) => (d && d.ok && Array.isArray(d.proposals) ? d.proposals : []));
  }

  window.TWProjects = { status, rowsHtml, load, me, esc, find };
})();
