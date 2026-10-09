/* Report cards page.
 *
 * Choosing a class, session or term fetches that class's students in-page (no reload) and shows a
 * spinner meanwhile. Each student has a Comment and a Traits button that open a small window; saving
 * posts that one student and updates just their row. CSP-safe: no inline handlers, no innerHTML with
 * data (everything is built with textContent). */
(function () {
  'use strict';
  var app = document.getElementById('rc-app');
  if (!app) { return; }

  var $ = function (id) { return document.getElementById(id); };
  var canComment = app.dataset.canComment === '1';
  var csrf = app.dataset.csrf;
  var groups = window.RC_TRAITS || [];
  var scale = window.RC_SCALE || [];
  var selClass = $('rc-class'), selSession = $('rc-session'), selTerm = $('rc-term');
  var state = { data: null, students: [], loadToken: 0, current: null };

  /* ------------------------------------------------------------------ small helpers */
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text !== undefined) { n.textContent = text; }
    return n;
  }
  function show(id) {
    ['rc-idle', 'rc-loading', 'rc-error', 'rc-loaded'].forEach(function (k) { $(k).hidden = (k !== id); });
    $('rc-panel').setAttribute('aria-busy', id === 'rc-loading' ? 'true' : 'false');
  }
  function post(url, fields) {
    var body = new URLSearchParams();
    Object.keys(fields).forEach(function (k) { body.append(k, fields[k]); });
    return fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'X-CSRF-Token': csrf, 'Accept': 'application/json' }, body: body
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok || j.error) { throw new Error(j.error || 'Could not save. Please try again.'); }
        return j;
      });
    });
  }
  function ctx() { return { session_id: selSession.value, term: selTerm.value }; }

  /* ------------------------------------------------------------------ loading the class */
  function updateAddress() {
    var q = new URLSearchParams();
    if (selClass.value) { q.set('class_id', selClass.value); }
    q.set('session_id', selSession.value);
    q.set('term', selTerm.value);
    try { history.replaceState(null, '', location.pathname + '?' + q.toString()); } catch (e) { /* not essential */ }
  }

  function load() {
    updateAddress();
    if (!selClass.value) { state.data = null; show('rc-idle'); return; }
    var token = ++state.loadToken;
    show('rc-loading');
    var q = new URLSearchParams({ class_id: selClass.value, session_id: selSession.value, term: selTerm.value });
    fetch(app.dataset.studentsUrl + '?' + q.toString(), { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) { throw new Error(j.error || 'The server could not list this class.'); }
          return j;
        });
      })
      .then(function (data) {
        if (token !== state.loadToken) { return; }          // a newer choice has taken over
        state.data = data; state.students = data.students;
        render();
        show('rc-loaded');
      })
      .catch(function (err) {
        if (token !== state.loadToken) { return; }
        $('rc-error-text').textContent = err.message || 'Check your connection and try again.';
        show('rc-error');
      });
  }

  /* ------------------------------------------------------------------ drawing the list */
  function renderStats() {
    var d = state.data, box = $('rc-stats'), withComment = 0, rated = 0;
    state.students.forEach(function (s) {
      if (s.comment && s.comment.trim()) { withComment++; }
      if (Object.keys(s.ratings || {}).length) { rated++; }
    });
    box.textContent = '';
    [[d.class.name, null], [d.ready + ' of ' + d.total, ' cards ready'], [withComment + ' of ' + d.total, ' commented'],
     [rated + ' of ' + d.total, ' rated']].forEach(function (p) {
      var chip = el('span', 'rc-stat');
      if (p[1] === null) { chip.appendChild(el('b', '', p[0])); } else { chip.appendChild(el('b', '', p[0])); chip.appendChild(document.createTextNode(p[1])); }
      box.appendChild(chip);
    });
    var pdf = $('rc-class-pdf');
    pdf.hidden = !d.class_pdf_url;
    if (d.class_pdf_url) { pdf.href = d.class_pdf_url; }
  }

  function stateCell(s) {
    var pill = el('span', 'rc-pill ' + s.state);
    pill.textContent = s.state === 'ready' ? 'Ready'
      : s.state === 'waiting' ? 'Waiting for ' + s.pending + ' result' + (s.pending === 1 ? '' : 's')
      : 'No results yet';
    return pill;
  }

  function commentCell(s) {
    var td = el('td'), text = (s.comment || '').trim();
    if (!text) { td.appendChild(el('span', 'rc-none', 'Not written')); return td; }
    var line = el('span', 'rc-cell-text', text.replace(/\s+/g, ' '));
    line.title = text;
    td.appendChild(line);
    if (s.comment_by) { td.appendChild(el('span', 'rc-cell-sub', s.comment_by)); }
    return td;
  }

  function traitsCell(s) {
    var td = el('td'), n = Object.keys(s.ratings || {}).length, total = state.data.trait_total;
    if (!n) { td.appendChild(el('span', 'rc-none', 'Not rated')); return td; }
    var meter = el('span', 'rc-meter'), bar = el('i');
    bar.style.width = Math.round(n / total * 100) + '%';
    meter.appendChild(bar);
    td.appendChild(meter);
    td.appendChild(document.createTextNode(n + ' of ' + total));
    if (s.rated_by) { td.appendChild(el('span', 'rc-cell-sub', s.rated_by)); }
    return td;
  }

  function actionsCell(s) {
    var td = el('td', 'rc-col-end'), box = el('div', 'rc-row-actions');
    function btn(label, fn) {
      var b = el('button', 'rc-btn rc-btn--sm', label); b.type = 'button';
      b.addEventListener('click', fn); box.appendChild(b);
    }
    if (canComment) {
      btn((s.comment || '').trim() ? 'Edit comment' : 'Comment', function () { openComment(s); });
      btn(Object.keys(s.ratings || {}).length ? 'Edit traits' : 'Rate traits', function () { openTraits(s); });
    }
    if (s.view_url) {
      var v = el('a', 'rc-btn rc-btn--sm', 'View'); v.href = s.view_url; box.appendChild(v);
      var p = el('a', 'rc-btn rc-btn--sm', 'PDF'); p.href = s.pdf_url; box.appendChild(p);
    }
    td.appendChild(box);
    return td;
  }

  function rowFor(s) {
    var tr = el('tr'); tr.dataset.id = s.student_id;
    var name = el('td', 'rc-name'); name.appendChild(el('strong', '', s.name));
    if (s.admission_no) { name.appendChild(el('small', '', s.admission_no)); }
    tr.appendChild(name);
    var st = el('td'); st.appendChild(stateCell(s)); tr.appendChild(st);
    tr.appendChild(commentCell(s));
    tr.appendChild(traitsCell(s));
    tr.appendChild(actionsCell(s));
    return tr;
  }

  function renderRows() {
    var body = $('rc-rows'), needle = $('rc-filter').value.trim().toLowerCase(), shown = 0;
    body.textContent = '';
    state.students.forEach(function (s) {
      if (needle && (s.name + ' ' + (s.admission_no || '')).toLowerCase().indexOf(needle) === -1) { return; }
      body.appendChild(rowFor(s)); shown++;
    });
    $('rc-none').hidden = shown > 0;
    $('rc-none').textContent = state.students.length ? 'No student matches.' : 'No student is enrolled in this class for this session.';
  }

  function render() { renderStats(); renderRows(); }

  function refreshRow(s) {
    var old = $('rc-rows').querySelector('tr[data-id="' + s.student_id + '"]');
    if (old) { old.replaceWith(rowFor(s)); }
    renderStats();
  }

  /* ------------------------------------------------------------------ the windows */
  function wireDialog(dlg) {
    if (!dlg) { return; }
    dlg.addEventListener('click', function (e) {
      if (e.target.closest('[data-close]')) { dlg.close(); }
      else if (e.target === dlg) { dlg.close(); }            // a click on the backdrop
    });
  }
  ['rc-comment-modal', 'rc-traits-modal', 'rc-signature-modal'].forEach(function (id) { wireDialog($(id)); });

  function setBusy(button, busy, idle, working) { button.disabled = busy; button.textContent = busy ? working : idle; }
  function showError(id, msg) { var e = $(id); e.textContent = msg || ''; e.hidden = !msg; }

  /* ---- comment ---- */
  function countText() {
    var n = $('rc-comment-text').value.length;
    $('rc-comment-count').textContent = n + ' / ' + app.dataset.maxLength;
  }
  function openComment(s) {
    state.current = s;
    $('rc-comment-title').textContent = 'Comment for ' + s.name;
    $('rc-comment-sub').textContent = selClass.options[selClass.selectedIndex].text + ' · ' + selTerm.value;
    $('rc-comment-text').value = s.comment || '';
    $('rc-comment-by').textContent = s.comment_by ? 'Written by ' + s.comment_by : '';
    $('rc-comment-clear').hidden = !(s.comment || '').trim();
    showError('rc-comment-error', '');
    countText();
    $('rc-comment-modal').showModal();
    $('rc-comment-text').focus();
  }
  function saveComment(text) {
    var s = state.current, btn = $('rc-comment-save');
    setBusy(btn, true, 'Save comment', 'Saving…');
    showError('rc-comment-error', '');
    post(app.dataset.commentUrl, Object.assign(ctx(), { student_id: s.student_id, comment: text }))
      .then(function (r) {
        s.comment = r.comment; s.comment_by = r.comment_by;
        refreshRow(s);
        $('rc-comment-modal').close();
      })
      .catch(function (err) { showError('rc-comment-error', err.message); })
      .then(function () { setBusy(btn, false, 'Save comment', 'Saving…'); });
  }
  $('rc-comment-text').addEventListener('input', countText);
  $('rc-comment-form').addEventListener('submit', function (e) { e.preventDefault(); saveComment($('rc-comment-text').value); });
  $('rc-comment-clear').addEventListener('click', function () { saveComment(''); });

  /* ---- traits ---- */
  var traitBox = $('rc-trait-groups');
  function buildTraitForm() {
    traitBox.textContent = '';
    groups.forEach(function (g) {
      var section = el('section', 'rc-tg');
      section.appendChild(el('h3', '', g.name));
      g.items.forEach(function (t) {
        var row = el('div', 'rc-trait'); row.setAttribute('role', 'radiogroup');
        var label = el('span', 'rc-trait__label', t.label); label.id = 'rc-t-' + t.key;
        row.setAttribute('aria-labelledby', label.id);
        row.appendChild(label);
        var rate = el('div', 'rc-rate');
        scale.forEach(function (sc) {
          var lab = el('label'); lab.title = sc.label;
          var input = document.createElement('input');
          input.type = 'radio'; input.name = 'trait_' + t.key; input.value = sc.value;
          input.setAttribute('aria-label', t.label + ': ' + sc.label);
          lab.appendChild(input); lab.appendChild(el('span', '', String(sc.value)));
          rate.appendChild(lab);
        });
        row.appendChild(rate);
        section.appendChild(row);
      });
      traitBox.appendChild(section);
    });
  }
  function readRatings() {
    var out = {};
    traitBox.querySelectorAll('input:checked').forEach(function (i) { out[i.name] = i.value; });
    return out;
  }
  function openTraits(s) {
    state.current = s;
    $('rc-traits-title').textContent = 'Traits for ' + s.name;
    $('rc-traits-sub').textContent = selClass.options[selClass.selectedIndex].text + ' · ' + selTerm.value;
    traitBox.querySelectorAll('input').forEach(function (i) { i.checked = String(s.ratings && s.ratings[i.name.slice(6)]) === i.value; i.dataset.was = i.checked ? '1' : ''; });
    $('rc-traits-by').textContent = s.rated_by ? 'Rated by ' + s.rated_by : '';
    showError('rc-traits-error', '');
    $('rc-traits-modal').showModal();
  }
  /* a rating that is clicked again is taken back */
  traitBox.addEventListener('click', function (e) {
    var input = e.target.closest('input[type="radio"]');
    if (!input) { return; }
    if (input.dataset.was === '1') { input.checked = false; input.dataset.was = ''; return; }
    traitBox.querySelectorAll('input[name="' + input.name + '"]').forEach(function (i) { i.dataset.was = ''; });
    input.dataset.was = '1';
  });
  function saveTraits(ratings) {
    var s = state.current, btn = $('rc-traits-save');
    setBusy(btn, true, 'Save ratings', 'Saving…');
    showError('rc-traits-error', '');
    post(app.dataset.traitsUrl, Object.assign(ctx(), ratings, { student_id: s.student_id }))
      .then(function (r) {
        s.ratings = r.ratings; s.rated_by = r.rated_by;
        refreshRow(s);
        $('rc-traits-modal').close();
      })
      .catch(function (err) { showError('rc-traits-error', err.message); })
      .then(function () { setBusy(btn, false, 'Save ratings', 'Saving…'); });
  }
  $('rc-traits-form').addEventListener('submit', function (e) { e.preventDefault(); saveTraits(readRatings()); });
  $('rc-traits-clear').addEventListener('click', function () { saveTraits({}); });
  buildTraitForm();

  /* ---- the teacher's signature ---- */
  var sigDialog = $('rc-signature-modal');
  if ($('rc-signature-open') && sigDialog) { $('rc-signature-open').addEventListener('click', function () { sigDialog.showModal(); }); }
  if (sigDialog && app.dataset.openSignature === '1') { sigDialog.showModal(); }

  /* ------------------------------------------------------------------ wiring */
  [selClass, selSession, selTerm].forEach(function (s) { s.addEventListener('change', load); });
  $('rc-retry').addEventListener('click', load);
  $('rc-filter').addEventListener('input', renderRows);
  load();
})();
