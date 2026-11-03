/* Campus Audit Web UI - trace list page (issues #429, #698, #803).
 *
 * One URL-driven view for both modes:
 * - /audit/traces                → all traces, newest first (cursor pagination)
 * - /audit/traces?journey_id=X   → group-by-journey view: a journey header
 *   card with its member traces beneath, oldest first
 *
 * Filters live in the URL: the form syncs from URL params on load and
 * back/forward, and Apply/Clear push the form state into the URL.
 */

(function () {
    'use strict';

    const form = document.getElementById('trace-filters');
    const tableBody = document.getElementById('trace-table-body');
    const statusRegion = document.getElementById('list-status');
    const loadMoreBtn = document.getElementById('load-more');
    const journeyHeader = document.getElementById('journey-header');
    const journeyCardId = document.getElementById('journey-card-id');
    const journeyCardMeta = document.getElementById('journey-card-meta');
    const journeyWaterfall = document.getElementById('journey-waterfall');
    const journeyWaterfallRuler = document.getElementById('journey-waterfall-ruler');
    const journeyWaterfallRows = document.getElementById('journey-waterfall-rows');

    // UI data endpoints (served in-process). The versioned API requires
    // an API key the browser cannot hold, so it must not be called here.
    const LIST_URL = '/audit/api/traces';
    const JOURNEY_URL = '/audit/api/journeys';

    const JOURNEY_PARAM = 'journey_id';

    // Cursor pagination state: token of the last loaded page. Reset on
    // any fresh (filter-driven) load. Unused in journey view — members
    // arrive as one unpaginated payload.
    const state = {
        cursor: null,
        hasMore: false,
    };

    // ---------- URL <-> form ----------

    function readUrlParams() {
        return new URLSearchParams(window.location.search);
    }

    function journeyIdFromUrl() {
        return (readUrlParams().get(JOURNEY_PARAM) || '').trim();
    }

    function syncFormFromUrl() {
        const params = readUrlParams();
        for (const field of form.elements) {
            if (!field.name) {
                continue;
            }
            const value = params.get(field.name);
            if (field.type === 'checkbox') {
                // Ticked or not, not a value (#941).
                field.checked = value !== null && value !== '0' && value !== 'false';
                continue;
            }
            if (value !== null) {
                field.value = value;
            } else if (field.tagName !== 'SELECT') {
                // Absent param = cleared field; selects keep their
                // default option instead of falling off the option list.
                field.value = '';
            }
        }
    }

    function syncUrlFromForm() {
        const params = new URLSearchParams();
        for (const field of form.elements) {
            if (field.name && field.value) {
                params.set(field.name, field.value);
            }
        }
        const query = params.toString();
        window.history.pushState(
            null,
            '',
            window.location.pathname + (query ? `?${query}` : '')
        );
    }

    // ---------- rendering ----------

    function setStatus(message, isError) {
        statusRegion.innerHTML = message
            ? `<span class="${isError ? 'error' : ''}">${escapeHtml(message)}</span>`
            : '';
    }

    function updateLoadMore() {
        loadMoreBtn.hidden = !state.hasMore;
        loadMoreBtn.disabled = false;
        loadMoreBtn.textContent = 'Load more';
    }

    function renderJourneyHeader(journeyId, traces) {
        journeyHeader.hidden = false;
        // Named journeys (#840): the yapper-style label attached by
        // @flask_campus.journey rides member spans' tags; the raw id
        // stays visible as the card's secondary line.
        const named = traces.find(
            (t) => t.root_span && t.root_span.tags &&
                t.root_span.tags.journey_name
        );
        journeyCardId.textContent =
            (named && named.root_span.tags.journey_name) || journeyId;
        if (named) {
            journeyCardId.title = journeyId;
        }
        const totalMs = traces.reduce(
            (sum, trace) => sum + (Number(trace.duration_ms) || 0),
            0
        );
        const first = traces.length ? traces[0].started_at : null;
        const last = traces.length ? traces[traces.length - 1].started_at : null;
        const span = first && last
            ? `${formatTimestamp(first)} → ${formatTimestamp(last)}`
            : '';
        journeyCardMeta.textContent =
            `${traces.length} trace${traces.length === 1 ? '' : 's'}` +
            (span ? ` · ${span}` : '') +
            (traces.length ? ` · total ${formatDuration(totalMs)}` : '');
    }

    // ---------- journey waterfall ----------

    /**
     * One bar per member trace on the journey's shared timeline:
     * positioned by each trace's started_at, sized by its duration.
     * Rows link to the trace detail page. Mirrors the trace-detail
     * waterfall's grid (same CSS classes); the journey timescale is
     * first-trace-start → last-trace-end with a 1 ms floor.
     */
    function toEpochMs(iso) {
        const parsed = new Date(iso);
        return isNaN(parsed.getTime()) ? null : parsed.getTime();
    }

    function renderJourneyRuler(totalMs) {
        const ticks = 5;
        const marks = [];
        for (let i = 0; i < ticks; i++) {
            const frac = i / (ticks - 1);
            const label = i === ticks - 1 ? formatDuration(totalMs) : formatDuration(totalMs * frac);
            marks.push(`<span class="ruler-mark" style="left: ${(frac * 100).toFixed(3)}%">${escapeHtml(label)}</span>`);
        }
        journeyWaterfallRuler.innerHTML =
            `<div class="ruler-spacer"></div><div class="ruler-track">${marks.join('')}</div>` +
            '<div class="ruler-start"></div><div class="ruler-time"></div><div class="ruler-status"></div>';
    }

    function renderJourneyWaterfall(traces) {
        const starts = traces.map((t) => toEpochMs(t.started_at));
        const known = starts.filter((ms) => ms !== null);
        if (known.length === 0) {
            journeyWaterfall.hidden = true;
            return;
        }
        const startMs = Math.min(...known);
        const endMs = Math.max(
            ...traces.map((t, i) => {
                const start = starts[i];
                return start === null ? startMs : start + (Number(t.duration_ms) || 0);
            })
        );
        const totalMs = Math.max(1, endMs - startMs);
        renderJourneyRuler(totalMs);

        journeyWaterfallRows.innerHTML = traces.map((trace, i) => {
            const start = starts[i];
            const offsetMs = start === null ? 0 : Math.max(0, start - startMs);
            const durationMs = Number(trace.duration_ms) || 0;
            const root = trace.root_span || {};
            const left = (offsetMs / totalMs) * 100;
            const width = Math.max((durationMs / totalMs) * 100, 0.5);
            const barStyle = `left: ${left.toFixed(3)}%; width: ${width.toFixed(3)}%;`;
            const status = root.status_code === null || root.status_code === undefined
                ? '—' : String(root.status_code);
            const detailHref = `/audit/traces/${encodeURIComponent(trace.trace_id)}`;
            const spans = trace.span_count
                ? `<span class="wf-child-count" title="Spans in this trace">${escapeHtml(trace.span_count)}</span>`
                : '';
            return `
                <a class="wf-row" href="${detailHref}"
                   title="${escapeHtml(formatDuration(durationMs))} at +${escapeHtml(formatDuration(offsetMs))} — open trace">
                    <div class="wf-label"><span class="wf-method">${escapeHtml(root.method || '—')}</span>
                        <span class="wf-path wrap">${escapeHtml(root.path || '—')}</span>${spans}</div>
                    <div class="wf-track">
                        <div class="wf-bar ${statusBadgeClass(root.status_code)}" style="${barStyle}"></div>
                    </div>
                    <div class="wf-start" title="Start offset from the journey's first trace">+${escapeHtml(formatDuration(offsetMs))}</div>
                    <div class="wf-time" title="Duration">${escapeHtml(formatDuration(durationMs))}</div>
                    <div class="wf-status">${escapeHtml(status)}</div>
                </a>`;
        }).join('');
        journeyWaterfall.hidden = false;
    }

    // ---------- data ----------

    function readFilters() {
        const params = new URLSearchParams();
        const fields = form.elements;
        for (const field of fields) {
            if (!field.name || !field.value) {
                continue;
            }
            // A checkbox's value is truthy whether or not it is ticked,
            // so its state has to be read from .checked (#941).
            if (field.type === 'checkbox') {
                if (field.checked) {
                    params.set(field.name, '1');
                }
                continue;
            }
            params.set(field.name, field.value);
        }
        return params;
    }

    async function loadNextPage() {
        try {
            const params = readFilters();
            if (state.cursor) {
                params.set('cursor', state.cursor);
            }
            const data = await fetchJson(`${LIST_URL}?${params.toString()}`);
            const traces = data.traces || [];
            tableBody.insertAdjacentHTML('beforeend', traces.map(renderTraceRow).join(''));
            state.cursor = (data.cursor && data.cursor.next) || null;
            state.hasMore = Boolean(data.cursor && data.cursor.has_more && state.cursor);
            const total = tableBody.rows.length;
            if (total === 0) {
                setStatus('No traces match the current filters.', false);
            } else {
                setStatus(`Showing ${total} trace${total === 1 ? '' : 's'}.`, false);
            }
        } catch (err) {
            state.cursor = null;
            state.hasMore = false;
            setStatus(`Failed to load traces: ${err.message}`, true);
        }
        updateLoadMore();
    }

    async function loadJourney(journeyId) {
        try {
            setStatus('Loading journey…', false);
            const data = await fetchJson(`${JOURNEY_URL}/${encodeURIComponent(journeyId)}`);
            const traces = data.traces || [];
            renderJourneyHeader(journeyId, traces);
            renderJourneyWaterfall(traces);
            tableBody.innerHTML = traces
                .map((trace) => renderTraceRow(trace, {journeyView: true}))
                .join('');
            state.cursor = null;
            state.hasMore = false;
            if (traces.length === 0) {
                setStatus('No traces found for this journey.', false);
            } else {
                setStatus(
                    `Showing ${traces.length} trace${traces.length === 1 ? '' : 's'} in this journey, oldest first.`,
                    false
                );
            }
        } catch (err) {
            tableBody.innerHTML = '';
            journeyHeader.hidden = true;
            journeyWaterfall.hidden = true;
            state.cursor = null;
            state.hasMore = false;
            setStatus(`Failed to load journey: ${err.message}`, true);
        }
        updateLoadMore();
    }

    function setJourneyMode(active) {
        form.classList.toggle('journey-mode', active);
        if (!active) {
            journeyHeader.hidden = true;
            journeyWaterfall.hidden = true;
        }
    }

    function loadTraces() {
        tableBody.innerHTML = '';
        state.cursor = null;
        state.hasMore = false;
        loadMoreBtn.hidden = true;
        const journeyId = journeyIdFromUrl();
        setJourneyMode(Boolean(journeyId));
        if (journeyId) {
            loadJourney(journeyId);
        } else {
            setStatus('Loading traces…', false);
            loadNextPage();
        }
    }

    // ---------- events ----------

    loadMoreBtn.addEventListener('click', () => {
        loadMoreBtn.disabled = true;
        loadMoreBtn.textContent = 'Loading…';
        loadNextPage();
    });

    form.addEventListener('submit', (event) => {
        event.preventDefault();
        syncUrlFromForm();
        loadTraces();
    });

    form.addEventListener('reset', () => {
        // Let the browser clear the fields, then reflect the cleared
        // state in the URL and reload (Clear also leaves journey view).
        window.setTimeout(() => {
            syncUrlFromForm();
            loadTraces();
        }, 0);
    });

    window.addEventListener('popstate', () => {
        // Back/forward moves between filtered and journey views
        syncFormFromUrl();
        loadTraces();
    });

    syncFormFromUrl();
    loadTraces();
})();
