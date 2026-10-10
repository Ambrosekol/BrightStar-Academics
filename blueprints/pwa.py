"""The portal as an installable app (a Progressive Web App), in each school's own name and logo.

* ``/manifest.webmanifest`` - the school's name, colours and icons, so a phone or computer can install its portal like
  an app, with the school's logo on the home screen.
* ``/pwa/icon-<size>.png`` and ``/pwa/maskable-512.png`` - the school's own logo, fitted onto a square at each size an
  app icon, a home-screen icon or a browser tab needs. A school with no logo gets a monogram in its own colour. Every
  page links them with a version taken from the logo itself (``pwa_icon_url``), so a new logo shows at once and an
  unchanged one is never fetched twice.
* ``/favicon.ico`` - the same logo, for the browser that asks for it by that name; every page also names its icon.
* ``/sw.js`` - the service worker. It keeps only the portal's own static files (styles, scripts, fonts) so pages open
  faster, and shows ``/offline`` when there is no connection. It never stores a page someone is signed in to, nor an
  uploaded file, so nothing private stays on a shared device.

On the platform's own host there is no school: the manifest and icons are the platform's.
"""

import hashlib
import io
import json

from flask import Response, render_template, request

from app import app
from core.branding import PLATFORM_NAME, school_brand
from control_plane.context import current_tenant

SIZES = (16, 32, 48, 180, 192, 512)
_icons = {}          # (tenant id, logo, colour, size, maskable) -> png bytes
_ICON_CACHE_MAX = 256
SW_VERSION = '1'


def _brand():
    brand = school_brand()
    return brand, (brand.get('primary') or '#0d2b52'), (brand.get('accent') or '#1674b9')


def _logo_bytes(brand):
    path = brand.get('logo_path') or ''
    if not path:
        return None
    try:
        from core.storage import read_upload_bytes
        return read_upload_bytes(path)
    except Exception:
        return None


def _initials(name):
    words = [w for w in (name or '').split() if w.lower() not in ('the', 'of', 'and', '&')]
    return ((words[0][:1] if words else '') + (words[1][:1] if len(words) > 1 else '')).upper() or '·'


def _font(px):
    from PIL import ImageFont
    for name in ('DejaVuSans-Bold.ttf', 'Arial Bold.ttf', 'arialbd.ttf'):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=px)
    except TypeError:
        return ImageFont.load_default()


def _hex(colour):
    colour = colour.lstrip('#')
    return tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4)) if len(colour) == 6 else (13, 43, 82)


def _render_icon(brand, primary, size, maskable):
    """The school's logo on a square: on white, with a little room around it (or, maskable, with the wide safe area
    an operating system may crop into a circle or a rounded square). No logo: its initials on its own colour."""
    from PIL import Image, ImageDraw
    logo = _logo_bytes(brand)
    if logo:
        try:
            pic = Image.open(io.BytesIO(logo))
            pic.load()
            pic = pic.convert('RGBA')
            canvas = Image.new('RGBA', (size, size), (255, 255, 255, 255))
            room = 0.62 if maskable else (0.92 if size <= 48 else 0.84)
            box = max(1, int(size * room))
            pic.thumbnail((box, box), Image.LANCZOS)
            canvas.alpha_composite(pic, ((size - pic.width) // 2, (size - pic.height) // 2))
            out = io.BytesIO()
            canvas.convert('RGB').save(out, 'PNG', optimize=True)
            return out.getvalue()
        except Exception:
            pass   # an unreadable logo falls back to the monogram, never to a broken icon
    canvas = Image.new('RGB', (size, size), _hex(primary))
    draw = ImageDraw.Draw(canvas)
    text = _initials(brand.get('name'))
    font = _font(int(size * (0.34 if maskable else 0.44)))
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(((size - (right - left)) / 2 - left, (size - (bottom - top)) / 2 - top), text, font=font, fill=(255, 255, 255))
    out = io.BytesIO()
    canvas.save(out, 'PNG', optimize=True)
    return out.getvalue()


def _icon(size, maskable=False):
    brand, primary, _ = _brand()
    tenant = current_tenant(required=False)
    key = (tenant.id if tenant else None, brand.get('logo_path') or '', primary, brand.get('name'), size, maskable)
    data = _icons.get(key)
    if data is None:
        data = _render_icon(brand, primary, size, maskable)
        if len(_icons) >= _ICON_CACHE_MAX:
            _icons.clear()
        _icons[key] = data
    return data


def _png(data):
    response = Response(data, mimetype='image/png')
    # The address carries the logo's version (pwa_icon_url), so the browser may keep it; an address without one
    # (the browser's own /favicon.ico) is kept for a day.
    response.headers['Cache-Control'] = ('public, max-age=31536000, immutable' if request.args.get('v')
                                         else 'public, max-age=86400')
    return response


def _icon_version():
    brand, primary, _ = _brand()
    raw = f"{brand.get('logo_path') or ''}|{primary}|{brand.get('name')}"
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def pwa_icon_url(size=192, maskable=False):
    """The icon's address with the logo's version, or '' outside a request (a page rendered on its own, such as the
    platform's maintenance notice), where there is no school and no address to give."""
    from flask import has_request_context, url_for
    if not has_request_context():
        return ''
    if maskable:
        return url_for('pwa_maskable_icon', v=_icon_version())
    return url_for('pwa_icon', size=size, v=_icon_version())


app.jinja_env.globals.update(pwa_icon_url=pwa_icon_url)


@app.route('/pwa/icon-<int:size>.png')
def pwa_icon(size):
    if size not in SIZES:
        return Response(status=404)
    return _png(_icon(size))


@app.route('/pwa/maskable-512.png')
def pwa_maskable_icon():
    return _png(_icon(512, maskable=True))


@app.route('/favicon.ico')
def favicon():
    return _png(_icon(48))


@app.route('/manifest.webmanifest')
def pwa_manifest():
    brand, primary, _ = _brand()
    name = brand.get('name') or PLATFORM_NAME
    words = name.split()
    short = name if len(name) <= 12 else (_initials(name) if len(words) > 1 and len(words[0]) > 12 else words[0][:12])
    manifest = {
        'name': name,
        'short_name': short,
        'description': f'{name}: the school portal for staff, parents and students.',
        'id': '/',
        'start_url': '/?source=app',
        'scope': '/',
        'display': 'standalone',
        'orientation': 'any',
        'background_color': '#ffffff',
        'theme_color': primary,
        'icons': [
            {'src': pwa_icon_url(192), 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': pwa_icon_url(512), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': pwa_icon_url(maskable=True), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
        ],
    }
    response = Response(json.dumps(manifest, ensure_ascii=False), mimetype='application/manifest+json')
    response.headers['Cache-Control'] = 'public, max-age=300'
    return response


@app.route('/offline')
def pwa_offline():
    """What the installed app shows when there is no connection (the service worker keeps a copy)."""
    brand, primary, _ = _brand()
    response = Response(render_template('offline.html', brand=brand, primary=primary))
    response.headers['Cache-Control'] = 'public, max-age=300'
    return response


SERVICE_WORKER = """/* The portal's service worker (blueprints/pwa.py). It keeps the portal's own static files - never a signed-in
   page and never an uploaded file - and shows the offline page when there is no connection. */
var CACHE = 'bs-static-v%(version)s';
var OFFLINE = '/offline';
self.addEventListener('install', function (event) {
  event.waitUntil(caches.open(CACHE).then(function (cache) { return cache.add(new Request(OFFLINE, { cache: 'reload' })); })
    .then(function () { return self.skipWaiting(); }));
});
self.addEventListener('activate', function (event) {
  event.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k.indexOf('bs-static-') === 0 && k !== CACHE; })
      .map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});
self.addEventListener('fetch', function (event) {
  var request = event.request;
  if (request.method !== 'GET') return;
  var url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // The portal's own files: from the cache when there, refreshed in the background.
  if (url.pathname.indexOf('/static/') === 0 && url.pathname.indexOf('/static/uploads/') !== 0) {
    event.respondWith(caches.open(CACHE).then(function (cache) {
      return cache.match(request).then(function (hit) {
        var fresh = fetch(request).then(function (response) {
          if (response && response.ok) cache.put(request, response.clone());
          return response;
        }).catch(function () { return hit; });
        return hit || fresh;
      });
    }));
    return;
  }
  // A page: always from the network (it may be private); the offline page only when there is no connection.
  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(function () { return caches.match(OFFLINE); }));
  }
});
"""


@app.route('/sw.js')
def pwa_service_worker():
    response = Response(SERVICE_WORKER % {'version': SW_VERSION}, mimetype='application/javascript')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Service-Worker-Allowed'] = '/'
    return response
