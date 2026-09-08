(function () {
    'use strict';

    const auditSectionIds = [
        'yahoo-breakdown',
        'points-breakdown',
        'mamba-breakdown'
    ];

    function scrollAuditTablesToLatestWeek() {
        auditSectionIds.forEach(function (sectionId) {
            const section = document.getElementById(sectionId);
            if (!section) return;

            const scroller = section.querySelector('.table-scroll');
            if (!scroller) return;

            const headers = scroller.querySelectorAll('thead th');
            const latestWeek = headers[headers.length - 1];
            if (!latestWeek || !/^W\d+$/.test(latestWeek.textContent.trim())) return;

            // Align the actual final week with the right edge of the viewport.
            // Do not scroll to the end of a fixed-width table: short histories
            // can otherwise leave their only week hidden behind frozen columns.
            const target = scroller.scrollLeft
                + latestWeek.getBoundingClientRect().right
                - scroller.getBoundingClientRect().right;
            const maximum = Math.max(0, scroller.scrollWidth - scroller.clientWidth);
            scroller.scrollLeft = Math.max(0, Math.min(target, maximum));
        });
    }

    function restoreVerticalPosition() {
        const saved = sessionStorage.getItem('mamba-live-scroll-y');
        if (saved === null) return;
        sessionStorage.removeItem('mamba-live-scroll-y');
        const y = Number(saved);
        if (Number.isFinite(y)) {
            requestAnimationFrame(function () {
                window.scrollTo(0, y);
            });
        }
    }

    function initialize() {
        requestAnimationFrame(scrollAuditTablesToLatestWeek);
        restoreVerticalPosition();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize);
    } else {
        initialize();
    }

    window.addEventListener('load', function () {
        requestAnimationFrame(scrollAuditTablesToLatestWeek);
    });
})();
