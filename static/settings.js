/* Settings: "Find a setting" narrows the list (beside a setting's page) or the index (the overview) as you type, and
   "/" jumps to it. On a phone the list is a sheet: the switcher opens it, the scrim, its close button or Escape shut it.
   The frame is drawn again with each page loaded in place, so this runs again for each; data-ready keeps it from
   binding twice to the same frame. */
(function () {
  'use strict';
  var app = document.querySelector('[data-sx]');
  if (!app || app.hasAttribute('data-ready')) { return; }
  app.setAttribute('data-ready', '');
  var box = app.querySelector('[data-sx-find]');

  function filter() {
    var needle = box.value.trim().toLowerCase(), any = false;
    app.querySelectorAll('[data-sx-group]').forEach(function (group) {
      var shown = 0;
      group.querySelectorAll('[data-find]').forEach(function (item) {
        var hit = !needle || item.getAttribute('data-find').indexOf(needle) !== -1;
        item.hidden = !hit;
        if (hit) { shown++; }
      });
      group.hidden = shown === 0;
      if (shown) { any = true; }
    });
    app.querySelectorAll('[data-sx-none]').forEach(function (none) { none.hidden = any; });
  }
  if (box) {
    box.addEventListener('input', filter);
    // Enter opens the first setting still showing; Escape clears the search.
    box.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && box.value) { box.value = ''; filter(); e.stopPropagation(); return; }
      if (e.key !== 'Enter') { return; }
      e.preventDefault();
      var first = Array.prototype.find.call(app.querySelectorAll('[data-sx-entry]'), function (a) { return a.offsetParent !== null; });
      if (first) { first.click(); }
    });
  }

  // The phone sheet.
  var side = app.querySelector('[data-sx-side]'), opener = app.querySelector('[data-sx-open]'), scrim = app.querySelector('.sx-scrim');
  function sheet(open) {
    if (!side) { return; }
    side.classList.toggle('is-open', open);
    if (scrim) { scrim.hidden = !open; }
    if (opener) { opener.setAttribute('aria-expanded', open ? 'true' : 'false'); }
    document.documentElement.style.overflow = open ? 'hidden' : '';
    if (open) {
      var on = side.querySelector('.sx-link.is-on');
      if (on) { on.scrollIntoView({ block: 'center' }); }
      side.querySelector('.sx-side__close').focus();
    } else if (opener) { opener.focus(); }
  }
  if (opener) { opener.addEventListener('click', function () { sheet(true); }); }
  app.querySelectorAll('[data-sx-close]').forEach(function (el) { el.addEventListener('click', function () { sheet(false); }); });
  if (side) {
    side.addEventListener('click', function (e) { if (e.target.closest('[data-sx-entry]') && side.classList.contains('is-open')) { sheet(false); } });
  }

  // One document listener for the whole visit: "/" finds a setting, Escape closes the sheet.
  if (!window.__sxKeys) {
    window.__sxKeys = true;
    document.addEventListener('keydown', function (e) {
      var frame = document.querySelector('[data-sx]');
      if (!frame) { return; }
      var open = frame.querySelector('[data-sx-side].is-open');
      if (e.key === 'Escape' && open) { frame.querySelector('[data-sx-close]').click(); return; }
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) { return; }
      var t = e.target, tag = (t && t.tagName) || '';
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (t && t.isContentEditable) || document.querySelector('dialog[open]')) { return; }
      var find = Array.prototype.find.call(frame.querySelectorAll('[data-sx-find]'), function (b) { return b.offsetParent !== null; });
      if (find) { e.preventDefault(); find.focus(); find.select(); }
    });
  }
})();
