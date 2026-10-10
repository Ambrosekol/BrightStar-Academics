/* Settings: "Find a setting" narrows the list on the left (and, on the overview, its cards) as you type.
   The frame is drawn again with each page loaded in place, so this runs again for each; data-ready keeps it
   from binding twice to the same box. */
(function () {
  'use strict';
  var app = document.querySelector('[data-sx]');
  if (!app) { return; }
  var box = app.querySelector('[data-sx-find]');
  if (!box || box.hasAttribute('data-ready')) { return; }
  box.setAttribute('data-ready', '');

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
    var none = app.querySelector('[data-sx-none]');
    if (none) { none.hidden = any; }
  }
  box.addEventListener('input', filter);
  // Enter opens the first setting still showing.
  box.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter') { return; }
    e.preventDefault();
    var first = app.querySelector('.sx-side li:not([hidden]) .sx-link');
    if (first) { first.click(); }
  });
  // The open entry stays in view in a long list.
  var on = app.querySelector('.sx-link.is-on');
  if (on && on.scrollIntoView && window.matchMedia('(max-width: 900px)').matches) {
    on.scrollIntoView({ block: 'nearest', inline: 'center' });
  }
})();
