"""Draws report cards as a PDF from a school's own design (core/report_card_design.py): each student's card on one A4
page, or two when the design has a second page.

    pdf_bytes = render_designed_pdf(cards, layout, assets)

``cards`` are the same dictionaries core/report_card_pdf.py documents; ``layout`` is a design that has already been
through core/report_card_design.clean_layout; ``assets`` maps the school's image ids to their stored uploads.

Everything is drawn with reportlab's canvas, element by element, with the same rules as the studio's canvas
(static/id-card-render.js and static/report-card-render.js): text wraps in its box and is made smaller until it fits
(never below 4 pt); a table's rows share their box between a least and a most height; pictures are fitted and
clipped; gradients, outlines, turns and opacity are the PDF's own. Text is always drawn as text, never interpreted.
A picture that cannot be read is left out.
"""
import io
import math
import re

from PIL import Image
import core.pdf_setup  # noqa: F401  (how every PDF is written)
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as rl_canvas

from core.id_cards import barcode_path
from core.report_card_design import PAGE_MM, SIDES, card_fields, view_data
from core.report_card_pdf import _Pictures, _clean, _fonts

PT = mm                                   # points in a millimetre
WIDTHS = {'subject': 30, 'test': 9, 'assignment': 13, 'project': 11, 'ca': 11, 'exam': 12, 'total': 12, 'average': 12,
          'grade': 9, 'remark': 26}
HEADS = {'subject': 'Subject', 'test': 'Test', 'assignment': 'Assignment', 'project': 'Project', 'ca': 'CA', 'exam': 'Exam',
         'total': 'Total', 'average': 'Class avg', 'grade': 'Grade', 'remark': 'Remark'}
LABELS = {'position': 'Position', 'highest': 'Highest in class', 'lowest': 'Lowest in class',
          'opened': 'School opened', 'present': 'Days present', 'absent': 'Days absent', 'late': 'Days late',
          'attendance': 'Attendance', 'total': 'Total obtained', 'percentage': 'Percentage', 'grade': 'Overall grade', 'average': 'Class average',
          'size': 'Class size', 'subjects': 'Subjects'}
PATHS = {
    'triangle': 'M50 0L100 100L0 100Z', 'diamond': 'M50 0L100 50L50 100L0 50Z',
    'hexagon': 'M25 0L75 0L100 50L75 100L25 100L0 50Z', 'pentagon': 'M50 0L100 38L81 100L19 100L0 38Z',
    'star': 'M50 0L61 35L98 35L68 57L79 91L50 70L21 91L32 57L2 35L39 35Z', 'chevron': 'M0 0L75 0L100 50L75 100L0 100L25 50Z',
    'arrow': 'M0 30L60 30L60 0L100 50L60 100L60 70L0 70Z', 'wave': 'M0 40C20 10 40 10 55 35S85 65 100 30L100 100L0 100Z',
    'corner': 'M0 0L100 0C70 10 30 40 0 100Z',
}
CLIPS = {'hexagon': [(25, 0), (75, 0), (100, 50), (75, 100), (25, 100), (0, 50)],
         'diamond': [(50, 0), (100, 50), (50, 100), (0, 50)], 'shield': [(0, 0), (100, 0), (100, 62), (50, 100), (0, 62)],
         'star': [(50, 0), (61, 35), (98, 35), (68, 57), (79, 91), (50, 70), (21, 91), (32, 57), (2, 35), (39, 35)]}
STANDARD = {'helvetica': ('Helvetica', 'Helvetica-Bold', 'Helvetica-Oblique', 'Helvetica-BoldOblique'),
            'times': ('Times-Roman', 'Times-Bold', 'Times-Italic', 'Times-BoldItalic'),
            'mono': ('Courier', 'Courier-Bold', 'Courier-Oblique', 'Courier-BoldOblique')}


def _fillc(c, colour):
    """Set the fill colour without losing the element's opacity (reportlab resets it with every colour)."""
    c.setFillColor(colour)
    c.setFillAlpha(getattr(c, '_bs_alpha', 1.0))


def _strokec(c, colour):
    c.setStrokeColor(colour)
    c.setStrokeAlpha(getattr(c, '_bs_alpha', 1.0))


def _clamp(n, lo, hi):
    return max(lo, min(hi, n))


# ---------------------------------------------------------------- colours and fonts
class _Paint:
    def __init__(self, school):
        self.brand = self._hex(school.get('primary')) or colors.HexColor('#0d2b52')
        self.accent = self._hex(school.get('accent')) or colors.HexColor('#1d6fd8')

    @staticmethod
    def _hex(value):
        value = str(value or '').strip()
        return colors.HexColor(value) if re.fullmatch(r'#[0-9a-fA-F]{6}', value) else None

    def __call__(self, name, default=None):
        if name in (None, '', 'none'):
            return default
        if name == 'brand':
            return self.brand
        if name == 'accent':
            return self.accent
        if name == 'white':
            return colors.white
        if name == 'ink':
            return colors.HexColor('#15171a')
        return self._hex(name) or default


def _font(key, bold=False, italic=False):
    """(font name, slant the text ourselves?) for a design's font."""
    if key in STANDARD:
        regular, b, i, bi = STANDARD[key]
        return (bi if bold and italic else b if bold else i if italic else regular), False
    regular, b, _ = _fonts()
    return (b if bold else regular), italic


def _safe(text, font):
    """Text the font can draw: the built-in PDF fonts know only Western European letters."""
    text = _clean(text)
    if font.startswith(('Helvetica', 'Times', 'Courier')):
        # The Naira sign is not among them: the usual stand-in, N, rather than a question mark.
        text = text.replace('₦', 'N').encode('cp1252', 'replace').decode('cp1252')
    return text


def _width(text, font, size, letter=0.0):
    return stringWidth(text, font, size) + letter * len(text)


# ---------------------------------------------------------------- one element's own space
class _Box:
    """Draws inside one element: (0, 0) is its bottom-left corner, sizes in points, y upwards, turned and faded as the
    element is."""

    def __init__(self, c, page_h, el):
        self.c, self.W, self.H = c, el['w'] * PT, el['h'] * PT
        c.saveState()
        cx, cy = (el['x'] + el['w'] / 2) * PT, page_h - (el['y'] + el['h'] / 2) * PT
        c.translate(cx, cy)
        if el.get('rotate'):
            c.rotate(-el['rotate'])
        c.translate(-self.W / 2, -self.H / 2)
        alpha = (el.get('opacity', 100) or 0) / 100
        c.setFillAlpha(alpha)
        c.setStrokeAlpha(alpha)
        self.alpha = alpha
        self._before = getattr(c, '_bs_alpha', 1.0)
        c._bs_alpha = alpha

    def done(self):
        self.c.restoreState()
        self.c._bs_alpha = self._before

    def top(self, y_mm):
        """A distance from the element's top, in millimetres, as a y in points."""
        return self.H - y_mm * PT


def _shadow(box, el, radius=0, path=None):
    """A soft or strong shadow under the element, the shape of ``path`` (or of its box)."""
    if el.get('shadow') not in ('soft', 'strong'):
        return
    c = box.c
    c.saveState()
    strong = el['shadow'] == 'strong'
    off = (0.8 if strong else 0.4) * PT
    c.translate(off * 0.3, -off)
    c.setFillColor(colors.black)
    c.setFillAlpha(box.alpha * (0.32 if strong else 0.16))
    if path is not None:
        c.drawPath(path, stroke=0, fill=1)
    else:
        c.roundRect(0, 0, box.W, box.H, radius, stroke=0, fill=1)
    c.restoreState()


def _path_from(c, spec, W, H):
    """A reportlab path from one of the shape strings (M, L, C, S, Z in a 100 x 100 box)."""
    p = c.beginPath()
    tokens = re.findall(r'[MLCSZ]|-?\d+(?:\.\d+)?', spec)
    i, last_ctrl, cur = 0, None, (0, 0)

    def pt(x, y):
        return float(x) / 100 * W, H - float(y) / 100 * H
    cmd = None
    while i < len(tokens):
        t = tokens[i]
        if t in 'MLCSZ':
            cmd = t
            i += 1
            if cmd == 'Z':
                p.close()
            continue
        if cmd == 'M':
            cur = pt(tokens[i], tokens[i + 1]); p.moveTo(*cur); i += 2; last_ctrl = None
        elif cmd == 'L':
            cur = pt(tokens[i], tokens[i + 1]); p.lineTo(*cur); i += 2; last_ctrl = None
        elif cmd == 'C':
            c1, c2, end = pt(tokens[i], tokens[i + 1]), pt(tokens[i + 2], tokens[i + 3]), pt(tokens[i + 4], tokens[i + 5])
            p.curveTo(*c1, *c2, *end); last_ctrl, cur = c2, end; i += 6
        elif cmd == 'S':
            c1 = (2 * cur[0] - last_ctrl[0], 2 * cur[1] - last_ctrl[1]) if last_ctrl else cur
            c2, end = pt(tokens[i], tokens[i + 1]), pt(tokens[i + 2], tokens[i + 3])
            p.curveTo(*c1, *c2, *end); last_ctrl, cur = c2, end; i += 4
        else:
            i += 1
    return p


def _gradient_fill(c, gradient, paint, W, H):
    start, end = paint(gradient.get('from'), colors.white), paint(gradient.get('to'), colors.white)
    if gradient.get('type') == 'radial':
        c.radialGradient(0.3 * W, 0.7 * H, 0.9 * max(W, H), (start, end), extend=True)
        return
    a = math.radians(gradient.get('angle', 135))
    dx, dy = math.sin(a), math.cos(a)                      # CSS: 0deg points up, 90deg right
    half = abs(W / 2 * dx) + abs(H / 2 * dy)
    cx, cy = W / 2, H / 2
    c.linearGradient(cx - dx * half, cy - dy * half, cx + dx * half, cy + dy * half, (start, end), extend=True)


# ---------------------------------------------------------------- shapes
def _shape(box, el, paint):
    c, W, H = box.c, box.W, box.H
    stroke = paint(el.get('stroke'))
    sw = (el.get('stroke_w') or 0) * PT
    radius = min((el.get('radius') or 0) * PT, W / 2, H / 2)
    if el['shape'] == 'line':
        colour = stroke or paint(el.get('fill'), colors.HexColor('#15171a'))
        _strokec(c, colour)
        c.setLineWidth(sw or 0.4 * PT)
        _dash(c, el, sw or 0.4 * PT)
        c.line(0, H / 2, W, H / 2)
        return
    if el['shape'] == 'rect':
        path = c.beginPath(); path.roundRect(0, 0, W, H, radius)
    elif el['shape'] == 'ellipse':
        path = c.beginPath(); path.ellipse(0, 0, W, H)
    else:
        path = _path_from(c, PATHS.get(el['shape'], PATHS['triangle']), W, H)
    _shadow(box, el, radius, path)
    fill = paint(el.get('fill'))
    if el.get('gradient'):
        c.saveState(); c.clipPath(path, stroke=0, fill=0); _gradient_fill(c, el['gradient'], paint, W, H); c.restoreState()
    elif fill is not None:
        _fillc(c, fill); c.drawPath(path, stroke=0, fill=1)
    if stroke is not None and sw:
        _strokec(c, stroke); c.setLineWidth(sw); _dash(c, el, sw)
        c.drawPath(path, stroke=1, fill=0)


def _dash(c, el, sw):
    if el.get('dash') == 'dashed':
        c.setDash(sw * 4, sw * 2.5)
    elif el.get('dash') == 'dotted':
        c.setLineCap(1); c.setDash(0.01, sw * 2.2)


# ---------------------------------------------------------------- pictures
def _clip_shape(c, el, W, H):
    clip, radius = el.get('clip', 'none'), min((el.get('radius') or 0) * PT, W / 2, H / 2)
    p = c.beginPath()
    if clip in ('circle', 'ellipse'):
        p.ellipse(0, 0, W, H)
    elif clip == 'arch':
        r = min(W / 2, H)
        p.moveTo(0, 0); p.lineTo(W, 0); p.lineTo(W, H - r); p.arcTo(0, H - 2 * r, W, H, 0, 180); p.lineTo(0, 0); p.close()
    elif clip in CLIPS:
        points = CLIPS[clip]
        p.moveTo(points[0][0] / 100 * W, H - points[0][1] / 100 * H)
        for x, y in points[1:]:
            p.lineTo(x / 100 * W, H - y / 100 * H)
        p.close()
    else:
        p.roundRect(0, 0, W, H, radius if clip == 'rounded' or radius else 0)
    return p


def _reader(pictures, stored, data, grayscale=False):
    """One ImageReader for each picture in a PDF, so a logo or a watermark on every card is decoded and embedded once,
    not once a page."""
    readers = pictures.__dict__.setdefault('_readers', {})
    key = (stored, grayscale, len(data))
    if key not in readers:
        if grayscale:
            try:
                with Image.open(io.BytesIO(data)) as im:
                    out = io.BytesIO(); im.convert('L').save(out, 'PNG'); data = out.getvalue()
            except Exception:
                pass
        readers[key] = ImageReader(io.BytesIO(data))
    return readers[key]


def _picture(box, el, stored, pictures, paint, photo=False, initials=''):
    c, W, H = box.c, box.W, box.H
    loaded = pictures.load(stored, max_px=900, photo=photo) if stored else None
    path = _clip_shape(c, el, W, H)
    _shadow(box, el, 0, path)
    if loaded:
        data, iw, ih = loaded
        fit = el.get('fit', 'contain')
        if fit == 'fill':
            dw, dh = W, H
        else:
            scale = (max if fit == 'cover' else min)(W / iw, H / ih)
            dw, dh = iw * scale, ih * scale
        c.saveState()
        c.clipPath(path, stroke=0, fill=0)
        try:
            c.drawImage(_reader(pictures, stored, data, bool(el.get('grayscale'))), (W - dw) / 2, (H - dh) / 2, dw, dh, mask='auto')
        except Exception:
            pass
        c.restoreState()
    elif photo:
        c.saveState(); _fillc(c, colors.HexColor('#e9edf3')); c.drawPath(path, stroke=0, fill=1)
        if initials:
            font = _fonts()[1]
            size = min(H, W) * 0.3
            _fillc(c, colors.HexColor('#5d6673')); c.setFont(font, size)
            c.drawCentredString(W / 2, H / 2 - size * 0.35, initials)
        c.restoreState()
    border = paint(el.get('border'))
    if border is not None and el.get('border_w'):
        _strokec(c, border); c.setLineWidth(el['border_w'] * PT); c.drawPath(path, stroke=1, fill=0)


# ---------------------------------------------------------------- text
def _wrap(runs, width, size, letter):
    """Lines of (text, font, alpha) pieces, wrapped to ``width``. ``runs`` is [(text, font, alpha)]; a newline starts a
    new line; a word longer than the line stands on its own (and the size is then made smaller by the caller)."""
    lines, line, line_w = [], [], 0.0
    for text, font, alpha in runs:
        for part_i, part in enumerate(text.split('\n')):
            if part_i:
                lines.append(line); line, line_w = [], 0.0
            for word in re.findall(r'\S+\s*', part):
                if line and line_w + _width(word.rstrip(), font, size, letter) > width:
                    lines.append(line); line, line_w = [], 0.0
                line.append((word, font, alpha))
                line_w += _width(word, font, size, letter)
    lines.append(line)
    return lines


def _text(box, el, value, label, paint):
    c, W, H = box.c, box.W, box.H
    pad = (el.get('pad') or 0) * PT
    radius = min((el.get('radius') or 0) * PT, W / 2, H / 2)
    fill = paint(el.get('fill'))
    if fill is not None:
        _shadow(box, el, radius)
        _fillc(c, fill); c.roundRect(0, 0, W, H, radius, stroke=0, fill=1)
    if el.get('upper'):
        value, label = value.upper(), label.upper()
    bold = (el.get('weight') or 400) >= 600
    font, slant = _font(el.get('font'), bold, el.get('italic'))
    label_font = _font(el.get('font'), False, el.get('italic'))[0]
    value = _safe(value, font)
    label = _safe(label, label_font)
    if not value and not label:
        return
    size = el.get('size') or 8
    room_w, room_h = max(W - 2 * pad, 1), max(H - 2 * pad, 1)
    runs = ([(label + ': ', label_font, 0.72)] if label and value else []) + [(value, font, 1.0)]

    def layout_at(sz):
        letter_ = (el.get('letter') or 0) / 100 * sz
        lead_ = (el.get('line') or 118) / 100 * sz
        lines_ = _wrap(runs, room_w, sz, letter_)
        widest = max((sum(_width(t, f, sz, letter_) for t, f, _ in ln) for ln in lines_), default=0)
        return (len(lines_) * lead_ <= room_h + 0.5 and widest <= room_w + 2), letter_, lead_, lines_

    fits, letter, lead, lines = layout_at(size)
    if not fits and size > 4:
        # The largest size, in half points, that fits: halving the range takes a handful of tries, not dozens.
        lo, hi = 0, int((size - 4) * 2)          # steps of 0.5 above 4 pt; hi does not fit
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if layout_at(4 + mid / 2)[0]:
                lo = mid
            else:
                hi = mid - 1
        size = 4 + lo / 2
        fits, letter, lead, lines = layout_at(size)
    colour = paint(el.get('color'), colors.HexColor('#15171a'))
    block_h = len(lines) * lead
    valign = el.get('valign', 'top')
    top = H - pad - (0 if valign == 'top' else ((room_h - block_h) / 2 if valign == 'middle' else room_h - block_h))
    c.saveState()
    clip = c.beginPath(); clip.rect(0, 0, W, H); c.clipPath(clip, stroke=0, fill=0)
    _fillc(c, colour); _strokec(c, colour)
    for n, line in enumerate(lines):
        base = top - lead * n - lead * 0.5 - size * 0.34
        pieces = [(t.rstrip() if i == len(line) - 1 else t, f, a) for i, (t, f, a) in enumerate(line)]
        line_w = sum(_width(t, f, size, letter) for t, f, _ in pieces)
        align = el.get('align', 'left')
        gap = 0.0
        if align == 'justify' and n < len(lines) - 1 and len(pieces) > 1:
            gap = (room_w - line_w) / (len(pieces) - 1)
        x = pad + (0 if align in ('left', 'justify') else ((room_w - line_w) / 2 if align == 'center' else room_w - line_w))
        for text, f, a in pieces:
            t = c.beginText()
            t.setTextOrigin(x, base)
            if slant:
                t.setTextTransform(1, 0, 0.2, 1, x, base)
            t.setFont(f, size)
            t.setCharSpace(letter)
            c.setFillAlpha(box.alpha * a)
            t.textOut(text)
            c.drawText(t)
            w = _width(text, f, size, letter)
            if el.get('underline') and text.strip():
                c.setLineWidth(max(0.4, size * 0.06)); c.line(x, base - size * 0.12, x + _width(text.rstrip(), f, size, letter), base - size * 0.12)
            x += w + gap
    c.restoreState()


def _cell(c, box, text, x, y, w, h, size, colour, bold=False, align='left', pad_x=0.0, font_key='dejavu', alpha=1.0, shrink=False):
    """One line of text in a cell given from the element's top-left, in millimetres. Too long, it is made smaller (to
    six tenths of its size) when ``shrink``, then shortened with '…'."""
    font = _font(font_key, bold)[0]
    text = _safe(text, font)
    if not text:
        return
    room = (w - 2 * pad_x) * PT
    width = _width(text, font, size)
    if shrink and width > room:
        # A text's width grows in step with its size, so the size that fits is worked out at once.
        size = max(size * 0.6, size * room / width)
        width = _width(text, font, size)
    if width > room:
        # Cut it short with '…', finding the longest start that fits by halving, not a letter at a time.
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if _width(text[:mid] + '…', font, size) <= room:
                lo = mid
            else:
                hi = mid - 1
        text = (text[:lo].rstrip() + '…') if lo else ''
        if font.startswith(('Helvetica', 'Times', 'Courier')):
            text = text.replace('…', '...')
    base = box.top(y + h / 2) - size * 0.35
    c.setFillColor(colour)
    c.setFillAlpha(box.alpha * alpha)
    c.setFont(font, size)
    left = (x + pad_x) * PT
    if align == 'center':
        c.drawCentredString((x + w / 2) * PT, base, text)
    elif align == 'right':
        c.drawRightString((x + w - pad_x) * PT, base, text)
    else:
        c.drawString(left, base, text)
    if alpha != 1.0:
        c.setFillAlpha(box.alpha)


def _band(c, box, x, y, w, h, colour):
    _fillc(c, colour); c.rect(x * PT, box.top(y + h), w * PT, h * PT, stroke=0, fill=1)


def _block_frame(c, box, el, paint):
    fill = paint(el.get('fill'))
    radius = min((el.get('radius') or 0) * PT, box.W / 2, box.H / 2)
    if fill is not None:
        _shadow(box, el, radius)
        _fillc(c, fill); c.roundRect(0, 0, box.W, box.H, radius, stroke=0, fill=1)
    clip = c.beginPath(); clip.roundRect(0, 0, box.W, box.H, radius)
    c.saveState(); c.clipPath(clip, stroke=0, fill=0)
    return radius


def _block_outline(c, box, el, paint, radius, height_mm=None):
    line = paint(el.get('line'))
    if line is not None and el.get('line_w'):
        lw = el['line_w'] * PT
        h = (height_mm * PT) if height_mm else box.H
        _strokec(c, line); c.setLineWidth(lw)
        c.roundRect(lw / 2, box.H - h + lw / 2, box.W - lw, h - lw, max(radius - lw / 2, 0), stroke=1, fill=0)


# ---------------------------------------------------------------- the blocks
def _results(c, box, el, rep, paint):
    radius = _block_frame(c, box, el, paint)
    rows = rep['subjects']
    cols = el.get('columns') or ['subject', 'total', 'grade']
    weights = sum(WIDTHS.get(k, 10) for k in cols)
    n = max(len(rows), 1)
    w_mm, h_mm = el['w'], el['h']
    row_h = _clamp(h_mm / (n + 1), 3.2, 9)
    size = min(el['size'], row_h * PT * 0.62)
    head_size = min(el.get('head_size') or el['size'], row_h * PT * 0.6)
    pad = min(1.4, el.get('pad', 1.6))
    xs, x = [], 0.0
    for k in cols:
        cw = w_mm * WIDTHS.get(k, 10) / weights
        xs.append((x, cw)); x += cw
    text = paint(el.get('color'), colors.HexColor('#15171a'))
    head_fill = paint(el.get('head_fill'))
    if head_fill is not None:
        _band(c, box, 0, 0, w_mm, row_h, head_fill)
    heads = [HEADS[k] + (f" ({rep['maxes'][k]})" if rep.get('maxes', {}).get(k) else '') for k in cols]
    # Every heading the same size: the largest at which the longest one fits its column (to six tenths of the size).
    head_font = _font(el['font'], True)[0]
    for text_, (_, cw) in zip(heads, xs):
        room = (cw - 2 * pad) * PT
        while _width(_safe(text_, head_font), head_font, head_size) > room and head_size > (el.get('head_size') or el['size']) * 0.6:
            head_size -= 0.25
    for k, head, (cx, cw) in zip(cols, heads, xs):
        _cell(c, box, head, cx, 0, cw, row_h, head_size, paint(el.get('head_color'), colors.white), True,
              'left' if k in ('subject', 'remark') else 'center', pad, el['font'])
    if not rows:
        _cell(c, box, 'No results recorded.', 0, row_h, w_mm, row_h, size, text, False, 'center', 0, el['font'], 0.6)
    zebra = paint(el.get('zebra'))
    for ri, r in enumerate(rows):
        y = row_h * (ri + 1)
        if zebra is not None and ri % 2 == 1:
            _band(c, box, 0, y, w_mm, row_h, zebra)
        for k, (cx, cw) in zip(cols, xs):
            align = 'left' if k in ('subject', 'remark') else ('right' if el.get('numbers') == 'right' else 'center')
            _cell(c, box, r.get(k, ''), cx, y, cw, row_h, size, text, k == 'total', align, pad, el['font'], shrink=k != 'remark')
    line = paint(el.get('line'))
    total_h = row_h * (n + 1)
    if line is not None and el.get('line_w'):
        lw = el['line_w']
        for r in range(1, n + 1):
            _band(c, box, 0, row_h * r - lw / 2, w_mm, lw, line)
        for cx, _ in xs[1:]:
            _band(c, box, cx - lw / 2, 0, lw, total_h, line)
    c.restoreState()
    _block_outline(c, box, el, paint, radius, total_h)


def _summary(c, box, el, rep, paint):
    radius = _block_frame(c, box, el, paint)
    s = rep['summary']
    items = el.get('items') or ['total', 'percentage', 'grade']
    k = len(items)
    text, accent = paint(el.get('color'), colors.HexColor('#15171a')), paint(el.get('accent'), colors.HexColor('#0d2b52'))
    line = paint(el.get('line')) if el.get('line_w') else None
    for i, item in enumerate(items):
        value = s.get(item) or ''
        if el.get('layout') == 'column':
            h = el['h'] / k; y = h * i
            label_h = min(h * 0.38, 5)
            vs = min(el['value_size'], (h - label_h) * PT * 0.7)
            _cell(c, box, LABELS[item], 0, y + h * 0.08, el['w'], label_h, min(el['size'], label_h * PT * 0.75), text, False, 'center', 0, el['font'], 0.85)
            _cell(c, box, value, 0, y + h * 0.08 + label_h, el['w'], h - label_h - h * 0.16, vs, accent, True, 'center', 0, el['font'], shrink=True)
            if line is not None and i:
                _band(c, box, el['w'] * 0.2, y, el['w'] * 0.6, el['line_w'], line)
        else:
            w = el['w'] / k; x = w * i
            lh = min(el['h'] * 0.38, 5)
            vs = min(el['value_size'], (el['h'] - lh - el['h'] * 0.16) * PT * 0.7)
            _cell(c, box, LABELS[item], x, el['h'] * 0.1, w, lh, min(el['size'], lh * PT * 0.75), text, False, 'center', 0.8, el['font'], 0.85)
            _cell(c, box, value, x, el['h'] * 0.1 + lh, w, el['h'] - lh - el['h'] * 0.2, vs, accent, True, 'center', 0.8, el['font'], shrink=True)
            if line is not None and i:
                _band(c, box, x - el['line_w'] / 2, el['h'] * 0.15, el['line_w'], el['h'] * 0.7, line)
    c.restoreState()
    _block_outline(c, box, el, paint, radius)


def _traits(c, box, el, rep, paint):
    groups = rep['traits']
    if not groups:
        return
    radius = _block_frame(c, box, el, paint)
    text, accent = paint(el.get('color'), colors.HexColor('#15171a')), paint(el.get('accent'), colors.HexColor('#0d2b52'))
    head_fill, line = paint(el.get('head_fill')), paint(el.get('line')) if el.get('line_w') else None
    zebra = paint(el.get('zebra'))
    side, n, gutter = el.get('layout') != 'stack', len(groups), 3
    total_rows = sum(len(g['entries']) + 1 for g in groups)
    y_run = 0.0
    for gi, g in enumerate(groups):
        gw = (el['w'] - gutter * (n - 1)) / n if side else el['w']
        gx = gi * (gw + gutter) if side else 0
        gh = el['h'] if side else el['h'] * (len(g['entries']) + 1) / total_rows
        gy = 0 if side else y_run
        row_h = _clamp(gh / (len(g['entries']) + 1), 3, 7)
        size = min(el['size'], row_h * PT * 0.62)
        if head_fill is not None:
            _band(c, box, gx, gy, gw, row_h, head_fill)
        _cell(c, box, g['name'], gx, gy, gw, row_h, size, paint(el.get('head_color'), colors.white) if head_fill is not None else accent,
              True, 'left', 1.2, el['font'])
        for ei, e in enumerate(g['entries']):
            ry = gy + row_h * (ei + 1)
            if zebra is not None and ei % 2 == 1:
                _band(c, box, gx, ry, gw, row_h, zebra)
            _cell(c, box, e['label'], gx, ry, gw * 0.6, row_h, size, text, False, 'left', 1.2, el['font'])
            _cell(c, box, e['rating'], gx + gw * 0.6, ry, gw * 0.4, row_h, size, text, True, 'right', 1.2, el['font'])
            if line is not None:
                _band(c, box, gx, ry - el['line_w'] / 2, gw, el['line_w'], line)
        if line is not None:
            lw = el['line_w'] * PT
            h = row_h * (len(g['entries']) + 1) * PT
            _strokec(c, line); c.setLineWidth(lw)
            c.roundRect(gx * PT + lw / 2, box.top(gy) - h + lw / 2, gw * PT - lw, h - lw, max((el.get('radius') or 0) * PT - lw / 2, 0), stroke=1, fill=0)
        y_run += gh
    c.restoreState()


def _grading(c, box, el, rep, paint):
    radius = _block_frame(c, box, el, paint)
    keys = rep['grading']
    per = max(1, int(el.get('per_row') or 3))
    rows = max(1, math.ceil(len(keys) / per))
    title_h = min(el['h'] * 0.3, 5) if el.get('title') else 0
    row_h = (el['h'] - title_h) / rows
    size = min(el['size'], row_h * PT * 0.62)
    w = el['w'] / per
    text, accent = paint(el.get('color'), colors.HexColor('#15171a')), paint(el.get('accent'), colors.HexColor('#0d2b52'))
    if el.get('title'):
        _cell(c, box, el['title'], 0, 0, el['w'], title_h, min(el['size'] + 1, title_h * PT * 0.7), accent, True, 'left', 1.2, el['font'])
    for i, k in enumerate(keys):
        label = k['grade'] + (': ' + k['range'] if k['range'] else '') + (' (' + k['remark'] + ')' if k['remark'] else '')
        _cell(c, box, label, (i % per) * w, title_h + (i // per) * row_h, w, row_h, size, text, False, 'left', 1.2, el['font'])
    c.restoreState()
    _block_outline(c, box, el, paint, radius)


def _comment(c, box, el, rep, paint):
    radius = _block_frame(c, box, el, paint)
    pad = el.get('pad', 1.6)
    label_h = min(el['size'] * 1.5 / PT, el['h'] * 0.35) if el.get('label') else 0
    accent = paint(el.get('accent'), colors.HexColor('#0d2b52'))
    if el.get('label'):
        _cell(c, box, el['label'], pad, pad * 0.6, el['w'] - pad * 2, label_h, min(el['size'], label_h * PT * 0.75), accent, True, 'left', 0, el['font'])
    top = pad * 0.6 + label_h
    room = el['h'] - top - pad * 0.6
    text = rep['comment']
    if text:
        sub = dict(el, x=0, y=0, w=el['w'], h=el['h'], size=el['comment_size'], pad=0, fill='none', radius=0, line=130,
                   letter=0, valign='top', align='left', weight=400, upper=False, underline=False, italic=False)
        c.saveState()
        c.translate(pad * PT, box.top(top + room))
        inner = _Inner(c, (el['w'] - pad * 2) * PT, room * PT, box.alpha)
        _text(inner, sub, text, '', paint)
        c.restoreState()
    elif el.get('lines'):
        colour = paint(el.get('line')) or colors.HexColor('#c9d2de')
        for i in range(1, 4):
            _band(c, box, pad, top + room * i / 3.2, el['w'] - pad * 2, 0.25, colour)
    c.restoreState()
    _block_outline(c, box, el, paint, radius)


class _Inner:
    """A plain text area inside a block, for _text."""

    def __init__(self, c, W, H, alpha):
        self.c, self.W, self.H, self.alpha = c, W, H, alpha


BLOCKS = {'results': _results, 'summary': _summary, 'traits': _traits, 'grading': _grading, 'comment': _comment}


# ---------------------------------------------------------------- the barcode
def _barcode(box, el, value, paint):
    c, W, H = box.c, box.W, box.H
    bg = paint(el.get('bg'))
    if bg is not None:
        _fillc(c, bg); c.rect(0, 0, W, H, stroke=0, fill=1)
    code = barcode_path(value)
    if not code:
        return
    path, units = code
    bars_h = H * (0.75 if el.get('show_text') else 1)
    _fillc(c, paint(el.get('fg'), colors.HexColor('#15171a')))
    for x, w in re.findall(r'M(\d+) 0h(\d+)', path):
        c.rect(int(x) / units * W, H - bars_h, int(w) / units * W, bars_h, stroke=0, fill=1)
    if el.get('show_text'):
        size = min(H * 0.2, 8)
        c.setFont('Courier-Bold', size)
        c.drawCentredString(W / 2, (H - bars_h) / 2 - size * 0.3, _safe(value, 'Courier'))


# ---------------------------------------------------------------- a page
def draw_page(c, size_mm, layout, side, data, rep, pictures, assets, paint, picture_of, blocks):
    """One page of a designed document: the background, then each element in order. Shared by every studio's PDF (the
    report card here, the receipt in core/receipt_design_pdf.py): ``data`` is {field: text}, ``rep`` what the
    ``blocks`` draw from, and ``picture_of(el)`` gives (stored upload, is a photograph, initials) for a photo, logo or
    signature."""
    width, height = size_mm
    page_h = height * PT
    bg = layout['background']
    c.saveState()
    _fillc(c, paint(bg.get('color'), colors.white)); c.rect(0, 0, width * PT, page_h, stroke=0, fill=1)
    if bg.get('gradient'):
        clip = c.beginPath(); clip.rect(0, 0, width * PT, page_h); c.saveState(); c.clipPath(clip, stroke=0, fill=0)
        _gradient_fill(c, bg['gradient'], paint, width * PT, page_h); c.restoreState()
    if bg.get('image') and assets.get(bg['image']):
        loaded = pictures.load(assets[bg['image']], max_px=1600)
        if loaded:
            img, iw, ih = loaded
            W, H = width * PT, page_h
            if bg.get('fit') == 'fill':
                dw, dh = W, H
            else:
                s = (max if bg.get('fit', 'cover') == 'cover' else min)(W / iw, H / ih)
                dw, dh = iw * s, ih * s
            c.setFillAlpha((bg.get('opacity') or 100) / 100)
            try:
                c.drawImage(_reader(pictures, assets[bg['image']], img), (W - dw) / 2, (H - dh) / 2, dw, dh, mask='auto')
            except Exception:
                pass
    c.restoreState()
    for el in layout['sides'][side]:
        if el.get('hidden'):
            continue
        box = _Box(c, page_h, el)
        try:
            t = el['type']
            if t in blocks:
                blocks[t](c, box, el, rep, paint)
            elif t == 'shape':
                _shape(box, el, paint)
            elif t in ('text', 'field'):
                value = el['text'] if t == 'text' else data.get(el['field'], '')
                _text(box, el, value, el.get('label', '') if t == 'field' else '', paint)
            elif t == 'image':
                _picture(box, el, assets.get(el.get('asset')), pictures, paint)
            elif t in ('photo', 'logo', 'signature'):
                stored, photo, initials = picture_of(el)
                _picture(box, el, stored, pictures, paint, photo=photo, initials=initials)
            elif t == 'barcode':
                _barcode(box, el, data.get(el.get('field'), ''), paint)
        finally:
            box.done()


def _page(c, layout, side, card, view, pictures, assets, paint):
    school, student = card.get('school') or {}, card.get('student') or {}
    initials = ''.join(p[:1] for p in (student.get('name') or '').split()[:2]).upper()

    def picture_of(el):
        if el['type'] == 'photo':
            return student.get('photo_path'), True, initials
        if el['type'] == 'logo':
            return school.get('logo_path'), False, ''
        if el.get('who') == 'head':
            return (card.get('head') or {}).get('signature_path'), False, ''
        return (card.get('comment') or {}).get('teacher_signature_path'), False, ''

    draw_page(c, PAGE_MM[layout['orientation']], layout, side, view['data'], view['report'], pictures, assets, paint,
              picture_of, BLOCKS)


def _has_content(layout, side):
    return any(not el.get('hidden') for el in layout['sides'].get(side, []))


def render_designed_pdf(cards, layout, assets=None):
    """PDF bytes: each card drawn with the school's own design (one or two A4 pages each)."""
    assets = assets or {}
    width, height = PAGE_MM[layout['orientation']]
    school = (cards[0].get('school') if cards else {}) or {}
    buffer = io.BytesIO()
    # Compressed pages, written as they are (core/pdf_setup.py): the smallest file to send.
    c = rl_canvas.Canvas(buffer, pagesize=(width * PT, height * PT), pageCompression=1)
    c.setTitle(((school.get('name') or '') + ' - ' if school.get('name') else '') + 'Report Cards')
    c.setAuthor(school.get('name') or '')
    c.setCreator('Brightstars Academics')
    pictures = _Pictures()
    if not cards:
        c.setFont(_fonts()[0], 14); c.drawCentredString(width * PT / 2, height * PT / 2, 'No report cards.'); c.showPage()
    for card in cards:
        paint = _Paint(card.get('school') or {})
        view = view_data(card)
        for side in SIDES:
            if side != 'page1' and not _has_content(layout, side):
                continue
            _page(c, layout, side, card, view, pictures, assets, paint)
            c.showPage()
    c.save()
    return buffer.getvalue()
