/* The ID card designer (templates/school_id_card_designer.html). The card is drawn here from the design, at K pixels a
   millimetre, the same way templates/includes/_id_card.html prints it at real size. Items are dragged, resized, nudged
   and styled; Save sends the whole design, which the server rebuilds from known keys only (core/id_cards.py). Every
   text is set with textContent, never as HTML. A locked design is shown, not edited. */
(function () {
    "use strict";
    var root = document.querySelector("[data-idd]");
    if (!root || root.dataset.ready) return;
    root.dataset.ready = "1";
    var cfg = JSON.parse(document.getElementById("iddData").textContent);
    var layout = cfg.layout;
    var locked = root.dataset.locked === "1";
    var K = 5;                       // pixels a millimetre on screen
    var PT = 0.3528;                 // millimetres a point
    var SIZES = { landscape: [85.6, 54], portrait: [54, 85.6] };
    var COLOURS = [["brand", "School colour"], ["accent", "School accent"], ["white", "White"], ["ink", "Black"]];
    var side = "front", selected = -1, dirty = false;
    var canvas = root.querySelector("[data-canvas]");
    var status = root.querySelector("[data-idd-status]");

    function colour(name) {
        return { brand: "var(--idc-brand)", accent: "var(--idc-accent)", white: "#ffffff", ink: "#15171a" }[name] || name;
    }
    function items() { return layout.sides[side]; }
    function size() { return SIZES[layout.orientation] || SIZES.landscape; }
    function round(n) { return Math.round(n * 2) / 2; }
    function markDirty() {
        dirty = true;
        if (status && !locked) { status.textContent = "Changed, not saved yet. Press Save design to keep it."; status.classList.add("is-dirty"); }
    }

    /* ------------------------------------------------ drawing */
    function draw() {
        var s = size();
        canvas.textContent = "";
        canvas.style.width = s[0] * K + "px";
        canvas.style.height = s[1] * K + "px";
        canvas.style.background = colour(layout.background);
        items().forEach(function (el, i) {
            var box = document.createElement("div");
            box.className = "idd-el idd-" + el.type + (i === selected ? " is-selected" : "");
            box.style.left = el.x * K + "px"; box.style.top = el.y * K + "px";
            box.style.width = el.w * K + "px"; box.style.height = el.h * K + "px";
            if (el.radius) box.style.borderRadius = el.radius * K + "px";
            if (el.type === "rect") box.style.background = colour(el.fill);
            if (el.type === "text" || el.type === "field") {
                box.style.fontSize = el.size * PT * K + "px";
                box.style.textAlign = el.align;
                box.style.color = colour(el.color);
                if (el.bold) box.style.fontWeight = "700";
                if (el.upper) { box.style.textTransform = "uppercase"; box.style.letterSpacing = ".02em"; }
                if (el.type === "field" && el.label) {
                    var l = document.createElement("span"); l.className = "idd-label"; l.textContent = el.label + ": "; box.appendChild(l);
                }
                box.appendChild(document.createTextNode(el.type === "text" ? el.text : (cfg.data[el.field] || "[" + (cfg.fields[el.field] || el.field) + "]")));
            }
            if (el.type === "photo") {
                box.style.borderColor = colour(el.border);
                if (cfg.photo) { var img = document.createElement("img"); img.src = cfg.photo; img.alt = ""; box.appendChild(img); }
                else box.textContent = cfg.initials || "Photo";
            }
            if (el.type === "logo" || el.type === "signature") {
                var src = el.type === "logo" ? cfg.logo : cfg.signature;
                if (src) { var im = document.createElement("img"); im.src = src; im.alt = ""; box.appendChild(im); }
                else box.textContent = el.type === "logo" ? "Logo" : "Signature";
            }
            if (el.type === "qr" && cfg.qr) {
                var ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg");
                svg.setAttribute("viewBox", "0 0 " + cfg.qr[1] + " " + cfg.qr[1]);
                svg.setAttribute("shape-rendering", "crispEdges");
                var bg = document.createElementNS(ns, "rect"); bg.setAttribute("width", "100%"); bg.setAttribute("height", "100%"); bg.setAttribute("fill", "#fff");
                var path = document.createElementNS(ns, "path"); path.setAttribute("d", cfg.qr[0]); path.setAttribute("fill", "#15171a");
                svg.appendChild(bg); svg.appendChild(path); box.appendChild(svg);
            }
            if (!locked) {
                var handle = document.createElement("span"); handle.className = "idd-handle"; box.appendChild(handle);
                box.addEventListener("pointerdown", function (e) { startDrag(e, i, e.target === handle); });
            }
            canvas.appendChild(box);
        });
        root.querySelectorAll("[data-side]").forEach(function (b) { b.classList.toggle("is-active", b.dataset.side === side); });
        root.querySelector('[data-card="orientation"]').value = layout.orientation;
        root.querySelector('[data-card="background"]').value = layout.background;
        props();
    }

    /* ------------------------------------------------ moving and resizing */
    function startDrag(e, i, resize) {
        e.preventDefault(); e.stopPropagation();
        select(i);
        var el = items()[i], s = size(), x0 = e.clientX, y0 = e.clientY, start = { x: el.x, y: el.y, w: el.w, h: el.h };
        function move(ev) {
            var dx = (ev.clientX - x0) / K, dy = (ev.clientY - y0) / K;
            if (resize) {
                el.w = Math.max(1, Math.min(s[0] - el.x, round(start.w + dx)));
                el.h = Math.max(1, Math.min(s[1] - el.y, round(start.h + dy)));
            } else {
                el.x = Math.max(0, Math.min(s[0] - el.w, round(start.x + dx)));
                el.y = Math.max(0, Math.min(s[1] - el.h, round(start.y + dy)));
            }
            draw(); markDirty();
        }
        function up() { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); }
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
    }
    canvas.addEventListener("pointerdown", function (e) { if (e.target === canvas) select(-1); });
    canvas.addEventListener("keydown", function (e) {
        if (locked || selected < 0) return;
        var el = items()[selected], step = e.shiftKey ? 2 : 0.5, s = size();
        var moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
        if (moves[e.key]) {
            e.preventDefault();
            el.x = Math.max(0, Math.min(s[0] - el.w, round(el.x + moves[e.key][0])));
            el.y = Math.max(0, Math.min(s[1] - el.h, round(el.y + moves[e.key][1])));
            draw(); markDirty();
        } else if (e.key === "Delete" || e.key === "Backspace") {
            e.preventDefault(); act("delete");
        }
    });

    /* ------------------------------------------------ the selected item's properties */
    function select(i) { selected = i; draw(); if (i >= 0) canvas.focus({ preventScroll: true }); }
    var propsBox = root.querySelector("[data-props]");
    function props() {
        var el = items()[selected];
        propsBox.querySelector("[data-props-body]").hidden = !el;
        propsBox.querySelector("[data-props-empty]").hidden = !!el;
        var names = { text: "Text", field: "Detail", photo: "Photo", logo: "Logo", qr: "QR code", rect: "Colour block", signature: "Signature" };
        propsBox.querySelector("[data-props-title]").textContent = el ? names[el.type] : "Nothing selected";
        if (!el) return;
        propsBox.querySelectorAll("[data-for]").forEach(function (row) { row.hidden = row.dataset.for.split(" ").indexOf(el.type) < 0; });
        propsBox.querySelectorAll("[data-p]").forEach(function (input) {
            var key = input.dataset.p, value = el[key];
            if (input.type === "checkbox") input.checked = !!value;
            else input.value = value === undefined || value === null ? "" : value;
            input.disabled = locked;
        });
        propsBox.querySelectorAll("[data-act]").forEach(function (b) { b.disabled = locked; });
    }
    propsBox.addEventListener("input", function (e) {
        var input = e.target, key = input.dataset.p, el = items()[selected];
        if (!key || !el || locked) return;
        if (input.type === "checkbox") el[key] = input.checked;
        else if (input.type === "number") { var n = parseFloat(input.value); if (!isNaN(n)) el[key] = n; }
        else el[key] = input.value;
        var s = size();
        el.x = Math.max(0, Math.min(s[0] - 1, el.x)); el.y = Math.max(0, Math.min(s[1] - 1, el.y));
        el.w = Math.max(1, Math.min(s[0] - el.x, el.w)); el.h = Math.max(1, Math.min(s[1] - el.y, el.h));
        draw(); markDirty();
    });
    function act(what) {
        var list = items(), el = list[selected];
        if (!el || locked) return;
        if (what === "delete") { list.splice(selected, 1); selected = -1; }
        if (what === "copy") {
            if (list.length >= cfg.max) return;
            var copy = JSON.parse(JSON.stringify(el)); copy.x = Math.min(size()[0] - copy.w, copy.x + 2); copy.y = Math.min(size()[1] - copy.h, copy.y + 2);
            list.push(copy); selected = list.length - 1;
        }
        if (what === "up" && selected < list.length - 1) { list.splice(selected, 1); list.splice(selected + 1, 0, el); selected++; }
        if (what === "down" && selected > 0) { list.splice(selected, 1); list.splice(selected - 1, 0, el); selected--; }
        draw(); markDirty();
    }
    propsBox.querySelectorAll("[data-act]").forEach(function (b) { b.addEventListener("click", function () { act(b.dataset.act); }); });

    /* ------------------------------------------------ adding, the card, the sides */
    var defaults = {
        field: { w: 40, h: 5, size: 8, align: "left", color: "ink", field: Object.keys(cfg.fields)[0], label: "" },
        text: { w: 40, h: 5, size: 8, align: "left", color: "ink", text: "Text" },
        photo: { w: 22, h: 27, radius: 1.5, border: "brand" },
        logo: { w: 10, h: 10, radius: 0 },
        qr: { w: 16, h: 16, radius: 0 },
        rect: { w: 30, h: 6, fill: "brand", radius: 0 },
        signature: { w: 26, h: 9 }
    };
    root.querySelectorAll("[data-add]").forEach(function (b) {
        b.disabled = locked;
        b.addEventListener("click", function () {
            var list = items();
            if (list.length >= cfg.max) { if (window.paperToast) window.paperToast("A side can hold at most " + cfg.max + " items."); return; }
            var el = Object.assign({ type: b.dataset.add, x: 4, y: 4 }, defaults[b.dataset.add]);
            list.push(el); selected = list.length - 1; draw(); markDirty();
        });
    });
    root.querySelectorAll("[data-colour]").forEach(function (sel) {
        COLOURS.forEach(function (c) { var o = document.createElement("option"); o.value = c[0]; o.textContent = c[1]; sel.appendChild(o); });
        sel.disabled = locked;
    });
    root.querySelector('[data-card="orientation"]').disabled = locked;
    root.querySelectorAll("[data-card]").forEach(function (input) {
        input.addEventListener("change", function () {
            if (locked) return;
            layout[input.dataset.card] = input.value;
            if (input.dataset.card === "orientation") {
                var s = size();
                ["front", "back"].forEach(function (sd) { layout.sides[sd].forEach(function (el) {
                    el.w = Math.min(el.w, s[0]); el.h = Math.min(el.h, s[1]);
                    el.x = Math.max(0, Math.min(s[0] - el.w, el.x)); el.y = Math.max(0, Math.min(s[1] - el.h, el.y));
                }); });
            }
            draw(); markDirty();
        });
    });
    root.querySelectorAll("[data-side]").forEach(function (b) { b.addEventListener("click", function () { side = b.dataset.side; selected = -1; draw(); }); });

    /* ------------------------------------------------ saving */
    function save() {
        var body = new FormData();
        body.append("_csrf_token", document.getElementById("csrfToken").value);
        body.append("layout", JSON.stringify(layout));
        return fetch(root.dataset.saveUrl, { method: "POST", body: body, credentials: "same-origin" })
            .then(function (r) { return r.json().then(function (j) { return { ok: r.ok && j.ok, j: j }; }); })
            .then(function (res) {
                if (!res.ok) { status.textContent = res.j.error || "The design could not be saved."; status.classList.add("is-error"); return false; }
                layout = res.j.layout; dirty = false; draw();
                status.textContent = "Saved. Print cards from the ID cards page."; status.classList.remove("is-dirty", "is-error");
                return true;
            })
            .catch(function () { status.textContent = "The design could not be saved. Check your connection."; status.classList.add("is-error"); return false; });
    }
    var saveBtn = root.querySelector("[data-idd-save]");
    if (saveBtn) saveBtn.addEventListener("click", function () { saveBtn.disabled = true; save().finally(function () { saveBtn.disabled = false; }); });
    var lockForm = root.querySelector("[data-idd-lock]");
    if (lockForm) lockForm.addEventListener("submit", function (e) {
        if (!dirty) return;
        e.preventDefault();                      // save what is on screen first, then lock it
        save().then(function (ok) { if (ok) lockForm.submit(); });
    });
    window.addEventListener("beforeunload", function (e) { if (dirty && !locked) { e.preventDefault(); e.returnValue = ""; } });

    draw();
})();
