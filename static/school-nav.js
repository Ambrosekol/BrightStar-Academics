/* The school navigation rail: opening and closing, the pin, the phone drawer, tooltips.

   The page works without any of this. Without scripting the rail opens on hover and focus (CSS) and
   on a phone it is an ordinary block above the page. This file only adds the refinements:

     - it opens after a short hover, at once on keyboard focus, and stays open when pinned;
     - the pin is remembered in localStorage (inside try/catch: a blocked or full store changes nothing);
     - on a phone the rail is a drawer opened by the menu button, closed by Escape, the backdrop or a link;
     - a small tooltip names each icon while the rail is narrow;
     - the current page is marked the way admin.js always marked it (the nearest matching link);
     - a count of unread messages (which admin.js keeps up to date) shows as a dot while the rail is narrow.
*/
(function () {
    'use strict';

    var root = document.documentElement;
    var rail = document.getElementById('school-rail');
    if (!rail) { return; }

    var STORE_KEY = 'bs-rail';
    var HOVER_DELAY = 320;     /* ms of hovering before the rail opens: brushing past does not open it */
    var LEAVE_DELAY = 160;
    var phone = window.matchMedia('(max-width: 768px)');

    function pinned() { return root.getAttribute('data-rail') === 'pinned'; }

    /* ---- the current page ------------------------------------------------------------------ */
    (function markCurrent() {
        var links = [].slice.call(rail.querySelectorAll('a.rail-link[href]'));
        // The menu already names the current page for every section it knows (Academics covers several
        // addresses, for one); the address is only a fallback for a page it does not list.
        if (rail.querySelector('a.rail-link.active')) { return; }
        var here = window.location.pathname;
        var best = null;
        var bestLength = -1;
        links.forEach(function (link) {
            var path;
            try { path = new URL(link.href, window.location.origin).pathname; } catch (e) { return; }
            var exact = path === here;
            var parent = path !== '/admin/home' && path !== '/admin' && here.indexOf(path + '/') === 0;
            if ((exact || parent) && path.length > bestLength) { best = link; bestLength = path.length; }
        });
        links.forEach(function (link) { link.classList.remove('active'); link.removeAttribute('aria-current'); });
        if (best) { best.classList.add('active'); best.setAttribute('aria-current', 'page'); }
    })();

    /* ---- opening and closing on a wide screen ----------------------------------------------- */
    var hoverTimer = null;
    var leaveTimer = null;

    function open() { clearTimeout(leaveTimer); hideTip(); rail.classList.add('is-open'); }
    function close() {
        clearTimeout(hoverTimer);
        if (rail.contains(document.activeElement) && document.activeElement.matches(':focus-visible')) { return; }
        rail.classList.remove('is-open');
    }

    rail.addEventListener('mouseenter', function (event) {
        if (phone.matches || pinned() || event.buttons) { return; }
        clearTimeout(leaveTimer);
        hoverTimer = setTimeout(open, HOVER_DELAY);
    });
    rail.addEventListener('mouseleave', function () {
        clearTimeout(hoverTimer);
        hideTip();
        leaveTimer = setTimeout(close, LEAVE_DELAY);
    });
    rail.addEventListener('focusin', function (event) {
        if (phone.matches) { return; }
        var target = event.target;
        var byKeyboard = true;
        try { byKeyboard = target.matches(':focus-visible'); } catch (e) { /* older browsers: treat as keyboard */ }
        if (byKeyboard) { open(); }
    });
    rail.addEventListener('focusout', function (event) {
        if (phone.matches) { return; }
        if (event.relatedTarget && rail.contains(event.relatedTarget)) { return; }
        leaveTimer = setTimeout(function () {
            if (!rail.matches(':hover')) { rail.classList.remove('is-open'); }
        }, 0);
    });
    document.addEventListener('keydown', function (event) {
        if (event.key !== 'Escape') { return; }
        if (rail.classList.contains('is-drawer-open')) { closeDrawer(true); return; }
        if (rail.classList.contains('is-open') && !pinned()) {
            rail.classList.remove('is-open');
            hideTip();
        }
    });

    /* ---- the pin ---------------------------------------------------------------------------- */
    var pin = document.querySelector('[data-rail-pin]');
    if (pin) {
        pin.setAttribute('aria-pressed', pinned() ? 'true' : 'false');
        pin.addEventListener('click', function () {
            var next = !pinned();
            if (next) { root.setAttribute('data-rail', 'pinned'); } else { root.removeAttribute('data-rail'); }
            pin.setAttribute('aria-pressed', next ? 'true' : 'false');
            try {
                if (next) { window.localStorage.setItem(STORE_KEY, 'pinned'); } else { window.localStorage.removeItem(STORE_KEY); }
            } catch (e) { /* private window or blocked storage: the pin still works for this page */ }
            rail.classList.remove('is-open');
            hideTip();
        });
    }

    /* ---- tooltips while the rail is narrow --------------------------------------------------- */
    var tip = document.createElement('div');
    tip.className = 'rail-tip';
    tip.setAttribute('aria-hidden', 'true');
    document.body.appendChild(tip);

    function hideTip() { tip.classList.remove('is-shown'); }
    function showTip(link) {
        if (phone.matches || pinned() || rail.classList.contains('is-open')) { return; }
        if (link.hasAttribute('title')) { return; }             /* it already has its own (unread messages) */
        var text = link.getAttribute('data-tip');
        if (!text) { return; }
        var box = link.getBoundingClientRect();
        tip.textContent = text;
        tip.style.left = Math.round(box.right + 10) + 'px';
        tip.style.top = Math.round(box.top + (box.height - 30) / 2) + 'px';
        tip.classList.add('is-shown');
    }
    [].slice.call(rail.querySelectorAll('.rail-link[data-tip]')).forEach(function (link) {
        link.addEventListener('mouseenter', function () { showTip(link); });
        link.addEventListener('mouseleave', hideTip);
    });
    rail.addEventListener('scroll', hideTip, true);

    /* ---- unread counts as a dot on the icon ---------------------------------------------------- */
    [].slice.call(rail.querySelectorAll('[data-live-messages-nav]')).forEach(function (link) {
        function sync() {
            if (link.querySelector('.nav-badge')) { link.setAttribute('data-unread', ''); } else { link.removeAttribute('data-unread'); }
        }
        sync();
        if (window.MutationObserver) {
            new MutationObserver(sync).observe(link, { childList: true, subtree: true });
        }
    });

    /* ---- the drawer on a phone --------------------------------------------------------------- */
    var menuButton = document.querySelector('[data-rail-menu]');
    var scrim = document.querySelector('[data-rail-scrim]');
    var main = document.getElementById('main');

    function setInert(on) {
        if (!main) { return; }
        try { main.inert = !!on; } catch (e) { /* not supported: the backdrop still covers the page */ }
    }
    function openDrawer() {
        rail.classList.add('is-drawer-open');
        if (scrim) { scrim.hidden = false; }
        if (menuButton) { menuButton.setAttribute('aria-expanded', 'true'); }
        root.classList.add('rail-locked');
        setInert(true);
        var first = rail.querySelector('.rail-nav a.rail-link');
        if (first) { first.focus(); }
    }
    function closeDrawer(returnFocus) {
        rail.classList.remove('is-drawer-open');
        if (scrim) { scrim.hidden = true; }
        if (menuButton) { menuButton.setAttribute('aria-expanded', 'false'); }
        root.classList.remove('rail-locked');
        setInert(false);
        if (returnFocus && menuButton) { menuButton.focus(); }
    }
    if (menuButton) {
        menuButton.addEventListener('click', function () {
            if (rail.classList.contains('is-drawer-open')) { closeDrawer(true); } else { openDrawer(); }
        });
    }
    if (scrim) { scrim.addEventListener('click', function () { closeDrawer(true); }); }
    rail.addEventListener('click', function (event) {
        var link = event.target.closest ? event.target.closest('a[href]') : null;
        if (link && rail.classList.contains('is-drawer-open')) { closeDrawer(false); }
    });
    function onViewportChange() {
        if (!phone.matches && rail.classList.contains('is-drawer-open')) { closeDrawer(false); }
        rail.classList.remove('is-open');
    }
    if (phone.addEventListener) { phone.addEventListener('change', onViewportChange); }
    else if (phone.addListener) { phone.addListener(onViewportChange); }
})();
