/* Small, generic behaviours every page used to attach with an inline onclick/onchange/onsubmit
   attribute, now wired here instead, from data-* attributes, so the Content-Security-Policy no
   longer needs 'unsafe-inline' for script-src (an inline event-handler attribute is exactly as
   dangerous, to CSP, as an inline <script> block - a nonce covers neither, so the only fix is not
   to have any). One delegated listener per event type covers every page that loads this file.

   It also gives every page the school's own dialog in place of the browser's confirm() and alert():
   window.brandDialog.confirm(message, options) and .alert(message, options), each a Promise, in the
   school's colours (--navy, --blue). data-confirm on a button, link or form asks with it; the
   optional data-confirm-title, data-confirm-ok and data-confirm-danger ("1" or "0") shape it. */
(function () {
    "use strict";

    /* ------------------------------------------------------------ the branded dialog */
    var STYLE =
        ".bd{width:min(440px,calc(100vw - 32px));max-width:none;padding:0;border:0;border-radius:14px;background:var(--card,#fff);" +
        "color:var(--ink,var(--text,#1b2430));box-shadow:0 24px 60px rgba(9,20,40,.28);overflow:hidden;font:inherit}" +
        ".bd::backdrop{background:rgba(9,20,40,.45)}" +
        ".bd__box{display:flex;flex-direction:column}" +
        ".bd__bar{height:5px;background:linear-gradient(90deg,var(--navy,#0d2b52),var(--blue,#1d6fd8))}" +
        ".bd__body{display:flex;gap:12px;padding:18px 20px 6px}" +
        ".bd__icon{flex:0 0 34px;width:34px;height:34px;border-radius:50%;display:grid;place-items:center;font-weight:800;font-size:17px;" +
        "background:color-mix(in srgb,var(--blue,#1d6fd8) 13%,transparent);color:var(--navy,#0d2b52)}" +
        ".bd--danger .bd__icon{background:color-mix(in srgb,var(--danger,#c0392b) 13%,transparent);color:var(--danger,#c0392b)}" +
        ".bd__title{margin:2px 0 4px;font-size:15px;font-weight:700;color:var(--navy,#0d2b52)}" +
        ".bd--danger .bd__title{color:var(--danger,#c0392b)}" +
        ".bd__msg{margin:0;font-size:13.5px;line-height:1.5;white-space:pre-line;overflow-wrap:anywhere}" +
        ".bd__actions{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:8px;padding:14px 20px 18px}" +
        ".bd__btn{min-height:36px;padding:0 16px;border-radius:8px;font:inherit;font-size:13px;font-weight:700;cursor:pointer;" +
        "border:1px solid var(--line,#d8dee8);background:var(--card,#fff);color:var(--ink,#1b2430)}" +
        ".bd__btn:focus-visible{outline:3px solid color-mix(in srgb,var(--blue,#1d6fd8) 45%,transparent);outline-offset:2px}" +
        ".bd__btn--ok{border-color:var(--blue,#1d6fd8);background:var(--blue,#1d6fd8);color:#fff}" +
        ".bd__btn--ok:hover{background:var(--blue-2,var(--navy,#0d2b52))}" +
        ".bd--danger .bd__btn--ok{border-color:var(--danger,#c0392b);background:var(--danger,#c0392b)}" +
        "@media (max-width:420px){.bd__actions{flex-direction:column-reverse}.bd__btn{width:100%}}";
    // Words that mark a confirmation as undoing or removing something: its button is then red, and
    // the keyboard starts on "No, go back", so a stray Enter does not do it.
    var DANGER = /\b(delete|remove|void|refund|archive|cancel|suspend|revoke|discard|reset|end|deactivate|take .* off|sign (everyone|them|all) out)\b/i;
    var queue = Promise.resolve();
    var counter = 0;

    function ensureStyle() {
        if (document.getElementById("bd-style")) return;
        var style = document.createElement("style");
        style.id = "bd-style";
        style.textContent = STYLE;
        document.head.appendChild(style);
    }

    function show(kind, message, options) {
        options = options || {};
        var confirming = kind === "confirm";
        var danger = confirming && (options.danger !== undefined ? !!options.danger : DANGER.test(message || ""));
        if (typeof HTMLDialogElement !== "function") {
            // A very old browser: its own box, rather than nothing at all.
            return Promise.resolve(confirming ? window.confirm(message) : (window.alert(message), true));
        }
        return new Promise(function (resolve) {
            ensureStyle();
            var id = "bd-" + (++counter);
            var dialog = document.createElement("dialog");
            dialog.className = "bd" + (danger ? " bd--danger" : "");
            dialog.setAttribute("aria-labelledby", id + "-t");
            dialog.setAttribute("aria-describedby", id + "-m");
            if (confirming) dialog.setAttribute("role", "alertdialog");

            var box = document.createElement("div"); box.className = "bd__box";
            var bar = document.createElement("div"); bar.className = "bd__bar";
            var body = document.createElement("div"); body.className = "bd__body";
            var icon = document.createElement("span"); icon.className = "bd__icon"; icon.setAttribute("aria-hidden", "true");
            icon.textContent = danger ? "!" : (confirming ? "?" : "i");
            var text = document.createElement("div");
            var title = document.createElement("h2"); title.className = "bd__title"; title.id = id + "-t";
            title.textContent = options.title || (confirming ? "Please confirm" : "Please note");
            var msg = document.createElement("p"); msg.className = "bd__msg"; msg.id = id + "-m";
            msg.textContent = message || "";
            text.append(title, msg);
            body.append(icon, text);

            var actions = document.createElement("div"); actions.className = "bd__actions";
            var ok = document.createElement("button"); ok.type = "button"; ok.className = "bd__btn bd__btn--ok";
            ok.textContent = options.ok || (confirming ? "Yes, continue" : "OK");
            var cancel = null;
            if (confirming) {
                cancel = document.createElement("button"); cancel.type = "button"; cancel.className = "bd__btn";
                cancel.textContent = options.cancel || "No, go back";
                actions.append(cancel);
            }
            actions.append(ok);
            box.append(bar, body, actions);
            dialog.append(box);

            var before = document.activeElement;
            var settled = false;
            function finish(answer) {
                if (settled) return;
                settled = true;
                if (dialog.open) dialog.close();
                dialog.remove();
                if (before && before.focus && document.contains(before)) { try { before.focus(); } catch (e) {} }
                resolve(answer);
            }
            ok.addEventListener("click", function () { finish(true); });
            if (cancel) cancel.addEventListener("click", function () { finish(false); });
            // Escape, or a press outside the box, is "no" for a question and "OK" for a notice.
            dialog.addEventListener("cancel", function (e) { e.preventDefault(); finish(!confirming); });
            dialog.addEventListener("click", function (e) { if (e.target === dialog) finish(!confirming); });

            document.body.appendChild(dialog);
            dialog.showModal();
            (danger && cancel ? cancel : ok).focus();
        });
    }

    // One dialog at a time: a second question waits for the first to be answered.
    function enqueue(kind, message, options) {
        var next = queue.then(function () { return show(kind, message, options); });
        queue = next.catch(function () {});
        return next;
    }

    function optionsFrom(el) {
        var d = el.dataset;
        return {
            title: d.confirmTitle || undefined,
            ok: d.confirmOk || undefined,
            danger: d.confirmDanger === undefined ? undefined : d.confirmDanger !== "0"
        };
    }

    function resubmit(form, submitter) {
        if (form.requestSubmit) {
            if (submitter && submitter.form === form) form.requestSubmit(submitter); else form.requestSubmit();
        } else {
            form.submit();
        }
    }

    // Elements whose question has just been answered "yes": the repeated press or submit goes through.
    var answered = new WeakSet();

    window.brandDialog = {
        confirm: function (message, options) { return enqueue("confirm", message, options); },
        alert: function (message, options) { return enqueue("alert", message, options); },
        /* For a form's own submit handler that asks only sometimes: returns true when the submit may go on
           (it was just confirmed), otherwise stops it, asks, and sends the form again on "yes". */
        confirmSubmit: function (e, message, options) {
            var form = e.target;
            if (answered.has(form)) { answered.delete(form); return true; }
            e.preventDefault();
            e.stopImmediatePropagation();
            var submitter = e.submitter;
            enqueue("confirm", message, options).then(function (yes) {
                if (yes) { answered.add(form); resubmit(form, submitter); }
            });
            return false;
        }
    };

    /* data-confirm, asked before anything else on the page sees the press or the submit (capture
       phase): on "no" nothing happens at all; on "yes" the same press or submit is made again. */
    document.addEventListener("click", function (e) {
        var el = e.target.closest && e.target.closest("[data-confirm]");
        if (!el || el instanceof HTMLFormElement || el.disabled) return;
        if (answered.has(el)) { answered.delete(el); return; }
        e.preventDefault();
        e.stopPropagation();
        enqueue("confirm", el.dataset.confirm, optionsFrom(el)).then(function (yes) {
            if (yes) { answered.add(el); el.click(); }
        });
    }, true);

    document.addEventListener("submit", function (e) {
        var form = e.target;
        if (!(form instanceof HTMLFormElement) || !form.dataset.confirm) return;
        if (answered.has(form)) { answered.delete(form); return; }
        e.preventDefault();
        e.stopPropagation();
        var submitter = e.submitter;
        enqueue("confirm", form.dataset.confirm, optionsFrom(form)).then(function (yes) {
            if (yes) { answered.add(form); resubmit(form, submitter); }
        });
    }, true);

    /* ------------------------------------------------------------ the other data-* behaviours */
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
        var el = e.target.closest("[data-print],[data-reload],[data-modal-open],[data-modal-close],[data-go-back],[data-call],[data-toggle-class]");
        if (!el) return;

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
