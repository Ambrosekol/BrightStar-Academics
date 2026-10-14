/* The report card's own blocks, drawn in the browser: the results table, the summary, the trait ratings, the grading
   key and the class teacher's comment. They plug into static/id-card-render.js (IdCardRender.blocks), which draws
   everything else on the page.

   Every block is laid out with explicit sizes in millimetres and the same rules as the PDF
   (core/report_card_design_pdf.py): a row is the box's height shared by the rows, between a least and a most height,
   and its type is never larger than the row allows. So a class with eight subjects and one with sixteen both fill
   their table, and what the studio shows is what the PDF prints. Text is set with textContent only. */
(function () {
    "use strict";
    var R = window.IdCardRender;
    if (!R) return;
    var PT = 2.8346;                                   // points a millimetre
    var WIDTHS = { subject: 30, test: 9, assignment: 13, project: 11, ca: 11, exam: 12, total: 12, average: 12, grade: 9, remark: 26 };
    var HEADS = { subject: "Subject", test: "Test", assignment: "Assignment", project: "Project", ca: "CA", exam: "Exam",
                  total: "Total", average: "Class avg", grade: "Grade", remark: "Remark" };
    var LABELS = { charged: "Fees for the session", paid: "Paid so far", balance: "Balance due", position: "Position", highest: "Highest in class", lowest: "Lowest in class",
                   opened: "School opened", present: "Days present", absent: "Days absent", late: "Days late",
                   attendance: "Attendance", total: "Total obtained", percentage: "Percentage", grade: "Overall grade", average: "Class average",
                   size: "Class size", subjects: "Subjects" };

    function clamp(n, lo, hi) { return Math.max(lo, Math.min(hi, n)); }
    function mm(v) { return v + "mm"; }
    function div(cls, style) { var d = document.createElement("div"); d.className = cls; if (style) Object.assign(d.style, style); return d; }
    var ruler = document.createElement("canvas").getContext("2d");
    function fitted(text, size, bold, family, room) {
        // As the PDF does: too wide, the type is made smaller, to six tenths of its size, before it is cut short.
        // A width grows in step with the size, so the size that fits is worked out from one measurement.
        ruler.font = (bold ? "700 " : "400 ") + size + "pt " + family;
        var w = ruler.measureText(text).width / 96 * 25.4;
        return w > room && w > 0 ? Math.max(size * 0.6, size * room / w) : size;
    }
    function cell(parent, text, x, y, w, h, o) {
        text = text == null ? "" : String(text);
        var size = o.size;
        if (o.shrink && text) size = fitted(text, size, o.bold, (parent.closest(".rc-block") || parent).style.fontFamily || "sans-serif", w - 2 * (o.padX || 0));
        var d = div("rc-cell", { left: mm(x), top: mm(y), width: mm(w), height: mm(h), lineHeight: mm(h),
                                 fontSize: size + "pt", fontWeight: o.bold ? "700" : "400", color: o.color,
                                 textAlign: o.align || "left", paddingLeft: mm(o.padX || 0), paddingRight: mm(o.padX || 0) });
        if (o.opacity) d.style.opacity = o.opacity;
        d.textContent = text;
        parent.appendChild(d);
        return d;
    }
    function rect(parent, x, y, w, h, fill) { var d = div("rc-rect", { left: mm(x), top: mm(y), width: mm(w), height: mm(h), background: fill }); parent.appendChild(d); return d; }
    function frame(box, el, colour) {
        // The block's own box: its background, outline and corners; everything else is drawn inside it.
        var f = div("rc-block", { fontFamily: R.FONTS[el.font] || R.FONTS.dejavu });
        if (el.fill && el.fill !== "none") f.style.background = colour(el.fill);
        if (el.radius) f.style.borderRadius = mm(el.radius);
        box.appendChild(f);
        return f;
    }
    function outline(f, el, colour) {
        if (el.line && el.line !== "none" && el.line_w) {
            var o = div("rc-outline", { border: mm(el.line_w) + " solid " + colour(el.line), borderRadius: el.radius ? mm(el.radius) : "" });
            f.appendChild(o);
        }
    }
    function report(view) { return (view && view.report) || { subjects: [], summary: {}, traits: [], grading: [], comment: "", maxes: {} }; }

    /* ------------------------------------------------ the results table */
    R.blocks.results = function (box, el, view, opts, colour) {
        var f = frame(box, el, colour), rep = report(view), rows = rep.subjects || [];
        var cols = el.columns || ["subject", "total", "grade"], weights = cols.reduce(function (t, c) { return t + (WIDTHS[c] || 10); }, 0);
        var n = Math.max(rows.length, 1), rowH = clamp(el.h / (n + 1), 3.2, 9);
        var size = Math.min(el.size, rowH * PT * 0.62), headSize = Math.min(el.head_size || el.size, rowH * PT * 0.6);
        var line = el.line && el.line !== "none" && el.line_w ? colour(el.line) : null, pad = Math.min(1.4, el.pad);
        var x = 0, xs = cols.map(function (c) { var w = el.w * (WIDTHS[c] || 10) / weights, at = x; x += w; return [at, w]; });
        if (el.head_fill && el.head_fill !== "none") rect(f, 0, 0, el.w, rowH, colour(el.head_fill));
        var heads = cols.map(function (c) { return HEADS[c] + ((rep.maxes || {})[c] ? " (" + rep.maxes[c] + ")" : ""); });
        var family = R.FONTS[el.font] || R.FONTS.dejavu;
        // Every heading the same size: the largest at which the longest one fits its column.
        heads.forEach(function (h, i) { headSize = Math.max(Math.min(headSize, fitted(h, headSize, true, family, xs[i][1] - 2 * pad)), (el.head_size || el.size) * 0.6); });
        cols.forEach(function (c, i) {
            cell(f, heads[i], xs[i][0], 0, xs[i][1], rowH, { size: headSize, bold: true, color: colour(el.head_color),
                 align: c === "subject" || c === "remark" ? "left" : "center", padX: pad });
        });
        if (!rows.length) {
            cell(f, opts.editor ? "The student's subjects and scores appear here" : "No results recorded.", 0, rowH, el.w, rowH,
                 { size: size, color: colour(el.color), align: "center", opacity: 0.6 });
        }
        rows.forEach(function (r, ri) {
            var y = rowH * (ri + 1);
            if (el.zebra && el.zebra !== "none" && ri % 2 === 1) rect(f, 0, y, el.w, rowH, colour(el.zebra));
            cols.forEach(function (c, i) {
                cell(f, r[c], xs[i][0], y, xs[i][1], rowH, { size: size, bold: c === "total", color: colour(el.color),
                     align: c === "subject" || c === "remark" ? "left" : (el.numbers === "right" ? "right" : "center"), padX: pad, shrink: c !== "remark" });
            });
        });
        if (line) {
            var w = el.line_w, total = rowH * (n + 1);
            for (var r = 1; r <= n; r++) rect(f, 0, rowH * r - w / 2, el.w, w, line);
            xs.slice(1).forEach(function (c) { rect(f, c[0] - w / 2, 0, w, total, line); });
            var o = div("rc-outline", { height: mm(total), bottom: "auto", border: mm(w) + " solid " + line, borderRadius: el.radius ? mm(el.radius) : "" });
            f.appendChild(o);
        }
    };

    /* ------------------------------------------------ the summary */
    R.blocks.summary = function (box, el, view, opts, colour) {
        var f = frame(box, el, colour), s = report(view).summary || {}, items = el.items || ["total", "percentage", "grade"];
        var k = items.length, line = el.line && el.line !== "none" && el.line_w ? colour(el.line) : null;
        items.forEach(function (item, i) {
            var value = s[item] || (opts.editor ? "—" : "");
            if (el.layout === "column") {
                var h = el.h / k, y = h * i, labelH = Math.min(h * 0.38, 5), vs = Math.min(el.value_size, (h - labelH) * PT * 0.7);
                cell(f, LABELS[item], 0, y + h * 0.08, el.w, labelH, { size: Math.min(el.size, labelH * PT * 0.75), color: colour(el.color), align: "center", opacity: 0.85 });
                cell(f, value, 0, y + h * 0.08 + labelH, el.w, h - labelH - h * 0.16, { size: vs, bold: true, color: colour(el.accent), align: "center", shrink: true });
                if (line && i) rect(f, el.w * 0.2, y, el.w * 0.6, el.line_w, line);
            } else {
                var w = el.w / k, x = w * i, lh = Math.min(el.h * 0.38, 5), vsize = Math.min(el.value_size, (el.h - lh - el.h * 0.16) * PT * 0.7);
                cell(f, LABELS[item], x, el.h * 0.1, w, lh, { size: Math.min(el.size, lh * PT * 0.75), color: colour(el.color), align: "center", opacity: 0.85, padX: 0.8 });
                cell(f, value, x, el.h * 0.1 + lh, w, el.h - lh - el.h * 0.2, { size: vsize, bold: true, color: colour(el.accent), align: "center", padX: 0.8, shrink: true });
                if (line && i) rect(f, x - el.line_w / 2, el.h * 0.15, el.line_w, el.h * 0.7, line);
            }
        });
        outline(f, el, colour);
    };

    /* ------------------------------------------------ the trait ratings */
    R.blocks.traits = function (box, el, view, opts, colour) {
        var f = frame(box, el, colour), groups = report(view).traits || [];
        if (!groups.length) {
            if (opts.editor) cell(f, "Trait ratings appear here when the school rates them", 0, 0, el.w, Math.min(el.h, 8), { size: Math.min(el.size, 8), color: colour(el.color), align: "center", opacity: 0.6 });
            return;
        }
        var line = el.line && el.line !== "none" && el.line_w ? colour(el.line) : null, gutter = 3;
        var side = el.layout !== "stack", n = groups.length;
        var totalRows = groups.reduce(function (t, g) { return t + g.entries.length + 1; }, 0);
        var y = 0;
        groups.forEach(function (g, gi) {
            var gw = side ? (el.w - gutter * (n - 1)) / n : el.w, gx = side ? gi * (gw + gutter) : 0;
            var gh = side ? el.h : el.h * (g.entries.length + 1) / totalRows, gy = side ? 0 : y;
            var rowH = clamp(gh / (g.entries.length + 1), 3, 7), size = Math.min(el.size, rowH * PT * 0.62);
            if (el.head_fill && el.head_fill !== "none") rect(f, gx, gy, gw, rowH, colour(el.head_fill));
            cell(f, g.name, gx, gy, gw, rowH, { size: size, bold: true, color: el.head_fill && el.head_fill !== "none" ? colour(el.head_color) : colour(el.accent), padX: 1.2 });
            g.entries.forEach(function (e, ei) {
                var ry = gy + rowH * (ei + 1);
                if (el.zebra && el.zebra !== "none" && ei % 2 === 1) rect(f, gx, ry, gw, rowH, colour(el.zebra));
                cell(f, e.label, gx, ry, gw * 0.6, rowH, { size: size, color: colour(el.color), padX: 1.2 });
                cell(f, e.rating, gx + gw * 0.6, ry, gw * 0.4, rowH, { size: size, bold: true, color: colour(el.color), align: "right", padX: 1.2 });
                if (line) rect(f, gx, ry - el.line_w / 2, gw, el.line_w, line);
            });
            if (line) {
                var o = div("rc-outline", { left: mm(gx), top: mm(gy), width: mm(gw), height: mm(rowH * (g.entries.length + 1)), right: "auto", bottom: "auto",
                                            border: mm(el.line_w) + " solid " + line, borderRadius: el.radius ? mm(el.radius) : "" });
                f.appendChild(o);
            }
            y += gh;
        });
    };

    /* ------------------------------------------------ the grading key */
    R.blocks.grading = function (box, el, view, opts, colour) {
        var f = frame(box, el, colour), keys = report(view).grading || [];
        var per = Math.max(1, el.per_row || 3), rows = Math.max(1, Math.ceil(keys.length / per));
        var titleH = el.title ? Math.min(el.h * 0.3, 5) : 0, rowH = (el.h - titleH) / rows;
        var size = Math.min(el.size, rowH * PT * 0.62), w = el.w / per;
        if (el.title) cell(f, el.title, 0, 0, el.w, titleH, { size: Math.min(el.size + 1, titleH * PT * 0.7), bold: true, color: colour(el.accent), padX: 1.2 });
        keys.forEach(function (k, i) {
            var text = k.grade + (k.range ? ": " + k.range : "") + (k.remark ? " (" + k.remark + ")" : "");
            cell(f, text, (i % per) * w, titleH + Math.floor(i / per) * rowH, w, rowH, { size: size, color: colour(el.color), padX: 1.2 });
        });
        outline(f, el, colour);
    };

    /* ------------------------------------------------ the class teacher's comment */
    R.blocks.comment = function (box, el, view, opts, colour) {
        var f = frame(box, el, colour), text = report(view).comment || "";
        var pad = el.pad, labelH = el.label ? Math.min(el.size * 1.5 / PT, el.h * 0.35) : 0;
        if (el.label) cell(f, el.label, pad, pad * 0.6, el.w - pad * 2, labelH, { size: Math.min(el.size, labelH * PT * 0.75), bold: true, color: colour(el.accent) });
        var top = pad * 0.6 + labelH, room = el.h - top - pad * 0.6;
        if (text) {
            var t = div("idc-tx rc-comment", { left: mm(pad), top: mm(top), width: mm(el.w - pad * 2), height: mm(room), right: "auto", bottom: "auto",
                                               fontSize: el.comment_size + "pt", color: colour(el.color), lineHeight: 1.3 });
            t.textContent = text;
            f.appendChild(t);
        } else if (el.lines) {
            var lineColour = el.line && el.line !== "none" ? colour(el.line) : "#c9d2de";
            for (var i = 1; i <= 3; i++) rect(f, pad, top + room * i / 3.2, el.w - pad * 2, 0.25, lineColour);
        }
        outline(f, el, colour);
    };

    // The building pieces, for the receipt's own blocks (static/receipt-render.js).
    R.rc = { frame: frame, outline: outline, cell: cell, rect: rect, div: div, mm: mm, PT: PT };
})();
