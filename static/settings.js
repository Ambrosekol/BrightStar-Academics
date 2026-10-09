/* Settings page: filter the rows as you type, and mark the section you are reading in the index. */
(function () {
  'use strict';
  var app = document.getElementById('set-app');
  if (!app) { return; }
  var filter = document.getElementById('set-filter');
  var groups = [].slice.call(app.querySelectorAll('.set-group'));
  var links = [].slice.call(app.querySelectorAll('.set-nav a'));

  filter.addEventListener('input', function () {
    var needle = filter.value.trim().toLowerCase(), any = false;
    groups.forEach(function (g) {
      var shown = 0;
      g.querySelectorAll('li[data-find]').forEach(function (li) {
        var hit = !needle || li.dataset.find.indexOf(needle) !== -1;
        li.hidden = !hit; if (hit) { shown++; }
      });
      g.hidden = shown === 0; if (shown) { any = true; }
      var link = app.querySelector('.set-nav a[data-group="' + g.id.replace('set-', '') + '"]');
      if (link) { link.parentNode.hidden = shown === 0; }
    });
    document.getElementById('set-none').hidden = any;
    document.getElementById('set-none-q').textContent = filter.value.trim();
  });

  if ('IntersectionObserver' in window) {
    var seen = {};
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { seen[e.target.id] = e.isIntersecting; });
      var first = groups.filter(function (g) { return !g.hidden && seen[g.id]; })[0];
      if (!first) { return; }
      links.forEach(function (a) { a.classList.toggle('is-on', a.getAttribute('href') === '#' + first.id); });
    }, { rootMargin: '-10% 0px -70% 0px' });
    groups.forEach(function (g) { io.observe(g); });
  }
})();
