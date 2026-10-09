/* Messages page: scroll conversations to the latest message, filter the colleague list, and the
   "message a parent" window with its parent search. CSP-safe: no inline handlers; search results are
   built with textContent. */
(function () {
  'use strict';
  var app = document.getElementById('msg-app');
  if (!app) { return; }
  var $ = function (id) { return document.getElementById(id); };

  /* ---- always start at the latest message ---- */
  [document.getElementById('msg-thread'), document.querySelector('[data-live-message-thread]')].forEach(function (box) {
    if (box) { box.scrollTop = box.scrollHeight; }
  });

  /* ---- colleague list filter ---- */
  var filter = $('msg-contact-filter');
  if (filter) {
    filter.addEventListener('input', function () {
      var needle = filter.value.trim().toLowerCase(), shown = 0;
      document.querySelectorAll('#msg-contacts .msg-item').forEach(function (a) {
        var hit = !needle || (a.dataset.name || '').indexOf(needle) !== -1;
        a.hidden = !hit; if (hit) { shown++; }
      });
      var none = $('msg-contact-none');
      if (none) { none.hidden = shown > 0; }
    });
  }

  /* ---- message a parent ---- */
  var dlg = $('msg-new-modal');
  if (!dlg) { return; }
  var q = $('msg-recipient-q'), list = $('msg-recipient-list'), picked = $('msg-picked');
  var timer = null, token = 0, active = -1, items = [];

  function hideList() { list.hidden = true; q.setAttribute('aria-expanded', 'false'); active = -1; }
  function note(text) {
    list.textContent = '';
    var li = document.createElement('li'); li.className = 'is-note'; li.textContent = text; list.appendChild(li);
    list.hidden = false; q.setAttribute('aria-expanded', 'true');
  }
  function choose(r) {
    $('msg-parent-id').value = r.parent_id;
    $('msg-student-id').value = r.student_id;
    $('msg-picked-text').textContent = r.parent + ' — parent of ' + r.student + (r.class ? ' (' + r.class + ')' : '');
    picked.hidden = false; q.hidden = true; hideList();
    $('msg-subject').focus();
  }
  function unpick() {
    $('msg-parent-id').value = ''; $('msg-student-id').value = '';
    picked.hidden = true; q.hidden = false; q.value = ''; q.focus();
  }
  function show(results) {
    items = results; list.textContent = '';
    if (!results.length) { note('No matching student with a linked parent.'); return; }
    results.forEach(function (r, i) {
      var li = document.createElement('li'); li.setAttribute('role', 'option');
      li.appendChild(document.createTextNode(r.student + (r.class ? ' · ' + r.class : '')));
      var small = document.createElement('small'); small.textContent = 'Parent: ' + r.parent; li.appendChild(small);
      li.addEventListener('mousedown', function (e) { e.preventDefault(); choose(r); });
      list.appendChild(li);
    });
    list.hidden = false; q.setAttribute('aria-expanded', 'true'); active = -1;
  }
  function search() {
    var text = q.value.trim();
    if (text.length < 2) { hideList(); return; }
    var mine = ++token;
    fetch(app.dataset.recipientsUrl + '?q=' + encodeURIComponent(text), { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) { if (!r.ok) { throw new Error(); } return r.json(); })
      .then(function (j) { if (mine === token) { show(j.results || []); } })
      .catch(function () { if (mine === token) { note('The search failed. Try again.'); } });
  }
  function highlight() {
    var rows = list.querySelectorAll('li[role="option"]');
    rows.forEach(function (li, i) { li.setAttribute('aria-selected', i === active ? 'true' : 'false'); });
    if (rows[active]) { rows[active].scrollIntoView({ block: 'nearest' }); }
  }
  q.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(search, 220); });
  q.addEventListener('blur', function () { setTimeout(hideList, 120); });
  q.addEventListener('keydown', function (e) {
    if (list.hidden || !items.length) { return; }
    if (e.key === 'ArrowDown') { active = Math.min(items.length - 1, active + 1); highlight(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { active = Math.max(0, active - 1); highlight(); e.preventDefault(); }
    else if (e.key === 'Enter' && active >= 0) { choose(items[active]); e.preventDefault(); }
    else if (e.key === 'Escape') { hideList(); e.stopPropagation(); }
  });
  $('msg-picked-clear').addEventListener('click', unpick);

  $('msg-new-form').addEventListener('submit', function (e) {
    if (!$('msg-parent-id').value) { e.preventDefault(); q.hidden = false; q.focus(); note("Choose a student's parent from the list first."); return; }
    $('msg-new-send').disabled = true;
  });

  var opener = $('msg-new-open');
  if (opener) { opener.addEventListener('click', function () { dlg.showModal(); q.focus(); }); }
  dlg.addEventListener('click', function (e) {
    if (e.target.closest('[data-close]') || e.target === dlg) { dlg.close(); }
  });
  if (dlg.dataset.open === '1') { dlg.showModal(); }
})();
