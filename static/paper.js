/* Taking a paper (templates/includes/_paper.html): one question at a time on one page, the palette, "mark for review",
   the keyboard, the timer, the summary before submitting, and exam mode. Saving and submitting belong to the page:
   it sets window.paperHooks = { save(qid, kind, value) -> Promise, submit(), timeUp() } before this file runs.

   Exam mode (data-paper-mode on [data-paper]): "record" for a test, an examination or the entrance examination, whose
   reports are sent to data-integrity-url (core/exam_integrity.py); "practice" applies the same rules without sending
   anything. It opens with a screen that starts full screen (a browser only allows that on a press), then: leaving the
   page or full screen shows a warning (and is reported), and copying, pasting and the right-click menu are turned off.
   None of this can stop a second device; it is a deterrent and a record. */
(function () {
    "use strict";
    var root = document.querySelector("[data-paper]");
    if (!root) return;
    var hooks = window.paperHooks || {};
    var questions = Array.prototype.slice.call(root.querySelectorAll("[data-pq]"));
    var tiles = Array.prototype.slice.call(root.querySelectorAll("[data-goto]"));
    var total = questions.length;
    if (!total) return;
    var mode = root.dataset.paperMode || "off";
    var csrfInput = document.getElementById("csrfToken") || document.querySelector('input[name="_csrf_token"]');
    var storeKey = "paper-flags-" + (root.dataset.paperKey || location.pathname);
    var current = 0, started = mode === "off", submitting = false;
    var flags = {};
    try { flags = JSON.parse(sessionStorage.getItem(storeKey) || "{}") || {}; } catch (e) { flags = {}; }

    var $ = function (sel) { return root.querySelector(sel); };
    var prevBtn = $("[data-paper-prev]"), nextBtn = $("[data-paper-next]"), flagBtn = $("[data-paper-flag]"),
        submitBtn = $("[data-paper-submit]"), countEl = $("[data-paper-answered]"), barEl = $("[data-paper-bar]"),
        palCount = $("[data-paper-palcount]");

    function qidOf(q) { return q.dataset.qid; }
    function answered(q) {
        if (q.querySelector("input[type=radio]:checked")) return true;
        var t = q.querySelector("textarea");
        return !!(t && t.value.trim());
    }
    function refresh() {
        var n = 0;
        questions.forEach(function (q, i) {
            var done = answered(q);
            if (done) n++;
            var tile = tiles[i];
            if (tile) {
                tile.classList.toggle("answered", done);
                tile.classList.toggle("is-flagged", !!flags[qidOf(q)]);
                tile.classList.toggle("is-current", i === current);
                if (i === current) tile.setAttribute("aria-current", "step"); else tile.removeAttribute("aria-current");
            }
        });
        if (countEl) countEl.textContent = n;
        if (palCount) palCount.textContent = n + " / " + total;
        if (barEl) barEl.style.width = Math.round(100 * n / total) + "%";
        if (flagBtn) flagBtn.setAttribute("aria-pressed", flags[qidOf(questions[current])] ? "true" : "false");
        return n;
    }
    function show(i, focus) {
        current = Math.max(0, Math.min(total - 1, i));
        questions.forEach(function (q, k) { q.hidden = k !== current; });
        if (prevBtn) prevBtn.disabled = current === 0;
        if (nextBtn) nextBtn.hidden = current === total - 1;
        if (submitBtn) submitBtn.classList.toggle("pp-btn--primary", current === total - 1);
        refresh();
        try {
            var url = new URL(location.href); url.searchParams.set("q", current + 1);
            history.replaceState(history.state, "", url);
        } catch (e) {}
        if (focus) {
            var h = questions[current].querySelector("[data-pq-head]");
            if (h) h.focus({ preventScroll: true });
            window.scrollTo({ top: 0, behavior: "smooth" });
        }
    }

    /* ------------------------------------------------ answering */
    var typing = {};
    root.addEventListener("change", function (e) {
        var input = e.target;
        if (!(input instanceof HTMLInputElement) || input.type !== "radio") return;
        var q = input.closest("[data-pq]");
        refresh();
        if (q && hooks.save) hooks.save(qidOf(q), "option", input.value);
    });
    root.addEventListener("input", function (e) {
        var t = e.target;
        if (!(t instanceof HTMLTextAreaElement)) return;
        var q = t.closest("[data-pq]");
        var counter = q && q.querySelector("[data-pq-count]");
        if (counter) counter.textContent = t.value.length + " / " + (t.maxLength > 0 ? t.maxLength : "");
        refresh();
        if (!q || !hooks.save) return;
        var id = qidOf(q);
        clearTimeout(typing[id]);
        typing[id] = setTimeout(function () { delete typing[id]; hooks.save(id, "text", t.value); }, 1200);
    });
    function flushTyping() {
        var waits = [];
        Object.keys(typing).forEach(function (id) {
            clearTimeout(typing[id]); delete typing[id];
            var q = root.querySelector('[data-pq][data-qid="' + id + '"]');
            var t = q && q.querySelector("textarea");
            if (t && hooks.save) waits.push(hooks.save(id, "text", t.value));
        });
        return Promise.all(waits);
    }
    window.paperFlush = flushTyping;

    /* ------------------------------------------------ moving around */
    tiles.forEach(function (tile, i) { tile.addEventListener("click", function (e) { e.preventDefault(); show(i, true); }); });
    if (prevBtn) prevBtn.addEventListener("click", function () { show(current - 1, true); });
    if (nextBtn) nextBtn.addEventListener("click", function () { show(current + 1, true); });
    if (flagBtn) flagBtn.addEventListener("click", function () {
        var id = qidOf(questions[current]);
        if (flags[id]) delete flags[id]; else flags[id] = 1;
        try { sessionStorage.setItem(storeKey, JSON.stringify(flags)); } catch (e) {}
        refresh();
    });
    document.addEventListener("keydown", function (e) {
        if (!started || e.altKey || e.ctrlKey || e.metaKey || document.querySelector("dialog[open]")) return;
        var tag = (e.target && e.target.tagName) || "";
        if (tag === "TEXTAREA" || (tag === "INPUT" && e.target.type !== "radio")) return;
        if (e.key === "ArrowRight") { e.preventDefault(); show(current + 1, true); return; }
        if (e.key === "ArrowLeft") { e.preventDefault(); show(current - 1, true); return; }
        var k = e.key.toLowerCase(), idx = "abcdefgh".indexOf(k);
        if (idx < 0 && /^[1-8]$/.test(k)) idx = parseInt(k, 10) - 1;
        if (idx < 0) return;
        var radios = questions[current].querySelectorAll("input[type=radio]");
        if (radios[idx]) { radios[idx].checked = true; radios[idx].dispatchEvent(new Event("change", { bubbles: true })); e.preventDefault(); }
    });

    /* ------------------------------------------------ submitting */
    function finish(reason) {
        submitting = true;
        if (document.fullscreenElement && document.exitFullscreen) { try { document.exitFullscreen(); } catch (e) {} }
        if (hooks.submit) hooks.submit(reason);
    }
    if (submitBtn) submitBtn.addEventListener("click", function () {
        var n = refresh(), left = total - n, flagged = Object.keys(flags).filter(function (id) {
            return root.querySelector('[data-pq][data-qid="' + id + '"]');
        }).length;
        var lines = n + " of " + total + " answered.";
        if (left) lines += " " + left + " not answered.";
        if (flagged) lines += " " + flagged + " marked for review.";
        lines += " You cannot change your answers after you submit.";
        // Every paper page loads static/interactions.js, which provides the school's own dialog.
        window.brandDialog.confirm(lines, { title: "Submit your paper?", ok: "Submit now", cancel: "Keep working", danger: false }).then(function (yes) { if (yes) flushTyping().then(function () { finish("submit"); }, function () { finish("submit"); }); });
    });

    /* ------------------------------------------------ the timer */
    var timer = $("[data-paper-timer]");
    if (timer) {
        var remaining = parseInt(timer.dataset.seconds, 10) || 0;
        var tick = function () {
            var s = Math.max(0, remaining);
            var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
            timer.textContent = (h ? h + ":" + String(m).padStart(2, "0") : String(m).padStart(2, "0")) + ":" + String(sec).padStart(2, "0");
            timer.classList.toggle("is-warn", s <= 300 && s > 60);
            timer.classList.toggle("is-danger", s <= 60);
            if (s === 300) toast("Five minutes left.");
            if (s === 60) toast("One minute left.");
            if (s <= 0) {
                clearInterval(clock);
                submitting = true;
                flushTyping().then(function () { if (hooks.timeUp) hooks.timeUp(); }, function () { if (hooks.timeUp) hooks.timeUp(); });
                return;
            }
            remaining--;
        };
        var clock = setInterval(tick, 1000);
        tick();
    }

    /* ------------------------------------------------ messages */
    var toastEl = null, toastTimer = null;
    function toast(text) {
        if (!toastEl) { toastEl = document.createElement("div"); toastEl.className = "pp-toast"; toastEl.setAttribute("role", "status"); document.body.appendChild(toastEl); }
        toastEl.textContent = text; toastEl.hidden = false;
        clearTimeout(toastTimer); toastTimer = setTimeout(function () { toastEl.hidden = true; }, 3200);
    }
    window.paperToast = toast;

    /* ------------------------------------------------ exam mode */
    var gate = document.querySelector("[data-paper-gate]");
    var warn = document.querySelector("[data-paper-warn]");
    var canFull = !!(document.documentElement.requestFullscreen && document.fullscreenEnabled !== false);
    var lastReport = {}, leftCount = parseInt(root.dataset.left || "0", 10) || 0;
    var limit = parseInt(root.dataset.leaveLimit || "0", 10) || 0;

    function report(kind) {
        if (mode !== "record" || submitting) return;
        var now = Date.now();
        if (lastReport[kind] && now - lastReport[kind] < (kind === "left_page" ? 2000 : 4000)) return;
        lastReport[kind] = now;
        if (kind === "left_page") leftCount++;
        var body = new FormData();
        body.append("kind", kind);
        body.append("question", String(current + 1));
        if (csrfInput) body.append("_csrf_token", csrfInput.value);
        fetch(root.dataset.integrityUrl, { method: "POST", body: body, credentials: "same-origin", keepalive: true })
            .then(function (r) { return r.json().catch(function () { return {}; }); })
            .then(function (j) {
                if (typeof j.left === "number") leftCount = j.left;
                if (j.ended) { submitting = true; location.href = root.dataset.resultUrl; }
            })
            .catch(function () {});
    }
    function enterFull() {
        if (!canFull) return Promise.resolve(false);
        return document.documentElement.requestFullscreen({ navigationUI: "hide" }).then(function () { return true; }, function () { return false; });
    }
    function showWarn(kind) {
        if (!warn || mode === "off" || submitting) return;
        var title = warn.querySelector("[data-warn-title]"), text = warn.querySelector("[data-warn-text]"), count = warn.querySelector("[data-warn-count]");
        if (kind === "fullscreen_exit") {
            title.textContent = "You left full screen";
            text.textContent = "Stay in full screen until you submit." + (mode === "record" ? " This has been recorded for your teacher." : "");
        } else {
            title.textContent = "You left the paper";
            text.textContent = "Stay on this page until you submit." + (mode === "record" ? " Every time you leave is recorded for your teacher." : "");
        }
        if (count) {
            count.hidden = mode !== "record" || !leftCount;
            count.textContent = "Left " + leftCount + " time" + (leftCount === 1 ? "" : "s") + (limit ? " · the paper ends after " + limit : "");
        }
        warn.hidden = false;
        var back = warn.querySelector("[data-warn-back]");
        if (back) back.focus();
    }
    if (gate && mode !== "off") {
        var startBtn = gate.querySelector("[data-gate-start]");
        if (!canFull && startBtn) startBtn.textContent = "Start";
        gate.hidden = false;
        startBtn.addEventListener("click", function () {
            enterFull().then(function (ok) {
                started = true;
                gate.hidden = true;
                root.classList.add("is-locked");
                if (!ok && mode === "record") report("fullscreen_unavailable");
                show(current, true);
            });
        });
    } else {
        started = true;
    }
    if (warn) {
        warn.querySelector("[data-warn-back]").addEventListener("click", function () {
            enterFull().then(function () { warn.hidden = true; });
        });
    }
    if (mode !== "off") {
        var away = false;
        var leave = function () {
            if (!started || submitting || away) return;
            away = true;
            report("left_page");
        };
        var back = function () {
            if (!away) return;
            away = false;
            if (started && !submitting) showWarn("left_page");
        };
        document.addEventListener("visibilitychange", function () { if (document.hidden) leave(); else back(); });
        window.addEventListener("blur", function () { setTimeout(function () { if (!document.hasFocus()) leave(); }, 150); });
        window.addEventListener("focus", back);
        document.addEventListener("fullscreenchange", function () {
            if (!document.fullscreenElement && started && !submitting && canFull) { report("fullscreen_exit"); showWarn("fullscreen_exit"); }
        });
        var block = function (kind, message) {
            return function (e) {
                if (!started || submitting) return;
                e.preventDefault();
                toast(message);
                report(kind);
            };
        };
        document.addEventListener("copy", block("copy", "Copying is turned off during the paper."));
        document.addEventListener("cut", block("copy", "Copying is turned off during the paper."));
        document.addEventListener("paste", block("paste", "Pasting is turned off during the paper. Type your answer."));
        document.addEventListener("drop", block("paste", "Dropping text in is turned off during the paper."));
        document.addEventListener("contextmenu", block("context_menu", "The right-click menu is turned off during the paper."));
        document.addEventListener("dragstart", function (e) { if (started && !submitting) e.preventDefault(); });
    }

    window.paper = { refresh: refresh, show: show, isSubmitting: function () { return submitting; }, finish: finish };
    var startAt = parseInt(root.dataset.startQ || "1", 10) - 1;
    show(isNaN(startAt) ? 0 : startAt, false);
    questions.forEach(function (q) {
        var t = q.querySelector("textarea"), c = q.querySelector("[data-pq-count]");
        if (t && c) c.textContent = t.value.length + " / " + (t.maxLength > 0 ? t.maxLength : "");
    });
})();
