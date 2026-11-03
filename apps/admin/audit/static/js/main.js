/* Campus Audit Web UI - shared helpers (issue #429) */

/**
 * Escape text for safe insertion via innerHTML.
 */
function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value === null || value === undefined ? '' : String(value);
    return div.innerHTML;
}

/**
 * Map an HTTP status code to a badge CSS class.
 */
function statusBadgeClass(statusCode) {
    if (statusCode === null || statusCode === undefined) {
        return 'pending';
    }
    if (statusCode < 300) {
        return 'ok';
    }
    if (statusCode < 400) {
        return 'redirect';
    }
    if (statusCode < 500) {
        return 'client-error';
    }
    return 'server-error';
}

/**
 * Render an HTTP status code as a badge element.
 */
function renderStatusBadge(statusCode) {
    const label = statusCode === null || statusCode === undefined ? '—' : String(statusCode);
    return `<span class="status-badge ${statusBadgeClass(statusCode)}">${label}</span>`;
}

/**
 * Format a duration in milliseconds for display.
 */
function formatDuration(durationMs) {
    if (durationMs === null || durationMs === undefined) {
        return '—';
    }
    if (durationMs >= 1000) {
        return `${(durationMs / 1000).toFixed(2)} s`;
    }
    return `${Number(durationMs).toFixed(1)} ms`;
}

/**
 * Parse an ISO 8601 timestamp and format it in the browser's local
 * timezone as YYYY-MM-DD HH:MM:SS (docs/web-ui-requirements.md §7.1).
 * Values that fail to parse are shown as-is rather than "Invalid Date".
 */
function formatTimestamp(value) {
    if (value === null || value === undefined || value === '') {
        return '—';
    }
    const date = new Date(value);
    if (isNaN(date.getTime())) {
        return String(value);
    }
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
        `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/**
 * Render the journey meta line for a trace row (#803): a small link
 * above the trace id pointing at the group-by-journey view
 * (/audit/traces?journey_id=…). Named journeys (#840) show their
 * yapper-style label; unnamed ones fall back to the id.
 */
function renderJourneyLine(journeyId, journeyName) {
    if (!journeyId) {
        return '';
    }
    const href = `/audit/traces?journey_id=${encodeURIComponent(journeyId)}`;
    const label = journeyName || journeyId;
    return `<div class="cell-journey"><a class="journey-link" href="${href}" title="${escapeHtml(journeyId)}">${escapeHtml(label)}</a></div>`;
}

/**
 * Truncated display form of a trace id (#823): first 12 chars plus an
 * ellipsis keeps rows dense; the full id stays in the tooltip and the
 * detail link.
 */
const TRACE_ID_DISPLAY_CHARS = 12;

function renderTraceIdLabel(traceId) {
    if (!traceId || traceId.length <= TRACE_ID_DISPLAY_CHARS + 1) {
        return traceId || '';
    }
    return traceId.slice(0, TRACE_ID_DISPLAY_CHARS) + '…';
}

/**
 * Render one trace-summary row (shared by the traces list and the
 * group-by-journey view). Expects the summary resource shape:
 * {trace_id, started_at, duration_ms, span_count, root_span}.
 * opts.journeyView drops the per-row journey line — the group header
 * already names the journey.
 */
function renderTraceRow(summary, opts) {
    const options = opts || {};
    const root = summary.root_span || {};
    const detailHref = `/audit/traces/${encodeURIComponent(summary.trace_id)}`;
    const client = root.client_name || root.client_id || '—';
    const user = root.user_id || '—';
    const journeyId = (root.tags && root.tags.journey_id) || '';
    const journeyName = (root.tags && root.tags.journey_name) || '';
    const journeyLine = options.journeyView
        ? ''
        : renderJourneyLine(journeyId, journeyName);
    return `
        <tr>
            <td class="trace-id">${journeyLine}<a href="${detailHref}" title="${escapeHtml(summary.trace_id)}">${escapeHtml(renderTraceIdLabel(summary.trace_id))}</a></td>
            <td title="${escapeHtml(summary.started_at || '')}">${escapeHtml(formatTimestamp(summary.started_at))}</td>
            <td>${escapeHtml(root.method || '—')}</td>
            <td class="wrap">${escapeHtml(root.path || '—')}</td>
            <td>${renderStatusBadge(root.status_code)}</td>
            <td>${escapeHtml(formatDuration(summary.duration_ms))}</td>
            <td>${escapeHtml(summary.span_count)}</td>
            <td title="${escapeHtml(root.client_id || '')}">${escapeHtml(client)}</td>
            <td title="${escapeHtml(root.user_id || '')}">${escapeHtml(user)}</td>
        </tr>`;
}

/**
 * Fetch JSON from the audit API and return the parsed body.
 * Throws an Error with a readable message on non-2xx responses.
 */
async function fetchJson(url) {
    const response = await fetch(url, {headers: {'Accept': 'application/json'}});
    if (!response.ok) {
        throw new Error(`Request failed (${response.status} ${response.statusText})`);
    }
    return response.json();
}
