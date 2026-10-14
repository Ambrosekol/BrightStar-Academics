/* Draws one side of a design at real size in millimetres: an ID card (core/id_cards.py, version 2) or a report card
   page (core/report_card_design.py, whose own blocks - the results table and the rest - are added by
   static/report-card-render.js through ``blocks``). The same code draws the studio's canvas
   (static/id-card-designer.js, which scales it on screen), every card on the ID card print sheet and a report card on
   its web page, so what is designed is exactly what is shown.

   Everything is built with createElement, textContent and style properties; nothing is ever parsed as HTML. The
   values come from the server, which rebuilt the design from known keys only, and are checked again here (colours,
   choices, numbers) before they reach a style. */
(function () {
    "use strict";
    var NS = "http://www.w3.org/2000/svg";
    var SIZES = { landscape: [85.6, 54], portrait: [54, 85.6] };
    var PAGES = { portrait: [210, 297], landscape: [297, 210] };     // A4, for a report card (or a receipt)
    var A5 = { landscape: [210, 148], portrait: [148, 210] };       // A5, for a receipt
    var blocks = {};                                                 // extra element types: blocks[type](box, el, view, opts, colour)
    function sizeOf(layout) {
        if (layout.page === "A4") return PAGES[layout.orientation] || PAGES.portrait;
        if (layout.page === "A5") return A5[layout.orientation] || A5.landscape;
        return SIZES[layout.orientation] || SIZES.landscape;
    }
    var FONTS = {
        sans: 'Arial, "Helvetica Neue", Helvetica, sans-serif', helvetica: 'Helvetica, Arial, sans-serif',
        verdana: "Verdana, Geneva, sans-serif", tahoma: "Tahoma, Verdana, sans-serif", trebuchet: '"Trebuchet MS", Helvetica, sans-serif',
        georgia: 'Georgia, "Times New Roman", serif', times: '"Times New Roman", Times, serif', garamond: 'Garamond, Georgia, serif',
        mono: '"Courier New", Courier, monospace', impact: 'Impact, "Arial Black", sans-serif', black: '"Arial Black", Arial, sans-serif',
        dejavu: '"BS DejaVu Sans", "DejaVu Sans", Verdana, sans-serif'
    };
    // Shapes drawn in a 100 x 100 box, stretched to the element's own.
    var PATHS = {
        triangle: "M50 0L100 100L0 100Z",
        diamond: "M50 0L100 50L50 100L0 50Z",
        hexagon: "M25 0L75 0L100 50L75 100L25 100L0 50Z",
        pentagon: "M50 0L100 38L81 100L19 100L0 38Z",
        star: "M50 0L61 35L98 35L68 57L79 91L50 70L21 91L32 57L2 35L39 35Z",
        chevron: "M0 0L75 0L100 50L75 100L0 100L25 50Z",
        arrow: "M0 30L60 30L60 0L100 50L60 100L60 70L0 70Z",
        wave: "M0 40C20 10 40 10 55 35S85 65 100 30L100 100L0 100Z",
        corner: "M0 0L100 0C70 10 30 40 0 100Z"
    };
    var CLIPS = {
        hexagon: "polygon(25% 0, 75% 0, 100% 50%, 75% 100%, 25% 100%, 0 50%)",
        diamond: "polygon(50% 0, 100% 50%, 50% 100%, 0 50%)",
        shield: "polygon(0 0, 100% 0, 100% 62%, 50% 100%, 0 62%)",
        star: "polygon(50% 0, 61% 35%, 98% 35%, 68% 57%, 79% 91%, 50% 70%, 21% 91%, 32% 57%, 2% 35%, 39% 35%)"
    };
    var SHADOWS = { soft: "drop-shadow(0 0.4mm 0.8mm rgba(0,0,0,.25))", strong: "drop-shadow(0 0.8mm 1.4mm rgba(0,0,0,.45))" };
    var HEX = /^#[0-9a-fA-F]{6}$/;
    var counter = 0;

    function num(v, d) { v = Number(v); return isFinite(v) ? v : d; }
    function mm(v) { return num(v, 0) + "mm"; }

    function colourOf(view) {
        var brand = HEX.test(view.brand && view.brand.primary || "") ? view.brand.primary : "#0d2b52";
        var accent = HEX.test(view.brand && view.brand.accent || "") ? view.brand.accent : "#1d6fd8";
        return function (name, fallback) {
            if (name === "brand") return brand;
            if (name === "accent") return accent;
            if (name === "white") return "#ffffff";
            if (name === "ink") return "#15171a";
            if (name === "none") return "transparent";
            return HEX.test(name || "") ? name : (fallback || "transparent");
        };
    }
    function cssGradient(g, colour) {
        var a = num(g.angle, 135);
        return g.type === "radial" ? "radial-gradient(circle at 30% 30%, " + colour(g.from) + ", " + colour(g.to) + ")"
            : "linear-gradient(" + a + "deg, " + colour(g.from) + ", " + colour(g.to) + ")";
    }
    function cssUrl(url) { return 'url("' + String(url).replace(/["\\\n\r]/g, "") + '")'; }
    function svg(tag, attrs) {
        var n = document.createElementNS(NS, tag);
        Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
        return n;
    }

    function drawText(box, el, view, opts, colour) {
        var t = document.createElement("div");
        t.className = "idc-tx";
        var s = t.style;
        s.fontFamily = FONTS[el.font] || FONTS.sans;
        s.fontSize = num(el.size, 8) + "pt";
        s.fontWeight = String(num(el.weight, 400));
        s.fontStyle = el.italic ? "italic" : "normal";
        s.textDecoration = el.underline ? "underline" : "none";
        s.textTransform = el.upper ? "uppercase" : "none";
        s.letterSpacing = num(el.letter, 0) / 100 + "em";
        s.lineHeight = num(el.line, 118) / 100;
        s.color = colour(el.color, "#15171a");
        s.textAlign = ["left", "center", "right", "justify"].indexOf(el.align) >= 0 ? el.align : "left";
        s.justifyContent = { top: "flex-start", middle: "center", bottom: "flex-end" }[el.valign] || "flex-start";
        if (el.fill && el.fill !== "none") s.background = colour(el.fill);
        if (el.radius) s.borderRadius = mm(el.radius);
        if (el.pad) s.padding = mm(el.pad);
        var inner = document.createElement("div");
        if (el.type === "text") {
            inner.textContent = el.text || "";
        } else {
            var value = (view.data || {})[el.field] || "";
            if (!value && opts.editor) {
                inner.className = "idc-ph";
                value = "[" + ((view.fields || {})[el.field] || el.field) + "]";
            }
            if (el.label && value) {
                var l = document.createElement("span"); l.className = "idc-label"; l.textContent = el.label + ": "; inner.appendChild(l);
            }
            inner.appendChild(document.createTextNode(value));
        }
        t.appendChild(inner);
        box.appendChild(t);
    }

    function drawShape(box, el, colour) {
        var fill = el.gradient ? null : colour(el.fill);
        var stroke = el.stroke && el.stroke !== "none" ? colour(el.stroke) : null;
        var sw = num(el.stroke_w, 0);
        if (el.shape === "rect" || el.shape === "ellipse") {
            var d = document.createElement("div");
            d.className = "idc-fill";
            d.style.background = el.gradient ? cssGradient(el.gradient, colour) : fill;
            if (stroke && sw) d.style.border = mm(sw) + " " + (el.dash || "solid") + " " + stroke;
            d.style.borderRadius = el.shape === "ellipse" ? "50%" : mm(el.radius);
            box.appendChild(d);
            return;
        }
        var s = svg("svg", { viewBox: "0 0 100 100", preserveAspectRatio: "none", "class": "idc-svg" });
        var paint = fill;
        if (el.gradient) {
            var gid = "idcg" + (++counter);
            var defs = svg("defs"), g;
            if (el.gradient.type === "radial") g = svg("radialGradient", { id: gid, cx: "0.3", cy: "0.3", r: "0.9" });
            else g = svg("linearGradient", { id: gid, x1: "0", y1: "0", x2: "1", y2: "0",
                                              gradientTransform: "rotate(" + (num(el.gradient.angle, 135) - 90) + " .5 .5)" });
            g.appendChild(svg("stop", { offset: "0", "stop-color": colour(el.gradient.from) }));
            g.appendChild(svg("stop", { offset: "1", "stop-color": colour(el.gradient.to) }));
            defs.appendChild(g); s.appendChild(defs);
            paint = "url(#" + gid + ")";
        }
        var shape;
        if (el.shape === "line") {
            shape = svg("line", { x1: "0", y1: "50", x2: "100", y2: "50" });
            stroke = stroke || colour(el.fill === "none" ? "ink" : el.fill);
            sw = sw || 0.4;
        } else {
            shape = svg("path", { d: PATHS[el.shape] || PATHS.triangle, fill: paint || "transparent" });
        }
        if (stroke && sw) {
            shape.setAttribute("stroke", stroke);
            shape.style.strokeWidth = mm(sw);
            shape.setAttribute("vector-effect", "non-scaling-stroke");
            shape.setAttribute("stroke-linecap", el.dash === "dotted" ? "round" : "butt");
            if (el.dash === "dashed") shape.style.strokeDasharray = num(sw, 0.4) * 4 + "mm " + num(sw, 0.4) * 2.5 + "mm";
            if (el.dash === "dotted") shape.style.strokeDasharray = "0.01mm " + num(sw, 0.4) * 2.2 + "mm";
        }
        s.appendChild(shape);
        box.appendChild(s);
    }

    function drawPicture(box, el, view, opts, colour) {
        var src = null, missing = "";
        if (el.type === "photo") { src = view.photo; missing = view.initials || (opts.editor ? "Photo" : ""); }
        if (el.type === "logo") { src = view.logo; missing = opts.editor ? "Logo" : ""; }
        if (el.type === "image") { src = (view.assets || {})[el.asset]; missing = opts.editor ? "Image" : ""; }
        if (el.type === "signature") {
            src = (view.signatures || {})[el.who];
            missing = opts.editor ? ({ holder: "Their signature", teacher: "Teacher's signature" }[el.who] || "Head's signature") : "";
        }
        var p = document.createElement("div");
        p.className = "idc-pic" + (el.type === "photo" ? " is-photo" : "");
        if (el.border && el.border !== "none" && num(el.border_w, 0)) p.style.border = mm(el.border_w) + " solid " + colour(el.border);
        if (el.clip === "circle" || el.clip === "ellipse") p.style.borderRadius = "50%";
        else if (el.clip === "rounded") p.style.borderRadius = mm(el.radius);
        else if (el.clip === "arch") p.style.borderRadius = mm(num(el.w, 10) / 2) + " " + mm(num(el.w, 10) / 2) + " 0 0";
        else if (CLIPS[el.clip]) p.style.clipPath = CLIPS[el.clip];
        else if (el.radius) p.style.borderRadius = mm(el.radius);
        if (src) {
            var img = document.createElement("img");
            img.src = src; img.alt = "";
            img.style.objectFit = ["cover", "contain", "fill"].indexOf(el.fit) >= 0 ? el.fit : "contain";
            if (el.grayscale) img.style.filter = "grayscale(1)";
            p.appendChild(img);
        } else if (missing) {
            var m = document.createElement("span"); m.className = "idc-missing"; m.textContent = missing; p.appendChild(m);
        }
        box.appendChild(p);
    }

    function drawCode(box, el, view, opts, colour) {
        var bg = el.bg && el.bg !== "none" ? colour(el.bg) : "transparent";
        var wrap = document.createElement("div");
        wrap.className = "idc-code";
        wrap.style.background = bg;
        if (el.radius) wrap.style.borderRadius = mm(el.radius);
        var code = el.type === "qr" ? view.qr : (view.barcodes || {})[el.field];
        if (code) {
            var s = svg("svg", { viewBox: "0 0 " + code[1] + " " + (el.type === "qr" ? code[1] : 100),
                                 preserveAspectRatio: el.type === "qr" ? "xMidYMid meet" : "none", "shape-rendering": "crispEdges" });
            s.appendChild(svg("path", { d: code[0], fill: colour(el.fg, "#15171a") }));
            wrap.appendChild(s);
            if (el.type === "barcode" && el.show_text) {
                var cap = document.createElement("div");
                cap.className = "idc-code-text"; cap.style.color = colour(el.fg, "#15171a");
                cap.textContent = (view.data || {})[el.field] || "";
                wrap.appendChild(cap);
                wrap.classList.add("has-text");
            }
        } else if (opts.editor) {
            var m = document.createElement("span"); m.className = "idc-missing"; m.textContent = el.type === "qr" ? "QR" : "Barcode";
            wrap.appendChild(m);
        }
        box.appendChild(wrap);
    }

    function element(el, i, view, opts, colour) {
        var box = document.createElement("div");
        box.className = "idc-el idc-" + el.type;
        box.dataset.i = i;
        var s = box.style;
        s.left = mm(el.x); s.top = mm(el.y); s.width = mm(el.w); s.height = mm(el.h);
        if (num(el.rotate, 0)) s.transform = "rotate(" + num(el.rotate, 0) + "deg)";
        if (num(el.opacity, 100) < 100) s.opacity = num(el.opacity, 100) / 100;
        if (SHADOWS[el.shadow]) s.filter = SHADOWS[el.shadow];
        if (el.hidden) { if (!opts.editor) return null; box.classList.add("is-hidden"); }
        if (blocks[el.type]) blocks[el.type](box, el, view, opts, colour);
        else if (el.type === "text" || el.type === "field") drawText(box, el, view, opts, colour);
        else if (el.type === "shape") drawShape(box, el, colour);
        else if (el.type === "qr" || el.type === "barcode") drawCode(box, el, view, opts, colour);
        else drawPicture(box, el, view, opts, colour);
        return box;
    }

    /* Fill ``host`` with one side of the card. ``view`` is what the card shows (see the module comment of
       blueprints/school/id_cards.py, _card_view); ``opts.editor`` adds placeholders for what is missing. */
    function draw(host, layout, side, view, opts) {
        opts = opts || {};
        var colour = colourOf(view || {});
        var size = sizeOf(layout);
        var card = document.createElement("div");
        card.className = "idc";
        card.style.width = size[0] + "mm"; card.style.height = size[1] + "mm";
        var bg = layout.background || {};
        card.style.background = colour(bg.color, "#ffffff");
        if (bg.gradient) {
            var gl = document.createElement("div"); gl.className = "idc-bg"; gl.style.background = cssGradient(bg.gradient, colour);
            card.appendChild(gl);
        }
        var bgUrl = bg.image && (view.assets || {})[bg.image];
        if (bgUrl) {
            var il = document.createElement("div"); il.className = "idc-bg";
            il.style.backgroundImage = cssUrl(bgUrl);
            il.style.backgroundSize = bg.fit === "contain" ? "contain" : (bg.fit === "fill" ? "100% 100%" : "cover");
            il.style.opacity = num(bg.opacity, 100) / 100;
            card.appendChild(il);
        }
        ((layout.sides || {})[side] || []).forEach(function (el, i) {
            var node = element(el, i, view || {}, opts, colour);
            if (node) card.appendChild(node);
        });
        host.textContent = "";
        host.appendChild(card);
        fitText(card);
        return card;
    }

    /* Text that does not fit its box is made smaller until it does (down to 4 pt), as the PDF does, rather than cut
       off: a long name still shows whole. Measured once the card is on the page. */
    function fitText(card) {
        card.querySelectorAll(".idc-tx").forEach(function (t) {
            var size = parseFloat(t.style.fontSize), tries = 0;
            if (!size || !t.clientHeight) return;
            while (t.scrollHeight > t.clientHeight + 0.5 && size > 4 && tries < 30) {
                size = Math.max(4, size - 0.5); t.style.fontSize = size + "pt"; tries++;
            }
        });
    }

    window.IdCardRender = { draw: draw, SIZES: SIZES, PAGES: PAGES, FONTS: FONTS, colourOf: colourOf, sizeOf: sizeOf,
                            blocks: blocks, mm: mm, num: num, svg: svg, cssGradient: cssGradient };
})();
