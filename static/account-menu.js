/* The account button in the top bar: the signed-in administrator's name and photo. A click opens a
   small menu (Sign out). It closes on a click anywhere else, or on Escape. */
(function () {
    "use strict";
    var account = document.querySelector("[data-account]");
    if (!account) return;
    var button = account.querySelector("[data-account-toggle]");
    var menu = account.querySelector(".ui-account__menu");
    if (!button || !menu) return;

    function setOpen(open) {
        menu.hidden = !open;
        button.setAttribute("aria-expanded", open ? "true" : "false");
    }

    button.addEventListener("click", function (event) {
        event.stopPropagation();
        setOpen(menu.hidden);
    });
    document.addEventListener("click", function (event) {
        if (!account.contains(event.target)) setOpen(false);
    });
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && !menu.hidden) {
            setOpen(false);
            button.focus();
        }
    });
})();
