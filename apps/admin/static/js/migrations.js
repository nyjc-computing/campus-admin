/* Campus Admin Portal - Migrations view polling (issue #75).
 *
 * While a service section shows an apply in flight (running: true at
 * render), poll its JSON status endpoint; when the run settles,
 * reload the page so the plan and ledger re-render server-side. The
 * ledger is the state machine (pending/applied/failed per revision),
 * so no new realtime channel is needed.
 *
 * Also drives the shared rollback modal: the row buttons carry the
 * service and revision in data attributes; the modal fills from them
 * and adds the second confirmation on submit.
 */

(function () {
    'use strict';

    var POLL_INTERVAL_MS = 3000;

    // ---------- rollback modal ----------

    var rollbackModal = document.getElementById('rollbackModal');
    var rollbackForm = document.getElementById('rollbackForm');
    var rollbackRevision = document.getElementById('rollbackRevision');
    var rollbackTarget = document.getElementById('rollbackTarget');

    function openRollback(button) {
        var service = button.getAttribute('data-service');
        var serviceLabel = button.getAttribute('data-service-label');
        var revision = button.getAttribute('data-revision');
        rollbackForm.action = '/admin/auth/migrations/' + service + '/rollback';
        rollbackRevision.value = revision;
        rollbackTarget.textContent = 'Roll back ' + revision + ' on ' +
            serviceLabel + ' (' + service + '): the revision becomes ' +
            'pending again and its changes are undone.';
    }

    if (rollbackForm) {
        rollbackForm.addEventListener('submit', function (event) {
            // The second confirmation: the modal is the first.
            var message = 'Roll back ' + rollbackRevision.value + ' now? ' +
                'Its changes are undone and it becomes pending again.';
            if (!window.confirm(message)) {
                event.preventDefault();
            }
        });
    }

    if (rollbackModal) {
        rollbackModal.addEventListener('show.bs.modal', function (event) {
            var trigger = event.relatedTarget;
            if (trigger) {
                openRollback(trigger);
            }
        });
    }

    // ---------- polling while an apply is in flight ----------

    function escapeHtml(value) {
        var div = document.createElement('div');
        div.textContent = value === null || value === undefined ? '' : String(value);
        return div.innerHTML;
    }

    function pollSection(section) {
        var service = section.getAttribute('data-service');
        var statusLine = section.querySelector('.migration-run-status');
        var startedAt = Date.now();
        statusLine.hidden = false;

        var timer = window.setInterval(function () {
            fetch('/admin/auth/migrations/' + service, {
                headers: {Accept: 'application/json'}
            }).then(function (response) {
                if (!response.ok) {
                    throw new Error('status ' + response.status);
                }
                return response.json();
            }).then(function (data) {
                if (data.running === true) {
                    var seconds = Math.round((Date.now() - startedAt) / 1000);
                    statusLine.textContent = 'Apply in flight (' + seconds +
                        's elapsed) — polling; the page reloads when it settles.';
                    return;
                }
                // Settled (or the service stopped answering running:
                // true) — one reload re-renders the plan and ledger.
                window.clearInterval(timer);
                window.location.reload();
            }).catch(function () {
                // Transient poll failures keep the loop waiting: the
                // page still reflects the in-flight run.
                statusLine.textContent = 'Apply in flight — polling ' +
                    '(status check unavailable; the page reloads when ' +
                    'it settles).';
            });
        }, POLL_INTERVAL_MS);
    }

    document.querySelectorAll('.migration-service[data-running="true"]')
        .forEach(pollSection);
})();
