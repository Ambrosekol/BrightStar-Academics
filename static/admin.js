


















window.portalGoBack = function(fallback) {
    try {
        if (window.history.length > 1) { window.history.back(); return; }
    } catch (e) {}
    window.location.href = fallback;
};

document.addEventListener("DOMContentLoaded", () => {

    





    const prefersReducedMotion = window.matchMedia(
        "(prefers-reduced-motion: reduce)"
    ).matches;


    









    const currentPath = window.location.pathname;

    const navLinks = [...document.querySelectorAll(".nav a")];
    let bestLink = null;
    let bestLength = -1;

    navLinks.forEach((link) => {
        const linkPath = new URL(link.href, window.location.origin).pathname;
        const exact = linkPath === currentPath;
        const parent = linkPath !== "/admin/home" && linkPath !== "/admin" && currentPath.startsWith(linkPath + "/");
        if ((exact || parent) && linkPath.length > bestLength) {
            bestLink = link;
            bestLength = linkPath.length;
        }
    });

    navLinks.forEach((link) => link.classList.remove("active"));
    if (bestLink) bestLink.classList.add("active");


    








    const activeNav = document.querySelector(".nav a.active");

    if (
        activeNav &&
        window.innerWidth <= 650 &&
        !prefersReducedMotion
    ) {
        setTimeout(() => {
            activeNav.scrollIntoView({
                behavior: "smooth",
                block: "nearest",
                inline: "center"
            });
        }, 250);
    }


    














    const metrics = document.querySelectorAll(".metric, .metric-number");

    const animateMetric = (element) => {

const originalText = element.textContent.trim();











const numberMatch = originalText.match(
    /[-+]?\d[\d,]*(?:\.\d+)?/
);

if (!numberMatch) {
    return;
}

const numberText = numberMatch[0];
const target = Number(numberText.replace(/,/g, ""));

if (!Number.isFinite(target)) {
    return;
}

const prefix = originalText.slice(
    0,
    numberMatch.index
);

const suffix = originalText.slice(
    numberMatch.index + numberText.length
);





const decimalPart = numberText.includes(".")
    ? numberText.split(".")[1]
    : "";

const decimalPlaces = decimalPart.length;


        


        if (prefersReducedMotion) {
            return;
        }

        const duration = element.closest(".dashboard-page") ? 1100 : 700;
        const startTime = performance.now();

        element.textContent =
    `${prefix}0${decimalPlaces > 0 ? "." + "0".repeat(decimalPlaces) : ""}${suffix}`;

        const updateCounter = (currentTime) => {

            const elapsed = currentTime - startTime;

            const progress = Math.min(
                elapsed / duration,
                1
            );

            


            const easedProgress =
                1 - Math.pow(1 - progress, 3);

            const currentValue =
    decimalPlaces > 0
        ? Number(
            (target * easedProgress).toFixed(decimalPlaces)
        )
        : Math.round(target * easedProgress);

            element.textContent =
    `${prefix}${currentValue.toLocaleString(
        undefined,
        {
            minimumFractionDigits: decimalPlaces,
            maximumFractionDigits: decimalPlaces
        }
    )}${suffix}`;

            if (progress < 1) {
                requestAnimationFrame(updateCounter);
            }

        };

        requestAnimationFrame(updateCounter);
    };


    




    if (metrics.length && !prefersReducedMotion) {

        setTimeout(() => {

            metrics.forEach((metric) => {
                animateMetric(metric);
            });

        }, 180);
    }


    








    const buttons = document.querySelectorAll(
        ".btn, button"
    );

    buttons.forEach((button) => {

        button.addEventListener("pointerdown", () => {
            button.classList.add("is-pressed");
        });

        button.addEventListener("pointerup", () => {
            button.classList.remove("is-pressed");
        });

        button.addEventListener("pointercancel", () => {
            button.classList.remove("is-pressed");
        });

        button.addEventListener("pointerleave", () => {
            button.classList.remove("is-pressed");
        });

    });


    












    const forms = document.querySelectorAll("form");

    forms.forEach((form) => {

        form.addEventListener("submit", (event) => {

            


            let submitButton = null;

            if (
                event.submitter &&
                (
                    event.submitter.matches(
                        "button[type='submit']"
                    ) ||
                    event.submitter.matches(
                        "input[type='submit']"
                    )
                )
            ) {
                submitButton = event.submitter;
            }

            



            if (!submitButton) {
                submitButton = form.querySelector(
                    "button[type='submit'], input[type='submit']"
                );
            }

            if (!submitButton) {
                return;
            }

            


            if (!submitButton.dataset.originalText) {

                submitButton.dataset.originalText =
                    submitButton.textContent;

            }

            








            submitButton.classList.add(
                "is-loading"
            );

            


            if (
                submitButton.tagName.toLowerCase() ===
                "button"
            ) {
                submitButton.textContent =
                    "Please wait...";
            }

        });

    });


    










    const successMessages =
        document.querySelectorAll(".flash");

    successMessages.forEach((message) => {

        if (prefersReducedMotion) {
            return;
        }

        setTimeout(() => {

            message.classList.add(
                "flash-dismiss"
            );

            setTimeout(() => {

                message.remove();

            }, 350);

        }, 5000);

    });


    








    const tableRows = document.querySelectorAll(
        "tbody tr"
    );

    tableRows.forEach((row) => {

        row.addEventListener("keydown", (event) => {

            if (event.key === "Enter") {

                const firstLink =
                    row.querySelector("a");

                if (firstLink) {
                    firstLink.click();
                }

            }

        });

    });


    










    const links = document.querySelectorAll("a");

    links.forEach((link) => {

        link.addEventListener("click", () => {

            link.classList.add("is-navigating");

        });

    });


    







    document.documentElement.classList.add(
        "admin-js-ready"
    );

});











(function () {
    const nav = document.querySelector('[data-live-messages-nav]');
    if (!nav) return;

    const toast = document.getElementById('live-message-toast');
    const page = document.querySelector('[data-live-message-page]');
    const threadEl = document.querySelector('[data-live-message-thread]');
    const selectedId = page ? (page.dataset.selectedAdmin || '') : '';
    let lastUnread = null;
    let lastThreadSignature = null;
    let toastTimer = null;

    const escapeHtml = (value) => String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');

    const formatStamp = (value) => {
        const text = String(value || '').replace('T', ' ');
        return text.slice(0, 16) + (text.length >= 16 ? ' UTC' : '');
    };

    const updateNav = (data) => {
        const count = Number(data.unread_messages || 0);
        const copy = nav.querySelector('.nav-link-copy');
        if (!copy) return;
        const label = copy.querySelector(':scope > span:first-child');
        if (!label) return;
        const oldBadge = label.querySelector('.nav-badge');
        if (oldBadge) oldBadge.remove();
        if (count > 0) {
            const badge = document.createElement('b');
            badge.className = 'nav-badge';
            badge.textContent = count;
            label.appendChild(badge);
        }
        let sender = copy.querySelector('.nav-message-sender');
        if (count > 0 && data.summaries && data.summaries.length) {
            const names = data.summaries.slice(0, 2).map(x => x.sender_name);
            const extra = data.summaries.length > 2 ? ` +${data.summaries.length - 2}` : '';
            const text = count === 1 ? `New from ${names[0]}` : `From ${names.join(', ')}${extra}`;
            if (!sender) {
                sender = document.createElement('small');
                sender.className = 'nav-message-sender';
                copy.appendChild(sender);
            }
            sender.textContent = text;
            nav.title = count === 1 ? `New message from ${names[0]}` : `${count} unread messages`;
        } else if (sender) {
            sender.remove();
            nav.removeAttribute('title');
        }
    };

    const showToast = (summary) => {
        if (!toast || !summary) return;
        toast.innerHTML = `<span class="live-message-toast-icon">âœ‰</span><span><strong>New message from ${escapeHtml(summary.sender_name)}</strong><small>${escapeHtml(formatStamp(summary.sent_at))}</small></span><a href="/admin/administration/messages?with=${encodeURIComponent(summary.sender_id)}">Open message</a><button type="button" aria-label="Dismiss">Ã—</button>`;
        toast.hidden = false;
        if (toastTimer) clearTimeout(toastTimer);
        const close = toast.querySelector('button');
        if (close) close.addEventListener('click', () => { toast.hidden = true; });
        toastTimer = setTimeout(() => { toast.hidden = true; }, 7000);
    };

    const updateThread = (messages) => {
        if (!threadEl || !Array.isArray(messages)) return;

        const signature = messages.map(m => {
            return `${m.id}:${m.sent_at}:${m.attachment_path || ''}`;
        }).join('|');

        if (signature === lastThreadSignature) return;
        lastThreadSignature = signature;

        if (!messages.length) {
            threadEl.innerHTML =
                '<div class="empty-state">No messages yet. Start the conversation below.</div>';
            return;
        }

        threadEl.innerHTML = messages.map(m => {
            const mine =
                String(m.sender_admin_id) ===
                String(window.BRIGHTSTARS_CURRENT_ADMIN_ID || '');

            const body = escapeHtml(m.body || '').replace(/\n/g, '<br>');
            let attachment = '';

            if (m.attachment_path) {
                const attachmentUrl =
                    `/admin/administration/messages/attachment/${encodeURIComponent(m.id)}`;

                const attachmentName =
                    escapeHtml(m.attachment_name || 'Attachment');

                if (m.attachment_type === 'image') {
                    attachment =
                        `<a class="message-attachment message-attachment-image" ` +
                        `href="${attachmentUrl}" target="_blank" rel="noopener">` +
                        `<img src="${attachmentUrl}" alt="${attachmentName}">` +
                        `</a>`;
                } else {
                    const attachmentType =
                        escapeHtml(
                            (m.attachment_type || 'file').replace(/_/g, ' ')
                        );

                    attachment =
                        `<a class="message-attachment" ` +
                        `href="${attachmentUrl}" target="_blank" rel="noopener">` +
                        `<span class="message-attachment-icon">↗</span>` +
                        `<span class="message-attachment-copy">` +
                        `<strong>${attachmentName}</strong>` +
                        `<small>${attachmentType} · Open attachment</small>` +
                        `</span></a>`;
                }
            }

            return (
                `<div class="message-bubble-wrap ${mine ? 'mine' : ''}">` +
                `<div class="message-bubble">` +
                `${body ? `<div class="message-body">${body}</div>` : ''}` +
                attachment +
                `<small>${escapeHtml(formatStamp(m.sent_at))}` +
                `${mine ? ' · You' : ''}</small>` +
                `</div></div>`
            );
        }).join('');

        threadEl.scrollTop = threadEl.scrollHeight;
    };
    const poll = async () => {
        try {
            const query = selectedId ? `?with=${encodeURIComponent(selectedId)}` : '';
            const response = await fetch(`/admin/administration/messages/unread-state${query}`, {
                credentials: 'same-origin',
                cache: 'no-store',
                headers: { 'Accept': 'application/json' }
            });
            if (!response.ok) return;
            const data = await response.json();
            updateNav(data);
            const unread = Number(data.unread_messages || 0);
            if (lastUnread !== null && unread > lastUnread && data.summaries && data.summaries.length) {
                showToast(data.summaries[0]);
            }
            lastUnread = unread;
            if (page && data.thread) updateThread(data.thread);
        } catch (_) {
            // Network interruptions on a LAN should not disturb the rest of the UI.
        }
    };

    // Establish the baseline immediately, then keep the UI fresh every 3 seconds.
    poll();
    window.setInterval(poll, 3000);
})();

