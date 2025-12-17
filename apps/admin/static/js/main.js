/**
 * Campus Admin Portal - Main JavaScript
 */

document.addEventListener('DOMContentLoaded', function() {
    // Sidebar toggle for mobile
    const sidebarToggle = document.getElementById('sidebarToggle');
    const sidebar = document.getElementById('dashboardSidebar');

    if (sidebarToggle && sidebar) {
        sidebarToggle.addEventListener('click', function() {
            sidebar.classList.toggle('show');
        });

        // Close sidebar when clicking outside on mobile
        document.addEventListener('click', function(e) {
            if (window.innerWidth <= 768) {
                if (!sidebar.contains(e.target) && !sidebarToggle.contains(e.target)) {
                    sidebar.classList.remove('show');
                }
            }
        });
    }

    // Resource navigation
    const sidebarLinks = document.querySelectorAll('.sidebar-link:not(.disabled)[href^="#"]');
    const resourceViews = document.querySelectorAll('.resource-view');

    sidebarLinks.forEach(link => {
        link.addEventListener('click', function(e) {
            e.preventDefault();

            const resourceName = this.getAttribute('data-resource');

            // Update active sidebar link
            sidebarLinks.forEach(l => l.classList.remove('active'));
            this.classList.add('active');

            // Show corresponding resource view
            resourceViews.forEach(view => view.classList.remove('active'));
            const targetView = document.getElementById(resourceName + '-view');
            if (targetView) {
                targetView.classList.add('active');
            }

            // Close sidebar on mobile after selection
            if (window.innerWidth <= 768 && sidebar) {
                sidebar.classList.remove('show');
            }

            // Update URL hash
            window.location.hash = resourceName;
        });
    });

    // Prevent navigation for disabled links
    document.querySelectorAll('.sidebar-link.disabled').forEach(link => {
        link.addEventListener('click', function(e) {
            e.preventDefault();
        });
    });

    // Restore view from URL hash on page load
    if (window.location.hash) {
        const hash = window.location.hash.substring(1);
        const link = document.querySelector(`[data-resource="${hash}"]`);
        if (link && !link.classList.contains('disabled')) {
            link.click();
        }
    }

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
