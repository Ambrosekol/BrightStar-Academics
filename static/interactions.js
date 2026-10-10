/* Small, generic behaviours every page used to attach with an inline onclick/onchange/onsubmit
   attribute, now wired here instead, from data-* attributes, so the Content-Security-Policy no
   longer needs 'unsafe-inline' for script-src (an inline event-handler attribute is exactly as
   dangerous, to CSP, as an inline <script> block - a nonce covers neither, so the only fix is not
   to have any). One delegated listener per event type covers every page that loads this file. */
(function () {
    "use strict";

    document.addEventListener("submit", function (e) {
        var form = e.target;
        if (!(form instanceof HTMLFormElement)) return;
        var msg = form.dataset.confirm;
        if (msg && !window.confirm(msg)) {
            e.preventDefault();
        }
    });

    document.addEventListener("change", function (e) {
        var el = e.target;
        if (!el) return;
        if ("autosubmit" in el.dataset && el.form) {
            // requestSubmit fires the submit event, so a page that loads its next view in place
            // (static/inplace.js) can take the submission over; submit() would skip it.
            if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit();
            return;
        }
        var fn = el.dataset.onchange;
        if (fn && typeof window[fn] === "function") {
            window[fn](el);
        }
        if (el.dataset.showWhen !== undefined && el.dataset.showTarget) {
            var shown = document.getElementById(el.dataset.showTarget);
            if (shown) shown.hidden = el.value !== el.dataset.showWhen;
        }
    });

    document.addEventListener("click", function (e) {
        var el = e.target.closest("[data-confirm],[data-print],[data-reload],[data-modal-open],[data-modal-close],[data-go-back],[data-call],[data-toggle-class]");
        if (!el) return;

        // A confirm on the element that was actually clicked (a button, usually inside a form) -
        // preventing its default click action also stops the form it would have submitted.
        // data-confirm on a <form> itself is handled separately, by the submit listener above,
        // for the handful of pages that trigger a submit some other way (e.g. pressing Enter).
        if (el.dataset.confirm && !(el instanceof HTMLFormElement) && !window.confirm(el.dataset.confirm)) {
            e.preventDefault();
            return;
        }

        if ("print" in el.dataset) {
            window.print();
        }
        if ("reload" in el.dataset) {
            window.location.reload();
        }
        if (el.dataset.modalOpen) {
            var toOpen = document.getElementById(el.dataset.modalOpen);
            if (toOpen && toOpen.showModal) toOpen.showModal();
        }
        if (el.dataset.modalClose) {
            var toClose = document.getElementById(el.dataset.modalClose);
            if (toClose && toClose.close) toClose.close();
        }
        if (el.dataset.goBack) {
            if (typeof window.portalGoBack === "function") {
                window.portalGoBack(el.dataset.goBack);
            } else {
                window.location.href = el.dataset.goBack;
            }
        }
        if (el.dataset.call && typeof window[el.dataset.call] === "function") {
            window[el.dataset.call](el, el.dataset.callArg);
        }
        if (el.dataset.toggleClass) {
            var target = el.dataset.toggleTarget ? document.querySelector(el.dataset.toggleTarget) : document.body;
            if (target) target.classList.toggle(el.dataset.toggleClass);
        }
    });
})();
