/* Parent portal: the child profile's tabs, and the messages page behaving like a chat room.
   Everything works as plain links and forms without this; the script only removes page loads. */
(function () {
  'use strict';

  /* ---------------------------------------------------------------- tabs (child profile) */
  document.querySelectorAll('[data-tabs]').forEach(function (root) {
    var tabs = [].slice.call(root.querySelectorAll('[role="tab"]'));
    var panels = tabs.map(function (t) { return document.getElementById(t.getAttribute('aria-controls')); });

    function show(index, focus) {
      tabs.forEach(function (t, i) {
        var on = i === index;
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
        panels[i].hidden = !on;
      });
      // Changing the session reloads the page: bring the person back to the tab they were on.
      document.querySelectorAll('form[data-keep-tab]').forEach(function (f) { f.action = location.pathname + '#' + panels[index].id; });
      if (focus) { tabs[index].focus(); }
    }
    function fromHash() {
      var id = decodeURIComponent((location.hash || '').slice(1));
      if (!id) { return false; }
      var target = document.getElementById(id);
      var at = panels.findIndex(function (p) { return p && target && (p === target || p.contains(target)); });
      if (at < 0) { return false; }
      show(at, false);
      target.scrollIntoView({ block: 'start' });
      return true;
    }

    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { show(i, false); });
      t.addEventListener('keydown', function (e) {
        var next = e.key === 'ArrowRight' ? (i + 1) % tabs.length : e.key === 'ArrowLeft' ? (i - 1 + tabs.length) % tabs.length
          : e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : -1;
        if (next >= 0) { e.preventDefault(); show(next, true); }
      });
    });
    show(0, false);
    fromHash();
    window.addEventListener('hashchange', fromHash);
  });

  /* ---------------------------------------------------------------- messages */
  var chat = document.getElementById('pp-chat');
  if (!chat) { return; }
  var threadLinks = [].slice.call(chat.querySelectorAll('[data-thread]'));
  var convs = [].slice.call(chat.querySelectorAll('[data-conv]'));

  function toBottom(conv) {
    var feed = conv.querySelector('[data-feed]');
    if (feed) { feed.scrollTop = feed.scrollHeight; }
  }
  function open(id, push) {
    var found = false;
    convs.forEach(function (c) {
      var on = c.dataset.conv === String(id);
      c.hidden = !on;
      if (on) { found = true; toBottom(c); }
    });
    if (!found) { return false; }
    threadLinks.forEach(function (a) { a.classList.toggle('is-on', a.dataset.thread === String(id)); });
    chat.classList.add('is-open');
    if (push) {
      try { history.replaceState(null, '', id === 'new' ? location.pathname : location.pathname + '?c=' + id); } catch (e) { /* not essential */ }
    }
    return true;
  }

  threadLinks.forEach(function (a) {
    a.addEventListener('click', function (e) {
      if (e.metaKey || e.ctrlKey || e.shiftKey) { return; }
      if (open(a.dataset.thread, true)) { e.preventDefault(); }
    });
  });
  chat.querySelectorAll('[data-back]').forEach(function (b) {
    b.addEventListener('click', function () { chat.classList.remove('is-open'); });
  });

  /* Enter sends, Shift+Enter makes a new line; the box grows to fit what is typed. */
  chat.querySelectorAll('textarea[data-enter-send]').forEach(function (box) {
    function fit() { box.style.height = 'auto'; box.style.height = Math.min(box.scrollHeight, 140) + 'px'; }
    box.addEventListener('input', fit);
    box.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' || e.shiftKey || e.isComposing) { return; }
      e.preventDefault();
      if (box.value.trim() && box.form) { box.form.requestSubmit ? box.form.requestSubmit() : box.form.submit(); }
    });
    box.form.addEventListener('submit', function () {
      var btn = box.form.querySelector('button[type="submit"]');
      if (btn) { btn.disabled = true; }
    });
  });

  var start = chat.dataset.start;
  convs.forEach(function (c) { if (!c.hidden) { toBottom(c); } });
  if (start && start !== 'new') { var active = document.getElementById('conv-' + start); if (active) { toBottom(active); } }
})();
