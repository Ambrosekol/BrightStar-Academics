"""Faster pages without changing what they show: the browser keeps the portal's own files, and what is sent is
compressed.

* **Static files kept by the browser.** Every ``url_for('static', ...)`` for the portal's own files (styles, scripts,
  fonts, images - not uploads, which have their own rule in app.uploaded_file) gets ``m=<the file's modification
  time>``. An address that names its version can be kept for a year (``immutable``): the browser stops asking again
  on every page, and the moment a file changes its address changes, so nobody sees an old copy. A file asked for
  without ``m`` (an old bookmark, a hand-written link) is kept for five minutes only.
* **One address per file.** A hand-written ``v=`` (``v='4'``, ``v='11'``) is dropped in favour of ``m``: the same file
  named with two different ``v`` values on two pages was two cache entries, so going from one page to the next fetched
  a stylesheet the browser already had, and the next page could not paint until it arrived.
* **Fonts.** ``static/fonts/fonts.css`` names its font files without ``m`` (a stylesheet cannot call url_for), so a
  font's ``url_for`` (the ``<link rel="preload">`` in each page's head) is left without ``m`` too: the preload then
  fetches the very file the stylesheet asks for, instead of a second copy under another address. Font files never
  change under the same name, so they are kept for thirty days.
* **Compression.** Pages, fragments loaded in place, JSON and the static text files are gzipped for a browser that
  accepts it (every current one does): a page of a few hundred kilobytes travels as a few dozen. A static file is
  compressed once per version and kept. Set ``BRIGHTSTARS_GZIP=0`` where a proxy in front already compresses.
"""

import gzip
import os
import time

from flask import request

_COMPRESSIBLE = ('text/html', 'text/css', 'text/plain', 'text/csv', 'application/json', 'application/javascript',
                 'text/javascript', 'image/svg+xml', 'application/xml', 'text/xml')
_MIN_BYTES = 1024
_mtimes = {}            # path -> (checked_at, mtime)
_MTIME_TTL = 10         # seconds before a file's modification time is looked at again
_static_gz = {}         # (path, mtime) -> gzipped bytes
_STATIC_GZ_MAX = 512


def _enabled():
    return os.environ.get('BRIGHTSTARS_GZIP', '1').strip().lower() not in ('0', 'false', 'no', 'off')


def _mtime(static_folder, filename):
    now = time.monotonic()
    hit = _mtimes.get(filename)
    if hit and now - hit[0] < _MTIME_TTL:
        return hit[1]
    try:
        value = int(os.path.getmtime(os.path.join(static_folder, filename)))
    except OSError:
        value = 0
    if len(_mtimes) > 4096:
        _mtimes.clear()
    _mtimes[filename] = (now, value)
    return value


def _timestamped(value):
    """A ``v=`` that is already the file's modification time (static/upload-limit.js is given one by core/uploads.py)."""
    return str(value or '').isdigit() and len(str(value)) >= 9


def _is_font(filename):
    return filename.startswith('fonts/') and filename.endswith(('.woff2', '.woff'))


def init_app(app):
    @app.url_defaults
    def _version_static(endpoint, values):
        if endpoint != 'static':
            return
        filename = values.get('filename') or ''
        if not filename or filename.startswith('uploads/') or 'm' in values or _timestamped(values.get('v')):
            return
        values.pop('v', None)
        if _is_font(filename):
            return
        version = _mtime(app.static_folder, filename)
        if version:
            values['m'] = version

    @app.after_request
    def _cache_and_compress(response):
        if request.endpoint == 'static' and response.status_code == 200 and not (request.view_args or {}).get('filename', '').startswith('uploads/'):
            versioned = request.args.get('m') or _timestamped(request.args.get('v'))
            if versioned:
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            elif _is_font((request.view_args or {}).get('filename', '')):
                response.headers['Cache-Control'] = 'public, max-age=2592000'
            else:
                response.headers['Cache-Control'] = 'public, max-age=300'
        if not _enabled() or response.status_code != 200 or 'Content-Encoding' in response.headers:
            return response
        if 'gzip' not in (request.headers.get('Accept-Encoding') or '').lower():
            return response
        if (response.mimetype or '') not in _COMPRESSIBLE or response.is_streamed and request.endpoint != 'static':
            return response
        if request.endpoint == 'static':
            filename = (request.view_args or {}).get('filename', '')
            key = (filename, _mtime(app.static_folder, filename))
            data = _static_gz.get(key)
            if data is None:
                response.direct_passthrough = False
                raw = response.get_data()
                if len(raw) < _MIN_BYTES:
                    return response
                data = gzip.compress(raw, 6)
                if len(_static_gz) >= _STATIC_GZ_MAX:
                    _static_gz.clear()
                _static_gz[key] = data
            else:
                if hasattr(response.response, 'close'):
                    response.response.close()   # the file it would have sent is not read at all
                response.direct_passthrough = False
        else:
            raw = response.get_data()
            if len(raw) < _MIN_BYTES:
                return response
            data = gzip.compress(raw, 5)
        response.set_data(data)
        response.headers['Content-Encoding'] = 'gzip'
        response.headers['Content-Length'] = str(len(data))
        response.vary.add('Accept-Encoding')
        # A weak validator still describes the content; a strong one would not match the compressed bytes.
        if response.headers.get('ETag', '').startswith('"'):
            response.headers['ETag'] = 'W/' + response.headers['ETag']
        return response


def request_memo(name, compute):
    """``compute()`` once for the rest of this request (outside a request, every time). For a value one page reads
    several times over - the school's id, its delivery or payment settings - that cannot change unless the page itself
    changes it, in which case request_forget(name) drops it."""
    from flask import g, has_request_context
    if not has_request_context():
        return compute()
    memo = g.setdefault('_request_memo', {})
    if name not in memo:
        memo[name] = compute()
    return memo[name]


def request_forget(*names):
    from flask import g, has_request_context
    if has_request_context():
        memo = g.get('_request_memo') or {}
        for name in names:
            memo.pop(name, None)
