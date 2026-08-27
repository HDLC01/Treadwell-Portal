"""The shut drawer must be unable to take a tap, and a phone must never inherit a desktop's rail.

Hanz, 2026-08-27, after a browser walk of the staff tool at 375px: "Sidebar makes every page
unusable at phone width. redesign the phone UI for both Portal and proposals."

WHAT WAS FOUND ON THE OTHER SIDE, and why this file exists even though the portal was not the
reported failure. The staff tool's `#tw-sidebar` had the same restore-at-any-width flag with an OPEN
default at desktop, so one visit on a laptop wrote it and every later phone visit inherited a 240px
rail across 64% of a 375px screen; clicking anything timed out with the drawer's own nav link named
as the element intercepting pointer events. This shell defaults CLOSED, so it could not go that
wrong — but it was the same two mistakes:

  * `setOpen(saved === "1")` read a viewport-agnostic flag, so a customer who opened the menu on a
    laptop handed their own phone an open drawer over the proposal they came to read; and
  * the shut drawer was only `transform: translateX(-100%)`. Off-screen is not inert. transform
    animates, so mid-slide the sheet is over the page, and its six links stay in the tab order the
    whole time it is shut — a keyboard or a screen reader walks a menu nobody can see.

WHY THIS RESOLVES THE CASCADE INSTEAD OF GREPPING. `#shell-side` is styled by two rules whose
declarations disagree, plus a third inside a media query, and which one wins is the entire question.
A regex for "visibility:hidden" passes just as happily on a stylesheet where a heavier rule later
puts it back. So the checks below collect the declarations that actually apply at a given viewport
width and open/shut state, order them by (important, specificity, source position) the way a browser
does, and read the winner.

The resolver refuses to skip a rule it cannot parse (test_the_resolver_is_not_quietly_blind): a
detector that silently matches nothing is worse than no detector.
"""
import pathlib
import re

import pytest

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
SHELL_JS = FRONTEND / "shell.js"
STYLES = FRONTEND / "styles.css"

SIDE = "shell-side"
SCRIM = "shell-back"
OPEN_CLASS = "shell-open"
# The portal's drawer breakpoint is 900px, not the tool's 768: its reading column is 760px wide, so
# a rail beside it needs more room before it stops squeezing the proposal.
DESKTOP_MIN = 900
PHONE_WIDTHS = (320, 360, 375, 414, 899)

# Where each piece of chrome really sits, as (ancestor id, ancestor classes) pairs. The resolver
# needs a COMPLETE ancestor description to answer a descendant selector exactly rather than guess:
# `.fb-head .shell-x` is the feedback dialog's close button, not the drawer's, and must not be read
# as a rule about the drawer's.
IN_DRAWER = (("shell-side", ()),)
IN_BRAND = (("", ("shell-brand",)),) + IN_DRAWER
IN_HEADER = (("", ("site-header",)),)


def shell_css(src=None):
    """The stylesheet shell.js injects. It has no file of its own; this is where it lives."""
    src = src if src is not None else SHELL_JS.read_text(encoding="utf-8")
    start = src.index("s.textContent = `")
    end = src.index("`;", start + 17)
    return src[start + 17:end]


def _strip_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def rules(css):
    """[(selector, declarations, media_condition, order)] with @media conditions KEPT.

    Flattening @media away would make every assertion here meaningless: `html.shell-open
    #shell-back { display: none }` lives inside `@media (min-width:900px)`, and a parser that loses
    the condition reports the scrim as permanently gone and passes for the wrong reason.
    """
    css = _strip_comments(css)
    out, order, stack, buf, i, n = [], 0, [], "", 0, len(css)
    while i < n:
        ch = css[i]
        if ch == "{":
            head, buf = buf.strip(), ""
            if head.startswith("@media"):
                stack.append(head[len("@media"):].strip())
                i += 1
                continue
            if head.startswith("@"):
                i = _matching_brace(css, i) + 1        # @keyframes, @import: skip the body whole
                continue
            close = css.find("}", i)
            assert close != -1, "unbalanced braces after %r" % head[:60]
            if head:
                out.append((head, css[i + 1:close], " and ".join(stack), order))
                order += 1
            i = close + 1
            continue
        if ch == "}":
            # Only ever a media block's closing brace: a declaration block's was consumed above.
            if stack:
                stack.pop()
            buf, i = "", i + 1
            continue
        buf += ch
        i += 1
    return out


def _matching_brace(css, open_at):
    depth = 0
    for j in range(open_at, len(css)):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    raise AssertionError("unbalanced braces in the stylesheet")


_WIDTH = re.compile(r"\((min|max)-width\s*:\s*(\d+)px\)")


def media_applies(cond, width):
    """True/False for a width query; None for a condition this resolver cannot evaluate."""
    if not cond.strip():
        return True
    verdict = True
    for p in (x.strip() for x in cond.replace("and", "&").split("&") if x.strip()):
        m = _WIDTH.fullmatch(p)
        if not m:
            return None
        n = int(m.group(2))
        verdict = verdict and (width >= n if m.group(1) == "min" else width <= n)
    return verdict


def specificity(sel):
    ids = len(re.findall(r"#[\w-]+", sel))
    classes = len(re.findall(r"\.[\w-]+|\[[^\]]+\]|:[\w-]+(?!\()", sel))
    types = len(re.findall(r"(?:^|[\s>+~])([a-z][\w-]*)", sel))
    return (ids, classes, types)


_COMPOUND = re.compile(r"([#.]?)([\w-]+)|(:[\w-]+)|(\*)")


def _compound_matches(one, el_id, el_classes):
    """Does one COMPOUND simple selector (`#id.class`, `.a.b`, `div`) match the element?

    A compound, not a single token. Comparing `one == "#" + el_id` makes every `#id.class` rule
    invisible, and in the staff tool's copy of this resolver that blind spot let a mutation survive:
    the in-row burger is styled `#tw-burger.tw-burger-inline` at (1,1,0), so a phone rule written as
    `#tw-burger` alone is outweighed by it and the resolver could not see the loss.
    """
    if ":" in one and not one.startswith(":"):
        one = one[:one.index(":")]              # a state pseudo-class; the element can be in it
    ids, classes, types, pos = [], [], [], 0
    for m in _COMPOUND.finditer(one):
        if m.start() != pos:
            raise ValueError(one)
        pos = m.end()
        if m.group(3) or m.group(4):
            continue                            # a pseudo-class (handled above) or `*`
        (ids if m.group(1) == "#" else classes if m.group(1) == "." else types).append(m.group(2))
    if pos != len(one) or len(ids) > 1:
        raise ValueError(one)
    if ids and ids[0] != el_id:
        return False
    if any(c not in el_classes for c in classes):
        return False
    if types:
        return False                            # nothing in this stylesheet types the drawer
    return bool(ids or classes)


def _selector_matches(sel, el_id, el_classes, root_open, ancestors=()):
    """Match one descendant selector against the element, or raise if it cannot be PARSED.

    `ancestors` is a COMPLETE description of the ids and classes on the element's ancestors, so a
    descendant selector is answered exactly rather than guessed at or refused. That completeness is
    the contract: a compound naming something not in the set genuinely does not match. The earlier
    version raised on any descendant selector whose last compound matched, which is honest but
    useless - `.fb-head .shell-x` sets no size and still stopped the resolver dead.
    """
    sel = sel.strip()
    if any(c in sel for c in ">+~") or "::" in sel:
        raise ValueError(sel)
    parts = sel.split()
    if not parts:
        raise ValueError(sel)
    if parts[0].startswith("html"):
        rest = parts[0][len("html"):]
        if rest and rest != "." + OPEN_CLASS:
            raise ValueError(sel)
        if rest and not root_open:
            return False
        parts = parts[1:]
        if not parts:
            return False                        # a rule about <html> itself, not this element
    if not parts:
        return False
    if not _compound_matches(parts[-1], el_id, el_classes):
        # Parse the ancestor compounds anyway, so an unreadable one is still reported.
        for p in parts[:-1]:
            _compound_matches(p, "", ())
        return False
    for p in parts[:-1]:
        if not any(_compound_matches(p, a_id, a_cls) for a_id, a_cls in ancestors):
            return False
    return True


def resolve(css, prop, el_id, width, root_open, el_classes=(), ancestors=()):
    """The declaration a browser would use for `prop` on this element, or None."""
    winner, best = None, None
    for sel, body, cond, order in rules(css):
        applies = media_applies(cond, width)
        assert applies is not None, (
            "the resolver cannot evaluate `@media %s`, so it would silently ignore every rule "
            "inside it" % cond)
        if not applies:
            continue
        for one in (s.strip() for s in sel.split(",")):
            if not _selector_matches(one, el_id, el_classes, root_open, ancestors):
                continue
            for decl in body.split(";"):
                if ":" not in decl:
                    continue
                name, _, value = decl.partition(":")
                if name.strip() != prop:
                    continue
                key = ("!important" in value, specificity(one), order)
                if best is None or key > best:
                    best, winner = key, value.replace("!important", "").strip()
    return winner


# ── the drawer ────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("width", PHONE_WIDTHS + (1024, 1440))
def test_a_shut_drawer_cannot_be_the_click_target(width):
    """`visibility: hidden` takes the subtree out of hit-testing AND out of the tab order;
    `pointer-events: none` says the first half again in a property that reads plainly.

    Both are required. Either alone would do for the pointer, but only visibility keeps a keyboard
    out of a menu nobody can see, and only pointer-events states the intent in a form a reader of
    this stylesheet cannot mistake. Checked at desktop widths too: the rail is shut there whenever
    the customer has collapsed it.
    """
    css = shell_css()
    vis = resolve(css, "visibility", SIDE, width, root_open=False)
    pe = resolve(css, "pointer-events", SIDE, width, root_open=False)
    assert vis == "hidden", (
        "at %dpx a SHUT #%s resolves visibility:%s. Off-screen is not inert: transform animates, so "
        "mid-slide the sheet is over the proposal, and its links never leave the tab order."
        % (width, SIDE, vis))
    assert pe == "none", (
        "at %dpx a SHUT #%s resolves pointer-events:%s, so it can still take a tap meant for the "
        "page underneath" % (width, SIDE, pe))


@pytest.mark.parametrize("width", PHONE_WIDTHS + (1024, 1440))
def test_an_open_drawer_is_visible_and_clickable_again(width):
    """The other half. A test that only asserts the shut state passes on a drawer that never opens."""
    css = shell_css()
    assert resolve(css, "visibility", SIDE, width, root_open=True) == "visible"
    assert resolve(css, "pointer-events", SIDE, width, root_open=True) == "auto"
    assert resolve(css, "transform", SIDE, width, root_open=True) == "translateX(0)"


@pytest.mark.parametrize("width", PHONE_WIDTHS)
def test_the_shut_scrim_is_the_other_half_of_the_guarantee(width):
    """A full-viewport scrim is the one element that can eat every tap on the page by itself.

    `display:none` was already there; `pointer-events:none` is added because display is the property
    most likely to be put back by something else, and the scrim has no business taking a click in a
    state it is not visible in.
    """
    css = shell_css()
    assert resolve(css, "display", SCRIM, width, root_open=False) == "none"
    assert resolve(css, "pointer-events", SCRIM, width, root_open=False) == "none"
    # And it is a real scrim when the drawer is open on a phone, or tapping away does nothing.
    assert resolve(css, "display", SCRIM, width, root_open=True) == "block"
    assert resolve(css, "pointer-events", SCRIM, width, root_open=True) == "auto"


def test_the_desktop_rail_is_not_a_scrim():
    """At >=900px the drawer sits BESIDE the reading column (body takes margin-left), so a scrim
    there would dim and block a proposal the customer is still reading."""
    assert resolve(shell_css(), "display", SCRIM, 1440, root_open=True) == "none"


@pytest.mark.parametrize("width", PHONE_WIDTHS)
def test_the_chrome_is_reachable_with_a_thumb(width):
    """44px is Apple's floor and 48 is Material's. A menu row is a full-width target either way, so
    it takes the larger; the lone glyphs take 44.

    The burger shipped 54x34 and the bell was a 17px glyph in 5px of padding — both under either
    floor, on the app most of these customers only ever open on a phone. Desktop sizes are
    deliberately untouched.
    """
    css = shell_css()

    def px(prop, el_id="", classes=(), anc=()):
        v = resolve(css, prop, el_id, width, root_open=True, el_classes=classes, ancestors=anc)
        return float(re.sub(r"[^\d.]", "", v)) if v and v.endswith("px") else None

    row = px("min-height", classes=("shell-item",), anc=IN_DRAWER)
    assert row and row >= 48, "a menu row resolves min-height %s at %dpx" % (row, width)
    assert px("height", el_id="shell-burger", anc=IN_HEADER) >= 44, (
        "the burger is still under the touch floor")
    for cls, anc in (("shell-x", IN_BRAND), ("bell", IN_HEADER)):
        h = px("min-height", classes=(cls,), anc=anc)
        assert h and h >= 44, ".%s resolves min-height %s at %dpx" % (cls, h, width)


def test_the_sheet_leaves_a_strip_of_the_page_showing():
    """It is a modal sheet on a phone, not a rail. 240px of 375 truncates "Send feedback" and the
    signed-in address in the footer, on the one screen where the label is all there is — so the
    phone width goes UP, but bounded, or a full-bleed drawer reads as having navigated away."""
    css = shell_css()
    for width in PHONE_WIDTHS:
        v = resolve(css, "width", SIDE, width, root_open=True)
        assert v and v.startswith("min("), (
            "#%s resolves width %r at %dpx; a phone sheet needs a bounded fluid width"
            % (SIDE, v, width))


# ── the JS half: who decides the starting state ───────────────────────────────────────────────
def _build_sidebar(src=None):
    src = src if src is not None else SHELL_JS.read_text(encoding="utf-8")
    i = src.index("function buildSidebar(")
    j = src.index("\n  /** Paint the step list.", i)
    return src[i:j]


def test_the_remembered_flag_is_never_read_or_written_at_phone_width():
    """The defect, banned by shape rather than by the one line it appeared on.

    Any `setItem("tw_portal_nav", ...)` outside a width guard puts it back: a phone toggle writes the
    flag and the next load reads it, on either device.
    """
    body = _build_sidebar()
    writes = list(re.finditer(r'localStorage\.setItem\(\s*"tw_portal_nav"', body))
    assert writes, "nothing writes tw_portal_nav any more, so this test grades nothing"
    for m in writes:
        line = body[body.rfind("\n", 0, m.start()) + 1:body.find("\n", m.start())]
        assert re.search(r"if\s*\(\s*wide\(\)\s*\)", line), (
            "tw_portal_nav is written outside a wide() guard:\n  %s" % line.strip())
    restore = re.search(r"setOpen\(([^;]*?)\);", body)
    assert restore, "buildSidebar no longer sets an initial state"
    assert "wide()" in restore.group(1), (
        "the initial state is decided without asking the width: setOpen(%s)"
        % restore.group(1).strip())


def test_the_width_is_asked_every_time_not_once_at_mount():
    """A one-shot boolean answers for whichever the page was when the shell was built. The same page
    is a phone in portrait and a small tablet in landscape."""
    body = _build_sidebar()
    assert re.search(r"const wide = \(\) =>", body), (
        "wide is a one-shot boolean again, so a rotate leaves a rail across a phone")
    assert re.search(r"mql\.(addEventListener|addListener)", body), (
        "nothing listens for the viewport crossing %dpx" % DESKTOP_MIN)
    assert re.search(r"if \(!wide\(\)\) setOpen\(false\)", body), (
        "the width listener does not shut the drawer on the way down")
    assert ("(min-width: %dpx)" % DESKTOP_MIN) in body, (
        "the JS breakpoint no longer matches the stylesheet's %dpx, so the drawer can be modal in "
        "CSS and persistent in JS at the same width" % DESKTOP_MIN)


def test_the_drawer_is_escapable_and_gives_the_focus_back():
    """Open, close, tap-away and Escape all have to work, and the caret must not be left inside a
    subtree the CSS has just made invisible.

    Escape is gated on !wide() on purpose: at >=900px the drawer is a rail, and closing it on Escape
    would fight the feedback dialog and the PDF popup layered above it — both of which already own
    Escape, and both of which sit in this app's own 700-950 z-index band for the same reason.
    """
    body = _build_sidebar()
    esc = re.search(r'e\.key === "Escape"[^\n]*', body)
    assert esc, "Escape does not close the drawer"
    assert "!wide()" in esc.group(0), (
        "Escape closes the desktop rail too, which fights the dialogs above it: %s"
        % esc.group(0).strip())
    assert "b.focus()" in body, "focus is never returned to the burger when the drawer closes"
    assert "x.focus()" in body, "focus never enters the drawer when it opens"
    assert re.search(r'setAttribute\("aria-expanded", open \? "true" : "false"\)', body), (
        "the burger never says whether the drawer is open")
    assert re.search(r'setAttribute\("aria-hidden", open \? "false" : "true"\)', body), (
        "the assistive tree does not follow the cascade, so a screen reader still walks a menu the "
        "CSS has made inert")


def test_the_burger_states_its_position_even_though_it_is_built_after_setOpen():
    """The drawer is built before the header button exists, so the first setOpen cannot stamp it.

    Without the stamp at creation the button has no aria-expanded at all until the customer's first
    tap, which is the one moment a screen-reader user most needs it.
    """
    body = _build_sidebar()
    burger = body[body.index('burger.id = "shell-burger"'):]
    assert re.search(r'burger\.setAttribute\("aria-expanded"', burger[:600]), (
        "the burger is created without an initial aria-expanded")
    assert re.search(r'burger\.setAttribute\("aria-controls", "shell-side"\)', burger[:600]), (
        "the burger does not say which element it controls")


# ── the composer sits above the home indicator ────────────────────────────────────────────────
def test_the_sticky_composer_clears_the_home_indicator():
    """`.composer` is stuck to bottom:0, which on a gesture-navigation phone is UNDER the home
    indicator: the send button sat in the swipe-up zone, so a tap there is as likely to background
    the browser as to send the question.

    env() resolves to 0 wherever it does not apply, so this costs nothing on a desktop or a
    home-button phone — which is why it is unconditional rather than behind a media query.
    """
    css = STYLES.read_text(encoding="utf-8")
    m = re.search(r"\.composer\s*\{([^}]*)\}", css)
    assert m, ".composer is no longer styled where this test looks for it"
    assert "env(safe-area-inset-bottom)" in m.group(1), (
        "the composer does not reserve the home-indicator strip: %s" % m.group(1).strip())
    assert "position: sticky" in m.group(1), (
        "the composer is no longer sticky, so this test is asserting about the wrong element")


# ── the resolver has to be worth believing ────────────────────────────────────────────────────
def test_the_resolver_is_not_quietly_blind():
    """Every selector and every @media in this stylesheet is understood, or the checks above are
    grading a subset and cannot say which."""
    css = shell_css()
    all_rules = rules(css)
    assert len(all_rules) > 40, "only %d rules parsed out of the injected stylesheet" % len(all_rules)
    unknown_media, unknown_sel = set(), set()
    for sel, _body, cond, _order in all_rules:
        if media_applies(cond, 375) is None:
            unknown_media.add(cond)
        for one in (s.strip() for s in sel.split(",")):
            try:
                _selector_matches(one, SIDE, ("shell-item",), True, IN_DRAWER)
            except ValueError:
                unknown_sel.add(one)
    assert not unknown_media, "unevaluable @media conditions: %r" % sorted(unknown_media)
    assert not unknown_sel, (
        "selectors this resolver would silently ignore: %r. Any of them could carry a visibility or "
        "pointer-events declaration for the drawer." % sorted(unknown_sel))

    # AND THE REFUSAL REALLY FIRES. The clean run above is also what a resolver that answers False
    # to everything it cannot read would produce, so the shapes it must refuse are named here. A
    # mutation turning either raise into `return False` survives every other check in this file:
    # nothing in today's stylesheet uses these shapes, which is exactly why the guard has to be
    # tested directly rather than inferred from the scan.
    for unreadable in ("#shell-side[data-x]", "#a > #b", "#shell-side::after", "#a#b",
                       "#shell-side%bad", "#shell-side["):
        with pytest.raises(ValueError):
            _selector_matches(unreadable, SIDE, ("shell-item",), True, IN_DRAWER)


def test_the_resolver_really_does_resolve_rather_than_match():
    """A live proof that a later heavier rule beats an earlier one, and that WEIGHT beats ORDER.

    Without this the resolver could be a dressed-up regex and every assertion above would still
    pass. `#shell-side` at (1,0,0) sets visibility:hidden and is written FIRST;
    `html.shell-open #shell-side` at (1,1,0) sets visible. Reading the same property in both states
    and getting two answers is the mechanism under test.
    """
    css = shell_css()
    assert resolve(css, "visibility", SIDE, 375, root_open=False) == "hidden"
    assert resolve(css, "visibility", SIDE, 375, root_open=True) == "visible"
    assert css.index("visibility:hidden") < css.index("visibility:visible")
    reversed_order = ("html.shell-open #shell-side{pointer-events:auto}\n"
                      "#shell-side{pointer-events:none}")
    assert resolve(reversed_order, "pointer-events", SIDE, 375, root_open=True) == "auto", (
        "source order is beating specificity, which is not how the cascade works")


def test_the_two_breakpoints_meet_without_a_gap_or_an_overlap():
    """The stylesheet's phone block and its desktop block have to be exactly complementary.

    An off-by-one here is a width at which the drawer is a modal sheet with no scrim, or a rail with
    one. 899/900 rather than a shared number for the same reason the tool uses 767/768.
    """
    css = shell_css()
    conds = {c for _s, _b, c, _o in rules(css) if c.strip()}
    assert ("(max-width:%dpx)" % (DESKTOP_MIN - 1)) in {c.replace(" ", "") for c in conds}, conds
    assert ("(min-width:%dpx)" % DESKTOP_MIN) in {c.replace(" ", "") for c in conds}, conds
