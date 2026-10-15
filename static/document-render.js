/* A school document's own blocks, drawn in the browser: the paragraph (the school's words with the document's details
   put in) and the transcript table (the report card's results table, one column per session). They plug into
   static/id-card-render.js like the report card's blocks, with the same rules as the PDF (core/document_design_pdf.py).
   Text is set with textContent only: nothing a school types is ever read as HTML. */
(function () {
    "use strict";
    var R = window.IdCardRender;
    if (!R || !R.rc) return;
    var K = R.rc, PT = K.PT, mm = K.mm;
    var TOKEN = /\{([a-z_]+)\}/g;

    function fill(text, data) {
        // Each detail put in once; anything in curly brackets that is not a detail is printed as written.
        return String(text || "").replace(TOKEN, function (m, key) { return Object.prototype.hasOwnProperty.call(data, key) ? String(data[key] || "") : m; });
    }

    /* ------------------------------------------------ the paragraph */
    R.blocks.paragraph = function (box, el, view, opts, colour) {
        var f = K.frame(box, el, colour), data = (view && view.data) || {};
        var size = el.size || 12, pad = el.pad || 0;
        var t = K.div("doc-para", { left: mm(pad), top: mm(pad), width: mm(el.w - 2 * pad), height: mm(el.h - 2 * pad),
                                    fontSize: size + "pt", lineHeight: (el.leading || 150) / 100, textAlign: el.align || "justify",
                                    fontStyle: el.italic ? "italic" : "normal", color: colour(el.color), whiteSpace: "pre-wrap",
                                    overflow: "hidden", display: "flex", flexDirection: "column",
                                    justifyContent: el.valign === "middle" ? "center" : (el.valign === "bottom" ? "flex-end" : "flex-start") });
        var inner = document.createElement("span");
        inner.textContent = fill(el.text, data);
        t.appendChild(inner);
        f.appendChild(t);
        // As in the PDF: as large as set, smaller only if the words would not fit the box.
        function shrink() {
            var s = size;
            while (s > 5 && t.scrollHeight > t.clientHeight + 1) { s -= 0.5; t.style.fontSize = s + "pt"; }
        }
        if (t.isConnected) shrink(); else window.requestAnimationFrame(shrink);
        K.outline(f, el, colour);
    };

    /* ------------------------------------------------ the transcript */
    R.blocks.transcript = function (box, el, view, opts, colour) {
        var tr = ((view && view.report) || {}).transcript || {}, sessions = tr.sessions || [];
        var cols = ["subject"].concat(sessions.map(function (s) { return s.key; }), ["average"], el.show_grade === false ? [] : ["grade"]);
        var heads = { subject: "Subject", average: "Average", grade: "Grade" };
        sessions.forEach(function (s) { heads[s.key] = [s["class"], s.name].filter(Boolean).join(" "); });
        R.blocks.results(box, Object.assign({}, el, { columns: cols }),
                         { report: { subjects: tr.rows || [], maxes: {}, heads: heads } }, opts, colour);
    };

    R.documentFill = fill;
})();
