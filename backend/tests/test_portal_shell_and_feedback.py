"""Nothing shows before sign-in, the customer's own actions sit on their side, and they can
tell us what they need from the portal.

Three asks from Hanz on 2026-08-13, all against the live customer portal.

1. SAFETY — "For example a customer hasnt log in yet or doesnt get the OTP. it should not show
   the sidebar this is for safety purposes."

   `shell.js` booted off DOMContentLoaded with no notion of auth, and it loads BEFORE app.js —
   the only script that learns whether the session is valid. So somebody sitting on the
   one-time-code screen was shown the whole project navigation (Chat, My projects, Proposal,
   Deposit, Contact info, Schedule), the notification bell, and had `/api/me/proposals` called
   for them. The routes behind those links refuse an unauthenticated caller, so nothing could be
   opened — but "My projects" on a login screen still tells a stranger this address has projects,
   and a navigation you cannot use is a promise the page has no business making before it knows
   who is reading it.

   The signal lives INSIDE `renderPortal` rather than at its nine call sites. renderPortal is the
   definition of "we hold an authenticated view" — including the path where the code was just
   typed — and one signal in one place cannot be the one somebody forgets next time.

2. SIDES — "If its the customer's action and chat it should appear to the right Opposite of what
   it looks like to the chatbox in the proposal tool."

   Speech bubbles were already right. Every EVENT card was flush left, so a customer saw their
   own approval and their own deposit filed on Treadwell's side of their own thread while their
   typed messages sat correctly on the right — the thread contradicted itself about who had done
   what. The classification is copied from the staff drawer's `sideOf`; only the sides swap.

3. FEEDBACK — "Here create a Feedback form for the customer of what queries or update they want
   from this system", in the sidebar. Stored in `portal_feedback` and emailed, deliberately NOT
   posted to the project chat: a request about the software is not a question about a job, and in
   the thread it would reach the estimator as though it were, then vanish with the project.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import main

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
SHELL = (FRONTEND / "shell.js").read_text(encoding="utf-8")
APP = (FRONTEND / "app.js").read_text(encoding="utf-8")
CSS = (FRONTEND / "styles.css").read_text(encoding="utf-8")

client = TestClient(main.app)
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


# ── 1. the sidebar must not exist before sign-in ─────────────────────────────
def test_the_shell_does_not_build_itself_on_page_load():
    """The regression, stated as the mechanism: booting off DOMContentLoaded is what showed the
    navigation to somebody who had not signed in."""
    assert not re.search(r'addEventListener\("DOMContentLoaded",\s*boot\)', SHELL), (
        "shell.js boots on DOMContentLoaded again, so the sidebar renders before auth")
    assert "TW_PORTAL_AUTHED" in SHELL, "the shell no longer waits for an auth signal"
    assert 'addEventListener("tw-portal-authed"' in SHELL


def _statements(src):
    """Source with `//` comment lines dropped. A commented-out call still contains its own text,
    so a raw substring check passes on the very mutation it exists to catch."""
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))


def test_the_signal_comes_from_inside_renderPortal():
    """Not from its call sites. There are nine ways into the authenticated portal — first load,
    just-entered code, project switch, post-approval refresh — and a signal per call site is a
    signal somebody forgets."""
    i = APP.index("function renderPortal(vm)")
    body = _statements(APP[i:i + 900])
    assert "signalAuthed();" in body, "renderPortal does not signal that the session is valid"
    # First statement: anything before it runs for an authenticated view anyway, but putting the
    # signal late invites a return path that skips it.
    assert body.index("signalAuthed();") < body.index("SHOWN_REVISION"), (
        "the signal is not the first thing renderPortal does")


def test_the_gate_paths_do_not_signal():
    """The three unauthenticated outcomes — no such link, wrong account, needs a code — must all
    leave the shell unbuilt."""
    for fname in ("renderGate", "renderNotFound", "renderWrongAccount"):
        i = APP.index("function %s(" % fname)
        body = APP[i:APP.index("\nfunction ", i + 10)]
        assert "signalAuthed" not in body, "%s signals the shell to build" % fname


def test_the_shell_mounts_only_once_however_it_is_told():
    """renderPortal runs on every poll and project switch. A second mount would append a second
    sidebar and a second bell."""
    assert "if (booted) return;" in SHELL, "bootOnce is not idempotent"


@needs_node
def test_the_shell_really_stays_empty_until_signalled():
    """Executed, because the claim is what the DOM contains. A `TW_PORTAL_AUTHED` reference in
    the source proves nothing about whether the sidebar was appended."""
    script = r"""
    const fs = require("fs");
    const body = [];                       // what the shell put on the PAGE
    const all = [];                        // ...anywhere, including inside the header
    const el = () => ({ id:"", innerHTML:"", textContent:"", style:{}, dataset:{}, hidden:false,
      value:"", classList:{add(){},remove(){},toggle(){}}, setAttribute(){},
      appendChild(n){ all.push(n); }, insertBefore(n){ all.push(n); },
      addEventListener(){}, querySelector:()=>null, querySelectorAll:()=>[],
      remove(){}, focus(){} });
    // Real getElementById semantics: nothing resolves until it has been appended, so
    // buildSidebar's `if ($("shell-side")) return;` guard behaves as it does in a browser.
    const byId = (id) => all.find((n) => n.id === id)
      || (all.some((n) => (n.innerHTML || "").includes('id="' + id + '"')) ? el() : null);
    const header = el();                   // every portal page has the sticky .site-header
    const doc = { getElementById: byId, createElement: el, addEventListener(){},
      querySelector: (s) => (s === ".site-header" ? header : null),
      documentElement:{classList:{toggle(){},add(){},remove(){}}},
      body:{ appendChild(n){ body.push(n); all.push(n); },
             firstChild:null, insertBefore(n){ body.push(n); all.push(n); } },
      head:{ appendChild(){} }, readyState:"complete" };
    let fetched = [];
    const win = { addEventListener(){}, location:{ pathname:"/p/tok123", href:"x" },
      setInterval(){}, setTimeout(){},
      fetch: (u) => { fetched.push(u); return Promise.resolve({ json: async () => ({}) }); },
      localStorage:{ getItem:()=>null, setItem(){} } };
    const src = fs.readFileSync(process.argv[1], "utf8");
    new Function("window","document","localStorage","fetch","setInterval","setTimeout","location",src)(
      win, doc, win.localStorage, win.fetch, win.setInterval, win.setTimeout, win.location);
    const before = { nodes: body.length, calls: fetched.length };
    win.TWShell.mount();
    win.TWShell.mount();                   // a second signal must not build a second sidebar
    const html = all.map((n) => n.innerHTML || "").join("");
    console.log(JSON.stringify({ before, nodes: body.length,
      sides: all.filter((n) => n.id === "shell-side").length,
      calls: fetched.length, projects: /My projects/.test(html) }));
    """
    proc = subprocess.run(["node", "-e", script, "--", str(FRONTEND / "shell.js")],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["before"]["nodes"] == 0, (
        "the shell appended %s nodes before anybody said the session was valid"
        % got["before"]["nodes"])
    assert got["before"]["calls"] == 0, (
        "the shell called the API on behalf of somebody who has not signed in")
    assert got["nodes"] > 0 and got["projects"] is True, (
        "the shell built no navigation even after being signalled")
    assert got["sides"] == 1, "two signals built %s sidebars" % got["sides"]


# ── 2. event cards take the side of whoever caused them ──────────────────────
@needs_node
def test_the_customers_own_actions_sit_on_the_customers_side():
    """Run the real `cardSide` over the cards from his screenshot. Approval, contacts and the
    deposit are things the CUSTOMER did; "Deposit received" and a staff reply are not."""
    script = r"""
    const fs = require("fs");
    const src = fs.readFileSync(process.argv[1], "utf8");
    const ce = /const CUSTOMER_EVENTS = \[[^\]]*\];/.exec(src)[0];
    const fn = /function cardSide\(m\) \{[\s\S]*?\n\}/.exec(src)[0];
    const cardSide = new Function(ce + "\n" + fn + "\nreturn cardSide;")();
    const cases = {
      approved:   cardSide({ body:"Approved by HANZ URIEL A DE LA CRUZ", msg_type:"system", author_kind:"staff" }),
      contacts:   cardSide({ body:"Project contacts received (2): HANZ, Uriel.", msg_type:"system", author_kind:"staff" }),
      depositSub: cardSide({ body:"Deposit initiated | ACH details provided", msg_type:"deposit_submitted", author_kind:"customer" }),
      depositRec: cardSide({ body:"Deposit received | thank you!", msg_type:"system", author_kind:"staff" }),
      staffNote:  cardSide({ body:"Update | Project delayed", msg_type:"system", author_kind:"staff" }),
      reworded:   cardSide({ body:"Customer approved the proposal", msg_type:"system", author_kind:"staff" }),
    };
    console.log(JSON.stringify(cases));
    """
    proc = subprocess.run(["node", "-e", script, "--", str(FRONTEND / "app.js")],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["approved"] == "mine", got
    assert got["contacts"] == "mine", got
    assert got["depositSub"] == "mine", got
    assert got["depositRec"] == "theirs", got
    assert got["staffNote"] == "theirs", got
    # Rewording a system line in the backend must degrade to author_kind, never crash or flip
    # every card. Stated in the comment; asserted here.
    assert got["reworded"] == "theirs", got


def test_the_classification_matches_the_staff_tool_word_for_word():
    """One conversation, two views. A card that changes sides between them is worse than one that
    never moves, so the prefix list is copied rather than paraphrased."""
    tool = (pathlib.Path(__file__).resolve().parents[3] / "treadwell-proposal-tool"
            / "frontend" / "js" / "portal.js")
    if not tool.exists():
        pytest.skip("the staff tool is not checked out beside the portal")
    mine = re.search(r"const CUSTOMER_EVENTS = (\[[^\]]*\]);", APP).group(1)
    theirs = re.search(r"const CUSTOMER_EVENTS = (\[[^\]]*\]);",
                       tool.read_text(encoding="utf-8")).group(1)
    assert mine == theirs, "the two views classify event cards differently: %s vs %s" % (mine, theirs)


def test_both_sides_are_styled_and_the_accent_follows():
    """Without a rule for `.mine` the class is inert and the card stays left — the bug, with
    extra markup. Read the rule BODY, never a character window: a window sweeps in whichever
    rule happens to sit next door and passes on somebody else's declarations."""
    def rule(sel):
        m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", CSS)
        assert m, "no CSS rule for %s — the class is inert" % sel
        return m.group(1)

    mine = rule(".chat-card.mine")
    assert "flex-end" in mine, mine
    # The stripe has to move too, or a right-aligned card points its accent the wrong way.
    assert "border-right" in mine, mine
    assert "flex-start" in rule(".chat-card.theirs")


# ── 3. the feedback form ─────────────────────────────────────────────────────
def test_feedback_is_in_the_sidebar_and_opens_a_dialog():
    assert 'id="shell-fb"' in SHELL, "there is no feedback item in the sidebar"
    assert "Send feedback" in SHELL
    assert "function openFeedback()" in SHELL
    assert 'back.id = "fb-back"' in SHELL, "the dialog has no scrim/root"


def test_the_form_asks_what_kind_of_feedback_it_is():
    """"How do I pay by check" and "the invoice button is broken" need different people to read
    them, so the category is asked rather than guessed."""
    for value in ("question", "request", "problem", "other"):
        assert 'value="%s"' % value in SHELL, value
    assert set(main.FEEDBACK_CATEGORIES) == {"question", "request", "problem", "other"}


def _wire(monkeypatch, email="cust@acme.com", saved=None, notify=None):
    monkeypatch.setattr(main, "_session_email", lambda request: email)
    monkeypatch.setattr(main.db, "add_feedback",
                        lambda *a, **k: (saved.append((a, k)) if saved is not None else None)
                        or {"id": 11, "created_at": None})
    monkeypatch.setattr(main.email_sender, "notify_team",
                        lambda subject, body, **kw:
                        (notify.append((subject, body, kw)) if notify is not None else None) or True)


def test_signed_in_feedback_is_saved_and_emailed(monkeypatch):
    saved, notify = [], []
    _wire(monkeypatch, saved=saved, notify=notify)
    r = client.post("/api/me/feedback",
                    json={"category": "request", "body": "Let me download all invoices at once."})
    assert r.status_code == 200, r.text
    assert saved, "the feedback was not stored"
    args = saved[0][0]
    assert args[0] == "cust@acme.com" and args[1] == "request"
    assert "download all invoices" in args[2]
    assert notify, "nobody was emailed about the feedback"
    assert "cust@acme.com" in notify[0][0], notify[0][0]


def test_the_email_address_comes_from_the_SESSION_not_the_body(monkeypatch):
    """Feedback anybody could post under anybody's name is feedback nobody can act on."""
    saved = []
    _wire(monkeypatch, email="real@acme.com", saved=saved)
    client.post("/api/me/feedback",
                json={"category": "other", "body": "hi", "email": "spoofed@evil.com"})
    assert saved[0][0][0] == "real@acme.com", saved


def test_a_stranger_cannot_post_feedback(monkeypatch):
    """An open POST on a public host is a spam relay."""
    monkeypatch.setattr(main, "_session_email", lambda request: None)
    r = client.post("/api/me/feedback", json={"category": "other", "body": "hi"})
    assert r.status_code == 401


def test_an_unknown_category_is_refused(monkeypatch):
    _wire(monkeypatch)
    r = client.post("/api/me/feedback", json={"category": "sql; drop", "body": "hi"})
    assert r.status_code == 400 and r.json()["error"] == "invalid_category"


def test_an_empty_message_is_refused(monkeypatch):
    _wire(monkeypatch)
    for body in ("", "   ", None):
        r = client.post("/api/me/feedback", json={"category": "other", "body": body})
        assert r.status_code == 400, body


def test_a_failed_SAVE_tells_the_customer(monkeypatch):
    """Their words are gone, so they must know to keep them."""
    monkeypatch.setattr(main, "_session_email", lambda request: "c@x.com")
    monkeypatch.setattr(main.db, "add_feedback", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no table")))
    r = client.post("/api/me/feedback", json={"category": "other", "body": "hi"})
    assert r.status_code == 500 and r.json()["error"] == "save_failed"


def test_a_failed_EMAIL_does_not(monkeypatch):
    """Already saved. Reporting failure here would invite a duplicate."""
    monkeypatch.setattr(main, "_session_email", lambda request: "c@x.com")
    monkeypatch.setattr(main.db, "add_feedback", lambda *a, **k: {"id": 1})
    monkeypatch.setattr(main.email_sender, "notify_team",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("resend down")))
    r = client.post("/api/me/feedback", json={"category": "problem", "body": "broken"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_project_context_is_taken_from_the_token_not_the_body(monkeypatch):
    """The page never learns a proposal id, and a client-supplied one would let any signed-in
    customer file feedback against somebody else's job."""
    saved = []
    _wire(monkeypatch, saved=saved)
    monkeypatch.setattr(main, "_require", lambda request, token:
                        {"proposal_id": "pid-real"} if token == "goodtok" else None)
    client.post("/api/me/feedback",
                json={"category": "question", "body": "q", "token": "goodtok"})
    assert saved[-1][0][3] == "pid-real", saved[-1]
    # A token this session may not see costs the CONTEXT, never the feedback.
    client.post("/api/me/feedback",
                json={"category": "question", "body": "q", "token": "someone-elses"})
    assert saved[-1][0][3] is None, saved[-1]


def test_feedback_never_becomes_a_project_chat_message(monkeypatch):
    """The thread is for the job. A feature request landing there reaches the estimator as if it
    were a question about their proposal, and disappears when the project closes."""
    calls = []
    _wire(monkeypatch)
    monkeypatch.setattr(main.db, "add_message", lambda *a, **k: calls.append(a) or {"id": 9})
    monkeypatch.setattr(main.db, "add_question", lambda *a, **k: calls.append(a) or {"id": 9})
    client.post("/api/me/feedback", json={"category": "request", "body": "dark mode please"})
    assert calls == [], "feedback was written into the project conversation"


def test_the_table_is_declared_and_survives_a_deleted_proposal():
    """proposal_id is context, not ownership: deleting a job must not erase what somebody told us
    about the software, so it is deliberately not a cascading foreign key."""
    schema = (pathlib.Path(__file__).resolve().parents[1] / "schema.sql").read_text(encoding="utf-8")
    i = schema.index("create table if not exists public.portal_feedback")
    block = schema[i:schema.index(");", i)]
    assert "proposal_id  text" in block
    assert "references" not in block, (
        "portal_feedback cascades from portal_proposals, so closing a job deletes its feedback")
    assert "check (category in ('question','request','problem','other'))" in block
