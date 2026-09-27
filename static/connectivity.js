/*
 * Says out loud what the browser already half-knows: that the connection just dropped, or just
 * came back. Plain JavaScript, no libraries, nothing the site's Content-Security-Policy forbids
 * (loaded from the site's own address, changes styles only through the DOM).
 *
 * A page opts in with <div data-connectivity-banner></div> (connectivity_banner(), core/uploads.py
 * -style template global, puts it on the page once). It shows a fixed banner across the top the
 * moment the browser fires 'offline', clears it on 'online', and says which just happened - a
 * flaky connection is often silent otherwise, leaving a person unsure whether a click did
 * anything.
 *
 * A form marked data-warn-unsaved warns before it is navigated away from once any of its fields
 * have changed, and stops warning again once it is actually submitted - a slow connection tempts
 * a person to reload or go back while a page still has something typed into it.
 */
(function () {
  'use strict';

  var MESSAGES = {
    offline: 'You are offline. Anything you do now will be tried again once the connection returns.',
    online: 'Back online.'
  };
  var custom = window.CONNECTIVITY_MESSAGES;
  if (custom && typeof custom === 'object') {
    for (var key in custom) {
      if (Object.prototype.hasOwnProperty.call(custom, key)) { MESSAGES[key] = custom[key]; }
    }
  }

  function banner() {
    var el = document.querySelector('[data-connectivity-banner]');
    return el || null;
  }

  function show(text, offline) {
    var el = banner();
    if (!el) { return; }
    el.textContent = text;
    el.classList.toggle('connectivity-banner-offline', !!offline);
    el.classList.toggle('connectivity-banner-visible', true);
  }

  function hideAfterOnline() {
    var el = banner();
    if (!el) { return; }
    window.setTimeout(function () {
      el.classList.remove('connectivity-banner-visible');
    }, 4000);
  }

  window.addEventListener('offline', function () { show(MESSAGES.offline, true); });
  window.addEventListener('online', function () { show(MESSAGES.online, false); hideAfterOnline(); });
  if (typeof navigator !== 'undefined' && navigator.onLine === false) { show(MESSAGES.offline, true); }

  // Unsaved-form warning: opt in per form, and only while a field has actually changed.
  document.addEventListener('change', function (event) {
    var form = event.target && event.target.closest ? event.target.closest('form[data-warn-unsaved]') : null;
    if (form) { form._dirty = true; }
  }, true);
  document.addEventListener('submit', function (event) {
    if (event.target && event.target.hasAttribute && event.target.hasAttribute('data-warn-unsaved')) {
      event.target._dirty = false;
    }
  }, true);
  window.addEventListener('beforeunload', function (event) {
    var anyDirty = false;
    document.querySelectorAll('form[data-warn-unsaved]').forEach(function (f) { if (f._dirty) { anyDirty = true; } });
    if (anyDirty) { event.preventDefault(); event.returnValue = ''; }
  });
})();
