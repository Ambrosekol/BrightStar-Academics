// The numbering editor on the platform console: clickable placeholder chips, a live preview and
// "Reset to default". The preview asks the server (which reads the pattern, never runs it); every
// result is drawn with textContent, so nothing typed or returned can become markup or script.
(function () {
  var root = document.querySelector('[data-numbering]');
  if (!root) { return; }

  var inputs = {
    candidate: document.getElementById('numbering-candidate'),
    student: document.getElementById('numbering-student'),
    first: document.getElementById('numbering-first')
  };
  var results = {
    candidate: document.getElementById('numbering-candidate-result'),
    student: document.getElementById('numbering-student-result'),
    first: document.getElementById('numbering-first-result')
  };
  var nameBox = document.getElementById('name');   // only on the create-school form
  var codeBox = document.getElementById('code');
  var chips = Array.prototype.slice.call(root.querySelectorAll('.np-chip'));
  var target = 'candidate';                          // the pattern the chips insert into
  var timer = null, serial = 0, controller = null;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  // ---- chips
  function refreshChips() {
    chips.forEach(function (chip) {
      var ok = chip.getAttribute('data-' + target) === '1';
      chip.setAttribute('aria-disabled', ok ? 'false' : 'true');
    });
  }
  function insert(text) {
    var box = inputs[target];
    var start = box.selectionStart === null ? box.value.length : box.selectionStart;
    var end = box.selectionEnd === null ? start : box.selectionEnd;
    box.value = box.value.slice(0, start) + text + box.value.slice(end);
    var caret = start + text.length;
    box.focus();
    box.setSelectionRange(caret, caret);
    schedule();
  }
  chips.forEach(function (chip) {
    // Keep the caret in the text box when a chip is pressed.
    chip.addEventListener('mousedown', function (event) { event.preventDefault(); });
    chip.addEventListener('click', function () {
      if (chip.getAttribute('aria-disabled') === 'true') { return; }
      insert(chip.getAttribute('data-token'));
    });
  });
  ['candidate', 'student'].forEach(function (kind) {
    inputs[kind].addEventListener('focus', function () { target = kind; refreshChips(); });
  });

  // ---- drawing a result
  function showProblem(box, input, result, pattern) {
    input.setAttribute('aria-invalid', 'true');
    var wrap = el('div', 'np-error');
    wrap.appendChild(el('p', '', result.error));
    if (pattern !== null && result.position) {
      var pre = el('pre');
      var length = Math.max(1, result.length || 1);
      pre.appendChild(document.createTextNode(pattern + '\n' + ' '.repeat(result.position - 1)));
      pre.appendChild(el('span', 'np-caret', '^'.repeat(Math.min(length, Math.max(1, pattern.length - result.position + 1)))));
      wrap.appendChild(pre);
    }
    box.replaceChildren(wrap);
  }
  function showSamples(box, input, result) {
    input.removeAttribute('aria-invalid');
    var wrap = el('div', 'np-samples');
    (result.samples || []).forEach(function (sample) { wrap.appendChild(el('code', 'np-sample', sample)); });
    if (result.note) { wrap.appendChild(el('span', 'np-note', result.note)); }
    box.replaceChildren(wrap);
  }
  function draw(data) {
    ['candidate', 'student'].forEach(function (kind) {
      var result = data[kind];
      if (!result) { return; }
      if (result.ok) {
        showSamples(results[kind], inputs[kind], result);
      } else {
        showProblem(results[kind], inputs[kind], result, inputs[kind].value);
      }
    });
    var first = data.first_number;
    if (first && !first.ok) {
      showProblem(results.first, inputs.first, first, null);
    } else {
      inputs.first.removeAttribute('aria-invalid');
      results.first.replaceChildren();
    }
    if (data.unreadable) {
      var note = el('p', 'np-status', data.unreadable);
      results.candidate.appendChild(note);
    }
  }
  function say(text) {
    results.candidate.replaceChildren(el('span', 'np-status', text));
  }

  // ---- asking the server (it only reads; nothing is saved by a preview)
  function ask() {
    var mine = ++serial;
    if (controller) { controller.abort(); }
    controller = window.AbortController ? new AbortController() : null;
    var body = new URLSearchParams();
    body.set('candidate_pattern', inputs.candidate.value);
    body.set('student_pattern', inputs.student.value);
    body.set('first_number', inputs.first.value);
    body.set('slug', root.getAttribute('data-slug') || '');
    body.set('name', nameBox ? nameBox.value : '');
    body.set('code', codeBox ? codeBox.value : '');
    fetch(root.getAttribute('data-preview-url'), {
      method: 'POST', credentials: 'same-origin', body: body,
      headers: { 'X-CSRF-Token': root.getAttribute('data-csrf'), 'Accept': 'application/json' },
      signal: controller ? controller.signal : undefined
    }).then(function (response) {
      if (!response.ok) { throw new Error(response.status === 403 ? 'expired' : 'failed'); }
      return response.json();
    }).then(function (data) {
      if (mine === serial) { draw(data); }
    }).catch(function (error) {
      if (error && error.name === 'AbortError') { return; }
      if (mine !== serial) { return; }
      say(error && error.message === 'expired'
        ? 'Your console session has expired. Reload the page and sign in again.'
        : 'The preview could not be loaded. The pattern is checked again when you save.');
    });
  }
  function schedule() {
    window.clearTimeout(timer);
    timer = window.setTimeout(ask, 220);
  }

  // ---- reset
  var reset = document.getElementById('numbering-reset');
  if (reset) {
    reset.addEventListener('click', function () {
      inputs.candidate.value = root.getAttribute('data-default-candidate');
      inputs.student.value = root.getAttribute('data-default-student');
      inputs.first.value = root.getAttribute('data-default-first');
      inputs.candidate.focus();
      schedule();
    });
  }

  ['candidate', 'student', 'first'].forEach(function (kind) {
    inputs[kind].addEventListener('input', schedule);
  });
  [nameBox, codeBox].forEach(function (box) { if (box) { box.addEventListener('input', schedule); } });
  refreshChips();
  ask();
})();
