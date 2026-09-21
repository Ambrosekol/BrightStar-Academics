// Search and status filter for the schools table. Purely a view over rows the
// server already rendered; nothing here changes what anyone is allowed to see.
(function () {
  var rows = Array.prototype.slice.call(document.querySelectorAll('#schools tbody tr'));
  var box = document.getElementById('school-search');
  var chips = Array.prototype.slice.call(document.querySelectorAll('.chip'));
  var none = document.getElementById('no-match');
  if (!rows.length || !box) { return; }
  var status = 'all';

  function apply() {
    var needle = box.value.trim().toLowerCase(), shown = 0;
    rows.forEach(function (row) {
      var ok = (status === 'all' || row.getAttribute('data-status') === status) &&
               (!needle || row.getAttribute('data-search').indexOf(needle) !== -1);
      row.hidden = !ok;
      if (ok) { shown += 1; }
    });
    none.hidden = shown !== 0;
  }

  box.addEventListener('input', apply);
  chips.forEach(function (chip) {
    chip.addEventListener('click', function () {
      status = chip.getAttribute('data-status');
      chips.forEach(function (c) { c.setAttribute('aria-pressed', c === chip ? 'true' : 'false'); });
      apply();
    });
  });
})();
