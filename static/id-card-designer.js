/* The ID card studio (templates/school_id_card_designer.html): a design tool for the school's student and staff cards.

   The card is drawn by static/id-card-render.js, at real size in millimetres, inside a box scaled to the zoom; this
   file adds everything around it: picking items (click, Shift-click, dragging a box round them), moving them with
   snapping to the card and to each other, resizing from eight handles, rotating, nudging with the arrow keys,
   aligning and spacing several at once, layers (order, show, lock, rename), undo and redo, copy and paste, zoom and a
   grid, every setting of the selected item, the school's own images, earlier versions and starting templates.

   Save sends the whole design; the server rebuilds it from known keys only (core/id_cards.py) and sends back what it
   kept, which is what is then shown. Every text is set with textContent or a form field's value, never as HTML. A
   locked design can be looked at, not changed.

   The same studio designs the school's report card (templates/school_report_card_studio.html, cfg.mode "report"): an
   A4 page or two instead of a card's two sides, and the report card's own blocks - the results table, the summary,
   attendance, the trait ratings, the grading key and the teacher's comment - drawn by static/report-card-render.js.
   And the school's receipt (templates/school_receipt_studio.html, cfg.mode "receipt"): an A5 or A4 page with the
   amount paid, the balance and a PAID stamp, drawn by static/receipt-render.js. */
(function () {
    "use strict";
    var root = document.querySelector("[data-idd]");
    if (!root || root.dataset.ready) return;
    root.dataset.ready = "1";
    var cfg = JSON.parse(document.getElementById("iddData").textContent);
    // Lists of choices come as [key, name] pairs, in the order they should be offered (a JSON object would be sorted by
    // its keys on the way): kept as lookups, with their order beside them.
    var ORDER = new WeakMap();
    function ordered(x) {
        if (!Array.isArray(x)) return x;
        var o = {}; x.forEach(function (p) { o[p[0]] = p[1]; });
        ORDER.set(o, x.map(function (p) { return p[0]; }));
        return o;
    }
    function keysOf(o) { return ORDER.get(o) || Object.keys(o); }
    ["fields", "barcode_fields", "fonts", "result_columns", "summary_items"].forEach(function (k) { if (cfg[k]) cfg[k] = ordered(cfg[k]); });
    var R = window.IdCardRender;
    var PX = 96 / 25.4;                                  // CSS pixels a millimetre at zoom 1
    var layout = cfg.layout;
    var locked = !!cfg.locked;
    var RECEIPT = cfg.mode === "receipt";
    var DOCUMENT = cfg.mode === "document";                     // a testimonial, transcript or leaving certificate
    var REPORT = cfg.mode === "report" || RECEIPT || DOCUMENT;  // a page (A4 or A5), not a card
    var SIDES = cfg.sides || ["front", "back"];
    var NOUN = REPORT ? "page" : "card";
    // The whole-window layout (the report card studio): tabbed panels, a page fitted to the space, folding settings.
    var APP = root.classList.contains("idd--app");
    var userZoomed = false;
    var closedSections = APP ? { "Position and size": true, "Look": true, "Box and lines": true } : {};
    if (APP) document.documentElement.classList.add("rcs-open");
    var side = SIDES[0], selection = [], zoom = 1.7, showGrid = false, snapOn = true, dirty = false;
    var history = [], future = [], clipboard = null, uid = 0;
    var stage = root.querySelector("[data-stage]");
    var holder = root.querySelector("[data-holder]");
    var cardHost = root.querySelector("[data-card-host]");
    var overlay = root.querySelector("[data-overlay]");
    var status = root.querySelector("[data-idd-status]");
    var propsBox = root.querySelector("[data-props]");
    var layersBox = root.querySelector("[data-layers]");
    var COLOUR_NAMES = { brand: "School colour", accent: "School accent", white: "White", ink: "Black", none: "None" };
    var TYPE_NAMES = { text: "Text", field: "Detail", shape: "Shape", photo: "Photo", logo: "Logo", image: "Image",
                       signature: "Signature", qr: "QR code", barcode: "Barcode", results: "Results table", summary: "Summary",
                       traits: "Trait ratings", grading: "Grading key", comment: "Teacher's comment",
                       cumulative: "Session so far", amount: "Amount paid", stamp: "Paid stamp",
                       paragraph: "Paragraph", transcript: "Transcript" };
    if (RECEIPT) TYPE_NAMES.summary = "Balance";
    var WHO_NAMES = { head: "Head's signature", holder: "Their signature", teacher: "Class teacher's signature", authorised: "Authorised signature" };
    var SHAPE_NAMES = { rect: "Rectangle", ellipse: "Ellipse", triangle: "Triangle", diamond: "Diamond", hexagon: "Hexagon",
                        pentagon: "Pentagon", star: "Star", chevron: "Chevron", arrow: "Arrow", wave: "Wave", corner: "Corner sweep", line: "Line" };
    var CLIP_NAMES = { none: "None", circle: "Circle", ellipse: "Ellipse", rounded: "Rounded", hexagon: "Hexagon",
                       diamond: "Diamond", shield: "Shield", star: "Star", arch: "Arch" };

    /* ------------------------------------------------ small helpers */
    function el$(tag, cls, text) { var n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
    function items() { return layout.sides[side]; }
    function size() { return R.sizeOf ? R.sizeOf(layout) : (R.SIZES[layout.orientation] || R.SIZES.landscape); }
    var LIMIT = REPORT ? 300 : 180;                      // the largest box, in millimetres
    function r1(n) { return Math.round(n * 10) / 10; }
    function clamp(n, lo, hi) { return Math.max(lo, Math.min(hi, n)); }
    function k() { return PX * zoom; }                   // screen pixels a millimetre now
    function selectedItems() { return selection.map(function (i) { return items()[i]; }).filter(Boolean); }
    function single() { return selection.length === 1 ? items()[selection[0]] : null; }
    function newId() { return "n" + Date.now().toString(36).slice(-5) + (uid++).toString(36); }
    function say(text, kind) {
        if (!status) return;
        status.textContent = text;
        status.className = "idd-status" + (kind ? " is-" + kind : "");
    }
    function markDirty() {
        dirty = true;
        if (!locked) say("Changed, not saved yet. Press Save design to keep it.", "dirty");
    }
    function snapshot() { return JSON.stringify(layout); }
    function commit() {
        var now = snapshot();
        if (history.length && history[history.length - 1] === now) return;
        history.push(now);
        if (history.length > 120) history.shift();
        future = [];
        markDirty();
        updateUndo();
    }
    function restore(json) {
        layout = JSON.parse(json);
        selection = selection.filter(function (i) { return i < items().length; });
        redraw();
    }
    function undo() {
        if (locked || history.length < 2) return;
        future.push(history.pop());
        restore(history[history.length - 1]); markDirty(); updateUndo();
    }
    function redo() {
        if (locked || !future.length) return;
        var next = future.pop(); history.push(next); restore(next); markDirty(); updateUndo();
    }
    function updateUndo() {
        root.querySelectorAll("[data-tool=undo]").forEach(function (b) { b.disabled = locked || history.length < 2; });
        root.querySelectorAll("[data-tool=redo]").forEach(function (b) { b.disabled = locked || !future.length; });
    }
    function label(el) {
        if (el.name) return el.name;
        if (el.type === "text") return (el.text || "Text").split("\n")[0].slice(0, 24);
        if (el.type === "field") return (cfg.fields[el.field] || el.field);
        if (el.type === "shape") return SHAPE_NAMES[el.shape] || "Shape";
        if (el.type === "signature") return WHO_NAMES[el.who] || "Signature";
        if (el.type === "summary" && (el.items || []).indexOf("opened") >= 0 && (el.items || []).indexOf("total") < 0) return "Attendance";
        if (el.type === "barcode") return "Barcode: " + (cfg.barcode_fields[el.field] || el.field);
        if (el.type === "image") { var a = (cfg.asset_list || []).filter(function (x) { return x.id === el.asset; })[0]; return a ? a.name : "Image"; }
        return TYPE_NAMES[el.type] || el.type;
    }

    /* ------------------------------------------------ drawing */
    function redraw() {
        var s = size();
        holder.style.width = s[0] * k() + "px";
        holder.style.height = s[1] * k() + "px";
        var card = R.draw(cardHost, layout, side, cfg.view, { editor: true });
        card.style.transform = "scale(" + zoom + ")";
        card.style.transformOrigin = "0 0";
        drawOverlay();
        props();
        layers();
        root.querySelectorAll("[data-side]").forEach(function (b) { b.classList.toggle("is-active", b.dataset.side === side); b.setAttribute("aria-pressed", b.dataset.side === side); });
        var zl = root.querySelector("[data-zoom-label]"); if (zl) zl.textContent = Math.round(zoom / (REPORT ? 1 : 1.7) * 100) + "%";
        root.querySelectorAll("[data-tool=grid]").forEach(function (b) { b.classList.toggle("is-on", showGrid); });
        root.querySelectorAll("[data-tool=snap]").forEach(function (b) { b.classList.toggle("is-on", snapOn); });
        cardPanel();
    }

    function drawOverlay(guides) {
        overlay.textContent = "";
        overlay.style.width = holder.style.width; overlay.style.height = holder.style.height;
        overlay.classList.toggle("has-grid", showGrid);
        overlay.style.backgroundSize = 5 * k() + "px " + 5 * k() + "px";
        selectedItems().forEach(function (el) {
            var box = el$("div", "idd-sel" + (el.locked ? " is-locked" : ""));
            box.style.left = el.x * k() + "px"; box.style.top = el.y * k() + "px";
            box.style.width = el.w * k() + "px"; box.style.height = el.h * k() + "px";
            if (el.rotate) box.style.transform = "rotate(" + el.rotate + "deg)";
            if (selection.length === 1 && !locked && !el.locked) {
                ["nw", "n", "ne", "e", "se", "s", "sw", "w"].forEach(function (d) {
                    var h = el$("span", "idd-h idd-h-" + d); h.dataset.handle = d; box.appendChild(h);
                });
                var rot = el$("span", "idd-rot"); rot.dataset.handle = "rotate"; rot.title = "Drag to turn (Shift: steps of 15°)";
                box.appendChild(rot);
                var dim = el$("span", "idd-dim", r1(el.w) + " × " + r1(el.h) + " mm" + (el.rotate ? " · " + Math.round(el.rotate) + "°" : ""));
                box.appendChild(dim);
            }
            if (el.locked) box.appendChild(el$("span", "idd-lockmark", "Locked"));
            overlay.appendChild(box);
        });
        (guides || []).forEach(function (g) {
            var line = el$("div", "idd-guide idd-guide-" + g.axis);
            if (g.axis === "x") { line.style.left = g.at * k() + "px"; } else { line.style.top = g.at * k() + "px"; }
            overlay.appendChild(line);
        });
    }

    /* ------------------------------------------------ choosing items */
    function select(list, add) {
        if (add) {
            list.forEach(function (i) {
                var at = selection.indexOf(i);
                if (at >= 0) selection.splice(at, 1); else selection.push(i);
            });
        } else {
            selection = list.slice();
        }
        drawOverlay(); props(); layers();
        if (APP && selection.length) showTab("right", "props");
    }
    function indexAt(e) {
        var node = e.target.closest && e.target.closest(".idc-el");
        return node && cardHost.contains(node) ? Number(node.dataset.i) : -1;
    }

    /* ------------------------------------------------ moving, resizing, turning, picking with a box */
    function pointer(e) {
        var r = holder.getBoundingClientRect();
        return { x: (e.clientX - r.left) / k(), y: (e.clientY - r.top) / k() };
    }
    function bounds(list) {
        var x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
        list.forEach(function (el) { x1 = Math.min(x1, el.x); y1 = Math.min(y1, el.y); x2 = Math.max(x2, el.x + el.w); y2 = Math.max(y2, el.y + el.h); });
        return { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
    }
    function snapMove(b) {
        // Line the moving box's edges and centre up with the card's and with every other item's, within 1 mm.
        if (!snapOn) return { dx: 0, dy: 0, guides: [] };
        var s = size(), tx = [0, s[0] / 2, s[0]], ty = [0, s[1] / 2, s[1]];
        items().forEach(function (el, i) {
            if (selection.indexOf(i) >= 0 || el.hidden) return;
            tx.push(el.x, el.x + el.w / 2, el.x + el.w); ty.push(el.y, el.y + el.h / 2, el.y + el.h);
        });
        var limit = Math.max(0.6, 6 / k());
        function best(points, targets) {
            var found = null;
            points.forEach(function (p) { targets.forEach(function (t) {
                var d = t - p; if (Math.abs(d) <= limit && (!found || Math.abs(d) < Math.abs(found.d))) found = { d: d, at: t };
            }); });
            return found;
        }
        var bx = best([b.x, b.x + b.w / 2, b.x + b.w], tx), by = best([b.y, b.y + b.h / 2, b.y + b.h], ty);
        var guides = [];
        if (bx) guides.push({ axis: "x", at: bx.at });
        if (by) guides.push({ axis: "y", at: by.at });
        return { dx: bx ? bx.d : 0, dy: by ? by.d : 0, guides: guides };
    }
    function grid(n) { return snapOn && showGrid ? Math.round(n / 0.5) * 0.5 : r1(n); }

    function startMove(e, list) {
        var start = pointer(e), orig = list.map(function (i) { var el = items()[i]; return { i: i, x: el.x, y: el.y }; });
        var moved = false, movable = list.filter(function (i) { return !items()[i].locked; });
        if (!movable.length) return;
        function move(ev) {
            var p = pointer(ev), dx = p.x - start.x, dy = p.y - start.y;
            if (!moved && Math.abs(dx) * k() < 3 && Math.abs(dy) * k() < 3) return;
            if (ev.shiftKey) { if (Math.abs(dx) > Math.abs(dy)) dy = 0; else dx = 0; }
            moved = true;
            orig.forEach(function (o) { if (!items()[o.i].locked) { items()[o.i].x = o.x + dx; items()[o.i].y = o.y + dy; } });
            var snap = snapMove(bounds(movable.map(function (i) { return items()[i]; })));
            orig.forEach(function (o) {
                var el = items()[o.i]; if (el.locked) return;
                el.x = grid(el.x + snap.dx); el.y = grid(el.y + snap.dy);
            });
            R.draw(cardHost, layout, side, cfg.view, { editor: true }).style.cssText += ";transform:scale(" + zoom + ");transform-origin:0 0";
            drawOverlay(snap.guides);
        }
        function up() {
            window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up);
            if (moved) { commit(); redraw(); }
        }
        window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
    }

    function startHandle(e, handle) {
        e.preventDefault(); e.stopPropagation();
        var el = single(); if (!el || el.locked) return;
        var start = pointer(e), o = { x: el.x, y: el.y, w: el.w, h: el.h, rotate: el.rotate || 0 };
        var cx = o.x + o.w / 2, cy = o.y + o.h / 2, ratio = o.w / o.h;
        function move(ev) {
            var p = pointer(ev);
            if (handle === "rotate") {
                var a = Math.atan2(p.y - cy, p.x - cx) * 180 / Math.PI + 90;
                if (a > 180) a -= 360;
                if (ev.shiftKey) a = Math.round(a / 15) * 15;
                el.rotate = Math.round(a * 10) / 10;
            } else {
                // The pointer's movement, turned into the item's own direction, so a turned item resizes along its sides.
                var t = -o.rotate * Math.PI / 180, dx0 = p.x - start.x, dy0 = p.y - start.y;
                var dx = dx0 * Math.cos(t) - dy0 * Math.sin(t), dy = dx0 * Math.sin(t) + dy0 * Math.cos(t);
                var w = o.w, h = o.h, x = o.x, y = o.y;
                if (handle.indexOf("e") >= 0) w = o.w + dx;
                if (handle.indexOf("w") >= 0) { w = o.w - dx; x = o.x + dx; }
                if (handle.indexOf("s") >= 0) h = o.h + dy;
                if (handle.indexOf("n") >= 0) { h = o.h - dy; y = o.y + dy; }
                if (ev.shiftKey && handle.length === 2) {          // keep the shape
                    if (Math.abs(w - o.w) > Math.abs(h - o.h) * ratio) { var nh = w / ratio; if (handle.indexOf("n") >= 0) y = o.y + o.h - nh; h = nh; }
                    else { var nw = h * ratio; if (handle.indexOf("w") >= 0) x = o.x + o.w - nw; w = nw; }
                }
                if (w < 0.5) { if (handle.indexOf("w") >= 0) x = o.x + o.w - 0.5; w = 0.5; }
                if (h < 0.5) { if (handle.indexOf("n") >= 0) y = o.y + o.h - 0.5; h = 0.5; }
                el.x = grid(x); el.y = grid(y); el.w = grid(w); el.h = grid(h);
                if (el.type === "photo" && el.clip === "circle") el.h = el.w;
            }
            R.draw(cardHost, layout, side, cfg.view, { editor: true }).style.cssText += ";transform:scale(" + zoom + ");transform-origin:0 0";
            drawOverlay();
        }
        function up() { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); commit(); redraw(); }
        window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
    }

    function startMarquee(e, add) {
        var start = pointer(e), box = el$("div", "idd-marquee");
        overlay.appendChild(box);
        function move(ev) {
            var p = pointer(ev), x = Math.min(p.x, start.x), y = Math.min(p.y, start.y), w = Math.abs(p.x - start.x), h = Math.abs(p.y - start.y);
            box.style.left = x * k() + "px"; box.style.top = y * k() + "px"; box.style.width = w * k() + "px"; box.style.height = h * k() + "px";
            box.dataset.x = x; box.dataset.y = y; box.dataset.w = w; box.dataset.h = h;
        }
        function up() {
            window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up);
            var x = +box.dataset.x || 0, y = +box.dataset.y || 0, w = +box.dataset.w || 0, h = +box.dataset.h || 0;
            box.remove();
            if (w < 0.5 && h < 0.5) { if (!add) select([]); return; }
            var hit = [];
            items().forEach(function (el, i) { if (!el.hidden && el.x < x + w && el.x + el.w > x && el.y < y + h && el.y + el.h > y) hit.push(i); });
            select(hit, add);
        }
        window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
    }

    stage.addEventListener("pointerdown", function (e) {
        if (e.button !== 0) return;
        var handle = e.target.dataset && e.target.dataset.handle;
        if (handle) { if (!locked) startHandle(e, handle); return; }
        var i = indexAt(e);
        if (i < 0) { e.preventDefault(); startMarquee(e, e.shiftKey); stage.focus({ preventScroll: true }); return; }
        e.preventDefault();
        stage.focus({ preventScroll: true });
        if (e.shiftKey) { select([i], true); return; }
        if (selection.indexOf(i) < 0) select([i]);
        if (!locked) startMove(e, selection.slice());
    });
    stage.addEventListener("dblclick", function (e) {
        var i = indexAt(e); if (i < 0) return;
        select([i]);
        var area = propsBox.querySelector("[data-key=text]");
        if (area) { area.focus(); area.select(); }
    });

    /* ------------------------------------------------ the keyboard */
    stage.addEventListener("keydown", function (e) {
        var mod = e.ctrlKey || e.metaKey, key = e.key;
        if (mod && key.toLowerCase() === "z") { e.preventDefault(); if (e.shiftKey) redo(); else undo(); return; }
        if (mod && key.toLowerCase() === "y") { e.preventDefault(); redo(); return; }
        if (mod && key.toLowerCase() === "a") { e.preventDefault(); select(items().map(function (_, i) { return i; })); return; }
        if (key === "Escape") { select([]); return; }
        if (locked || !selection.length) return;
        if (mod && key.toLowerCase() === "c") { e.preventDefault(); act("copy"); return; }
        if (mod && key.toLowerCase() === "x") { e.preventDefault(); act("copy"); act("delete"); return; }
        if (mod && key.toLowerCase() === "d") { e.preventDefault(); act("duplicate"); return; }
        if (mod && key === "]") { e.preventDefault(); act(e.shiftKey ? "front" : "forward"); return; }
        if (mod && key === "[") { e.preventDefault(); act(e.shiftKey ? "back" : "backward"); return; }
        var step = e.shiftKey ? 2 : (e.altKey ? 0.1 : 0.5);
        var moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
        if (moves[key]) {
            e.preventDefault();
            selectedItems().forEach(function (el) { if (!el.locked) { el.x = r1(el.x + moves[key][0]); el.y = r1(el.y + moves[key][1]); } });
            commit(); redraw();
        } else if (key === "Delete" || key === "Backspace") {
            e.preventDefault(); act("delete");
        }
    });
    document.addEventListener("keydown", function (e) {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "v" && clipboard && !locked && document.activeElement === stage) {
            e.preventDefault(); paste();
        }
    });

    /* ------------------------------------------------ acting on the selection */
    function paste() {
        if (!clipboard) return;
        var list = items(), added = [];
        clipboard.forEach(function (c) {
            if (list.length >= cfg.max) return;
            var el = JSON.parse(JSON.stringify(c)); el.id = newId(); el.x = r1(el.x + 2); el.y = r1(el.y + 2); el.locked = false;
            list.push(el); added.push(list.length - 1);
        });
        clipboard = clipboard.map(function (c) { var n = JSON.parse(JSON.stringify(c)); n.x += 2; n.y += 2; return n; });
        selection = added; commit(); redraw();
    }
    function act(what) {
        if (locked) return;
        var list = items(), chosen = selection.slice().sort(function (a, b) { return a - b; });
        if (!chosen.length) return;
        if (what === "copy") { clipboard = chosen.map(function (i) { return JSON.parse(JSON.stringify(list[i])); }); say("Copied. Press Ctrl+V on the card to paste."); return; }
        if (what === "duplicate") { clipboard = chosen.map(function (i) { return JSON.parse(JSON.stringify(list[i])); }); paste(); return; }
        if (what === "delete") {
            chosen.slice().reverse().forEach(function (i) { if (!list[i].locked) list.splice(i, 1); });
            selection = []; commit(); redraw(); return;
        }
        if (what === "lock" || what === "hide") {
            var key = what === "lock" ? "locked" : "hidden", on = !chosen.every(function (i) { return list[i][key]; });
            chosen.forEach(function (i) { list[i][key] = on; }); commit(); redraw(); return;
        }
        var moving = chosen.map(function (i) { return list[i]; }), rest = list.filter(function (_, i) { return chosen.indexOf(i) < 0; });
        var at;
        if (what === "front") at = rest.length;
        else if (what === "back") at = 0;
        else if (what === "forward") at = Math.min(rest.length, chosen[0] + 1);
        else if (what === "backward") at = Math.max(0, chosen[0] - 1);
        else return;
        rest.splice.apply(rest, [at, 0].concat(moving));
        layout.sides[side] = rest;
        selection = moving.map(function (el) { return rest.indexOf(el); });
        commit(); redraw();
    }
    function align(how) {
        if (locked) return;
        var list = selectedItems().filter(function (el) { return !el.locked; });
        if (!list.length) return;
        var s = size(), b = selection.length > 1 ? bounds(list) : { x: 0, y: 0, w: s[0], h: s[1] };
        list.forEach(function (el) {
            if (how === "left") el.x = b.x;
            if (how === "hcenter") el.x = r1(b.x + (b.w - el.w) / 2);
            if (how === "right") el.x = r1(b.x + b.w - el.w);
            if (how === "top") el.y = b.y;
            if (how === "vmiddle") el.y = r1(b.y + (b.h - el.h) / 2);
            if (how === "bottom") el.y = r1(b.y + b.h - el.h);
        });
        if ((how === "hspace" || how === "vspace") && list.length > 2) {
            var horiz = how === "hspace", sorted = list.slice().sort(function (a, c) { return horiz ? a.x - c.x : a.y - c.y; });
            var first = sorted[0], last = sorted[sorted.length - 1];
            var span = horiz ? (last.x + last.w - first.x) : (last.y + last.h - first.y);
            var used = sorted.reduce(function (t, el) { return t + (horiz ? el.w : el.h); }, 0);
            var gap = (span - used) / (sorted.length - 1), pos = horiz ? first.x : first.y;
            sorted.forEach(function (el) { if (horiz) { el.x = r1(pos); pos += el.w + gap; } else { el.y = r1(pos); pos += el.h + gap; } });
        }
        commit(); redraw();
    }
    root.querySelectorAll("[data-align]").forEach(function (b) { b.addEventListener("click", function () { align(b.dataset.align); }); });
    root.querySelectorAll("[data-act]").forEach(function (b) { b.addEventListener("click", function () { act(b.dataset.act); stage.focus({ preventScroll: true }); }); });

    /* ------------------------------------------------ the toolbar */
    function setZoom(z) { zoom = clamp(z, REPORT ? 0.25 : 0.6, 5); redraw(); }
    function fitZoom() {
        var s = size(), w = stage.clientWidth - 40, h = APP ? stage.clientHeight - 40 : Math.max(260, window.innerHeight - 260);
        userZoomed = false;
        setZoom(REPORT && !APP ? w / (s[0] * PX) : Math.min(w / (s[0] * PX), h / (s[1] * PX)));
    }
    root.querySelectorAll("[data-tool]").forEach(function (b) {
        b.addEventListener("click", function () {
            var t = b.dataset.tool;
            if (t === "undo") undo();
            if (t === "redo") redo();
            if (t === "zoomin") { userZoomed = true; setZoom(zoom * 1.2); }
            if (t === "zoomout") { userZoomed = true; setZoom(zoom / 1.2); }
            if (t === "fit") fitZoom();
            if (t === "grid") { showGrid = !showGrid; redraw(); }
            if (t === "snap") { snapOn = !snapOn; redraw(); }
            stage.focus({ preventScroll: true });
        });
    });
    stage.addEventListener("wheel", function (e) {
        if (!(e.ctrlKey || e.metaKey)) return;
        e.preventDefault(); userZoomed = true; setZoom(zoom * (e.deltaY < 0 ? 1.1 : 1 / 1.1));
    }, { passive: false });
    root.querySelectorAll("[data-side]").forEach(function (b) {
        b.addEventListener("click", function () { side = b.dataset.side; selection = []; redraw(); });
    });

    /* ------------------------------------------------ adding */
    var FONT = cfg.default_font || "sans";
    var DEFAULTS = {
        text: { w: 40, h: 6, text: "Your text", font: "sans", size: 9, weight: 400, italic: false, underline: false, upper: false, color: "ink",
                align: "left", valign: "top", letter: 0, line: 118, fill: "none", radius: 0, pad: 0 },
        field: { w: 40, h: 5, field: "name", label: "", font: "sans", size: 8, weight: 400, italic: false, underline: false, upper: false, color: "ink",
                 align: "left", valign: "top", letter: 0, line: 118, fill: "none", radius: 0, pad: 0 },
        shape: { w: 24, h: 14, shape: "rect", fill: "brand", stroke: "none", stroke_w: 0.4, dash: "solid", radius: 0 },
        photo: { w: 22, h: 27, fit: "cover", clip: "rounded", radius: 1.5, border: "white", border_w: 0.5 },
        logo: { w: 12, h: 12, fit: "contain", clip: "none", border: "none", border_w: 0.5 },
        image: { w: 20, h: 20, fit: "cover", clip: "none", border: "none", border_w: 0.5 },
        signature: { w: 26, h: 9, fit: "contain", who: "head", clip: "none", border: "none", border_w: 0.5 },
        qr: { w: 16, h: 16, fg: "ink", bg: "white", radius: 0 },
        barcode: { w: 34, h: 10, field: "serial", fg: "ink", bg: "white", show_text: true }
    };
    if (REPORT) {
        // A page is far bigger than a card: larger starting sizes, the report card's details, and its blocks.
        var BLOCK = { font: FONT, size: 8.5, color: "ink", head_fill: "brand", head_color: "white", line: "#c9d2de", line_w: 0.3,
                      fill: "none", zebra: "none", radius: 0, pad: 1.6, accent: "brand" };
        Object.assign(DEFAULTS.text, { w: 80, h: 8, size: 11 });
        Object.assign(DEFAULTS.field, { w: 70, h: 6, size: 10, field: "student_name" });
        Object.assign(DEFAULTS.shape, { w: 60, h: 30 });
        Object.assign(DEFAULTS.photo, { w: 30, h: 36 });
        Object.assign(DEFAULTS.logo, { w: 24, h: 24 });
        Object.assign(DEFAULTS.image, { w: 40, h: 40 });
        Object.assign(DEFAULTS.signature, { w: 50, h: 12 });
        Object.assign(DEFAULTS.barcode, { w: 50, h: 12, field: "admission_no" });
        DEFAULTS.results = Object.assign({ w: 186, h: 110, columns: ["subject", "ca", "exam", "total", "grade", "remark"], numbers: "center", head_size: 8.5, zebra: "#f3f6fa" }, BLOCK);
        DEFAULTS.summary = Object.assign({ w: 186, h: 16, items: ["total", "percentage", "grade", "position"], layout: "row", value_size: 11, line: "#c9d2de", radius: 2 }, BLOCK);
        DEFAULTS.traits = Object.assign({ w: 186, h: 40, layout: "side" }, BLOCK);
        DEFAULTS.grading = Object.assign({ w: 186, h: 14, per_row: 3, title: "Grading key" }, BLOCK);
        DEFAULTS.comment = Object.assign({ w: 186, h: 24, label: "Class teacher's comment", lines: true, comment_size: 9.5 }, BLOCK);
        DEFAULTS.cumulative = Object.assign({ w: 186, h: 70, numbers: "center", head_size: 8.5, zebra: "#f3f6fa" }, BLOCK);
        if (RECEIPT) {
            // A receipt is smaller than a report card: smaller starting sizes, and its own details and blocks.
            Object.assign(DEFAULTS.text, { w: 60, h: 7, size: 9 });
            Object.assign(DEFAULTS.field, { w: 70, h: 5.5, size: 8.5, field: "payer" });
            Object.assign(DEFAULTS.shape, { w: 40, h: 20 });
            Object.assign(DEFAULTS.logo, { w: 20, h: 20 });
            Object.assign(DEFAULTS.image, { w: 30, h: 30 });
            Object.assign(DEFAULTS.signature, { w: 44, h: 11, who: "authorised" });
            Object.assign(DEFAULTS.barcode, { w: 50, h: 12, field: "receipt_no" });
            DEFAULTS.amount = Object.assign({}, BLOCK, { w: 60, h: 34, label: "Amount paid", words: false, value_size: 16, fill: "#eef3f9", radius: 3, line: "brand", line_w: 0.4 });
            DEFAULTS.summary = Object.assign({}, BLOCK, { w: 120, h: 15, items: ["charged", "paid", "balance"], layout: "row", value_size: 10, size: 7, radius: 2 });
            DEFAULTS.stamp = Object.assign({}, BLOCK, { w: 30, h: 30, text: "PAID", shape: "round", show_date: true, accent: "#19734a", color: "#19734a", rotate: -12, opacity: 85 });
        }
        if (DOCUMENT) {
            // A school document: its own details, the head's signature, and the paragraph and transcript blocks.
            Object.assign(DEFAULTS.signature, { w: 52, h: 14, who: "head" });
            Object.assign(DEFAULTS.barcode, { w: 60, h: 12, field: "serial_no" });
            DEFAULTS.paragraph = Object.assign({}, BLOCK, { w: 166, h: 60, size: 12, line: "none", line_w: 0, align: "justify",
                                                            valign: "top", leading: 150, italic: false, pad: 0,
                                                            text: "This is to certify that {student_name} was a student of this school." });
            DEFAULTS.transcript = Object.assign({}, BLOCK, { w: 170, h: 110, numbers: "center", head_size: 8, zebra: "#f3f6fa", show_grade: true });
        }
    }
    Object.keys(DEFAULTS).forEach(function (t) { if (DEFAULTS[t].font === "sans") DEFAULTS[t].font = FONT; });
    function add(type, extra) {
        if (locked) return;
        var list = items();
        if (list.length >= cfg.max) { say("A " + (REPORT ? "page" : "side of the card") + " can hold at most " + cfg.max + " items.", "error"); return; }
        var s = size(), base = JSON.parse(JSON.stringify(DEFAULTS[type]));
        var el = Object.assign({ type: type, id: newId(), name: "", rotate: 0, opacity: 100, shadow: "none", locked: false, hidden: false }, base, extra || {});
        el.x = r1((s[0] - el.w) / 2); el.y = r1((s[1] - el.h) / 2);
        list.push(el); selection = [list.length - 1]; commit(); redraw(); stage.focus({ preventScroll: true });
    }
    root.querySelectorAll("[data-add]").forEach(function (b) {
        b.disabled = locked;
        b.addEventListener("click", function () {
            var extra = {};
            if (b.dataset.shape) { extra.shape = b.dataset.shape; if (b.dataset.shape === "line") { extra.h = 2; extra.w = 30; extra.stroke = "ink"; extra.fill = "none"; } if (b.dataset.shape === "ellipse" || b.dataset.shape === "star" || b.dataset.shape === "hexagon") { extra.w = 16; extra.h = 16; } }
            if (b.dataset.who) extra.who = b.dataset.who;
            if (b.dataset.field) extra.field = b.dataset.field;
            if (b.dataset.items) extra.items = b.dataset.items.split(",");
            if (b.dataset.label) extra.label = b.dataset.label;
            if (b.dataset.size) { var wh = b.dataset.size.split("x"); extra.w = Number(wh[0]); extra.h = Number(wh[1]); }
            add(b.dataset.add, extra);
        });
    });

    /* ------------------------------------------------ the colour picker */
    var recent = [];
    try { recent = JSON.parse(localStorage.getItem("idd-colours") || "[]").filter(function (c) { return /^#[0-9a-f]{6}$/i.test(c); }).slice(0, 8); } catch (e) { recent = []; }
    function remember(c) {
        if (!/^#[0-9a-f]{6}$/i.test(c)) return;
        recent = [c.toLowerCase()].concat(recent.filter(function (x) { return x !== c.toLowerCase(); })).slice(0, 8);
        try { localStorage.setItem("idd-colours", JSON.stringify(recent)); } catch (e) { /* not kept: fine */ }
    }
    var paint = R.colourOf(cfg.view);
    var openPop = null;
    function closePop() { if (openPop) { openPop.remove(); openPop = null; } }
    document.addEventListener("pointerdown", function (e) { if (openPop && !openPop.contains(e.target) && !e.target.closest("[data-colour-btn]")) closePop(); });
    function colourControl(value, allowNone, onChange) {
        var btn = el$("button", "idd-colour"); btn.type = "button"; btn.dataset.colourBtn = "1";
        var chip = el$("span", "idd-chip"), name = el$("span", "idd-chip-name");
        function show(v) {
            chip.style.background = v === "none" ? "repeating-linear-gradient(45deg,#fff 0 3px,#e3e6ea 3px 6px)" : paint(v);
            name.textContent = COLOUR_NAMES[v] || v;
        }
        show(value);
        btn.appendChild(chip); btn.appendChild(name);
        btn.disabled = locked;
        btn.addEventListener("click", function () {
            if (openPop) { closePop(); return; }
            var pop = el$("div", "idd-pop");
            var row = el$("div", "idd-swatches");
            ["brand", "accent", "white", "ink"].concat(allowNone ? ["none"] : []).concat(recent).forEach(function (c) {
                var s = el$("button", "idd-swatch"); s.type = "button"; s.title = COLOUR_NAMES[c] || c;
                s.style.background = c === "none" ? "repeating-linear-gradient(45deg,#fff 0 3px,#e3e6ea 3px 6px)" : paint(c);
                s.addEventListener("click", function () { show(c); onChange(c); closePop(); });
                row.appendChild(s);
            });
            pop.appendChild(row);
            var own = el$("div", "idd-own");
            var picker = document.createElement("input"); picker.type = "color";
            picker.value = /^#[0-9a-f]{6}$/i.test(value) ? value : paint(value).length === 7 ? paint(value) : "#1d6fd8";
            var hex = document.createElement("input"); hex.type = "text"; hex.maxLength = 7; hex.value = picker.value; hex.setAttribute("aria-label", "Colour code");
            picker.addEventListener("input", function () { hex.value = picker.value; show(picker.value); onChange(picker.value, true); });
            picker.addEventListener("change", function () { remember(picker.value); onChange(picker.value); });
            hex.addEventListener("change", function () {
                var v = hex.value.trim(); if (v[0] !== "#") v = "#" + v;
                if (/^#[0-9a-f]{6}$/i.test(v)) { picker.value = v.toLowerCase(); show(v.toLowerCase()); remember(v.toLowerCase()); onChange(v.toLowerCase()); }
            });
            own.appendChild(el$("span", "", "Any colour")); own.appendChild(picker); own.appendChild(hex);
            pop.appendChild(own);
            btn.parentNode.appendChild(pop);
            openPop = pop;
        });
        return btn;
    }

    /* ------------------------------------------------ the properties of what is chosen */
    function set(key, value, live) {
        selectedItems().forEach(function (el) { if (!el.locked || key === "locked") el[key] = value; });
        if (key === "clip" && value === "circle") selectedItems().forEach(function (el) { el.h = el.w; });
        if (live) { R.draw(cardHost, layout, side, cfg.view, { editor: true }).style.cssText += ";transform:scale(" + zoom + ");transform-origin:0 0"; drawOverlay(); return; }
        commit(); redraw();
    }
    function row(labelText, control, cls) {
        var r = el$("label", "idd-prop" + (cls ? " " + cls : ""));
        r.appendChild(el$("span", "idd-prop-l", labelText)); r.appendChild(control); return r;
    }
    function number(key, value, min, max, step, unit) {
        var wrap = el$("span", "idd-num");
        var i = document.createElement("input"); i.type = "number"; i.min = min; i.max = max; i.step = step || 0.1;
        i.value = value === undefined ? "" : value; i.dataset.key = key; i.disabled = locked;
        i.addEventListener("input", function () { var n = parseFloat(i.value); if (!isNaN(n)) set(key, clamp(n, min, max), true); });
        i.addEventListener("change", function () { var n = parseFloat(i.value); if (!isNaN(n)) set(key, clamp(n, min, max)); });
        wrap.appendChild(i); if (unit) wrap.appendChild(el$("span", "idd-unit", unit));
        return wrap;
    }
    function slider(key, value, min, max, step, unit) {
        var wrap = el$("span", "idd-slider");
        var i = document.createElement("input"); i.type = "range"; i.min = min; i.max = max; i.step = step || 1; i.value = value; i.disabled = locked;
        var out = el$("span", "idd-unit", value + (unit || ""));
        i.addEventListener("input", function () { out.textContent = i.value + (unit || ""); set(key, Number(i.value), true); });
        i.addEventListener("change", function () { set(key, Number(i.value)); });
        wrap.appendChild(i); wrap.appendChild(out); return wrap;
    }
    function choice(key, value, options) {
        var s = document.createElement("select"); s.disabled = locked; s.dataset.key = key;
        keysOf(options).forEach(function (v) { var o = el$("option", "", options[v]); o.value = v; if (String(value) === v) o.selected = true; s.appendChild(o); });
        s.addEventListener("change", function () { var v = s.value; set(key, /^\d+$/.test(v) && key !== "field" ? Number(v) : v); });
        return s;
    }
    function toggles(list) {
        var g = el$("span", "idd-toggles");
        list.forEach(function (t) {
            var b = el$("button", "idd-tg" + (t.on ? " is-on" : ""), t.label); b.type = "button"; b.title = t.title; b.disabled = locked;
            b.setAttribute("aria-pressed", !!t.on);
            if (t.style) b.style.cssText = t.style;
            b.addEventListener("click", function () { t.click(); });
            g.appendChild(b);
        });
        return g;
    }
    function section(title) {
        var s = el$("div", "idd-sec" + (closedSections[title] ? " is-closed" : "")), h = el$("h3", "", title);
        if (APP) {
            h.tabIndex = 0; h.setAttribute("role", "button"); h.setAttribute("aria-expanded", String(!closedSections[title]));
            var flip = function () { closedSections[title] = !closedSections[title]; s.classList.toggle("is-closed", !!closedSections[title]); h.setAttribute("aria-expanded", String(!closedSections[title])); };
            h.addEventListener("click", flip);
            h.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(); } });
        }
        s.appendChild(h); propsBox.appendChild(s); return s;
    }

    function props() {
        closePop();
        propsBox.textContent = "";
        var list = selectedItems();
        if (!list.length) {
            propsBox.appendChild(el$("h2", "", "Nothing chosen"));
            propsBox.appendChild(el$("p", "ac-hint", "Click an item on the card, or drag a box round several. Shift-click adds to the choice. Double-click a text to edit it."));
            var tips = el$("ul", "idd-tips");
            ["Arrow keys nudge (Shift: 2 mm, Alt: 0.1 mm)", "Ctrl+Z undo, Ctrl+Y redo", "Ctrl+C, Ctrl+V, Ctrl+D duplicate", "Ctrl+] forward, Ctrl+[ back", "Shift while resizing keeps the shape", "Ctrl + mouse wheel zooms"].forEach(function (t) { tips.appendChild(el$("li", "", t)); });
            propsBox.appendChild(tips);
            return;
        }
        if (list.length > 1) {
            propsBox.appendChild(el$("h2", "", list.length + " items chosen"));
            var a = section("Line them up");
            a.appendChild(alignBar());
            var o = section("Look");
            o.appendChild(row("Opacity", slider("opacity", list[0].opacity, 0, 100, 1, "%")));
            o.appendChild(row("Shadow", choice("shadow", list[0].shadow, { none: "None", soft: "Soft", strong: "Strong" })));
            propsBox.appendChild(actionsBar());
            return;
        }
        var el = list[0];
        var head = el$("div", "idd-prop-head");
        head.appendChild(el$("h2", "", label(el) === "Attendance" && !el.name ? "Attendance" : TYPE_NAMES[el.type]));
        var nm = document.createElement("input"); nm.type = "text"; nm.maxLength = 40; nm.placeholder = label(el); nm.value = el.name || "";
        nm.disabled = locked; nm.setAttribute("aria-label", "Name in the layers list");
        nm.addEventListener("change", function () { set("name", nm.value.trim().slice(0, 40)); });
        head.appendChild(nm);
        propsBox.appendChild(head);

        if (el.type === "text" || el.type === "field") {
            var c = section(el.type === "text" ? "Text" : "Detail");
            if (el.type === "text") {
                var ta = document.createElement("textarea"); ta.rows = 3; ta.maxLength = 300; ta.value = el.text; ta.dataset.key = "text"; ta.disabled = locked;
                ta.addEventListener("input", function () { set("text", ta.value.slice(0, 300), true); });
                ta.addEventListener("change", function () { set("text", ta.value.slice(0, 300)); });
                c.appendChild(ta);
            } else {
                c.appendChild(row("Shows", choice("field", el.field, cfg.fields)));
                var lb = document.createElement("input"); lb.type = "text"; lb.maxLength = 40; lb.value = el.label || ""; lb.placeholder = "No label"; lb.disabled = locked;
                lb.addEventListener("change", function () { set("label", lb.value.trim()); });
                c.appendChild(row("Label", lb));
            }
            var f = section("Type");
            f.appendChild(row("Font", choice("font", el.font, cfg.fonts)));
            var sz = el$("div", "idd-two");
            sz.appendChild(row("Size", number("size", el.size, 3, 72, 0.5, "pt")));
            sz.appendChild(row("Weight", choice("weight", String(el.weight), { "300": "Light", "400": "Regular", "500": "Medium", "600": "Semibold", "700": "Bold", "800": "Extra bold", "900": "Black" })));
            f.appendChild(sz);
            f.appendChild(row("Style", toggles([
                { label: "I", title: "Italic", on: el.italic, style: "font-style:italic", click: function () { set("italic", !el.italic); } },
                { label: "U", title: "Underline", on: el.underline, style: "text-decoration:underline", click: function () { set("underline", !el.underline); } },
                { label: "AA", title: "Capitals", on: el.upper, click: function () { set("upper", !el.upper); } }
            ])));
            f.appendChild(row("Align", toggles(["left", "center", "right", "justify"].map(function (a) {
                return { label: { left: "Left", center: "Centre", right: "Right", justify: "Justify" }[a], title: a, on: el.align === a, click: function () { set("align", a); } };
            }))));
            f.appendChild(row("Up/down", toggles(["top", "middle", "bottom"].map(function (a) {
                return { label: { top: "Top", middle: "Middle", bottom: "Bottom" }[a], title: a, on: el.valign === a, click: function () { set("valign", a); } };
            }))));
            var sp = el$("div", "idd-two");
            sp.appendChild(row("Letters", number("letter", el.letter, -20, 100, 1, "%")));
            sp.appendChild(row("Lines", number("line", el.line, 80, 250, 5, "%")));
            f.appendChild(sp);
            f.appendChild(row("Colour", colourControl(el.color, false, function (v, live) { set("color", v, live); })));
            var bx = section("Box behind the text");
            bx.appendChild(row("Fill", colourControl(el.fill || "none", true, function (v, live) { set("fill", v, live); })));
            var bp = el$("div", "idd-two");
            bp.appendChild(row("Corners", number("radius", el.radius, 0, 20, 0.5, "mm")));
            bp.appendChild(row("Padding", number("pad", el.pad, 0, 5, 0.2, "mm")));
            bx.appendChild(bp);
        }
        if (el.type === "shape") {
            var sh = section("Shape");
            sh.appendChild(row("Shape", choice("shape", el.shape, SHAPE_NAMES)));
            if (el.shape !== "line") {
                sh.appendChild(row("Fill", colourControl(el.fill, true, function (v, live) { set("fill", v, live); })));
                var gOn = !!el.gradient;
                sh.appendChild(row("Gradient", toggles([
                    { label: "None", title: "Plain fill", on: !gOn, click: function () { set("gradient", null); } },
                    { label: "Linear", title: "Straight gradient", on: gOn && el.gradient.type === "linear", click: function () { set("gradient", Object.assign({ from: "brand", to: "accent", angle: 135 }, el.gradient || {}, { type: "linear" })); } },
                    { label: "Radial", title: "Round gradient", on: gOn && el.gradient.type === "radial", click: function () { set("gradient", Object.assign({ from: "brand", to: "accent", angle: 135 }, el.gradient || {}, { type: "radial" })); } }
                ])));
                if (gOn) gradientRows(sh, el.gradient, function (g, live) { set("gradient", g, live); });
            }
            sh.appendChild(row(el.shape === "line" ? "Colour" : "Outline", colourControl(el.stroke, el.shape !== "line", function (v, live) { set("stroke", v, live); })));
            var ow = el$("div", "idd-two");
            ow.appendChild(row("Thickness", number("stroke_w", el.stroke_w, 0, 5, 0.1, "mm")));
            ow.appendChild(row("Line", choice("dash", el.dash, { solid: "Solid", dashed: "Dashed", dotted: "Dotted" })));
            sh.appendChild(ow);
            if (el.shape === "rect") sh.appendChild(row("Corners", number("radius", el.radius, 0, 40, 0.5, "mm")));
        }
        if (["photo", "logo", "image", "signature"].indexOf(el.type) >= 0) {
            var p = section("Picture");
            if (el.type === "signature") p.appendChild(row("Whose", choice("who", el.who, RECEIPT ? { authorised: "The authorised signature" } : DOCUMENT ? { head: "The head's" } : REPORT ? { head: "The head's", teacher: "The class teacher's" } : cfg.kind === "staff" ? { head: "The head's", holder: "The card holder's" } : { head: "The head's" })));
            if (el.type === "image") p.appendChild(row("Image", choice("asset", String(el.asset), assetOptions())));
            p.appendChild(row("Fit", choice("fit", el.fit, { cover: "Fill the box (crop)", contain: "Show it all", fill: "Stretch" })));
            p.appendChild(row("Clip to", choice("clip", el.clip, CLIP_NAMES)));
            if (el.clip === "rounded" || el.clip === "none") p.appendChild(row("Corners", number("radius", el.radius, 0, 40, 0.5, "mm")));
            p.appendChild(row("Frame", colourControl(el.border, true, function (v, live) { set("border", v, live); })));
            p.appendChild(row("Frame width", number("border_w", el.border_w, 0, 3, 0.1, "mm")));
            p.appendChild(row("Black and white", toggles([{ label: el.grayscale ? "On" : "Off", title: "Black and white", on: el.grayscale, click: function () { set("grayscale", !el.grayscale); } }])));
        }
        if (el.type === "qr" || el.type === "barcode") {
            var q = section(el.type === "qr" ? "QR code" : "Barcode");
            if (el.type === "barcode") {
                q.appendChild(row("Holds", choice("field", el.field, cfg.barcode_fields)));
                q.appendChild(row("Number under it", toggles([{ label: el.show_text ? "Shown" : "Hidden", title: "Show the number", on: el.show_text, click: function () { set("show_text", !el.show_text); } }])));
            }
            q.appendChild(row("Colour", colourControl(el.fg, false, function (v, live) { set("fg", v, live); })));
            q.appendChild(row("Background", colourControl(el.bg, true, function (v, live) { set("bg", v, live); })));
            if (el.type === "qr") q.appendChild(row("Corners", number("radius", el.radius, 0, 10, 0.5, "mm")));
            q.appendChild(el$("p", "ac-hint", "Keep it dark on light, and not too small, or phones cannot read it."));
        }
        if (R.blocks && R.blocks[el.type] && REPORT) blockProps(el);
        var pos = section("Position and size");
        var g4 = el$("div", "idd-two");
        g4.appendChild(row("X", number("x", el.x, -LIMIT / 2, LIMIT, 0.1, "mm")));
        g4.appendChild(row("Y", number("y", el.y, -LIMIT / 2, LIMIT, 0.1, "mm")));
        g4.appendChild(row("Width", number("w", el.w, 0.5, LIMIT, 0.1, "mm")));
        g4.appendChild(row("Height", number("h", el.h, 0.5, LIMIT, 0.1, "mm")));
        pos.appendChild(g4);
        pos.appendChild(row("Turn", number("rotate", el.rotate, -180, 180, 1, "°")));
        pos.appendChild(alignBar());
        var look = section("Look");
        look.appendChild(row("Opacity", slider("opacity", el.opacity, 0, 100, 1, "%")));
        look.appendChild(row("Shadow", choice("shadow", el.shadow, { none: "None", soft: "Soft", strong: "Strong" })));
        propsBox.appendChild(actionsBar());
    }
    function picks(key, chosen, options, keepFirst) {
        // Several of a list, in the list's order (the results table's columns, the summary's items).
        return toggles(keysOf(options).map(function (k2) {
            var on = chosen.indexOf(k2) >= 0;
            return { label: options[k2], title: options[k2], on: on, click: function () {
                if (keepFirst && k2 === keepFirst) return;
                var next = keysOf(options).filter(function (x) { return x === k2 ? !on : chosen.indexOf(x) >= 0; });
                if (next.length) set(key, next);
            } };
        }));
    }
    function blockProps(el) {
        var t = el.type, b = section(TYPE_NAMES[t]);
        if (t === "paragraph") {
            // The school's own words; a detail in curly brackets is filled in for each document.
            var ta = document.createElement("textarea"); ta.rows = 7; ta.maxLength = 1500; ta.value = el.text || ""; ta.disabled = locked;
            ta.className = "idd-para";
            ta.addEventListener("change", function () { set("text", ta.value.slice(0, 1500)); });
            b.appendChild(row("Words", ta, "idd-prop--wrap"));
            var chips = el$("div", "idd-chips");
            (cfg.fields || []).forEach(function (pair) {
                var c = el$("button", "idd-chip", pair[1]); c.type = "button"; c.title = "Put in {" + pair[0] + "}"; c.disabled = locked;
                c.addEventListener("click", function () {
                    var s = ta.selectionStart || ta.value.length, e2 = ta.selectionEnd || s, v = "{" + pair[0] + "}";
                    ta.value = (ta.value.slice(0, s) + v + ta.value.slice(e2)).slice(0, 1500); set("text", ta.value); ta.focus();
                });
                chips.appendChild(c);
            });
            b.appendChild(row("Put in", chips, "idd-prop--wrap"));
            b.appendChild(row("Align", toggles([["left", "Left"], ["center", "Centre"], ["justify", "Justify"]].map(function (a) {
                return { label: a[1], title: a[1], on: el.align === a[0], click: function () { set("align", a[0]); } };
            }))));
            b.appendChild(row("Line spacing", number("leading", el.leading, 100, 250, 5, "%")));
            b.appendChild(row("Italic", toggles([{ label: el.italic ? "On" : "Off", title: "Italic", on: el.italic, click: function () { set("italic", !el.italic); } }])));
        }
        if (t === "transcript") {
            b.appendChild(el$("p", "ac-hint", "One row per subject, one column per session the student has results in (headed with the class), each the average of that session's terms, then the overall average."));
            b.appendChild(row("Grade", toggles([{ label: el.show_grade === false ? "Hidden" : "Shown", title: "The grade column", on: el.show_grade !== false, click: function () { set("show_grade", el.show_grade === false); } }])));
            b.appendChild(row("Numbers", toggles([["center", "Centre"], ["right", "Right"]].map(function (a) {
                return { label: a[1], title: a[1], on: el.numbers === a[0], click: function () { set("numbers", a[0]); } };
            }))));
            b.appendChild(row("Heading size", number("head_size", el.head_size, 5, 16, 0.5, "pt")));
        }
        if (t === "cumulative") {
            b.appendChild(el$("p", "ac-hint", "Each subject's First, Second and Third Term marks, their average and grade. Shown on Third Term cards only; on the others this space stays empty."));
            b.appendChild(row("Numbers", toggles([["center", "Centre"], ["right", "Right"]].map(function (a) {
                return { label: a[1], title: a[1], on: el.numbers === a[0], click: function () { set("numbers", a[0]); } };
            }))));
            b.appendChild(row("Heading size", number("head_size", el.head_size, 5, 16, 0.5, "pt")));
        }
        if (t === "results") {
            b.appendChild(row("Columns", picks("columns", el.columns || [], cfg.result_columns, "subject"), "idd-prop--wrap"));
            b.appendChild(row("Numbers", toggles([["center", "Centre"], ["right", "Right"]].map(function (a) {
                return { label: a[1], title: a[1], on: el.numbers === a[0], click: function () { set("numbers", a[0]); } };
            }))));
            b.appendChild(row("Heading size", number("head_size", el.head_size, 5, 16, 0.5, "pt")));
        }
        if (t === "summary") {
            b.appendChild(row("Shows", picks("items", el.items || [], cfg.summary_items), "idd-prop--wrap"));
            b.appendChild(row("Arrange", toggles([["row", "Across"], ["column", "Down"]].map(function (a) {
                return { label: a[1], title: a[1], on: el.layout === a[0], click: function () { set("layout", a[0]); } };
            }))));
            b.appendChild(row("Figure size", number("value_size", el.value_size, 6, 24, 0.5, "pt")));
        }
        if (t === "traits") b.appendChild(row("Arrange", toggles([["side", "Side by side"], ["stack", "One under another"]].map(function (a) {
            return { label: a[1], title: a[1], on: el.layout === a[0], click: function () { set("layout", a[0]); } };
        }))));
        if (t === "amount") {
            var al = document.createElement("input"); al.type = "text"; al.maxLength = 40; al.value = el.label || ""; al.placeholder = "No heading"; al.disabled = locked;
            al.addEventListener("change", function () { set("label", al.value.trim()); });
            b.appendChild(row("Heading", al));
            b.appendChild(row("In words", toggles([{ label: el.words ? "Shown" : "Hidden", title: "The amount in words under the figure", on: el.words, click: function () { set("words", !el.words); } }])));
            b.appendChild(row("Figure size", number("value_size", el.value_size, 8, 40, 0.5, "pt")));
        }
        if (t === "stamp") {
            var sw = document.createElement("input"); sw.type = "text"; sw.maxLength = 16; sw.value = el.text || ""; sw.placeholder = "PAID"; sw.disabled = locked;
            sw.addEventListener("change", function () { set("text", sw.value.trim().slice(0, 16) || "PAID"); });
            b.appendChild(row("Word", sw));
            b.appendChild(row("Shape", toggles([["round", "Round"], ["box", "Box"]].map(function (a) {
                return { label: a[1], title: a[1], on: el.shape === a[0], click: function () { set("shape", a[0]); } };
            }))));
            b.appendChild(row("Date", toggles([{ label: el.show_date ? "Shown" : "Hidden", title: "The date paid inside the stamp", on: el.show_date, click: function () { set("show_date", !el.show_date); } }])));
            b.appendChild(el$("p", "ac-hint", "A voided receipt never shows the stamp: it is marked VOID instead."));
        }
        if (t === "grading" || t === "comment") {
            var key = t === "grading" ? "title" : "label";
            var ti = document.createElement("input"); ti.type = "text"; ti.maxLength = 60; ti.value = el[key] || ""; ti.placeholder = "No heading"; ti.disabled = locked;
            ti.addEventListener("change", function () { set(key, ti.value.trim()); });
            b.appendChild(row("Heading", ti));
        }
        if (t === "grading") b.appendChild(row("A row holds", number("per_row", el.per_row, 1, 6, 1, "")));
        if (t === "comment") {
            b.appendChild(row("Ruled lines", toggles([{ label: el.lines ? "On" : "Off", title: "Ruled lines", on: el.lines, click: function () { set("lines", !el.lines); } }])));
            b.appendChild(row("Comment size", number("comment_size", el.comment_size, 5, 16, 0.5, "pt")));
        }
        var s2 = section("Type and colours");
        s2.appendChild(row("Font", choice("font", el.font, cfg.fonts)));
        s2.appendChild(row("Size", number("size", el.size, 5, 16, 0.5, "pt")));
        s2.appendChild(row("Text", colourControl(el.color, false, function (v, live) { set("color", v, live); })));
        if (["results", "traits", "grading", "comment", "cumulative", "transcript"].indexOf(t) >= 0) {
            s2.appendChild(row("Heading band", colourControl(el.head_fill || "none", true, function (v, live) { set("head_fill", v, live); })));
            s2.appendChild(row("Heading text", colourControl(el.head_color, false, function (v, live) { set("head_color", v, live); })));
        }
        s2.appendChild(row(t === "stamp" ? "Ink" : "Highlight", colourControl(el.accent, false, function (v, live) { set("accent", v, live); })));
        if (t === "stamp") return;
        var s3 = section("Box and lines");
        s3.appendChild(row("Fill", colourControl(el.fill || "none", true, function (v, live) { set("fill", v, live); })));
        if (t === "results" || t === "traits" || t === "cumulative" || t === "transcript") s3.appendChild(row("Stripes", colourControl(el.zebra || "none", true, function (v, live) { set("zebra", v, live); })));
        s3.appendChild(row("Lines", colourControl(el.line || "none", true, function (v, live) { set("line", v, live); })));
        var two = el$("div", "idd-two");
        two.appendChild(row("Line width", number("line_w", el.line_w, 0, 2, 0.1, "mm")));
        two.appendChild(row("Corners", number("radius", el.radius, 0, 10, 0.5, "mm")));
        s3.appendChild(two);
        s3.appendChild(row("Padding", number("pad", el.pad, 0.5, 5, 0.1, "mm")));
    }
    function gradientRows(box, g, apply) {
        box.appendChild(row("From", colourControl(g.from, false, function (v, live) { apply(Object.assign({}, g, { from: v }), live); })));
        box.appendChild(row("To", colourControl(g.to, false, function (v, live) { apply(Object.assign({}, g, { to: v }), live); })));
        if (g.type === "linear") {
            var wrap = el$("span", "idd-slider");
            var i = document.createElement("input"); i.type = "range"; i.min = 0; i.max = 360; i.step = 5; i.value = g.angle; i.disabled = locked;
            var out = el$("span", "idd-unit", g.angle + "°");
            i.addEventListener("input", function () { out.textContent = i.value + "°"; apply(Object.assign({}, g, { angle: Number(i.value) }), true); });
            i.addEventListener("change", function () { apply(Object.assign({}, g, { angle: Number(i.value) })); });
            wrap.appendChild(i); wrap.appendChild(out);
            box.appendChild(row("Direction", wrap));
        }
    }
    function alignBar() {
        var bar = el$("div", "idd-alignbar");
        [["left", "Align left"], ["hcenter", "Centre across"], ["right", "Align right"], ["top", "Align top"], ["vmiddle", "Centre up and down"], ["bottom", "Align bottom"]]
            .concat(selection.length > 2 ? [["hspace", "Space evenly across"], ["vspace", "Space evenly up and down"]] : [])
            .forEach(function (a) {
                var b = el$("button", "idd-ab idd-ab-" + a[0]); b.type = "button"; b.title = a[1] + (selection.length > 1 ? "" : " (on the card)");
                b.setAttribute("aria-label", a[1]); b.disabled = locked;
                b.addEventListener("click", function () { align(a[0]); });
                bar.appendChild(b);
            });
        return bar;
    }
    function actionsBar() {
        var bar = el$("div", "idd-acts");
        [["duplicate", "Duplicate"], ["front", "To front"], ["forward", "Forward"], ["backward", "Back"], ["back", "To back"],
         ["lock", selectedItems().every(function (e) { return e.locked; }) ? "Unlock" : "Lock"],
         ["hide", selectedItems().every(function (e) { return e.hidden; }) ? "Show" : "Hide"], ["delete", "Remove"]].forEach(function (a) {
            var b = el$("button", "ac-btn ac-btn--sm" + (a[0] === "delete" ? " ac-btn--danger" : ""), a[1]); b.type = "button"; b.disabled = locked;
            b.addEventListener("click", function () { act(a[0]); stage.focus({ preventScroll: true }); });
            bar.appendChild(b);
        });
        return bar;
    }

    /* ------------------------------------------------ layers */
    function layers() {
        layersBox.textContent = "";
        var list = items();
        if (!list.length) { layersBox.appendChild(el$("p", "ac-hint", REPORT ? "Nothing on this page yet." + (side !== SIDES[0] ? " A second page is printed only when something is on it." : "") : "Nothing on this side yet.")); return; }
        for (var i = list.length - 1; i >= 0; i--) {
            (function (i) {
                var el = list[i];
                var li = el$("div", "idd-layer" + (selection.indexOf(i) >= 0 ? " is-on" : "") + (el.hidden ? " is-hidden" : ""));
                var name = el$("button", "idd-layer-name"); name.type = "button";
                name.appendChild(el$("span", "idd-layer-type", { text: "T", field: "D", shape: "\u25c6", photo: "P", logo: "L", image: "I", signature: "\u270d", qr: "Q", barcode: "B",
                                                                results: "\u25a6", summary: "\u03a3", traits: "\u2605", grading: "G", comment: "\u201c", cumulative: "\u2211", paragraph: "\u00b6", transcript: "\u25a6",
                                                                amount: "\u20a6", stamp: "\u2713" }[el.type] || "?"));
                name.appendChild(el$("span", "", label(el)));
                name.addEventListener("click", function (e) { select([i], e.shiftKey); stage.focus({ preventScroll: true }); });
                var eye = el$("button", "idd-layer-btn", el.hidden ? "Show" : "Hide"); eye.type = "button"; eye.disabled = locked;
                eye.addEventListener("click", function () { el.hidden = !el.hidden; commit(); redraw(); });
                var lk = el$("button", "idd-layer-btn", el.locked ? "Unlock" : "Lock"); lk.type = "button"; lk.disabled = locked;
                lk.addEventListener("click", function () { el.locked = !el.locked; commit(); redraw(); });
                li.appendChild(name); li.appendChild(eye); li.appendChild(lk);
                layersBox.appendChild(li);
            })(i);
        }
    }

    /* ------------------------------------------------ the card itself: shape and background */
    function cardPanel() {
        var box = root.querySelector("[data-cardpanel]");
        if (!box || box.dataset.built === snapshotCard()) return;
        box.dataset.built = snapshotCard();
        box.textContent = "";
        var bg = layout.background;
        var o = document.createElement("select"); o.disabled = locked; o.setAttribute("aria-label", "Shape of the " + NOUN);
        var choices = cfg.page_choices ? cfg.page_choices.map(function (c) { return [c[0] + "|" + c[1], c[2]]; })
            : (REPORT ? [["portrait", "A4 portrait (210 × 297 mm)"], ["landscape", "A4 landscape (297 × 210 mm)"]]
                      : [["landscape", "Landscape (85.6 × 54 mm)"], ["portrait", "Portrait (54 × 85.6 mm)"]]);
        var current = cfg.page_choices ? layout.page + "|" + layout.orientation : layout.orientation;
        choices.forEach(function (x) {
            var op = el$("option", "", x[1]); op.value = x[0]; if (current === x[0]) op.selected = true; o.appendChild(op);
        });
        o.addEventListener("change", function () {
            if (o.value === current) return;
            var before = size();
            if (cfg.page_choices) { layout.page = o.value.split("|")[0]; layout.orientation = o.value.split("|")[1]; }
            else layout.orientation = o.value;
            var after = size(), sx = after[0] / before[0], sy = after[1] / before[1];
            // Everything keeps its place in proportion to the card; sizes stay, so nothing is squashed.
            SIDES.forEach(function (sd) { layout.sides[sd].forEach(function (el) {
                el.x = r1((el.x + el.w / 2) * sx - el.w / 2); el.y = r1((el.y + el.h / 2) * sy - el.h / 2);
                if (el.w > after[0] && el.type === "shape") { el.w = after[0]; el.x = 0; }
            }); });
            commit(); fitZoom();
        });
        box.appendChild(row("Shape", o));
        box.appendChild(row("Background", colourControl(bg.color, false, function (v, live) { bg.color = v; if (live) redrawLive(); else { commit(); redraw(); } })));
        var gOn = !!bg.gradient;
        box.appendChild(row("Gradient", toggles([
            { label: "None", title: "Plain background", on: !gOn, click: function () { bg.gradient = null; commit(); redraw(); } },
            { label: "Linear", title: "Straight gradient", on: gOn && bg.gradient.type === "linear", click: function () { bg.gradient = Object.assign({ from: "brand", to: "accent", angle: 135 }, bg.gradient || {}, { type: "linear" }); commit(); redraw(); } },
            { label: "Radial", title: "Round gradient", on: gOn && bg.gradient.type === "radial", click: function () { bg.gradient = Object.assign({ from: "brand", to: "accent", angle: 135 }, bg.gradient || {}, { type: "radial" }); commit(); redraw(); } }
        ])));
        if (gOn) gradientRows(box, bg.gradient, function (g, live) { bg.gradient = g; if (live) redrawLive(); else { commit(); redraw(); } });
        var opts = { "": "No image" }; (cfg.asset_list || []).forEach(function (a) { opts[a.id] = a.name; });
        var im = document.createElement("select"); im.disabled = locked; im.setAttribute("aria-label", "Background image");
        Object.keys(opts).forEach(function (v) { var op = el$("option", "", opts[v]); op.value = v; if (String(bg.image || "") === v) op.selected = true; im.appendChild(op); });
        im.addEventListener("change", function () { bg.image = im.value ? Number(im.value) : null; commit(); redraw(); });
        box.appendChild(row("Image", im));
        if (bg.image) {
            var fit = document.createElement("select"); fit.disabled = locked;
            [["cover", "Fill the card"], ["contain", "Show it all"], ["fill", "Stretch"]].forEach(function (x) { var op = el$("option", "", x[1]); op.value = x[0]; if (bg.fit === x[0]) op.selected = true; fit.appendChild(op); });
            fit.addEventListener("change", function () { bg.fit = fit.value; commit(); redraw(); });
            box.appendChild(row("Fit", fit));
            var op2 = document.createElement("input"); op2.type = "range"; op2.min = 0; op2.max = 100; op2.value = bg.opacity; op2.disabled = locked;
            op2.addEventListener("input", function () { bg.opacity = Number(op2.value); redrawLive(); });
            op2.addEventListener("change", function () { commit(); redraw(); });
            box.appendChild(row("Image strength", op2));
        }
    }
    function snapshotCard() { return JSON.stringify([layout.page, layout.orientation, layout.background, (cfg.asset_list || []).length, locked]); }
    function redrawLive() { R.draw(cardHost, layout, side, cfg.view, { editor: true }).style.cssText += ";transform:scale(" + zoom + ");transform-origin:0 0"; }

    /* ------------------------------------------------ the school's own images */
    function assetOptions() { var o = {}; (cfg.asset_list || []).forEach(function (a) { o[a.id] = a.name; }); return o; }
    var library = root.querySelector("[data-library]");
    function drawLibrary() {
        if (!library) return;
        library.textContent = "";
        (cfg.asset_list || []).forEach(function (a) {
            var t = el$("div", "idd-asset");
            var b = el$("button", "idd-asset-pick"); b.type = "button"; b.title = "Put " + a.name + " on the card"; b.disabled = locked;
            var img = document.createElement("img"); img.src = a.url; img.alt = a.name; b.appendChild(img);
            b.addEventListener("click", function () { add("image", { asset: a.id }); });
            var x = el$("button", "idd-asset-del", "×"); x.type = "button"; x.title = "Remove " + a.name + " from the images"; x.disabled = locked;
            x.addEventListener("click", function () { removeAsset(a); });
            t.appendChild(b); t.appendChild(x);
            library.appendChild(t);
        });
        if (!(cfg.asset_list || []).length) library.appendChild(el$("p", "ac-hint", "No images yet. Upload a pattern, a badge or the school's stamp."));
    }
    function post(url, body) {
        body.append("_csrf_token", document.getElementById("csrfToken").value);
        return fetch(url, { method: "POST", body: body, credentials: "same-origin" })
            .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok && j.ok, j: j }; }); });
    }
    var upload = root.querySelector("[data-upload]");
    if (upload) {
        upload.disabled = locked;
        upload.addEventListener("change", function () {
            if (!upload.files.length) return;
            var body = new FormData(); body.append("image", upload.files[0]);
            say("Uploading…");
            post(cfg.urls.upload, body).then(function (res) {
                upload.value = "";
                var note = upload.closest("section") && upload.closest("section").querySelector("[data-upload-feedback]");
                if (note) note.textContent = "";
                if (!res.ok) { say(res.j.error || "The image could not be uploaded.", "error"); return; }
                cfg.asset_list.push(res.j.asset); cfg.view.assets[res.j.asset.id] = res.j.asset.url;
                drawLibrary(); cardPanel(); say("Image added. Click it to put it on the card.");
            }).catch(function () { say("The image could not be uploaded. Check your connection.", "error"); });
        });
    }
    function removeAsset(a) {
        var inUse = SIDES.some(function (sd) { return layout.sides[sd].some(function (el) { return el.type === "image" && el.asset === a.id; }); }) || layout.background.image === a.id;
        window.brandDialog.confirm("Remove " + a.name + " from the school's card images?" + (inUse ? " It is on this design: it will disappear from it." : ""),
            { title: "Remove image", ok: "Remove", danger: true }).then(function (yes) {
            if (!yes) return;
            post(cfg.urls.asset_delete.replace("/0/", "/" + a.id + "/"), new FormData()).then(function (res) {
                if (!res.ok) { say(res.j.error || "The image could not be removed.", "error"); return; }
                cfg.asset_list = cfg.asset_list.filter(function (x) { return x.id !== a.id; });
                delete cfg.view.assets[a.id];
                SIDES.forEach(function (sd) { layout.sides[sd] = layout.sides[sd].filter(function (el) { return !(el.type === "image" && el.asset === a.id); }); });
                if (layout.background.image === a.id) layout.background.image = null;
                selection = []; commit(); drawLibrary(); redraw();
            });
        });
    }

    /* ------------------------------------------------ earlier versions */
    var versionsBox = root.querySelector("[data-versions]");
    function drawVersions() {
        if (!versionsBox) return;
        versionsBox.textContent = "";
        if (!(cfg.versions || []).length) { versionsBox.appendChild(el$("p", "ac-hint", "Each save is kept here, so an earlier design can be brought back.")); return; }
        cfg.versions.forEach(function (v) {
            var r = el$("div", "idd-version");
            r.appendChild(el$("span", "", v.saved_at + (v.by ? " · " + v.by : "")));
            var b = el$("button", "ac-link", "Bring back"); b.type = "button"; b.disabled = locked;
            b.addEventListener("click", function () {
                fetch(cfg.urls.version.replace("/0", "/" + v.id), { credentials: "same-origin" }).then(function (r2) { return r2.json(); }).then(function (j) {
                    if (!j.ok) { say("That version could not be read.", "error"); return; }
                    layout = j.layout; selection = []; commit(); fitZoom();
                    say("The version from " + v.saved_at + " is on screen. Press Save design to keep it.", "dirty");
                });
            });
            r.appendChild(b); versionsBox.appendChild(r);
        });
    }

    /* ------------------------------------------------ templates: previews, and putting one on the card */
    var tplButton = root.querySelector("[data-templates]");
    var tplDialog = document.querySelector("[data-template-dialog]");
    function openTemplates() {
        if (locked || !tplDialog) return;
        var grid = tplDialog.querySelector("[data-template-grid]");
        grid.textContent = "";
        (cfg.templates || []).forEach(function (t) {
            var b = el$("button", "idd-tpl__item"); b.type = "button";
            var frame = el$("span", "idd-tpl__frame"), host = el$("span", "idd-tpl__card");
            frame.appendChild(host);
            b.appendChild(frame);
            b.appendChild(el$("strong", "", t.name));
            b.appendChild(el$("small", "", t.text));
            b.addEventListener("click", function () { useTemplate(t); });
            grid.appendChild(b);
            // A small picture of the template's front, with this school's colours and the sample person.
            var card = R.draw(host, t.layout, SIDES[0], cfg.view, { editor: false });
            var s = R.sizeOf ? R.sizeOf(t.layout) : (R.SIZES[t.layout.orientation] || R.SIZES.landscape);
            var scale = REPORT ? Math.min(150 / (s[0] * PX), 200 / (s[1] * PX)) : Math.min(150 / (s[0] * PX), 96 / (s[1] * PX));
            card.style.transform = "scale(" + scale + ")"; card.style.transformOrigin = "0 0";
            host.style.width = s[0] * PX * scale + "px"; host.style.height = s[1] * PX * scale + "px";
        });
        tplDialog.showModal();
    }
    function useTemplate(t) {
        tplDialog.close();
        layout = JSON.parse(JSON.stringify(t.layout));
        side = SIDES[0]; selection = [];
        commit(); fitZoom();
        say("The " + t.name + " template is on the " + NOUN + ". Press Save design to keep it, or Undo (Ctrl+Z) to go back to yours.", "dirty");
        stage.focus({ preventScroll: true });
    }
    if (tplButton) { tplButton.disabled = locked; tplButton.addEventListener("click", openTemplates); }
    if (tplDialog) {
        tplDialog.querySelector("[data-template-close]").addEventListener("click", function () { tplDialog.close(); });
        tplDialog.addEventListener("click", function (e) { if (e.target === tplDialog) tplDialog.close(); });
    }

    /* ------------------------------------------------ saving */
    function save() {
        var body = new FormData(); body.append("layout", JSON.stringify(layout));
        say("Saving…");
        return post(cfg.urls.save, body).then(function (res) {
            if (!res.ok) { say(res.j.error || "The design could not be saved.", "error"); return false; }
            layout = res.j.layout; dirty = false; cfg.versions = res.j.versions || cfg.versions;
            history = [snapshot()]; future = []; updateUndo();
            selection = selection.filter(function (i) { return i < items().length; });
            redraw(); drawVersions();
            var useBtn = root.querySelector("[data-use-btn]");
            if (useBtn) { useBtn.disabled = false; useBtn.removeAttribute("title"); }
            say(cfg.saved_text || "Saved. Print cards from the ID cards page.", "saved");
            return true;
        }).catch(function () { say("The design could not be saved. Check your connection.", "error"); return false; });
    }
    var saveBtn = root.querySelector("[data-idd-save]");
    if (saveBtn) saveBtn.addEventListener("click", function () { saveBtn.disabled = true; save().finally(function () { saveBtn.disabled = false; }); });
    document.addEventListener("keydown", function (e) {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s" && !locked) { e.preventDefault(); if (saveBtn) saveBtn.click(); }
    });
    var lockForm = root.querySelector("[data-idd-lock]");
    if (lockForm) lockForm.addEventListener("submit", function (e) {
        if (!dirty) return;
        e.preventDefault(); e.stopImmediatePropagation();       // save what is on screen first, then lock it
        save().then(function (ok) { if (ok) { dirty = false; lockForm.submit(); } });
    }, true);
    /* ------------------------------------------------ the tabbed panels and full screen (the whole-window layout) */
    function showTab(group, name) {
        var box = root.querySelector('[data-tabs="' + group + '"]');
        if (!box) return;
        box.querySelectorAll("[data-tab]").forEach(function (b) { var on = b.dataset.tab === name; b.classList.toggle("is-active", on); b.setAttribute("aria-selected", String(on)); });
        box.querySelectorAll("[data-tab-pane]").forEach(function (p) { p.hidden = p.dataset.tabPane !== name; });
    }
    root.querySelectorAll("[data-tabs]").forEach(function (box) {
        box.querySelectorAll("[data-tab]").forEach(function (b) {
            b.addEventListener("click", function () { showTab(box.dataset.tabs, b.dataset.tab); });
        });
    });
    var fsBtn = root.querySelector("[data-fullscreen]");
    if (fsBtn) {
        if (!document.documentElement.requestFullscreen) fsBtn.hidden = true;
        fsBtn.addEventListener("click", function () {
            if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(function () { /* refused: fine */ });
        });
    }
    if (APP) {
        var refit = null;
        window.addEventListener("resize", function () {
            clearTimeout(refit);
            refit = setTimeout(function () { if (userZoomed) redraw(); else fitZoom(); }, 120);
        });
    }
    var previewBtn = root.querySelector("[data-preview]");
    if (previewBtn && cfg.urls.preview) previewBtn.addEventListener("click", function () {
        // The design on screen, saved or not, drawn as the PDF the school will print, in a new tab.
        var f = document.createElement("form");
        f.setAttribute("method", "post"); f.setAttribute("action", cfg.urls.preview); f.setAttribute("target", "_blank"); f.hidden = true;
        [["layout", JSON.stringify(layout)], ["_csrf_token", document.getElementById("csrfToken").value]].forEach(function (p) {
            var i = document.createElement("input"); i.type = "hidden"; i.name = p[0]; i.value = p[1]; f.appendChild(i);
        });
        document.body.appendChild(f); f.submit(); f.remove();
    });
    window.addEventListener("beforeunload", function (e) { if (dirty && !locked) { e.preventDefault(); e.returnValue = ""; } });

    history = [snapshot()];
    updateUndo();
    drawLibrary();
    drawVersions();
    redraw();
    fitZoom();
})();
