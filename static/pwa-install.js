/* "Install <school>": a small bar at the bottom centre of the portal offering to install it as an app (blueprints/pwa.py).

   Chrome, Edge and Android say when the portal can be installed (beforeinstallprompt); the bar then offers Install,
   which opens the browser's own install dialog. Safari on an iPhone or iPad has no such event, so there the bar says how:
   Share, then Add to Home Screen. Nothing shows inside the installed app itself, and once the bar is closed with its x it
   stays away for two weeks on this device (local storage; a private window simply shows it again). */
(function () {
  'use strict';
  var me = document.currentScript;
  var name = (me && me.getAttribute('data-name')) || 'this portal';
  var icon = me && me.getAttribute('data-icon');
  var KEY = 'bs-install-closed', WAIT = 14 * 24 * 3600 * 1000;

  var standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  if (standalone) return;
  try { var closed = parseInt(window.localStorage.getItem(KEY) || '0', 10); if (closed && Date.now() - closed < WAIT) return; } catch (e) {}

  var deferred = null, bar = null;
  var ios = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  var safari = /safari/i.test(navigator.userAgent) && !/crios|fxios|edgios/i.test(navigator.userAgent);

  function close(remember) {
    if (!bar) return;
    bar.classList.remove('is-shown');
    var gone = bar; bar = null;
    setTimeout(function () { gone.remove(); }, 250);
    if (remember) { try { window.localStorage.setItem(KEY, String(Date.now())); } catch (e) {} }
  }

  function show(howTo) {
    if (bar) return;
    bar = document.createElement('div');
    bar.className = 'pwa-bar';
    bar.setAttribute('role', 'dialog');
    bar.setAttribute('aria-label', 'Install ' + name);
    var pic = document.createElement('img');
    pic.className = 'pwa-bar__ico'; pic.alt = ''; if (icon) pic.src = icon;
    var copy = document.createElement('div');
    copy.className = 'pwa-bar__copy';
    var title = document.createElement('strong'); title.textContent = 'Install ' + name;
    var text = document.createElement('span');
    text.textContent = howTo ? 'Tap Share, then Add to Home Screen, to open it like an app.' : 'Open it like an app, from your home screen or desktop.';
    copy.appendChild(title); copy.appendChild(text);
    bar.appendChild(pic); bar.appendChild(copy);
    if (!howTo) {
      var go = document.createElement('button');
      go.type = 'button'; go.className = 'pwa-bar__go'; go.textContent = 'Install';
      go.addEventListener('click', function () {
        if (!deferred) return close(false);
        deferred.prompt();
        deferred.userChoice.then(function (choice) { close(choice && choice.outcome !== 'accepted'); }).catch(function () { close(false); });
        deferred = null;
      });
      bar.appendChild(go);
    }
    var x = document.createElement('button');
    x.type = 'button'; x.className = 'pwa-bar__x'; x.setAttribute('aria-label', 'Close'); x.title = 'Not now';
    x.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>';
    x.addEventListener('click', function () { close(true); });
    bar.appendChild(x);
    document.body.appendChild(bar);
    requestAnimationFrame(function () { requestAnimationFrame(function () { if (bar) bar.classList.add('is-shown'); }); });
    document.addEventListener('keydown', function esc(e) { if (e.key === 'Escape' && bar) { close(true); document.removeEventListener('keydown', esc); } });
  }

  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault();
    deferred = e;
    setTimeout(function () { show(false); }, 1500);
  });
  window.addEventListener('appinstalled', function () { close(true); });
  if (ios && safari) {
    window.addEventListener('load', function () { setTimeout(function () { show(true); }, 1500); });
  }
})();
