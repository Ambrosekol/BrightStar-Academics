"""The hardened image-upload saver used across signatures, school branding,
question images, staff, student and candidate photographs, and any other
admin-uploaded image.

Files are written under the current school's uploads folder (core/storage.py),
not a shared one.

This module is also the single source of truth for *how large* an upload may be
and for every sentence that tells a person why one was refused. The number is
read once, here (``BRIGHTSTARS_MAX_UPLOAD_BYTES``, five megabytes unless the
school's host says otherwise); the server checks against it, the pages print it
under each file box (:func:`upload_hint`), and ``static/upload-limit.js`` reads
it from the page (:func:`upload_attrs`) so the person is told *before* they
submit. Nothing else in the application spells the number out.
"""

import io
import math
import os
import re
import secrets

from PIL import Image, ImageOps
from werkzeug.utils import secure_filename

from core.storage import save_upload_bytes

BASE=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC=os.path.join(BASE,'static')
IMAGE_EXTENSIONS={'png','jpg','jpeg','gif','webp'}
# What a message may carry besides a picture (the staff messages' and the parent feedback
# attachment boxes).
ATTACHMENT_EXTENSIONS={'.jpg','.jpeg','.png','.gif','.webp','.pdf','.doc','.docx','.txt','.xls','.xlsx'}
IMAGE_TYPES_LABEL='PNG, JPG, GIF or WEBP'
# A bulk-data upload (the student CSV importer, and anything like it later): plain-text data, not
# a picture or a document, so it gets its own kind, extensions and (smaller, fixed) limit.
DATA_EXTENSIONS={'.csv'}
DATA_TYPES_LABEL='a CSV file'


def attachment_kind(ext):
    """A broad category for a message attachment's extension, for its icon/label."""
    ext=(ext or '').lower()
    if ext in {'.jpg','.jpeg','.png','.gif','.webp'}: return 'image'
    if ext=='.pdf': return 'pdf'
    if ext in {'.doc','.docx'}: return 'doc'
    if ext in {'.xls','.xlsx'}: return 'spreadsheet'
    return 'file'

DEFAULT_IMAGE_LIMIT_BYTES=5*1024*1024
DEFAULT_REQUEST_LIMIT_BYTES=8*1024*1024
DEFAULT_DATA_LIMIT_BYTES=2*1024*1024
# What the rest of a form (its text fields, the security token, the multipart framing) takes
# out of the room a message attachment has in the whole-request limit.
REQUEST_OVERHEAD_BYTES=64*1024

_KB=1024
_MB=1024*1024
_GB=1024*1024*1024


# --------------------------------------------------------------------------------------------
# The limits, and how they are written down

def _env_bytes(name, default):
    """A positive whole number of bytes from the environment; anything else is the default."""
    try:
        value=int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value>0 else default


def image_limit_bytes():
    """The largest picture a person may upload, in bytes (one file)."""
    return _env_bytes('BRIGHTSTARS_MAX_UPLOAD_BYTES', DEFAULT_IMAGE_LIMIT_BYTES)


def request_limit_bytes(scope=''):
    """The most a whole form submission may carry, in bytes.

    This is Flask's own ``MAX_CONTENT_LENGTH`` (the page that raised it for itself is honoured);
    ``scope='branding'`` is the larger allowance of the pages that take a logo and a set of
    sign-in photographs together.
    """
    if scope=='branding':
        from core.branding import max_branding_request_bytes
        return max_branding_request_bytes()
    from flask import has_request_context, request, current_app
    if has_request_context() and request.max_content_length:
        return int(request.max_content_length)
    try:
        configured=current_app.config.get('MAX_CONTENT_LENGTH')
    except RuntimeError:
        configured=None
    return int(configured) if configured else _env_bytes('BRIGHTSTARS_MAX_REQUEST_BYTES', DEFAULT_REQUEST_LIMIT_BYTES)


def data_upload_limit_bytes():
    """The largest a bulk-data upload (a CSV import) may be, in bytes (one file)."""
    return _env_bytes('BRIGHTSTARS_MAX_DATA_UPLOAD_BYTES', DEFAULT_DATA_LIMIT_BYTES)


def attachment_limit_bytes():
    """The largest file a message attachment may be: the request limit less the form's own weight."""
    total=request_limit_bytes()
    return max(total-REQUEST_OVERHEAD_BYTES, total//2)


def format_bytes(size, decimals=1, mode='nearest'):
    """A size for people: "820 KB", "5 MB", "7.2 MB".

    ``mode`` is how the last digit is rounded: 'nearest' for a size, 'down' for a limit (never
    promise more room than there is) and 'up' for a size that is over a limit (never claim it is
    smaller than it is).
    """
    size=max(0, int(size))
    if size<_KB:
        return f'{size} byte' + ('' if size==1 else 's')
    for unit, factor in (('GB',_GB),('MB',_MB),('KB',_KB)):
        if size>=factor:
            break
    scale=10**decimals
    raw=size/factor*scale
    number={'down':math.floor,'up':math.ceil}.get(mode, round)(raw)/scale
    text=f'{number:.{decimals}f}'
    if '.' in text:
        text=text.rstrip('0').rstrip('.')
    return f'{text} {unit}'


def format_limit(size):
    """A limit as a person reads it: "5 MB"."""
    return format_bytes(size, mode='down')


def format_size_over(size, limit):
    """The size of a file that is over ``limit``, written so it never looks equal to it.

    A file of 5.02 MB against a 5 MB limit is "5.02 MB", not "5 MB is over the limit of 5 MB".
    """
    text=format_bytes(size)
    if text==format_limit(limit):
        text=format_bytes(size, decimals=2, mode='up')
    return text


# --------------------------------------------------------------------------------------------
# What a person is told

def _shown_name(file_obj):
    """The chosen file's own name, made safe and short enough to put in a sentence."""
    name=re.sub(r'[\x00-\x1f\x7f]', '', str(getattr(file_obj, 'filename', '') or ''))
    name=name.replace('\\', '/').rsplit('/', 1)[-1].strip()
    if not name:
        return 'The file'
    return '"'+(name if len(name)<=60 else name[:57].rstrip()+'...')+'"'


def too_large_message(name, size, limit):
    """Why a picture that is too large was refused."""
    return (f'{name} is {format_size_over(size, limit)}. The limit is {format_limit(limit)}. '
            'Choose a smaller picture, or reduce this one first.')


def wrong_type_message(name):
    return (f'{name} is not a picture we can use. Please choose a {IMAGE_TYPES_LABEL} image.')


def not_an_image_message(name):
    return (f'{name} does not appear to be a valid image. It may be another kind of file that was '
            f'renamed, or a damaged picture. Please choose a real {IMAGE_TYPES_LABEL} picture.')


def request_too_large_message(sent=None, limit=None):
    """Why a whole submission was refused for its size (HTTP 413)."""
    limit=limit or request_limit_bytes()
    lead=(f'The file you sent is larger than the limit of {format_limit(limit)} for one submission'
          if not sent or sent<=limit else
          f'The file you sent is {format_size_over(sent, limit)}, which is larger than the limit of '
          f'{format_limit(limit)} for one submission')
    single=image_limit_bytes()
    return (f'{lead}. A single picture can be up to {format_limit(single)}. '
            'Choose a smaller file, or reduce this one first, then try again.')


# --------------------------------------------------------------------------------------------
# What the page shows and hands to static/upload-limit.js

def _mtime_version():
    try:
        return int(os.path.getmtime(os.path.join(STATIC, 'upload-limit.js')))
    except OSError:
        return 0


def upload_attrs(kind='image', scope=''):
    """The data attributes for an ``<input type="file">``: what it may take, for the script.

    ``kind`` is 'image' (one of the allowed picture types, up to the picture limit), 'attachment'
    (a message attachment, limited only by the size of the whole request) or 'data' (a bulk-data
    upload such as the student CSV importer, with its own fixed limit).
    ``scope`` is 'branding' where the form also carries a logo and a set of photographs.
    """
    from markupsafe import Markup, escape
    if kind=='attachment':
        limit=attachment_limit_bytes()
        types=','.join(sorted(e.lstrip('.') for e in ATTACHMENT_EXTENSIONS))
        types_label='an image, PDF, Word, text or Excel file'
    elif kind=='data':
        limit=data_upload_limit_bytes()
        types=','.join(sorted(e.lstrip('.') for e in DATA_EXTENSIONS))
        types_label=DATA_TYPES_LABEL
    else:
        kind='image'
        limit=image_limit_bytes()
        types=','.join(sorted(IMAGE_EXTENSIONS))
        types_label=IMAGE_TYPES_LABEL
    request_limit=request_limit_bytes(scope)
    attrs={
        'data-upload-kind': kind,
        'data-upload-limit': limit,
        'data-upload-limit-label': format_limit(limit),
        'data-upload-request-limit': request_limit,
        'data-upload-request-label': format_limit(request_limit),
        'data-upload-types': types,
        'data-upload-types-label': types_label,
    }
    return Markup(' '.join(f'{k}="{escape(v)}"' for k, v in attrs.items()))


def upload_hint(kind='image', scope='', multiple=False):
    """The sentence under a file box that says what is allowed, and the place the script writes to.

    Also puts the script on the page, once per page.
    """
    from markupsafe import Markup, escape
    if kind=='attachment':
        text=f'An image, PDF, Word, text or Excel file, up to {format_limit(attachment_limit_bytes())}.'
    elif kind=='data':
        text=f'{DATA_TYPES_LABEL}, up to {format_limit(data_upload_limit_bytes())}.'
    else:
        each=' each' if multiple else ''
        text=f'{IMAGE_TYPES_LABEL}, up to {format_limit(image_limit_bytes())}{each}.'
    return Markup(
        f'<small class="field-help upload-hint" data-upload-hint>{escape(text)}</small>'
        '<div class="upload-feedback" data-upload-feedback aria-live="polite"></div>'
    ) + _upload_script()


def _upload_script():
    from markupsafe import Markup
    try:
        from flask import g, url_for
        if getattr(g, '_upload_limit_script', False):
            return Markup('')
        g._upload_limit_script=True
        src=url_for('static', filename='upload-limit.js', v=_mtime_version())
    except RuntimeError:
        return Markup('')
    return Markup(f'<script src="{src}" defer></script>')


# --------------------------------------------------------------------------------------------
# Checking and saving

def validate_image_upload(file_obj):
    """Check an uploaded image without saving it; returns its extension.

    Raises ValueError with a message safe to show. The stream is left where it
    was, so the same file can be validated first and saved afterwards.
    """
    name=_shown_name(file_obj)
    original=secure_filename(file_obj.filename)
    ext=original.rsplit('.',1)[-1].lower() if '.' in original else ''
    if ext not in IMAGE_EXTENSIONS:
        raise ValueError(wrong_type_message(name))
    # Defense in depth: enforce a conservative upload limit and validate the
    # actual image signature before persisting the file.
    max_bytes=image_limit_bytes()  # BRIGHTSTARS_MAX_UPLOAD_BYTES, five megabytes by default
    stream=getattr(file_obj,'stream',None)
    if stream is None:
        raise ValueError('Invalid upload.')
    pos=stream.tell()
    stream.seek(0,2); size=stream.tell(); stream.seek(pos)
    if size > max_bytes:
        raise ValueError(too_large_message(name, size, max_bytes))
    header=stream.read(16); stream.seek(pos)
    # Validate the file signature, not merely the filename extension.
    # The previous hardening patch accidentally escaped the hexadecimal
    # signatures twice, which rejected genuine JPEG/GIF/WEBP files.
    signatures={
        'png': header.startswith(b'\x89PNG\r\n\x1a\n'),
        'jpg': header.startswith(b'\xff\xd8\xff'),
        'jpeg': header.startswith(b'\xff\xd8\xff'),
        'gif': header.startswith((b'GIF87a',b'GIF89a')),
        'webp': header.startswith(b'RIFF') and len(header)>=12 and header[8:12]==b'WEBP',
    }
    if not signatures.get(ext,False):
        raise ValueError(not_an_image_message(name))
    return ext


_IMAGE_CONTENT_TYPES={'png':'image/png','jpg':'image/jpeg','jpeg':'image/jpeg','gif':'image/gif','webp':'image/webp'}

# core/report_card_pdf.py already draws exactly this distinction when it embeds a picture in a
# PDF - "photographs: small files… logos and signatures: exact pixels" - this applies the same
# rule to what is actually stored, not just what is embedded. A folder here holds a photograph of
# a person, where JPEG's compression is a fair trade for the space it saves; everywhere else
# (signatures, branding, question and assignment pictures) keeps PNG, because JPEG's softened
# edges are exactly wrong for a signature that ends up on an official document, or a diagram that
# needs to stay legible.
_PHOTO_SUBDIRS={'students','candidates','admins'}
MAX_STORED_PX=1600
STORED_JPEG_QUALITY=85


def _reencode_for_storage(raw, subdir):
    """Shrink and re-compress a validated image before it is stored, so a phone photo does not
    sit in the bucket (or on disk) at full camera resolution. Returns (bytes, ext, content_type),
    or None if it cannot be re-encoded - validate_image_upload has already refused anything that
    is not a real image, so that is only a defensive fallback, never the expected path.
    """
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            opened.load()
            try:
                picture=ImageOps.exif_transpose(opened)  # honour a phone's rotation flag
            except Exception:
                picture=opened
            see_through=picture.mode in ('RGBA','LA','PA') or (
                picture.mode=='P' and 'transparency' in picture.info)
            as_photo=subdir in _PHOTO_SUBDIRS and not see_through
            picture=picture.convert('RGBA' if see_through else 'RGB')
            if max(picture.size) > MAX_STORED_PX:
                picture.thumbnail((MAX_STORED_PX, MAX_STORED_PX), Image.LANCZOS)
            out=io.BytesIO()
            if as_photo:
                picture.save(out, 'JPEG', quality=STORED_JPEG_QUALITY)
                return out.getvalue(), 'jpg', 'image/jpeg'
            picture.save(out, 'PNG')
            return out.getvalue(), 'png', 'image/png'
    except Exception:
        return None


def _save_image_upload(file_obj, subdir, prefix='image'):
    if not file_obj or not getattr(file_obj, 'filename', ''):
        return None
    ext=validate_image_upload(file_obj)
    safe_prefix=secure_filename(str(prefix))[:80] or 'image'
    raw=file_obj.stream.read()
    reencoded=_reencode_for_storage(raw, subdir)
    if reencoded:
        data, ext, content_type=reencoded
    else:
        data, content_type=raw, _IMAGE_CONTENT_TYPES.get(ext)
    filename=f"{safe_prefix}_{secrets.token_hex(10)}.{ext}"
    return save_upload_bytes(subdir, filename, data, content_type)
