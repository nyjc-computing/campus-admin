/* Campus Audit Web UI - trace detail page: metadata, waterfall, span drawer (issue #429) */

(function () {
    'use strict';

    const detailRoot = document.getElementById('trace-detail');
    const statusRegion = document.getElementById('trace-status');
    const ruler = document.getElementById('waterfall-ruler');
    const rowsRoot = document.getElementById('waterfall-rows');
    const backdrop = document.getElementById('drawer-backdrop');
    const drawer = document.getElementById('span-drawer');
    const drawerBody = document.getElementById('drawer-body');

    // UI data endpoints (served in-process). The versioned API requires an
    // API key the browser cannot hold, so it must not be called from here.
    const TRACE_URL = '/audit/api/traces';
    // Header values never shown in the drawer, whatever was captured.
    const REDACTED_HEADERS = ['authorization', 'cookie', 'set-cookie', 'x-api-key'];

    let traceId = null;
    let spans = [];       // flattened, waterfall-ordered
    const spanCache = new Map();
    // Waterfall grouping state: collapsed holds span ids whose subtree
    // is hidden; rowById maps span id to its rendered row.
    const collapsed = new Set();
    let rowById = new Map();
    let groupSeq = 0;

    // ---------- rendering helpers ----------

    function setStatus(message, isError) {
        statusRegion.innerHTML = message
            ? `<span class="${isError ? 'error' : ''}">${escapeHtml(message)}</span>`
            : '';
    }

    function formatTimestamp(iso) {
        const parsed = new Date(iso);
        if (iso === null || iso === undefined || isNaN(parsed.getTime())) {
            return escapeHtml(iso || '—');
        }
        const pad = (n) => String(n).padStart(2, '0');
        return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ` +
            `${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`;
    }

    function toEpochMs(iso) {
        const parsed = new Date(iso);
        return isNaN(parsed.getTime()) ? null : parsed.getTime();
    }

    function renderKVTable(map, redact) {
        const names = Object.keys(map || {}).sort();
        if (names.length === 0) {
            return '<p class="drawer-empty">None</p>';
        }
        const rows = names.map((name) => {
            const value = redact && REDACTED_HEADERS.includes(name.toLowerCase())
                ? '***' : map[name];
            return `<tr><th>${escapeHtml(name)}</th><td class="mono wrap">${escapeHtml(value)}</td></tr>`;
        }).join('');
        return `<table class="kv-table"><tbody>${rows}</tbody></table>`;
    }

    function renderBody(body) {
        if (body === null || body === undefined || body === '') {
            return '<p class="drawer-empty">No body</p>';
        }
        let text;
        if (typeof body === 'string') {
            text = body;
        } else {
            text = JSON.stringify(body, null, 2);
        }
        let notice = '';
        if (typeof body === 'object' && body !== null && body._truncated !== undefined) {
            notice = `<p class="truncated-note">Response body was truncated (original size: ${escapeHtml(body._truncated)}).</p>`;
        }
        return `${notice}<pre class="body-pre">${escapeHtml(text)}</pre>`;
    }

    function drawerSection(title, contentHtml) {
        return `<section class="drawer-section"><h4>${escapeHtml(title)}</h4>${contentHtml}</section>`;
    }

    // ---------- metadata ----------

    function renderMetadata(root) {
        document.getElementById('meta-status').innerHTML = renderStatusBadge(root.status_code);
        document.getElementById('meta-duration').textContent = formatDuration(root.duration_ms);
        document.getElementById('meta-started').textContent = formatTimestamp(root.started_at);
        document.getElementById('meta-request').innerHTML =
            `<span class="wf-method">${escapeHtml(root.method || '—')}</span> ${escapeHtml(root.path || '—')}`;
        document.getElementById('meta-client').textContent = root.client_name || root.client_id || '—';
        document.getElementById('meta-client').title = root.client_id || '';
        document.getElementById('meta-user').textContent = root.user_id || '—';
        document.getElementById('meta-device').textContent = root.device_id || '—';
        document.getElementById('meta-user-agent').textContent = root.user_agent || '—';
    }

    // ---------- waterfall ----------

    /**
     * Flatten the trace tree depth-first into `spans`, computing each
     * span's absolute offset from the root's start. Prefers recorded
     * start timestamps; falls back to the model's parent-relative
     * offsets when timestamps are missing or unparseable. Each entry
     * also carries its ancestor chain, a stripe group (spans under the
     * same top-level request share one), and its subtree size — the
     * state the collapse affordances need.
     */
    function buildEntries(node, parentAbsMs, parentRelOffset, ancestors, groupId) {
        const relOffset = parentRelOffset + Number(node.offset || 0);
        const startMs = toEpochMs(node.started_at);
        const absOffsetMs = startMs !== null && parentAbsMs !== null
            ? Math.max(0, startMs - parentAbsMs)
            : relOffset;
        const entry = {
            spanId: node.span_id,
            method: node.method,
            path: node.path,
            statusCode: node.status_code,
            durationMs: Number(node.duration_ms || 0),
            offsetMs: absOffsetMs,
            depth: node.depth || 0,
            error: node.error_message,
            ancestors: ancestors,
            groupId: groupId,
            childCount: 0,
        };
        spans.push(entry);
        const childAncestors = ancestors.concat(node.span_id);
        let subtree = 0;
        for (const child of node.children || []) {
            // Each direct child of the root starts a new stripe, so the
            // waterfall reads as grouped requests rather than one flat
            // list; deeper spans inherit their request's stripe.
            const childGroup = entry.depth === 0 ? ++groupSeq : groupId;
            subtree += buildEntries(child, parentAbsMs, relOffset, childAncestors, childGroup);
        }
        entry.childCount = subtree;
        return subtree + 1;
    }

    function applyVisibility() {
        for (const entry of spans) {
            const row = rowById.get(entry.spanId);
            if (!row) {
                continue;
            }
            row.hidden = entry.ancestors.some((id) => collapsed.has(id));
            const caret = row.querySelector('.wf-caret');
            if (caret) {
                const isCollapsed = collapsed.has(entry.spanId);
                caret.setAttribute('aria-expanded', String(!isCollapsed));
                caret.textContent = isCollapsed ? '\u25B8' : '\u25BE';
            }
        }
    }

    function toggleSubtree(spanId) {
        if (collapsed.has(spanId)) {
            collapsed.delete(spanId);
        } else {
            collapsed.add(spanId);
        }
        applyVisibility();
    }

    function renderWaterfall(root) {
        const rootStartMs = toEpochMs(root.started_at);
        spans = [];
        groupSeq = 0;
        buildEntries(root, rootStartMs, 0, [], 0);

        const totalMs = Math.max(
            1,
            ...spans.map((s) => s.offsetMs + s.durationMs),
        );
        renderRuler(totalMs);

        rowsRoot.innerHTML = spans.map((span) => {
            const left = (span.offsetMs / totalMs) * 100;
            const width = Math.max((span.durationMs / totalMs) * 100, 0.5);
            const indent = `padding-left: ${span.depth * 16}px;`;
            const barStyle = `left: ${left.toFixed(3)}%; width: ${width.toFixed(3)}%;`;
            const status = span.statusCode === null || span.statusCode === undefined
                ? '—' : String(span.statusCode);
            const stripe = span.groupId % 2 === 1 ? ' group-alt' : '';
            // The caret toggles the subtree; leaves get a spacer so the
            // method labels stay aligned down the rows.
            const caret = span.childCount > 0
                ? `<button type="button" class="wf-caret" aria-expanded="true" data-span-id="${escapeHtml(span.spanId)}" title="Toggle ${span.childCount} nested span(s)">&#9662;</button>`
                : '<span class="wf-caret-spacer"></span>';
            const count = span.childCount > 0
                ? `<span class="wf-child-count" title="Nested spans">+${span.childCount}</span>`
                : '';
            return `
                <div class="wf-row${stripe}" role="button" tabindex="0" data-span-id="${escapeHtml(span.spanId)}"
                     title="${escapeHtml(formatDuration(span.durationMs))} at +${escapeHtml(formatDuration(span.offsetMs))}">
                    <div class="wf-label" style="${indent}">${caret}<span class="wf-method">${escapeHtml(span.method)}</span>
                        <span class="wf-path wrap">${escapeHtml(span.path)}</span>${count}</div>
                    <div class="wf-track">
                        <div class="wf-bar ${statusBadgeClass(span.statusCode)}" style="${barStyle}"></div>
                    </div>
                    <div class="wf-start" title="Start offset from the trace's first request">+${escapeHtml(formatDuration(span.offsetMs))}</div>
                    <div class="wf-time" title="Duration">${escapeHtml(formatDuration(span.durationMs))}</div>
                    <div class="wf-status">${escapeHtml(status)}</div>
                </div>`;
        }).join('');

        rowById = new Map();
        for (const row of rowsRoot.querySelectorAll('.wf-row')) {
            rowById.set(row.dataset.spanId, row);
            row.addEventListener('click', () => openDrawer(row.dataset.spanId));
            row.addEventListener('keydown', (event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    openDrawer(row.dataset.spanId);
                }
            });
        }
        for (const caret of rowsRoot.querySelectorAll('.wf-caret')) {
            caret.addEventListener('click', (event) => {
                event.stopPropagation();
                toggleSubtree(caret.dataset.spanId);
            });
        }
        collapsed.clear();
        applyVisibility();
    }

    function renderRuler(totalMs) {
        const ticks = 5;
        const marks = [];
        for (let i = 0; i < ticks; i++) {
            const frac = i / (ticks - 1);
            const label = i === ticks - 1 ? formatDuration(totalMs) : formatDuration(totalMs * frac);
            marks.push(`<span class="ruler-mark" style="left: ${(frac * 100).toFixed(3)}%">${escapeHtml(label)}</span>`);
        }
        ruler.innerHTML = `<div class="ruler-spacer"></div><div class="ruler-track">${marks.join('')}</div><div class="ruler-start"></div><div class="ruler-time"></div><div class="ruler-status"></div>`;
    }

    // ---------- span details drawer ----------

    function showDrawer() {
        backdrop.hidden = false;
        drawer.hidden = false;
        document.body.classList.add('drawer-open');
        document.getElementById('drawer-close').focus();
    }

    function hideDrawer() {
        backdrop.hidden = true;
        drawer.hidden = true;
        document.body.classList.remove('drawer-open');
    }

    async function openDrawer(spanId) {
        showDrawer();
        drawerBody.innerHTML = '<p class="drawer-empty">Loading span…</p>';
        let span;
        try {
            if (spanCache.has(spanId)) {
                span = spanCache.get(spanId);
            } else {
                span = await fetchJson(`${TRACE_URL}/${encodeURIComponent(traceId)}/spans/${encodeURIComponent(spanId)}`);
                spanCache.set(spanId, span);
            }
        } catch (err) {
            drawerBody.innerHTML = `<p class="error">Failed to load span: ${escapeHtml(err.message)}</p>`;
            return;
        }

        const entry = spans.find((s) => s.spanId === spanId) || {};
        const offsetLabel = entry.offsetMs !== undefined
            ? ` <span class="drawer-offset">(started at +${escapeHtml(formatDuration(entry.offsetMs))})</span>` : '';
        const summary = `
            <p><span class="wf-method">${escapeHtml(span.method)}</span> <span class="wrap">${escapeHtml(span.path)}</span></p>
            <p>${renderStatusBadge(span.status_code)} ${escapeHtml(formatDuration(span.duration_ms))}${offsetLabel}</p>
            <p class="mono wrap copy-line">${escapeHtml(span.span_id)}
                <button type="button" class="copy-btn" data-copy="${escapeHtml(span.span_id)}">Copy</button></p>
            <p class="drawer-empty">${formatTimestamp(span.started_at)}</p>`;

        const failed = span.status_code !== null && span.status_code >= 400;
        const errorSection = (span.error_message || failed)
            ? drawerSection('Error details', `<pre class="body-pre error-pre">${escapeHtml(span.error_message || 'HTTP ' + span.status_code)}</pre>`)
            : '';

        drawerBody.innerHTML = [
            drawerSection('Span summary', summary),
            drawerSection('Request headers', renderKVTable(span.request_headers, true)),
            drawerSection('Request body', renderBody(span.request_body)),
            drawerSection('Response headers', renderKVTable(span.response_headers, true)),
            drawerSection('Response body', renderBody(span.response_body)),
            errorSection,
        ].join('');
    }

    // ---------- copy buttons ----------

    function copyText(text, button) {
        const done = () => {
            const original = button.textContent;
            button.textContent = 'Copied!';
            window.setTimeout(() => { button.textContent = original; }, 1200);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done);
            return;
        }
        const scratch = document.createElement('textarea');
        scratch.value = text;
        document.body.appendChild(scratch);
        scratch.select();
        document.execCommand('copy');
        scratch.remove();
        done();
    }

    // ---------- boot ----------

    /**
     * The tree resource may predate the identity fields (older spans,
     * cached data), so also fill the client/user/device/user-agent
     * metadata from the root span's full record once it arrives.
     */
    async function loadIdentity(rootSpanId) {
        try {
            const span = await fetchJson(
                `${TRACE_URL}/${encodeURIComponent(traceId)}/spans/${encodeURIComponent(rootSpanId)}`
            );
            document.getElementById('meta-client').textContent = span.client_name || span.client_id || '—';
            document.getElementById('meta-client').title = span.client_id || '';
            document.getElementById('meta-user').textContent = span.user_id || '—';
            document.getElementById('meta-device').textContent =
                (span.tags && span.tags.device_id) || span.device_id || '—';
            document.getElementById('meta-user-agent').textContent = span.user_agent || '—';
        } catch {
            // Keep the em-dash placeholders on failure.
        }
    }

    async function loadTrace() {
        traceId = detailRoot.dataset.traceId;
        setStatus('Loading trace…', false);
        try {
            const data = await fetchJson(`${TRACE_URL}/${encodeURIComponent(traceId)}`);
            setStatus('', false);
            detailRoot.hidden = false;
            renderMetadata(data.root_span);
            renderWaterfall(data.root_span);
            loadIdentity(data.root_span.span_id);
        } catch (err) {
            setStatus(err.message.includes('404')
                ? 'Trace not found.'
                : `Failed to load trace: ${err.message}`, true);
        }
    }

    document.getElementById('copy-trace-id').addEventListener('click', (event) => {
        copyText(event.currentTarget.dataset.copy, event.currentTarget);
    });

    drawerBody.addEventListener('click', (event) => {
        const button = event.target.closest('.copy-btn');
        if (button) {
            copyText(button.dataset.copy, button);
        }
    });

    document.getElementById('drawer-close').addEventListener('click', hideDrawer);
    backdrop.addEventListener('click', hideDrawer);
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && !drawer.hidden) {
            hideDrawer();
        }
    });

    document.getElementById('wf-collapse-all').addEventListener('click', () => {
        collapsed.clear();
        for (const entry of spans) {
            if (entry.childCount > 0) {
                collapsed.add(entry.spanId);
            }
        }
        applyVisibility();
    });
    document.getElementById('wf-expand-all').addEventListener('click', () => {
        collapsed.clear();
        applyVisibility();
    });

    loadTrace();
})();
