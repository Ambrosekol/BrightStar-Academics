"""Draws a school's report cards as a PDF: one A4 portrait card per student.

This module is deliberately self-contained. It imports only the standard library, reportlab and
Pillow; it knows nothing about Flask, the database or any particular school. Everything that appears
on a card (name, logo, colours, grading key, signatures) comes in through the ``cards`` list.

    pdf_bytes = render_report_cards_pdf(cards)

Each item of ``cards`` is a dictionary (see the docstring of ``render_report_cards_pdf``). A card that
is too long for one page simply carries on onto a second page; nothing is cut off.
"""
import io
import math
import os
import re

from PIL import Image, ImageOps
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, HRFlowable, KeepTogether,
                                NextPageTemplate, PageBreak, PageTemplate, Spacer, Table, TableStyle)
from reportlab.platypus import Image as RLImage

# ---------------------------------------------------------------------------------------------
# Page geometry (all in points; mm is reportlab's millimetre constant)
# ---------------------------------------------------------------------------------------------
PAGE_W, PAGE_H = A4
MARGIN_X = 14 * mm                       # left and right margin
CONTENT_W = PAGE_W - 2 * MARGIN_X        # width of everything on the page
TOP_MARGIN_FIRST = 12 * mm               # first page of a card: room for the coloured band
TOP_MARGIN_LATER = 19 * mm               # continuation pages: room for the small running header
BOTTOM_MARGIN = 17 * mm                  # room for the footer
BAND_H = 6 * mm                          # height of the coloured band across the top of page one
STRIPE_H = 1.2 * mm                      # thin accent stripe just under the band

NEUTRAL_PRIMARY = '#0d2b52'              # used only when the school has not chosen a colour

# ---------------------------------------------------------------------------------------------
# Fonts. DejaVu (shipped with the app in static/fonts) covers accents and the Naira sign. If the
# files are missing we fall back to Helvetica and swap characters it cannot draw for '?'.
# ---------------------------------------------------------------------------------------------
_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'fonts')
_FONTS = {}


def _fonts():
    """(regular font name, bold font name, can draw any character?) - registered once."""
    if _FONTS:
        return _FONTS['regular'], _FONTS['bold'], _FONTS['unicode']
    regular, bold, unicode_ok = 'Helvetica', 'Helvetica-Bold', False
    try:
        pdfmetrics.registerFont(TTFont('RCBody', os.path.join(_FONT_DIR, 'DejaVuSans.ttf')))
        pdfmetrics.registerFont(TTFont('RCBold', os.path.join(_FONT_DIR, 'DejaVuSans-Bold.ttf')))
        pdfmetrics.registerFontFamily('RCBody', normal='RCBody', bold='RCBold',
                                      italic='RCBody', boldItalic='RCBold')
        regular, bold, unicode_ok = 'RCBody', 'RCBold', True
    except Exception:
        pass
    _FONTS.update(regular=regular, bold=bold, unicode=unicode_ok)
    return regular, bold, unicode_ok


# ---------------------------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------------------------
_CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def _clean(value):
    """Any value as safe plain text: '' for None, no control characters, newlines kept as \\n."""
    if value is None:
        return ''
    text = value if isinstance(value, str) else str(value)
    text = _CONTROL_CHARS.sub('', text.replace('\r\n', '\n').replace('\r', '\n'))
    if not _fonts()[2]:  # Helvetica cannot draw everything; replace what it cannot
        text = text.encode('cp1252', 'replace').decode('cp1252')
    return text


def _clip(text, limit):
    """Shorten text to at most ``limit`` characters, ending with an ellipsis if it was cut."""
    if limit and len(text) > limit:
        return text[:max(limit - 1, 0)].rstrip() + '…' if _fonts()[2] else text[:limit - 3].rstrip() + '...'
    return text


def _line(value, limit=None):
    """One tidy line of text: whitespace collapsed, optionally shortened."""
    return _clip(' '.join(_clean(value).split()), limit)


def _wrap_lines(text, font, size, width):
    """Break ``text`` into lines no wider than ``width`` points (blank lines in the text are kept).

    Words are moved to the next line when they do not fit; a single word that is wider than a whole
    line is cut into pieces, so nothing can ever run out of its box.
    """
    width = max(width, size)
    lines = []
    for paragraph in text.split('\n'):
        current = ''
        for word in paragraph.split():
            while stringWidth(word, font, size) > width:
                cut = 1
                while cut < len(word) and stringWidth(word[:cut + 1], font, size) <= width:
                    cut += 1
                if current:
                    lines.append(current)
                    current = ''
                lines.append(word[:cut])
                word = word[cut:]
            candidate = word if not current else current + ' ' + word
            if stringWidth(candidate, font, size) <= width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


class _Text(Flowable):
    """Plain text in a box: wraps to the width it is given, may sit on a tinted, bordered background.

    We draw the text ourselves, one line at a time, instead of using reportlab's Paragraph. A Paragraph
    reads its text as markup, so '<b>&amp;</b>' or 'Tolu & Sons <Ltd>' would need escaping and comes out of
    the PDF in separate pieces. Here the text is never interpreted: what is given is what is printed, and
    every line is a single piece of extractable text. A long boxed text can be split across two pages.
    """

    def __init__(self, text, style, lines=None):
        super().__init__()
        self.text, self.style = text, style
        self._lines = lines            # set when this is one half of a split text
        self._fixed = lines is not None
        self.spaceBefore, self.spaceAfter = style.spaceBefore, style.spaceAfter
        self.width = self.height = 0

    def _pad(self):
        return self.style.borderPadding if (self.style.backColor or self.style.borderWidth) else 0

    def _room(self, avail_w):
        """Width available to the words themselves."""
        return avail_w - self.style.leftIndent - self.style.rightIndent - 2 * self._pad()

    def _lines_for(self, avail_w):
        if not self._fixed:
            self._lines = _wrap_lines(self.text, self.style.fontName, self.style.fontSize, self._room(avail_w))
        return self._lines

    def wrap(self, avail_w, avail_h):
        self.width = avail_w
        self.height = len(self._lines_for(avail_w)) * self.style.leading + 2 * self._pad()
        return self.width, self.height

    def split(self, avail_w, avail_h):
        """Offer to break a long text between two lines so it can continue on the next page."""
        lines = self._lines_for(avail_w)
        room = int((avail_h - 2 * self._pad()) // self.style.leading)
        if len(lines) < 4 or room < 2:
            return []                                  # too short to split (or no room): move it whole
        room = min(room, len(lines) - 2)               # never leave a single line on the next page
        return [_Text(self.text, self.style, lines[:room]), _Text(self.text, self.style, lines[room:])]

    def draw(self):
        style, canv, pad = self.style, self.canv, self._pad()
        canv.saveState()
        if style.backColor:
            canv.setFillColor(style.backColor)
            canv.rect(0, 0, self.width, self.height, fill=1, stroke=0)
        if style.borderWidth:
            canv.setStrokeColor(style.borderColor)
            canv.setLineWidth(style.borderWidth)
            canv.rect(0, 0, self.width, self.height, fill=0, stroke=1)
        canv.setFillColor(style.textColor)
        canv.setFont(style.fontName, style.fontSize)
        left = pad + style.leftIndent
        room = self._room(self.width)
        # The first baseline sits so that a line's text is centred in its 'leading' high row.
        baseline = self.height - pad - (style.leading - 1.16 * style.fontSize) / 2 - 0.93 * style.fontSize
        for line in self._lines:
            if style.alignment == TA_CENTER:
                canv.drawCentredString(left + room / 2, baseline, line)
            elif style.alignment == TA_RIGHT:
                canv.drawRightString(left + room, baseline, line)
            else:
                canv.drawString(left, baseline, line)
            baseline -= style.leading
        canv.restoreState()


def _to_float(value):
    """A number, or None when the value is missing or not a usable number."""
    if value is None or isinstance(value, bool) or value == '':
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(number) or math.isinf(number)) else number


def _num(value):
    """Score formatting: '85' not '85.0', at most two decimals, '-' when there is no value."""
    number = _to_float(value)
    if number is None:
        return '-'
    text = ('%.2f' % number).rstrip('0').rstrip('.')
    return '0' if text in ('', '-0') else text


def _percent(value):
    """'61.4%' (one decimal), or '-' when there is no value."""
    number = _to_float(value)
    return '-' if number is None else '%.1f%%' % number


def _get(mapping, key):
    """mapping[key] when mapping is a dict, otherwise None. Never raises."""
    return mapping.get(key) if isinstance(mapping, dict) else None


def _dict(mapping, key):
    """A nested dictionary, or {} when the key is missing/None/not a dictionary."""
    value = _get(mapping, key)
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------------------------
# Colours. The school's own colours are used when given; otherwise a neutral dark blue.
# ---------------------------------------------------------------------------------------------
def _colour(value, default):
    """A reportlab colour from '#rrggbb' (or '#rgb'); ``default`` (a hex string) when unusable."""
    text = str(value or '').strip().lstrip('#')
    if re.fullmatch(r'[0-9a-fA-F]{3}', text):
        text = ''.join(ch * 2 for ch in text)
    if not re.fullmatch(r'[0-9a-fA-F]{6}', text):
        return colors.HexColor(default) if default else None
    return colors.HexColor('#' + text)


def _mix(colour, towards, amount):
    """``colour`` blended ``amount`` (0..1) of the way towards ``towards`` (another colour)."""
    return colors.Color(colour.red + (towards.red - colour.red) * amount,
                        colour.green + (towards.green - colour.green) * amount,
                        colour.blue + (towards.blue - colour.blue) * amount)


def _luminance(colour):
    """Rough brightness 0 (black) .. 1 (white)."""
    return 0.2126 * colour.red + 0.7152 * colour.green + 0.0722 * colour.blue


class _Theme:
    """All the colours of one card, worked out from the school's primary and accent colours."""

    def __init__(self, school):
        self.primary = _colour(_get(school, 'primary'), NEUTRAL_PRIMARY)
        self.accent = _colour(_get(school, 'accent'), None) or _mix(self.primary, colors.white, 0.4)
        # Text laid on the primary colour: white on dark colours, near-black on very light ones.
        self.on_primary = colors.white if _luminance(self.primary) < 0.6 else colors.HexColor('#111111')
        # Headings are printed in the primary colour, darkened if the colour is too pale to read.
        self.ink = self.primary if _luminance(self.primary) < 0.5 else _mix(self.primary, colors.black, 0.55)
        self.tint = _mix(self.primary, colors.white, 0.93)      # pale wash for boxes and stripes
        self.line = _mix(self.primary, colors.white, 0.72)      # thin grid lines
        self.text = colors.HexColor('#1a1a1a')
        self.muted = colors.HexColor('#555b66')


# ---------------------------------------------------------------------------------------------
# Pictures. Every picture is opened, fully decoded and re-saved in memory first, so a missing,
# corrupt or oddly formatted file is discovered here (and skipped) rather than breaking the PDF.
# ---------------------------------------------------------------------------------------------
class _Pictures:
    """Loads pictures once per PDF and remembers them (the same logo is on every card)."""

    def __init__(self):
        self._cache = {}

    def load(self, path, max_px=600, photo=False):
        """(png-or-jpeg bytes, width, height) for a readable picture file, else None."""
        if not path or not isinstance(path, (str, os.PathLike)):
            return None
        key = (os.fspath(path), max_px, photo)
        if key not in self._cache:
            self._cache[key] = self._read(os.fspath(path), max_px, photo)
        return self._cache[key]

    @staticmethod
    def _read(path, max_px, photo):
        try:
            if not os.path.isfile(path):
                return None
            with Image.open(path) as opened:
                opened.load()
                picture = opened
                try:
                    picture = ImageOps.exif_transpose(opened)  # honour a phone's rotation flag
                except Exception:
                    picture = opened
                see_through = picture.mode in ('RGBA', 'LA', 'PA') or (
                    picture.mode == 'P' and 'transparency' in picture.info)
                picture = picture.convert('RGBA' if see_through else 'RGB')
                if see_through and picture.getchannel('A').getextrema()[0] == 255:
                    picture = picture.convert('RGB')  # fully opaque: the alpha channel is not needed
                    see_through = False
                if max(picture.size) > max_px:
                    picture.thumbnail((max_px, max_px), Image.LANCZOS)
                width, height = picture.size
                if width < 1 or height < 1:
                    return None
                out = io.BytesIO()
                if photo and not see_through:
                    picture.save(out, 'JPEG', quality=88)   # photographs: small files
                else:
                    picture.save(out, 'PNG')                # logos and signatures: exact pixels
                return out.getvalue(), width, height
        except Exception:
            return None

    def flowable(self, path, max_w, max_h, photo=False, max_px=600):
        """A reportlab Image scaled to fit inside max_w x max_h (keeping its shape), or None."""
        loaded = self.load(path, max_px=max_px, photo=photo)
        if not loaded:
            return None
        data, width, height = loaded
        scale = min(max_w / width, max_h / height)
        try:
            return RLImage(io.BytesIO(data), width=width * scale, height=height * scale, mask='auto')
        except Exception:
            return None


# ---------------------------------------------------------------------------------------------
# Page furniture that is not part of the flowing text
# ---------------------------------------------------------------------------------------------
class _State:
    """Which card is being drawn right now (the page decorations need to know)."""

    def __init__(self):
        self.card = None      # the card dictionary
        self.theme = None     # its _Theme
        self.number = -1      # its position in the list


class _StartCard(Flowable):
    """An invisible marker at the start of each card: tells the page decorations which card is current."""

    def __init__(self, state, number, card, theme):
        super().__init__()
        self.state, self.number, self.card, self.theme = state, number, card, theme

    def wrap(self, avail_w, avail_h):
        return 0, 0

    def draw(self):
        self.state.card, self.state.theme, self.state.number = self.card, self.theme, self.number


class _TopBand(Flowable):
    """The full-width band in the school's colour across the very top of a card's first page."""

    def __init__(self, theme):
        super().__init__()
        self.theme = theme

    def wrap(self, avail_w, avail_h):
        return avail_w, 0

    def draw(self):
        canv = self.canv
        top = TOP_MARGIN_FIRST                     # the flowable sits at the frame top, page top is this far above
        canv.saveState()
        canv.setFillColor(self.theme.primary)
        canv.rect(-MARGIN_X, top - BAND_H, PAGE_W, BAND_H, fill=1, stroke=0)
        canv.setFillColor(self.theme.accent)
        canv.rect(-MARGIN_X, top - BAND_H - STRIPE_H, PAGE_W, STRIPE_H, fill=1, stroke=0)
        canv.restoreState()


def _fit(text, font, size, width):
    """Text shortened with an ellipsis so that it is no wider than ``width`` points."""
    if stringWidth(text, font, size) <= width:
        return text
    while text and stringWidth(text + '…', font, size) > width:
        text = text[:-1]
    return text.rstrip() + '…' if _fonts()[2] else text.rstrip() + '...'


def _running_header(state):
    """Draws the slim header on the second and later pages of a card (school, student)."""
    def draw(canv, doc):
        card, theme = state.card, state.theme
        if card is None or theme is None:
            return
        regular, bold, _ = _fonts()
        school, student = _dict(card, 'school'), _dict(card, 'student')
        canv.saveState()
        canv.setFillColor(theme.primary)
        canv.rect(0, PAGE_H - 11 * mm, PAGE_W, 11 * mm, fill=1, stroke=0)
        canv.setFillColor(theme.accent)
        canv.rect(0, PAGE_H - 11 * mm - STRIPE_H, PAGE_W, STRIPE_H, fill=1, stroke=0)
        canv.setFillColor(theme.on_primary)
        room = CONTENT_W / 2 - 4 * mm
        canv.setFont(bold, 10)
        canv.drawString(MARGIN_X, PAGE_H - 7.3 * mm, _fit(_line(school.get('name')), bold, 10, room))
        who = ' - '.join(part for part in (_line(student.get('name')), _line(student.get('class_name'))) if part)
        canv.setFont(regular, 9)
        canv.drawRightString(PAGE_W - MARGIN_X, PAGE_H - 7.3 * mm, _fit(who, regular, 9, room))
        canv.restoreState()
    return draw


class _NumberedCanvas(rl_canvas.Canvas):
    """A canvas that adds the footer ('Issued', 'Page x of y') after the whole card is laid out,
    because 'y' (the card's page count) is only known then. Pages are counted per card."""

    def __init__(self, *args, **kwargs):
        self._state = kwargs.pop('state')
        super().__init__(*args, **kwargs)
        self._saved_pages = []

    def showPage(self):
        # Remember the page (and which card it belongs to) instead of writing it out straight away.
        self._saved_pages.append((dict(self.__dict__), self._state.card, self._state.theme, self._state.number))
        self._startPage()

    def save(self):
        totals = {}
        for _, _, _, number in self._saved_pages:
            totals[number] = totals.get(number, 0) + 1
        seen = {}
        for page_state, card, theme, number in self._saved_pages:
            self.__dict__.update(page_state)
            seen[number] = seen.get(number, 0) + 1
            if card is not None and theme is not None:
                self._footer(card, theme, seen[number], totals[number])
            super().showPage()
        super().save()

    def _footer(self, card, theme, page_no, page_count):
        regular = _fonts()[0]
        y = 9.5 * mm
        self.saveState()
        self.setStrokeColor(theme.line)
        self.setLineWidth(0.6)
        self.line(MARGIN_X, y + 4.2 * mm, PAGE_W - MARGIN_X, y + 4.2 * mm)
        self.setFillColor(theme.muted)
        self.setFont(regular, 7.5)
        issued = _line(_get(card, 'issued_on'), 60)
        if issued:
            self.drawString(MARGIN_X, y, 'Issued: ' + issued)
        self.drawRightString(PAGE_W - MARGIN_X, y, 'Page %d of %d' % (page_no, page_count))
        self.restoreState()


# ---------------------------------------------------------------------------------------------
# Text styles for one card (a ParagraphStyle is just a convenient bag of font, size, colour, ...)
# ---------------------------------------------------------------------------------------------
def _styles(theme):
    regular, bold, _ = _fonts()

    def make(name, **overrides):
        settings = dict(fontName=regular, fontSize=8.5, leading=11, textColor=theme.text)
        settings.update(overrides)
        return ParagraphStyle(name, **settings)

    on = theme.on_primary
    return {
        'body': make('body'),
        'school': make('school', fontName=bold, fontSize=19, leading=22, textColor=theme.ink),
        'motto': make('motto', fontSize=9.5, leading=12, textColor=theme.muted),
        'contact': make('contact', fontSize=8.3, leading=10.8, textColor=theme.muted),
        'title': make('title', fontName=bold, fontSize=13, leading=16, textColor=on, alignment=TA_CENTER),
        'term': make('term', fontSize=9.5, leading=12, alignment=TA_CENTER, textColor=theme.ink),
        'label': make('label', fontSize=7.3, leading=9, textColor=theme.muted),
        'value': make('value', fontName=bold, fontSize=9.5, leading=12),
        'name': make('name', fontName=bold, fontSize=11.5, leading=14),
        'head': make('head', fontName=bold, fontSize=9.5, leading=12, textColor=theme.ink, spaceBefore=1, spaceAfter=3),
        'th': make('th', fontName=bold, fontSize=7, leading=8.4, textColor=on, alignment=TA_CENTER),
        'th_left': make('th_left', fontName=bold, fontSize=7.4, leading=9, textColor=on, alignment=TA_LEFT),
        'td': make('td', fontSize=8, leading=9.6),
        'td_r': make('td_r', fontSize=8, leading=9.6, alignment=TA_RIGHT),
        'td_rb': make('td_rb', fontName=bold, fontSize=8, leading=9.6, alignment=TA_RIGHT),
        'td_c': make('td_c', fontName=bold, fontSize=8, leading=9.6, alignment=TA_CENTER),
        'td_note': make('td_note', fontSize=8.5, leading=11, alignment=TA_CENTER, textColor=theme.muted),
        'stat': make('stat', fontSize=7.3, leading=9.5, textColor=theme.muted, spaceAfter=1.5),
        'stat_value': make('stat_value', fontName=bold, fontSize=10.5, leading=13),
        'comment': make('comment', fontSize=9, leading=12.5, spaceBefore=2, spaceAfter=2,
                        backColor=theme.tint, borderColor=theme.line, borderWidth=0.6, borderPadding=7),
        'sig': make('sig', fontName=bold, fontSize=8.8, leading=11, alignment=TA_CENTER),
        'sig_sub': make('sig_sub', fontSize=7.8, leading=10, alignment=TA_CENTER, textColor=theme.muted),
        'key': make('key', fontSize=7.6, leading=9.6),
        'next': make('next', fontName=bold, fontSize=9, leading=12, textColor=theme.ink),
        'empty': make('empty', fontSize=13, leading=18, alignment=TA_CENTER, textColor=theme.muted),
    }


def _p(text, style):
    """Plain text as a flowable. Nothing in it is read as markup, so it prints exactly as given."""
    return _Text(text, style)


# ---------------------------------------------------------------------------------------------
# The parts of a card, top to bottom
# ---------------------------------------------------------------------------------------------
def _header(card, theme, styles, pictures):
    """School logo (or name in its place), name, motto, address and contact lines, then a rule."""
    school = _dict(card, 'school')
    name = _line(school.get('name'), 120)
    motto = _line(school.get('motto'), 200)
    address = _clean(school.get('address')).strip()
    address = _clip('\n'.join(' '.join(part.split()) for part in address.split('\n') if part.strip()), 300)
    contact = '  |  '.join(part for part in (
        ('Tel: ' + _line(school.get('phone'), 60)) if _line(school.get('phone')) else '',
        ('Email: ' + _line(school.get('email'), 90)) if _line(school.get('email')) else '') if part)

    stack = []
    if name:
        stack.append(_p(name, styles['school']))
    if motto:
        stack.append(_p(motto, styles['motto']))
    if address:
        stack.append(_p(address, styles['contact']))
    if contact:
        stack.append(_p(contact, styles['contact']))

    logo = pictures.flowable(school.get('logo_path'), 26 * mm, 26 * mm)
    story = []
    if logo is not None and stack:
        table = Table([[logo, stack]], colWidths=[31 * mm, CONTENT_W - 31 * mm])
        table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                   ('RIGHTPADDING', (0, 0), (-1, -1), 0), ('TOPPADDING', (0, 0), (-1, -1), 0),
                                   ('BOTTOMPADDING', (0, 0), (-1, -1), 0), ('ALIGN', (0, 0), (0, 0), 'LEFT')]))
        story.append(table)
    elif logo is not None:
        story.append(logo)
        logo.hAlign = 'LEFT'
    else:
        # No logo: the school's name stands where the logo would have been.
        story.extend(stack)
    if story:
        story.append(Spacer(1, 2.5 * mm))
        story.append(HRFlowable(width='100%', thickness=1.6, color=theme.primary, spaceBefore=0, spaceAfter=3 * mm))
    return story


def _title_block(card, theme, styles):
    """The coloured title bar and the 'Term | Session' line under it."""
    title = _line(_get(card, 'title'), 100) or 'Report Card'
    bar = Table([[_p(title, styles['title'])]], colWidths=[CONTENT_W])
    bar.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), theme.primary), ('TOPPADDING', (0, 0), (-1, -1), 5),
                             ('BOTTOMPADDING', (0, 0), (-1, -1), 5)]))
    term, session = _line(_get(card, 'term'), 60), _line(_get(card, 'session'), 40)
    parts = []
    if term:
        parts.append('Term: ' + term)
    if session:
        parts.append('Session: ' + session)
    story = [bar]
    if parts:
        story += [Spacer(1, 1.8 * mm), _p('   |   '.join(parts), styles['term'])]
    story.append(Spacer(1, 3 * mm))
    return story


def _student_block(card, theme, styles, pictures):
    """Name, admission number, class, gender (blanks skipped) with the photograph on the right."""
    student = _dict(card, 'student')
    name = _line(student.get('name'), 120)
    pairs = [(label, _line(student.get(key), 60)) for label, key in
             (('ADMISSION NO.', 'admission_no'), ('CLASS', 'class_name'), ('GENDER', 'gender'))]
    pairs = [(label, value) for label, value in pairs if value]
    photo = pictures.flowable(student.get('photo_path'), 28 * mm, 34 * mm, photo=True, max_px=500)

    photo_col = 34 * mm if photo is not None else 0
    outer_pad = 7
    info_w = CONTENT_W - photo_col - 2 * outer_pad
    label_w = 26 * mm
    value_w = (info_w - 2 * label_w) / 2
    rows, spans = [], []
    if name:
        rows.append([_p('NAME', styles['label']), _p(name, styles['name']), '', ''])
        spans.append(('SPAN', (1, len(rows) - 1), (3, len(rows) - 1)))
    for i in range(0, len(pairs), 2):
        chunk = pairs[i:i + 2]
        row = []
        for label, value in chunk:
            row += [_p(label, styles['label']), _p(value, styles['value'])]
        row += [''] * (4 - len(row))
        rows.append(row)
    if not rows and photo is None:
        return []
    if not rows:
        rows = [['', '', '', '']]
    info = Table(rows, colWidths=[label_w, value_w, label_w, value_w])
    info.setStyle(TableStyle(spans + [('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                      ('RIGHTPADDING', (0, 0), (-1, -1), 4), ('TOPPADDING', (0, 0), (-1, -1), 2.5),
                                      ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5)]))
    if photo is not None:
        outer = Table([[info, photo]], colWidths=[CONTENT_W - photo_col, photo_col])
        outer_style = [('ALIGN', (1, 0), (1, 0), 'RIGHT'), ('VALIGN', (1, 0), (1, 0), 'TOP')]
    else:
        outer = Table([[info]], colWidths=[CONTENT_W])
        outer_style = []
    outer.setStyle(TableStyle(outer_style + [
        ('BACKGROUND', (0, 0), (-1, -1), theme.tint), ('BOX', (0, 0), (-1, -1), 0.6, theme.line),
        ('VALIGN', (0, 0), (0, 0), 'MIDDLE'), ('LEFTPADDING', (0, 0), (-1, -1), outer_pad),
        ('RIGHTPADDING', (0, 0), (-1, -1), outer_pad), ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6)]))
    return [outer, Spacer(1, 4 * mm)]


def _common_max(subjects, key):
    """The maximum score all subjects share (as text), or None when they differ or none is given."""
    seen = {_num(s.get(key)) for s in subjects if _to_float(s.get(key)) is not None}
    return seen.pop() if len(seen) == 1 else None


def _results_table(card, theme, styles):
    """The results table: one row per subject, header repeated if it runs onto another page."""
    subjects = [s for s in (_get(card, 'subjects') or []) if isinstance(s, dict)] \
        if isinstance(_get(card, 'subjects'), (list, tuple)) else []
    has_average = any(_to_float(s.get('class_average')) is not None for s in subjects)

    shared = {key: _common_max(subjects, key) for key in ('ca_max', 'exam_max', 'total_max')}

    def head(label, key=None):
        parts = [_p(label, styles['th'])]
        if key and shared.get(key):
            parts.append(_p('/' + shared[key], styles['th']))     # e.g. CA over /40
        return parts

    def scored(subject, value_key, max_key):
        """A score; with its own maximum ('12/40') only when subjects have different maximums."""
        text = _num(subject.get(value_key))
        if not shared.get(max_key) and _to_float(subject.get(max_key)) is not None:
            text += '/' + _num(subject.get(max_key))
        return _p(text, styles['td_r'])

    # (heading, width in mm before scaling) for each column.
    columns = [('subject', 34), ('test', 12), ('assignment', 19), ('project', 13), ('ca', 13), ('exam', 13),
               ('total', 14)] + ([('average', 14)] if has_average else []) + [('grade', 12), ('remark', 41)]
    scale = CONTENT_W / (sum(width for _, width in columns) * mm)
    widths = [width * mm * scale for _, width in columns]

    heads = {'subject': _p('Subject', styles['th_left']), 'test': head('Test'), 'assignment': head('Assignment'),
             'project': head('Project'), 'ca': head('CA', 'ca_max'), 'exam': head('Exam', 'exam_max'),
             'total': head('Total', 'total_max'), 'average': head('Class avg'), 'grade': head('Grade'),
             'remark': _p('Remark', styles['th_left'])}
    rows = [[heads[key] for key, _ in columns]]
    for subject in subjects:
        cells = {'subject': _p(_line(subject.get('name'), 120) or '-', styles['td']),
                 'test': _p(_num(subject.get('test')), styles['td_r']),
                 'assignment': _p(_num(subject.get('assignment')), styles['td_r']),
                 'project': _p(_num(subject.get('project')), styles['td_r']),
                 'ca': scored(subject, 'ca', 'ca_max'), 'exam': scored(subject, 'exam', 'exam_max'),
                 'total': _p(_num(subject.get('total')) + ('' if shared.get('total_max') or
                                                           _to_float(subject.get('total_max')) is None
                                                           else '/' + _num(subject.get('total_max'))), styles['td_rb']),
                 'average': _p(_num(subject.get('class_average')), styles['td_r']),
                 'grade': _p(_line(subject.get('grade'), 12) or '-', styles['td_c']),
                 'remark': _p(_line(subject.get('remark'), 160), styles['td'])}
        rows.append([cells[key] for key, _ in columns])

    style = [('BACKGROUND', (0, 0), (-1, 0), theme.primary), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
             ('GRID', (0, 0), (-1, -1), 0.4, theme.line), ('BOX', (0, 0), (-1, -1), 0.8, theme.primary),
             ('LEFTPADDING', (0, 0), (-1, -1), 3), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
             ('TOPPADDING', (0, 0), (-1, -1), 2.6), ('BOTTOMPADDING', (0, 0), (-1, -1), 2.6),
             ('TOPPADDING', (0, 0), (-1, 0), 4), ('BOTTOMPADDING', (0, 0), (-1, 0), 4)]
    if subjects:
        style.append(('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, theme.tint]))
    else:
        rows.append([_p('No results recorded.', styles['td_note'])] + [''] * (len(columns) - 1))
        style += [('SPAN', (0, 1), (-1, 1)), ('TOPPADDING', (0, 1), (-1, 1), 9), ('BOTTOMPADDING', (0, 1), (-1, 1), 9)]
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle(style))
    return [table, Spacer(1, 4 * mm)], bool(subjects)


def _summary_strip(card, theme, styles):
    """Total, percentage, overall grade, class average and subject count in one strip."""
    summary = _dict(card, 'summary')

    def cell(label, value):
        # A small label with the figure under it (two paragraphs in one cell, so they stay together).
        return [_p(label, styles['stat']), _p(value, styles['stat_value'])]

    total, total_max = _to_float(summary.get('total')), _to_float(summary.get('total_max'))
    obtained = _num(total) if total_max is None else '%s / %s' % (_num(total), _num(total_max))
    grade = ' - '.join(part for part in (_line(summary.get('grade'), 20), _line(summary.get('remark'), 60)) if part) or '-'
    average = _to_float(summary.get('class_average_percentage'))
    size = summary.get('class_size')
    size = int(size) if _to_float(size) is not None and _to_float(size) > 0 else 0
    count = _to_float(summary.get('subjects_count'))

    cells = [('Total obtained / obtainable', obtained, 42), ('Overall percentage', _percent(summary.get('percentage')), 29),
             ('Overall grade', grade, 40)]
    if average is not None:
        value = _percent(average) + (' (%d student%s)' % (size, '' if size == 1 else 's') if size else '')
        cells.append(('Class average', value, 44))
    if count is not None:
        cells.append(('Subjects', str(int(count)), 17))
    scale = CONTENT_W / (sum(width for _, _, width in cells) * mm)
    table = Table([[cell(label, value) for label, value, _ in cells]],
                  colWidths=[width * mm * scale for _, _, width in cells])
    table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), theme.tint), ('BOX', (0, 0), (-1, -1), 0.8, theme.primary),
                               ('LINEAFTER', (0, 0), (-2, -1), 0.5, theme.line), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                               ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                               ('LEFTPADDING', (0, 0), (-1, -1), 6), ('RIGHTPADDING', (0, 0), (-1, -1), 4)]))
    return [table, Spacer(1, 4 * mm)]


def _signature_block(pictures, styles, image_path, name, subtitle):
    """A signature (picture if readable) standing on a line, with the name and role under the line."""
    image = pictures.flowable(image_path, 46 * mm, 13.5 * mm, max_px=500)
    rows = [[image if image is not None else '']]
    if name:
        rows.append([_p(name, styles['sig'])])
        if subtitle:
            rows.append([_p(subtitle, styles['sig_sub'])])
    elif subtitle:
        rows.append([_p(subtitle, styles['sig'])])   # no name known: the role stands under the line
    else:
        rows.append([''])
    table = Table(rows, colWidths=[72 * mm], rowHeights=[15 * mm] + [None] * (len(rows) - 1))
    table.setStyle(TableStyle([('LINEABOVE', (0, 1), (0, 1), 0.9, colors.HexColor('#333333')),
                               ('VALIGN', (0, 0), (0, 0), 'BOTTOM'), ('ALIGN', (0, 0), (0, 0), 'CENTER'),
                               ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                               ('TOPPADDING', (0, 0), (-1, -1), 1), ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
                               ('TOPPADDING', (0, 1), (0, 1), 3)]))
    return table


def _traits_block(card, theme, styles):
    """The affective/psychomotor rating tables, side by side. Omitted entirely when the card has
    none (``card['traits']`` is falsy) — a school that never rates traits sees no change at all."""
    groups = _get(card, 'traits')
    if not groups or not isinstance(groups, (list, tuple)):
        return []
    parsed = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        name = _line(group.get('name'), 40)
        rows = []
        for item in (group.get('entries') or []) if isinstance(group.get('entries'), (list, tuple)) else []:
            if not isinstance(item, dict):
                continue
            label = _line(item.get('label'), 40)
            rating = _line(item.get('rating_label'), 20) or '-'
            rows.append([_p(label, styles['td']), _p(rating, styles['td_rb'])])
        if rows:
            parsed.append((name, rows))
    if not parsed:
        return []

    n = len(parsed)
    gutter = 4 * mm if n > 1 else 0
    block_w = (CONTENT_W - gutter * (n - 1)) / n
    rating_col = 26 * mm

    blocks = []
    for name, rows in parsed:
        table = Table(rows, colWidths=[block_w - 16 * mm - rating_col, rating_col])
        table.setStyle(TableStyle([('LINEBELOW', (0, 0), (-1, -2), 0.4, theme.line),
                                   ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
                                   ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                                   ('ALIGN', (1, 0), (1, -1), 'RIGHT')]))
        boxed = Table([[_p(name, styles['head'])], [table]], colWidths=[block_w])
        boxed.setStyle(TableStyle([('BOX', (0, 0), (-1, -1), 0.6, theme.line), ('TOPPADDING', (0, 0), (-1, -1), 6),
                                   ('BOTTOMPADDING', (0, 0), (-1, -1), 6), ('LEFTPADDING', (0, 0), (-1, -1), 8),
                                   ('RIGHTPADDING', (0, 0), (-1, -1), 8)]))
        blocks.append(boxed)

    row = Table([blocks], colWidths=[block_w] * n)
    style = [('VALIGN', (0, 0), (-1, -1), 'TOP'), ('TOPPADDING', (0, 0), (-1, -1), 0),
             ('BOTTOMPADDING', (0, 0), (-1, -1), 0), ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0)]
    if n > 1:
        style += [('LEFTPADDING', (i, 0), (i, 0), gutter) for i in range(1, n)]
    row.setStyle(TableStyle(style))
    return [row, Spacer(1, 4 * mm)]


def _comment_and_signatures(card, theme, styles, pictures):
    """The class teacher's comment box, then the teacher's and the head's signature blocks side by side."""
    comment = _get(card, 'comment')
    comment = comment if isinstance(comment, dict) else {}
    text = _clean(comment.get('text')).strip()
    parts = [_p("Class Teacher's Comment", styles['head'])]
    if text:
        parts.append(_p(text, styles['comment']))
    else:
        # No comment: an empty box with writing lines, so a teacher can still write one by hand.
        empty = Table([[''], [''], ['']], colWidths=[CONTENT_W], rowHeights=[7 * mm] * 3)
        empty.setStyle(TableStyle([('BOX', (0, 0), (-1, -1), 0.6, theme.line), ('BACKGROUND', (0, 0), (-1, -1), theme.tint),
                                   ('LINEBELOW', (0, 0), (-1, -2), 0.4, theme.line)]))
        parts.append(empty)
    parts.append(Spacer(1, 5 * mm))

    teacher_name = _line(comment.get('teacher_name'), 80)
    teacher = _signature_block(pictures, styles, comment.get('teacher_signature_path'),
                               teacher_name, 'Class Teacher')
    head = _dict(card, 'head')
    head_name = _line(head.get('name'), 80)
    head_title = _line(head.get('title'), 60) or 'Head of School'
    principal = _signature_block(pictures, styles, head.get('signature_path'), head_name, head_title)
    signatures = Table([[teacher, '', principal]], colWidths=[72 * mm, CONTENT_W - 144 * mm, 72 * mm])
    signatures.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),   # so both signature lines are level ('LEFTPADDING', (0, 0), (-1, -1), 0),
                                    ('RIGHTPADDING', (0, 0), (-1, -1), 0), ('TOPPADDING', (0, 0), (-1, -1), 0),
                                    ('BOTTOMPADDING', (0, 0), (-1, -1), 0)]))
    parts.append(signatures)
    parts.append(Spacer(1, 5 * mm))
    return [KeepTogether(parts)]


def _grading_key(card, theme, styles):
    """A compact grading key (grade: range (meaning)) and the 'Next term begins' line."""
    entries = []
    for item in (_get(card, 'grading_key') or []) if isinstance(_get(card, 'grading_key'), (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        grade, span, remark = _line(item.get('grade'), 12), _line(item.get('range'), 30), _line(item.get('remark'), 40)
        text = grade
        if span:
            text += (': ' if grade else '') + span
        if remark:
            text += ' (%s)' % remark
        if text:
            entries.append(_p(text, styles['key']))     # e.g. 'A: 70 - 100 (Excellent)'
    parts = []
    if entries:
        parts.append(_p('Grading key', styles['head']))
        per_row = 3 if len(entries) <= 6 else 4
        rows = [entries[i:i + per_row] for i in range(0, len(entries), per_row)]
        rows = [row + [''] * (per_row - len(row)) for row in rows]
        key = Table(rows, colWidths=[CONTENT_W / per_row] * per_row)
        key.setStyle(TableStyle([('BOX', (0, 0), (-1, -1), 0.6, theme.line), ('BACKGROUND', (0, 0), (-1, -1), theme.tint),
                                 ('TOPPADDING', (0, 0), (-1, -1), 2.5), ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5),
                                 ('LEFTPADDING', (0, 0), (-1, -1), 6), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
                                 ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]))
        parts.append(key)
    next_term = _line(_get(card, 'next_term_begins'), 80)
    if next_term:
        parts += [Spacer(1, 3 * mm), _p('Next term begins: ' + next_term, styles['next'])]
    return [KeepTogether(parts)] if parts else []


def _card_story(card, number, state, pictures):
    """Everything on one card, as a list of reportlab flowables."""
    school = _dict(card, 'school')
    theme = _Theme(school)
    styles = _styles(theme)
    story = [_StartCard(state, number, card, theme), NextPageTemplate('later'), _TopBand(theme)]
    story += _header(card, theme, styles, pictures)
    story += _title_block(card, theme, styles)
    story += _student_block(card, theme, styles, pictures)
    table, has_subjects = _results_table(card, theme, styles)
    story += table
    if has_subjects:
        story += _summary_strip(card, theme, styles)
    story += _traits_block(card, theme, styles)
    story += _comment_and_signatures(card, theme, styles, pictures)
    story += _grading_key(card, theme, styles)
    return story


# ---------------------------------------------------------------------------------------------
# The public function
# ---------------------------------------------------------------------------------------------
def render_report_cards_pdf(cards):
    """Render report cards to PDF bytes: one A4 portrait card per item of ``cards``.

    Each card is a dict with the keys ``school`` (name, motto, address, phone, email, logo_path,
    primary, accent), ``title``, ``term``, ``session``, ``student`` (name, admission_no, class_name,
    gender, photo_path), ``subjects`` (name, test, assignment, project, ca, ca_max, exam, exam_max,
    total, total_max, grade, remark, class_average), ``summary`` (total, total_max, percentage, grade,
    remark, class_average_percentage, class_size, subjects_count), ``comment`` (None or text,
    teacher_name, teacher_signature_path), ``traits`` (None, or a list of {name, entries: [{label,
    rating_label}]} — the affective/psychomotor ratings, omitted entirely when None so a school that
    never rates traits sees no change to its card), ``head`` (title, name, signature_path),
    ``grading_key`` (range, grade, remark), ``issued_on`` and ``next_term_begins``.

    Missing or unreadable pictures are left out (a missing logo is replaced by the school's name).
    A card that does not fit on one page carries on onto the next. An empty list gives a one-page
    PDF that says 'No report cards.'
    """
    cards = [card if isinstance(card, dict) else {} for card in (list(cards) if cards is not None else [])]
    regular = _fonts()[0]
    state, pictures = _State(), _Pictures()

    story = []
    if not cards:
        story.append(Spacer(1, 60 * mm))
        story.append(_p('No report cards.', _styles(_Theme({}))['empty']))
    for number, card in enumerate(cards):
        if number:
            story += [NextPageTemplate('first'), PageBreak()]
        story += _card_story(card, number, state, pictures)

    first_school = _dict(cards[0], 'school') if cards else {}
    school_name = _line(first_school.get('name'), 100)
    buffer = io.BytesIO()
    doc = BaseDocTemplate(buffer, pagesize=A4, title=(school_name + ' - ' if school_name else '') + 'Report Cards',
                          author=school_name, creator='Brightstars Academics')
    first_frame = Frame(MARGIN_X, BOTTOM_MARGIN, CONTENT_W, PAGE_H - TOP_MARGIN_FIRST - BOTTOM_MARGIN,
                        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id='first_frame')
    later_frame = Frame(MARGIN_X, BOTTOM_MARGIN, CONTENT_W, PAGE_H - TOP_MARGIN_LATER - BOTTOM_MARGIN,
                        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id='later_frame')
    doc.addPageTemplates([PageTemplate(id='first', frames=[first_frame]),
                          PageTemplate(id='later', frames=[later_frame], onPage=_running_header(state))])
    doc.build(story, canvasmaker=lambda *args, **kwargs: _NumberedCanvas(*args, state=state, **kwargs))
    return buffer.getvalue()
