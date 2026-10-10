/* Load the next view of a section in place instead of reloading the whole page.

   Active only inside the sections the body names in data-inplace-scope (Academics, Students, Finance, the exam
   timetable). A link or form there that leads to another address in scope is fetched with X-Fragment: 1, which
   templates/admin_base.html answers with just the page's messages and content; that replaces #page-body while
   the previous view stays on screen, dimmed under a spinner. The address bar follows (history.pushState), so
   Back, Forward, reload and bookmarks all still work, and every address still opens as a full page on its own.

   A link marked data-modal opens its page in a pop-up over the current one (a student, a payment, a form).
   Pop-ups stack: a data-modal link inside a pop-up opens another on top of it, and closing the top one shows
   the one beneath again (data-modal="replace" swaps the top pop-up's page instead). A pop-up page marked
   data-modal-stay (a long form) is not reloaded when one opened over it closes, so nothing typed in it is lost. Each page is asked for with
   modal=1 and return_to=<this address>, so it can leave out its frame; every request from a pop-up carries
   X-Modal: 1, so a page reached by a redirect still knows it is in one.

   Where an answer lands: a pop-up keeps its own page, any page beneath its address, and any address its page
   lists in data-modal-scope. An answer that belongs to a pop-up further down the stack closes the ones above it
   and shows there; anything else closes every pop-up and becomes the page underneath. After a save, closing a
   pop-up refreshes whatever is under it, so counts and lists are current.

   Anything else - a download, a PDF, a print view, another section, a new tab, or a link marked data-full - is
   left to the browser. If a fetch fails, the browser simply goes to the address. */
(function () {
    "use strict";
    var body = document.querySelector("[data-page-body]");
    var scope = (document.body.getAttribute("data-inplace-scope") || "").split(",").filter(Boolean);
    if (!body || !scope.length || !window.fetch || !window.history.pushState) return;

    var nonce = (document.querySelector("script[nonce]") || {}).nonce || "";
    var busy = null;
    var layers = [];   // the open pop-ups, bottom first: {dialog, body, path, scope, changed}

    function inScope(url) {
        if (url.origin !== window.location.origin) return false;
        if (/\.(pdf|csv|json|png|jpe?g)$/i.test(url.pathname) || /\/(print|pdf|export)(\/|$)/.test(url.pathname)) return false;
        // "=/path" is that one address only; "/path" is it and everything beneath it.
        return scope.some(function (prefix) {
            if (prefix.charAt(0) === "=") return url.pathname === prefix.slice(1);
            return url.pathname === prefix || url.pathname.indexOf(prefix + "/") === 0;
        });
    }
    function here() { return inScope(new URL(window.location.href)); }

    function spinner(target, on) {
        target.classList.toggle("is-loading", on);
        target.setAttribute("aria-busy", on ? "true" : "false");
    }

    function runScripts(root) {
        // Scripts inserted with innerHTML never run; re-create each one, carrying this page's own CSP nonce.
        Array.prototype.forEach.call(root.querySelectorAll("script"), function (old) {
            var s = document.createElement("script");
            // A page's own script file (report cards, say) runs again for its new content; the shell's shared
            // files are never part of a page's content, so they are not loaded twice.
            if (old.src) { s.src = old.src; if (nonce) s.nonce = nonce; old.replaceWith(s); return; }
            if (nonce) s.nonce = nonce;
            if (old.type) s.type = old.type;
            if (old.id) s.id = old.id;
            s.textContent = old.textContent;
            old.replaceWith(s);
        });
    }

    function show(html, url, push) {
        body.innerHTML = html;
        var title = body.querySelector("[data-fragment-title]");
        if (title) document.title = title.textContent.trim();
        document.body.style.overflow = "";
        if (push) window.history.pushState({ inplace: true }, "", url);
        else window.history.replaceState({ inplace: true }, "", url);
        runScripts(body);
        var hash = new URL(url, window.location.href).hash;
        var target = hash && document.getElementById(hash.slice(1));
        if (target) target.scrollIntoView({ block: "start" });
        else if (push) window.scrollTo({ top: 0 });
        var focus = body.querySelector("[autofocus]");
        if (focus) focus.focus({ preventScroll: true });
    }

    /* ---- pop-ups ---------------------------------------------------------------------------- */
    function top() { return layers[layers.length - 1] || null; }
    function layerOf(el) {
        for (var i = layers.length - 1; i >= 0; i--) if (layers[i].dialog.contains(el)) return layers[i];
        return null;
    }
    // A scope entry ending in "/" is every address beneath it (a new role's pop-up becomes that role's once saved).
    function owns(layer, path) {
        return path === layer.path || path.indexOf(layer.path + "/") === 0 || layer.scope.some(function (s) {
            return s === path || (s.charAt(s.length - 1) === "/" && path.indexOf(s) === 0);
        });
    }

    function newLayer() {
        var dialog = document.createElement("dialog");
        dialog.className = "ac-modal";
        dialog.setAttribute("aria-label", "Details");
        dialog.innerHTML = '<button type="button" class="ac-modal__close" aria-label="Close">×</button><div class="ac-modal__body" data-modal-body></div>';
        document.body.appendChild(dialog);
        var layer = { dialog: dialog, body: dialog.querySelector("[data-modal-body]"), path: "", scope: [], changed: false };
        dialog.querySelector(".ac-modal__close").addEventListener("click", function () { dismiss(layer); });
        dialog.addEventListener("click", function (e) {
            if (e.target === dialog) { dismiss(layer); return; }   // a click on the backdrop
            if (e.target.closest("[data-modal-dismiss]")) { e.preventDefault(); dismiss(layer); }   // Cancel on a page in the pop-up
        });
        dialog.addEventListener("close", function () { closed(layer); });
        layers.push(layer);
        return layer;
    }

    // A pop-up has closed (the ×, Escape, Cancel, the backdrop, or a save that left it): take it, and any
    // above it, off the stack; refresh what is beneath if something was saved in it.
    function closed(layer) {
        var i = layers.indexOf(layer);
        if (i === -1) return;
        var removed = layers.splice(i);
        var changed = removed.some(function (l) { return l.changed; });
        removed.forEach(function (l) { if (l.dialog.open) l.dialog.close(); l.dialog.remove(); });
        if (!changed || layer.silent) return;
        var under = top();
        // A pop-up holding a form someone is part-way through (data-modal-stay) is not reloaded, so nothing typed
        // in it is lost; whatever is under it is refreshed when it closes in turn.
        if (under && under.body.querySelector("[data-modal-stay]")) { under.changed = true; return; }
        if (under) { under.changed = true; load(under.url, { method: "GET" }, false, under); }
        else if (here()) load(window.location.href, { method: "GET" }, false, null);
    }
    // Closing a pop-up from here takes it off the page at once (the dialog's own close event comes a moment
    // later, and finds nothing left to do), so two copies of a page are never in the document together.
    function dismiss(layer, silent) {
        if (silent) layer.silent = true;
        if (layer.dialog.open) layer.dialog.close();
        closed(layer);
    }
    function closeAll() {
        layers.slice().reverse().forEach(function (l) { dismiss(l, true); });
    }

    function showIn(layer, html, url) {
        layer.body.innerHTML = html;
        layer.url = url;
        var t = layer.body.querySelector("[data-fragment-title]");
        if (t) layer.dialog.setAttribute("aria-label", t.textContent.trim());
        // A page may name the other addresses that are the same thing seen another way (a payment's receipt and
        // its "apply to fees" view), so a save that lands on either stays here; and ask for a wider pop-up.
        var home = layer.body.querySelector("[data-modal-scope]");
        layer.scope = home ? home.getAttribute("data-modal-scope").split(/\s+/).filter(Boolean) : [];
        var sized = layer.body.querySelector("[data-modal-size]");
        if (sized) layer.dialog.setAttribute("data-size", sized.getAttribute("data-modal-size")); else layer.dialog.removeAttribute("data-size");
        runScripts(layer.body);
        if (!layer.dialog.open) layer.dialog.showModal();
        var focus = layer.body.querySelector("[autofocus], input:not([type=hidden]):not([type=search]), select, textarea");
        if (focus) focus.focus({ preventScroll: true });
    }

    function openModal(href, replace) {
        var url = new URL(href, window.location.href);
        url.searchParams.set("modal", "1");
        url.searchParams.set("return_to", window.location.pathname + window.location.search);
        var layer = replace && top() ? top() : newLayer();
        layer.path = url.pathname;
        layer.scope = [];
        if (!layer.dialog.open) {
            layer.body.innerHTML = '<div class="ac-modal__wait" role="status" aria-label="Loading"></div>';
            layer.dialog.showModal();
        }
        load(url.href, { method: "GET" }, false, layer);
    }

    /* ---- fetching ---------------------------------------------------------------------------- */
    function load(url, options, push, layer) {
        if (busy) busy.abort();
        busy = new AbortController();
        var target = layer ? layer.body : body;
        spinner(target, true);
        options = options || {};
        // X-Modal tells the page it is in a pop-up, and survives redirects (a save that comes back here).
        options.headers = layer ? { "X-Fragment": "1", "X-Modal": "1" } : { "X-Fragment": "1" };
        options.credentials = "same-origin";
        options.signal = busy.signal;
        var posted = options.method === "POST";
        return fetch(url, options).then(function (r) {
            var type = r.headers.get("Content-Type") || "";
            var final = new URL(r.url);
            // A redirect that left the section, or anything that is not a page, opens normally.
            if (type.indexOf("text/html") !== 0 || !inScope(final)) {
                window.location.href = r.url;
                return;
            }
            return r.text().then(function (html) {
                if (layer && layers.indexOf(layer) !== -1) {
                    // This pop-up's own page, or one beneath its address: stay here.
                    if (owns(layer, final.pathname)) { if (posted) layer.changed = true; showIn(layer, html, r.url); return; }
                    // A page a pop-up further down shows: close the ones above it and show it there.
                    for (var i = layers.indexOf(layer) - 1; i >= 0; i--) {
                        if (owns(layers[i], final.pathname)) {
                            var keep = layers[i];
                            layers.slice(i + 1).reverse().forEach(function (l) { dismiss(l, true); });
                            keep.changed = keep.changed || posted;
                            showIn(keep, html, r.url);
                            return;
                        }
                    }
                    closeAll();
                }
                show(html, r.url, push || r.redirected);
            });
        }).catch(function (err) {
            if (err && err.name === "AbortError") return;
            window.location.href = typeof url === "string" ? url : String(url);
        }).finally(function () {
            spinner(target, false); busy = null;
            Array.prototype.forEach.call(document.querySelectorAll(".is-busy[data-inplace-busy]"), function (b) {
                b.classList.remove("is-busy"); b.removeAttribute("data-inplace-busy");
            });
        });
    }

    document.addEventListener("click", function (e) {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        var a = e.target.closest("a[href]");
        if (!a) return;
        var layer = layerOf(a);
        // A link to a part of the same pop-up scrolls the pop-up; the address (and so the page under it) stays.
        if (layer && (a.getAttribute("href") || "").charAt(0) === "#") {
            e.preventDefault();
            var part = layer.body.querySelector('[id="' + a.getAttribute("href").slice(1).replace(/"/g, "") + '"]');
            if (part) part.scrollIntoView({ block: "start", behavior: "smooth" });
            return;
        }
        if (!body.contains(a) && !layer && !a.closest("[data-inplace]")) return;
        if (a.target || a.hasAttribute("download") || "full" in a.dataset) return;
        var url = new URL(a.href, window.location.href);
        if (!here() || !inScope(url)) return;
        if (!layer && url.pathname === window.location.pathname && url.search === window.location.search && url.hash) return;
        e.preventDefault();
        if ("modal" in a.dataset) { openModal(a.href, a.dataset.modal === "replace"); return; }
        // A plain link inside a pop-up: its own pages load in it; anything else becomes the page underneath.
        if (layer && owns(layer, url.pathname)) { url.searchParams.set("modal", "1"); load(url.href, { method: "GET" }, false, layer); return; }
        if (layer) closeAll();
        load(url.href, { method: "GET" }, true, null);
    });

    document.addEventListener("submit", function (e) {
        if (e.defaultPrevented) return;   // a data-confirm the person cancelled (static/interactions.js), or an unfinished step
        var form = e.target;
        if (!(form instanceof HTMLFormElement) || form.target || "full" in form.dataset) return;
        var layer = layerOf(form);
        if (!body.contains(form) && !layer) return;
        var url = new URL(form.getAttribute("action") || (layer ? layer.url || layer.path : window.location.href), window.location.href);
        if (!here() || !inScope(url)) return;
        var data = e.submitter ? new FormData(form, e.submitter) : new FormData(form);
        e.preventDefault();
        // The button pressed shows a spinner until the answer arrives.
        var button = e.submitter || form.querySelector("button[type=submit], button:not([type])");
        if (button && button.classList) { button.classList.add("is-busy"); button.setAttribute("data-inplace-busy", ""); }
        if ((form.method || "get").toLowerCase() === "get") {
            url.search = new URLSearchParams(data).toString();
            if (layer) url.searchParams.set("modal", "1");
            load(url.href, { method: "GET" }, !layer, layer);
        } else {
            load(url.href, { method: "POST", body: data }, true, layer);
        }
    });

    window.addEventListener("popstate", function () {
        closeAll();
        if (here()) load(window.location.href, { method: "GET" }, false, null);
    });
})();
