/* Loading feedback for a whole-page load, on the staff pages (templates/admin_base.html), the student and parent
   portals (templates/portal_base.html) and the pages a paper is taken on; and the portals' avatars.

   A view that loads in place (static/inplace.js) already shows its own loading. Everything else - a link to another
   section, the side menu, a form that is sent as a whole page - used to show nothing until the next page arrived,
   and the installed app (blueprints/pwa.py) has no browser bar of its own to show progress. Now, as soon as such a
   link is followed or a form sent, the page shows exactly what a load in place shows (styled in static/school-ui.css):
   the page on screen stays, dimmed, under a small spinner in the school's accent, and the button pressed spins. A
   pop-up a form was sent from dims the same way. A page without a page body (taking a test or practice,
   static/exam-theme.css) shows a thin bar along the top instead.

   Left alone: anything static/inplace.js loads in place (it has already handled the click or the form), a new tab,
   a download, a file (a PDF, CSV, export or print view), a part of the same page, or a link marked data-no-loader.
   Coming back with the browser's Back clears it (the page is shown from memory), and so does a page that has not
   gone after a while - a "leave this page?" question answered Stay, or an answer that was a file download.

   Avatars: a photograph that cannot be loaded is replaced by the person's initials (data-avatar-fallback), never
   left as a broken picture. */
(function () {
  'use strict';
  var page = document.querySelector('[data-page-body]');
  var bar = null, giveUp = null, dimmed = [];

  // Addresses that answer with a file, not a page: these never leave the page, so it is not dimmed for them.
  // (Every download route in the app ends in one of these: .../pdf, /export/..., .../template.csv, .../attachment,
  // .../result/image, and uploads under /static/.)
  function isFile(url) {
    return url.pathname.indexOf('/static/') === 0 || /\.(pdf|csv|xlsx?|json|zip|png|jpe?g|gif|webp|docx?)$/i.test(url.pathname)
      || /\/(pdf|print|export|download|attachment|image)(\/|$)/i.test(url.pathname);
  }
  function busy(el) {
    // A button static/admin.js has already marked ("Please wait...") keeps that and is not marked twice.
    if (!el || (el.classList && el.classList.contains('is-loading'))) return;
    if (page) { el.classList.add('is-busy'); el.setAttribute('data-inplace-busy', ''); }
    else el.classList.add('pp-busy');
    el.setAttribute('aria-busy', 'true');
  }
  function dim(el) {
    if (!el || el.classList.contains('is-loading')) return;
    el.classList.add('is-loading'); el.setAttribute('aria-busy', 'true'); dimmed.push(el);
  }
  function start(from) {
    clearTimeout(giveUp);
    giveUp = setTimeout(stop, 15000);
    if (page) {
      dim(page);
      dim(from && from.closest && from.closest('.ac-modal__body'));
      return;
    }
    if (!bar) {
      bar = document.createElement('div'); bar.className = 'pp-loadbar'; bar.setAttribute('aria-hidden', 'true');
      document.body.appendChild(bar);
    }
    document.documentElement.classList.add('is-loading');
  }
  function stop() {
    clearTimeout(giveUp);
    dimmed.forEach(function (el) { el.classList.remove('is-loading'); el.setAttribute('aria-busy', 'false'); });
    dimmed = [];
    document.documentElement.classList.remove('is-loading');
    Array.prototype.forEach.call(document.querySelectorAll('.pp-busy, .is-busy[data-inplace-busy]'), function (b) {
      b.classList.remove('pp-busy', 'is-busy'); b.removeAttribute('data-inplace-busy'); b.removeAttribute('aria-busy');
    });
  }

  // Registered on window, so it runs after static/inplace.js (on document) has had its say: a link it loads in place
  // arrives here already handled.
  window.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest && e.target.closest('a[href]');
    if (!a || a.target || a.hasAttribute('download') || a.hasAttribute('data-no-loader')) return;
    var href = a.getAttribute('href') || '';
    if (!href || href.charAt(0) === '#' || /^(mailto|tel|sms|javascript):/i.test(href)) return;
    var url = new URL(a.href, window.location.href);
    if (url.origin !== window.location.origin || isFile(url)) return;
    if (url.pathname === window.location.pathname && url.search === window.location.search && url.hash) return;
    if (a.classList.contains('ac-btn') || a.classList.contains('ui-btn') || a.classList.contains('btn')) busy(a);
    start(a);
  });

  // A form being sent as a whole page: the button pressed spins. Runs after other handlers, so a form a script
  // stopped (a "are you sure?" answered No, or one static/inplace.js is loading in place) is not marked.
  window.addEventListener('submit', function (e) {
    var form = e.target;
    setTimeout(function () {
      if (e.defaultPrevented || form.target) return;
      var action = new URL(form.getAttribute('action') || window.location.href, window.location.href);
      if (isFile(action)) return;
      busy(e.submitter || form.querySelector('button[type=submit], button:not([type])'));
      start(form);
    }, 0);
  });

  window.addEventListener('pageshow', function (e) { if (e.persisted) stop(); });

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
