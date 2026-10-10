/* The parent portal's loading feedback, and its avatars (templates/parent_base.html; the tabs and messages are static/parent-portal.js).

   Loading: as soon as a link to another page of the portal is followed, or a form is sent, a thin bar runs along the
   top of the screen, the button pressed spins, and if the page takes more than a moment a spinner shows in the middle.
   This matters most in the installed app (blueprints/pwa.py), which has no browser bar of its own to show progress.
   Links that leave the page alone are left alone too: a new tab, a download, a PDF, a part of the same page, or one
   marked data-no-loader. Coming back with the browser's Back clears it (the page is shown from memory).

   Avatars: a child's photograph that cannot be loaded is replaced by their initials (data-avatar-fallback), never
   left as a broken picture. */
(function () {
  'use strict';
  var bar = null, veil = null, slow = null;

  function start() {
    if (!bar) {
      bar = document.createElement('div'); bar.className = 'pp-loadbar'; bar.setAttribute('aria-hidden', 'true');
      veil = document.createElement('div'); veil.className = 'pp-loadveil'; veil.setAttribute('role', 'status');
      veil.innerHTML = '<span class="pp-loadveil__spin"></span><span class="pp-loadveil__sr">Loading</span>';
      document.body.appendChild(bar); document.body.appendChild(veil);
    }
    document.documentElement.classList.add('is-loading');
    clearTimeout(slow);
    slow = setTimeout(function () { document.documentElement.classList.add('is-loading-slow'); }, 450);
  }
  function stop() {
    clearTimeout(slow);
    document.documentElement.classList.remove('is-loading', 'is-loading-slow');
    Array.prototype.forEach.call(document.querySelectorAll('.pp-busy'), function (b) { b.classList.remove('pp-busy'); b.removeAttribute('aria-busy'); });
  }

  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest('a[href]');
    if (!a || a.target || a.hasAttribute('download') || a.hasAttribute('data-no-loader')) return;
    var href = a.getAttribute('href') || '';
    if (!href || href.charAt(0) === '#' || /^(mailto|tel|javascript):/i.test(href)) return;
    var url = new URL(a.href, window.location.href);
    if (url.origin !== window.location.origin) return;
    if (/\.pdf$|\/pdf(\/|$)/i.test(url.pathname)) return;
    if (url.pathname === window.location.pathname && url.search === window.location.search && url.hash) return;
    if (a.classList.contains('ui-btn') || a.classList.contains('pp-btn-pay')) { a.classList.add('pp-busy'); a.setAttribute('aria-busy', 'true'); }
    start();
  });

  // A form being sent: the button pressed spins. Runs after other handlers, so a form a script stopped (a "are you
  // sure?" the person cancelled) is not marked.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    setTimeout(function () {
      if (e.defaultPrevented || form.target) return;
      var button = e.submitter || form.querySelector('button[type=submit], button:not([type])');
      if (button) { button.classList.add('pp-busy'); button.setAttribute('aria-busy', 'true'); }
      start();
    }, 0);
  });

  window.addEventListener('pageshow', function (e) { if (e.persisted) stop(); });
  window.addEventListener('pagehide', function () { clearTimeout(slow); });

  // Avatars: initials instead of a picture that will not load.
  function fallback(img) {
    var text = img.getAttribute('data-avatar-fallback') || '?';
    var span = document.createElement('span');
    span.className = 'pp-avatar-initials'; span.textContent = text; span.setAttribute('aria-hidden', 'true');
    img.replaceWith(span);
  }
  Array.prototype.forEach.call(document.querySelectorAll('img[data-avatar-fallback]'), function (img) {
    if (img.complete && img.naturalWidth === 0 && img.getAttribute('src')) { fallback(img); return; }
    img.addEventListener('error', function () { fallback(img); }, { once: true });
  });
})();
