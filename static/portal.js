/* The student and parent portals' frame (templates/portal_base.html): the More button on the phone tab bar opens the
   menu, and the parent's bell keeps itself up to date and marks everything read without leaving the page. */
(function () {
  'use strict';
  var more = document.querySelector('[data-pf-more]');
  var menu = document.querySelector('[data-rail-menu]');
  if (more && menu) more.addEventListener('click', function () { menu.click(); });

  var bell = document.getElementById('pp-bell');
  if (!bell) return;
  var badge = bell.querySelector('[data-count]'), list = bell.querySelector('[data-list]'), readAll = bell.querySelector('[data-readall]');
  document.addEventListener('click', function (e) { if (bell.open && !bell.contains(e.target)) bell.open = false; });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && bell.open) { bell.open = false; bell.querySelector('summary').focus(); } });
  function paint(d) {
    badge.textContent = d.unread > 99 ? '99+' : d.unread; badge.hidden = !d.unread; if (readAll) readAll.hidden = !d.unread;
    bell.querySelector('summary').setAttribute('aria-label', 'Notifications' + (d.unread ? ', ' + d.unread + ' new' : ''));
    list.textContent = '';
    if (!d.items.length) {
      var none = document.createElement('li'); none.className = 'pp-bell__none';
      none.textContent = 'Nothing yet. New results, fees and messages from the school will appear here.'; list.appendChild(none); return;
    }
    d.items.forEach(function (n) {
      var li = document.createElement('li'), a = document.createElement('a');
      a.className = 'pp-bell__item' + (n.unread ? ' is-new' : ''); a.href = n.url;
      [['strong', n.title], ['span', n.message], ['time', n.created_at]].forEach(function (p) { var e = document.createElement(p[0]); e.textContent = p[1]; a.appendChild(e); });
      li.appendChild(a); list.appendChild(li);
    });
  }
  function poll() {
    if (document.hidden) return;
    fetch(bell.dataset.stateUrl, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; }).then(function (d) { if (d) paint(d); }).catch(function () {});
  }
  setInterval(poll, 60000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
  if (readAll) readAll.form.addEventListener('submit', function (e) {
    e.preventDefault();
    readAll.classList.add('pp-busy');
    fetch(bell.dataset.readUrl, { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': bell.dataset.csrf, 'Accept': 'application/json' } })
      .then(function (r) { return r.json(); }).then(paint).catch(function () { readAll.form.submit(); })
      .finally(function () { readAll.classList.remove('pp-busy'); });
  });
})();
