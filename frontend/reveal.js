"use strict";
// Hold a page-to-page cross-fade until this page has something to show.
//
// Hanz, 2026-10-29, on the proposal tabs of a project priced as several proposals: "why does the
// whole page need to reload when transferring from one proposal to the other". A tab stays a REAL
// page load on purpose (the Approve button, the signature latch, the deposit, the chat and the poll
// all hang off the page's one TOKEN, so a fresh load is what guarantees Approve can only approve
// the proposal on screen). What changed is how the load LOOKS: styles.css opts every page into a
// cross-document view transition, so the browser cross-fades from the old page to the new one.
//
// THE CATCH THIS FILE EXISTS FOR. The browser captures the new page at its FIRST frame, and this
// page's first frame is the loading skeleton: everything real is drawn by app.js after its API
// call. Left alone, the cross-fade would go from the proposal to a skeleton at the top of the page,
// and then the content would pop in and jump to the document card, which is the flash the fade was
// meant to remove. So the fade's animations are paused at their first frame (the old page, still
// fully opaque) until app.js has drawn the page and scrolled to the card (TWReveal.done), then
// played. The new side of the fade is a LIVE image of the page, so it shows what is there when it
// plays, not what was there when it was captured.
//
// Bounded: released after HOLD_MS whatever happens, because the page under a running transition
// does not take clicks. Loaded in <head>, not with the other scripts, because `pagereveal` fires
// before the first frame and a listener added after it never hears it. A browser without
// cross-document transitions (or with reduced motion, see styles.css) never gets a viewTransition
// here, and the page simply loads.
(function () {
  var HOLD_MS = 1500;
  var drawn = false;
  var held = [];

  function play() {
    var a = held;
    held = [];
    for (var i = 0; i < a.length; i++) { try { a[i].play(); } catch (e) { /* already finished */ } }
  }

  /** app.js calls this once its boot has finished, on EVERY path (portal, login gate, not found,
   *  error): the page is whatever it is going to be, so the fade may run. */
  function done() {
    if (drawn) return;
    drawn = true;
    // One frame later, so the scroll to the document card has been laid out before the fade starts.
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(play); else play();
  }

  window.TWReveal = { done: done };

  window.addEventListener("pagereveal", function (e) {
    var vt = e && e.viewTransition;
    if (!vt) return;
    vt.ready.then(function () {
      // `drawn` is already true on a back/forward restore of a page that finished booting (its
      // first frame IS its content), and when the page drew before the transition was ready.
      if (drawn || typeof document.getAnimations !== "function") return;
      held = document.getAnimations().filter(function (a) {
        var pe = a.effect && a.effect.pseudoElement;
        return typeof pe === "string" && pe.indexOf("::view-transition") === 0;
      });
      for (var i = 0; i < held.length; i++) held[i].pause();
      setTimeout(done, HOLD_MS);
    }).catch(function () { /* the transition was skipped: nothing to hold */ });
  });
})();
