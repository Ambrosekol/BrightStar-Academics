// Live preview for the brand-colour pickers, in the platform console and in a
// school's own admin area.
//
// Shows the chosen colours on a miniature of a school's portal, and warns when a
// colour is too light for the white text that sits on it. The rule is the same
// one the server enforces (core/theme.py), so a warning here is what a rejected
// save would say; the server remains the authority.
(function () {
  var preview = document.querySelector('.preview[data-min-contrast]');
  if (!preview) { return; }
  var primary = document.getElementById('school_brand_primary'),
      accent = document.getElementById('school_brand_accent'),
      nameInput = document.getElementById('name'),
      bar = document.getElementById('preview-bar'),
      button = document.getElementById('preview-button'),
      label = document.getElementById('preview-name'),
      warning = document.getElementById('colour-warning'),
      minContrast = parseFloat(preview.getAttribute('data-min-contrast'));

  // WCAG relative luminance of a #rrggbb colour against white.
  function contrast(hex) {
    var c = [1, 3, 5].map(function (i) {
      var v = parseInt(hex.substr(i, 2), 16) / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 1.05 / (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2] + 0.05);
  }

  function paint() {
    bar.style.background = primary.value;
    button.style.background = accent.value;
    label.textContent = (nameInput && nameInput.value) || preview.getAttribute('data-name') || 'Your school';
    var tooLight = [['main', primary], ['accent', accent]].filter(function (pair) {
      return contrast(pair[1].value) < minContrast;
    }).map(function (pair) { return pair[0]; });
    warning.hidden = tooLight.length === 0;
    warning.textContent = tooLight.length
      ? 'The ' + tooLight.join(' and ') + ' colour is too light for white text. Choose a darker shade.'
      : '';
  }

  [primary, accent, nameInput].forEach(function (el) {
    if (el) { el.addEventListener('input', paint); }
  });
  paint();
})();
