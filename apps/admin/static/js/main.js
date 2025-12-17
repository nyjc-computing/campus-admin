/**
 * Campus Admin Portal - Main JavaScript
 */

document.addEventListener('DOMContentLoaded', function() {
    // Add loading spinner to forms on submit
    document.querySelectorAll('form').forEach(form => {
        form.addEventListener('submit', function(e) {
            const btn = this.querySelector('button[type="submit"]');
            if (btn && !btn.disabled) {
                btn.disabled = true;
                const originalText = btn.innerHTML;
                btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Loading...';

                // Re-enable after 10 seconds as a fallback
                setTimeout(() => {
                    btn.disabled = false;
                    btn.innerHTML = originalText;
                }, 10000);
            }
        });
    });

    // Auto-dismiss alerts after 5 seconds
    setTimeout(() => {
        document.querySelectorAll('.alert:not(.alert-permanent)').forEach(alert => {
            const bsAlert = new bootstrap.Alert(alert);
            bsAlert.close();
        });
    }, 5000);
});
