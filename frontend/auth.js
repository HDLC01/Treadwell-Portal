"use strict";
// Shared customer login (Google Sign-In + email one-time code).
// TWLogin.renderLogin(targetEl, { onSuccess(proposals) }) — builds the login UI
// into targetEl and calls onSuccess with the email's proposals once verified.
window.TWLogin = (function () {
  async function api(path, body) {
    const r = await fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      credentials: "same-origin", body: JSON.stringify(body || {}),
    });
    let d = {};
    try { d = await r.json(); } catch {}
    return { ok: r.ok && d.ok !== false, status: r.status, data: d };
  }

  const NO_PROJECT = "You don't have an existing project with this email.";

  function renderLogin(target, opts) {
    target.innerHTML = `
      <div class="card login-card">
        <h1>View your proposal</h1>
        <p class="muted">Sign in to see your Treadwell project — use the email your proposal was sent to.</p>
        <div id="lg-alert" role="alert" aria-live="polite"></div>
        <div id="lg-google" class="hidden" style="margin-bottom:12px"></div>
        <div id="lg-divider" class="divider hidden"><span>or use your email</span></div>
        <form id="lg-email-form" class="stack" novalidate>
          <div class="field">
            <label for="lg-email">Email</label>
            <input id="lg-email" type="email" autocomplete="email" inputmode="email" required placeholder="you@company.com">
          </div>
          <button class="btn btn-primary btn-block" type="submit" id="lg-send">Send my code</button>
        </form>
        <form id="lg-code-form" class="stack hidden" novalidate>
          <div class="field">
            <label for="lg-code">Enter the 6-digit code</label>
            <input id="lg-code" class="code-input" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="······">
            <p class="help">Check your inbox. <button type="button" id="lg-resend" class="linkbtn">Resend</button></p>
          </div>
          <button class="btn btn-primary btn-block" type="submit" id="lg-verify">View my proposal</button>
        </form>
      </div>`;

    const $ = (id) => target.querySelector("#" + id);
    const alert = (kind, msg) => { const a = $("lg-alert"); a.className = "alert " + kind; a.textContent = msg; a.classList.remove("hidden"); };
    const clearAlert = () => { const a = $("lg-alert"); a.textContent = ""; a.className = "hidden"; };
    let lastEmail = "";

    // Google Sign-In (only if a client id is configured)
    fetch("/api/public-config").then((r) => r.json()).then((cfg) => {
      if (!cfg || !cfg.google_client_id) return;
      $("lg-divider").classList.remove("hidden");
      const cont = $("lg-google"); cont.classList.remove("hidden");
      const init = () => {
        window.google.accounts.id.initialize({ client_id: cfg.google_client_id, callback: onGoogle });
        window.google.accounts.id.renderButton(cont, { theme: "outline", size: "large", text: "continue_with", width: 320 });
      };
      if (window.google && window.google.accounts) { init(); return; }
      const s = document.createElement("script");
      s.src = "https://accounts.google.com/gsi/client"; s.async = true; s.defer = true; s.onload = init;
      document.head.appendChild(s);
    }).catch(() => {});

    async function onGoogle(resp) {
      clearAlert();
      const { ok, data } = await api("/api/auth/google", { credential: resp.credential });
      if (!ok) {
        alert("error", data.error === "no_project"
          ? (data.email ? `${NO_PROJECT.slice(0, -1)} (${data.email}).` : NO_PROJECT)
          : (data.error || "Google sign-in failed."));
        return;
      }
      opts.onSuccess(data.proposals || []);
    }

    $("lg-email-form").addEventListener("submit", async (e) => {
      e.preventDefault(); clearAlert();
      const email = $("lg-email").value.trim();
      if (!email) { alert("error", "Enter your email."); return; }
      lastEmail = email;
      const b = $("lg-send"); b.disabled = true; b.innerHTML = '<span class="spinner"></span> Sending…';
      const { ok, data } = await api("/api/auth/request-code", { email });
      b.disabled = false; b.textContent = "Send my code";
      if (!ok) { alert("error", data.error === "no_project" ? NO_PROJECT : (data.error || "Could not send a code.")); return; }
      if (data.dev_code) { alert("info", `Staging — your code is ${data.dev_code}. (In production this is emailed.)`); $("lg-code").value = data.dev_code; }
      else { alert("info", "We emailed you a 6-digit code. Enter it below."); }
      $("lg-email-form").classList.add("hidden");
      $("lg-code-form").classList.remove("hidden");
      $("lg-code").focus();
    });

    $("lg-resend").addEventListener("click", async () => {
      const { data } = await api("/api/auth/request-code", { email: lastEmail });
      if (data && data.dev_code) { alert("info", `Staging — your code is ${data.dev_code}.`); $("lg-code").value = data.dev_code; }
      else { alert("info", "A new code was sent."); }
    });

    $("lg-code-form").addEventListener("submit", async (e) => {
      e.preventDefault(); clearAlert();
      const code = $("lg-code").value.trim();
      if (code.length < 6) { alert("error", "Enter the 6-digit code."); return; }
      const b = $("lg-verify"); b.disabled = true; b.innerHTML = '<span class="spinner"></span> Verifying…';
      const { ok, data } = await api("/api/auth/verify-code", { email: lastEmail, code });
      b.disabled = false; b.textContent = "View my proposal";
      if (!ok) { alert("error", data.error || "Incorrect code."); return; }
      opts.onSuccess(data.proposals || []);
    });
  }

  return { renderLogin };
})();
