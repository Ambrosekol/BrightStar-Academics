/*
 * Tells a person, right beside a file box, that the file they picked cannot be used, and why, before
 * they submit anything. Plain JavaScript, no libraries, and nothing that the site's
 * Content-Security-Policy forbids (it is loaded from the site's own address and changes styles only
 * through the DOM, never through markup).
 *
 * A file box opts in with the data attributes the server writes for it (core/uploads.py, upload_attrs):
 *   data-upload-kind            "image" or "attachment"
 *   data-upload-limit           the most one file may be, in bytes
 *   data-upload-limit-label     that limit written for people ("5 MB")
 *   data-upload-request-limit   the most one whole submission may carry, in bytes
 *   data-upload-request-label   that limit written for people
 *   data-upload-types           the extensions that are accepted, comma separated
 *   data-upload-types-label     those types written for people ("PNG, JPG, GIF or WEBP")
 * and the server writes an empty <div data-upload-feedback> under it, which is where the message goes.
 *
 * A file that is too large, or of a kind that is not accepted, is refused: the message appears and
 * the selection is cleared, so it cannot be submitted by accident. A good file shows its name and
 * size. With JavaScript off, the server gives the same explanation after submitting.
 *
 * The sentences are in MESSAGES below; a page in another language can replace any of them by setting
 * window.UPLOAD_LIMIT_MESSAGES before this file loads. {name}, {size}, {limit} and {types} are filled in.
 */
(function () {
  'use strict';

  var MESSAGES = {
    tooLarge: '{name} is {size}. The limit is {limit}. Choose a smaller {thing}, or reduce this one first.',
    wrongTypeImage: '{name} is not a picture we can use. Please choose a {types} image.',
    wrongTypeFile: '{name} cannot be attached. Please choose {types}.',
    tooLargeTogether: 'Together, the files you chose are {size}. One submission can carry at most {limit}. ' +
      'Choose fewer or smaller files.',
    chosen: '{name} ({size}) is ready to upload.',
    chosenMany: '{count} files ({size} in all) are ready to upload.',
    thingImage: 'picture',
    thingFile: 'file'
  };
  var custom = window.UPLOAD_LIMIT_MESSAGES;
  if (custom && typeof custom === 'object') {
    for (var key in custom) {
      if (Object.prototype.hasOwnProperty.call(custom, key)) { MESSAGES[key] = custom[key]; }
    }
  }

  var KB = 1024, MB = 1024 * 1024, GB = 1024 * 1024 * 1024;

  function fill(template, values) {
    return template.replace(/\{(\w+)\}/g, function (whole, name) {
      return Object.prototype.hasOwnProperty.call(values, name) ? values[name] : whole;
    });
  }

  // The same rules as core/uploads.py format_bytes: "820 KB", "5 MB", "7.2 MB".
  function formatBytes(size, decimals, mode) {
    size = Math.max(0, Math.floor(size));
    if (size < KB) { return size + (size === 1 ? ' byte' : ' bytes'); }
    var unit = 'KB', factor = KB;
    if (size >= GB) { unit = 'GB'; factor = GB; } else if (size >= MB) { unit = 'MB'; factor = MB; }
    var scale = Math.pow(10, decimals);
    var raw = size / factor * scale;
    var rounded = mode === 'down' ? Math.floor(raw) : (mode === 'up' ? Math.ceil(raw) : Math.round(raw));
    var text = (rounded / scale).toFixed(decimals);
    if (text.indexOf('.') !== -1) { text = text.replace(/0+$/, '').replace(/\.$/, ''); }
    return text + ' ' + unit;
  }

  function formatSizeOver(size, limit) {
    var text = formatBytes(size, 1, 'nearest');
    return text === formatBytes(limit, 1, 'down') ? formatBytes(size, 2, 'up') : text;
  }

  function shownName(file) {
    var name = String(file.name || '').replace(/[\u0000-\u001f\u007f]/g, '').replace(/\\/g, '/');
    name = name.substring(name.lastIndexOf('/') + 1).replace(/^\s+|\s+$/g, '');
    if (!name) { return 'The file'; }
    return '"' + (name.length <= 60 ? name : name.substring(0, 57).replace(/\s+$/, '') + '...') + '"';
  }

  function extensionOf(file) {
    var name = String(file.name || '').toLowerCase();
    var dot = name.lastIndexOf('.');
    return dot === -1 ? '' : name.substring(dot + 1);
  }

  function isFileBox(node) {
    return node && node.tagName === 'INPUT' && node.type === 'file' && node.hasAttribute('data-upload-limit');
  }

  // The message goes in the box the server placed under the file box, looking outwards a few levels
  // (a file box may sit inside a label or a wrapper); if the page has none, one is added after it.
  var counter = 0;
  function feedbackFor(input) {
    if (input._uploadFeedback) { return input._uploadFeedback; }
    var form = input.form, node = input, box = null, depth = 0;
    while (node && node !== form && depth < 4 && !box) {
      for (var sib = node.nextElementSibling; sib && !box; sib = sib.nextElementSibling) {
        if (sib.hasAttribute && sib.hasAttribute('data-upload-feedback')) { box = sib; }
        else if (sib.querySelector) { box = sib.querySelector('[data-upload-feedback]'); }
        if (sib.querySelector && sib.querySelector('input[type="file"]')) { break; }
      }
      node = node.parentNode;
      depth += 1;
    }
    if (!box) {
      box = document.createElement('div');
      box.setAttribute('data-upload-feedback', '');
      box.setAttribute('aria-live', 'polite');
      input.parentNode.insertBefore(box, input.nextSibling);
    }
    if (!box.id) { counter += 1; box.id = 'upload-feedback-' + counter; }
    input._uploadFeedback = box;
    return box;
  }

  function show(input, text, bad) {
    var box = feedbackFor(input);
    while (box.firstChild) { box.removeChild(box.firstChild); }
    input.removeAttribute('aria-invalid');
    if (!text) { return; }
    var line = document.createElement('div');
    line.setAttribute('role', bad ? 'alert' : 'status');
    line.textContent = text;
    line.style.marginTop = '6px';
    line.style.fontSize = '13px';
    line.style.lineHeight = '1.45';
    line.style.fontWeight = bad ? '600' : '500';
    line.style.color = bad ? '#b42318' : '#166534';
    box.appendChild(line);
    if (bad) { input.setAttribute('aria-invalid', 'true'); }
    var described = (input.getAttribute('aria-describedby') || '').split(/\s+/);
    if (described.indexOf(box.id) === -1) {
      described.push(box.id);
      input.setAttribute('aria-describedby', described.join(' ').replace(/^\s+/, ''));
    }
  }

  function refuse(input, text) {
    input.value = '';
    show(input, text, true);
  }

  function check(input) {
    var files = input.files ? Array.prototype.slice.call(input.files) : [];
    if (!files.length) { show(input, '', false); return; }

    var isImage = input.getAttribute('data-upload-kind') !== 'attachment';
    var limit = parseInt(input.getAttribute('data-upload-limit'), 10) || 0;
    var limitLabel = input.getAttribute('data-upload-limit-label') || formatBytes(limit, 1, 'down');
    var typesLabel = input.getAttribute('data-upload-types-label') || '';
    var types = (input.getAttribute('data-upload-types') || '').split(',').filter(Boolean);
    var thing = isImage ? MESSAGES.thingImage : MESSAGES.thingFile;

    for (var i = 0; i < files.length; i += 1) {
      var file = files[i];
      if (types.length && types.indexOf(extensionOf(file)) === -1) {
        refuse(input, fill(isImage ? MESSAGES.wrongTypeImage : MESSAGES.wrongTypeFile,
                           { name: shownName(file), types: typesLabel }));
        return;
      }
      if (limit && file.size > limit) {
        refuse(input, fill(MESSAGES.tooLarge, {
          name: shownName(file), size: formatSizeOver(file.size, limit), limit: limitLabel, thing: thing
        }));
        return;
      }
    }

    // Everything in the form travels together: check them all against what one submission may carry.
    var requestLimit = parseInt(input.getAttribute('data-upload-request-limit'), 10) || 0;
    if (requestLimit && input.form) {
      var total = 0, boxes = input.form.querySelectorAll('input[type="file"]');
      for (var b = 0; b < boxes.length; b += 1) {
        for (var f = 0; boxes[b].files && f < boxes[b].files.length; f += 1) { total += boxes[b].files[f].size; }
      }
      if (total > requestLimit) {
        refuse(input, fill(MESSAGES.tooLargeTogether, {
          size: formatSizeOver(total, requestLimit),
          limit: input.getAttribute('data-upload-request-label') || formatBytes(requestLimit, 1, 'down')
        }));
        return;
      }
    }

    var sum = 0;
    for (var s = 0; s < files.length; s += 1) { sum += files[s].size; }
    show(input, files.length === 1
      ? fill(MESSAGES.chosen, { name: shownName(files[0]), size: formatBytes(files[0].size, 1, 'nearest') })
      : fill(MESSAGES.chosenMany, { count: files.length, size: formatBytes(sum, 1, 'nearest') }), false);
  }

  // One listener for the whole page, so a file box added later is covered too. It runs before the
  // box's own onchange, which therefore sees the cleared selection when a file was refused.
  document.addEventListener('change', function (event) {
    if (isFileBox(event.target)) { check(event.target); }
  }, true);

  // A form that is reset has no chosen files, so no message about them either.
  document.addEventListener('reset', function (event) {
    var boxes = event.target && event.target.querySelectorAll ? event.target.querySelectorAll('input[type="file"]') : [];
    for (var i = 0; i < boxes.length; i += 1) {
      if (isFileBox(boxes[i])) { show(boxes[i], '', false); }
    }
  }, true);
})();
