/* A receipt's own blocks, drawn in the browser: the amount paid and the PAID stamp (the balance is the report card's
   summary block, static/report-card-render.js), and the VOID mark a voided receipt always carries. They plug into
   static/id-card-render.js like the report card's blocks, with the same rules as the PDF (core/receipt_design_pdf.py).
   Text is set with textContent only. */
(function () {
    "use strict";
    var R = window.IdCardRender;
    if (!R || !R.rc) return;
    var K = R.rc, PT = K.PT, mm = K.mm;
    function report(view) { return (view && view.report) || {}; }

    /* ------------------------------------------------ the amount paid */
    R.blocks.amount = function (box, el, view, opts, colour) {
        var f = K.frame(box, el, colour), rep = report(view), pad = el.pad || 1.6;
        var labelH = el.label ? Math.min(el.h * 0.24, 6) : 0;
        var words = el.words && (rep.words || opts.editor) ? (rep.words || "The amount in words") : "";
        var wordsH = words ? Math.min(el.h * 0.3, 9) : 0;
        if (labelH) K.cell(f, el.label, 0, pad * 0.6, el.w, labelH, { size: Math.min(el.size, labelH * PT * 0.7), color: colour(el.color), align: "center", opacity: 0.8, padX: pad, shrink: true });
        var figureH = el.h - labelH - wordsH - pad;
        K.cell(f, rep.amount || (opts.editor ? "₦0.00" : ""), 0, labelH + pad * 0.5, el.w, figureH,
               { size: Math.min(el.value_size, figureH * PT * 0.72), bold: true, color: colour(el.accent), align: "center", padX: pad, shrink: true });
        if (wordsH) {
            var t = K.div("idc-tx", { left: mm(pad), bottom: mm(pad * 0.5), top: "auto", width: mm(el.w - 2 * pad), height: mm(wordsH - pad * 0.5),
                                      fontSize: Math.min(el.size, 8) + "pt", fontStyle: "italic", color: colour(el.color), textAlign: "center",
                                      display: "flex", alignItems: "center", justifyContent: "center", lineHeight: 1.12, overflow: "hidden" });
            t.textContent = words;
            f.appendChild(t);
        }
        K.outline(f, el, colour);
    };

    /* ------------------------------------------------ the PAID stamp */
    R.blocks.stamp = function (box, el, view, opts, colour) {
        if (report(view).voided) return;
        var ink = colour(el.accent), lw = Math.max(0.28, Math.min(el.w, el.h) * 0.035), round = el.shape !== "box";
        var radius = round ? "50%" : mm(Math.min(el.w, el.h) * 0.08);
        var outer = K.div("rc-rect", { inset: "0", border: mm(lw) + " solid " + ink, borderRadius: radius, boxSizing: "border-box" });
        var inner = K.div("rc-rect", { inset: mm(lw * 1.8), border: mm(lw * 0.45) + " solid " + ink, borderRadius: radius, boxSizing: "border-box" });
        box.appendChild(outer); box.appendChild(inner);
        var word = el.text || "PAID", date = el.show_date ? (report(view).date || (opts.editor ? "14/10/2026" : "")) : "";
        var t = K.div("rc-stamp", { inset: "0", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
                                     color: ink, fontFamily: R.FONTS[el.font] || R.FONTS.dejavu, lineHeight: 1 });
        var w = document.createElement("strong");
        w.textContent = word;
        // As in the PDF: as large as the stamp's height allows, and no wider than three quarters of it.
        var size = Math.min(el.h * 0.34 * PT, (el.w * 0.74 * PT) / Math.max(word.length * 0.68, 1));
        w.style.fontSize = size + "pt"; w.style.fontWeight = "800";
        t.appendChild(w);
        if (date) { var d = document.createElement("span"); d.textContent = date; d.style.fontSize = Math.min(size * 0.32, el.h * 0.1 * PT) + "pt"; d.style.marginTop = "0.3em"; t.appendChild(d); }
        box.appendChild(t);
    };

    /* ------------------------------------------------ VOID, across a voided receipt */
    R.voidMark = function (page) {
        var v = document.createElement("div");
        v.className = "rcd-void";
        var big = document.createElement("strong"); big.textContent = "VOID";
        var small = document.createElement("span"); small.textContent = "This receipt was cancelled";
        v.appendChild(big); v.appendChild(small);
        page.appendChild(v);
    };
})();
