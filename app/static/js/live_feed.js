// Live view of /c/microblogs: new posts arrive without a reload, the way Mastodon's Live Feeds does.
// Spec: docs/superpowers/specs/2026-10-07-microblog-live-feed-design.md
//
// Every browser dependency is passed in, so tests/js/live_feed.test.mjs drives all of it under node
// with fakes. Only the bootstrap at the bottom touches real globals.

export const TOP_THRESHOLD_PX = 100;
export const MAX_TEASERS = 200;
export const POLL_MS = 15000;
export const SAFETY_POLL_MS = 60000;    // with SSE: Redis pub/sub is not durable, so a wake-up can be lost
export const COALESCE_MS = 5000;
export const MAX_BACKOFF_MS = 120000;
export const SSE_ERROR_LIMIT = 3;
export const HIGHLIGHT_MS = 2000;
// scripts.js setups that are safe to re-run over the whole page: some skip what they already
// bound; setupVotableElements and setupVideoSpoilers re-bind their listeners on every call, but
// the effect is idempotent. setupDynamicContent needs no call: scripts.js's
// MutationObserver runs it for inserted nodes.
export const TEASER_SETUPS = ['setupTeaserClick', 'setupPostTeaserHandler', 'setupBlurredPostImages',
                              'setupVideoSpoilers', 'setupVotableElements'];

export function shouldHold(scrollY) {
    return scrollY > TOP_THRESHOLD_PX;
}

export function nextBackoff(ms) {
    return Math.min(ms * 2, MAX_BACKOFF_MS);
}

// The nodes of `incoming` whose id is neither in `seenIds` nor earlier in `incoming`.
export function dedupeNodes(seenIds, incoming) {
    const ids = new Set(seenIds);
    return incoming.filter(node => {
        if (!node.id || ids.has(node.id)) return false;
        ids.add(node.id);
        return true;
    });
}

export function trimCount(length) {
    return Math.max(0, length - MAX_TEASERS);
}

export function createLiveFeed({ list, pill, status, fetch, EventSource, document, window, setTimeout,
                                 clearTimeout, now, parse, config }) {
    let cursor = config.cursor;
    let held = [];
    let interval = POLL_MS;
    let source = null;
    let sseErrors = 0;
    let sseFailed = false;
    let timer = null;
    let coalesceTimer = null;
    let lastFetch = -Infinity;
    let inFlight = false;
    let paused = false;
    let stopped = false;

    function setStatus(live) {
        status.textContent = live ? config.strings.live : config.strings.paused;
        status.classList.toggle('text-bg-success', live);
        status.classList.toggle('text-bg-secondary', !live);
    }

    function schedule(ms) {
        clearTimeout(timer);
        timer = setTimeout(fetchNow, ms);
    }

    function initTeasers(nodes) {
        for (const node of nodes) window.htmx?.process(node);
        const setups = config.lowBandwidth ? TEASER_SETUPS : TEASER_SETUPS.concat('setupLightboxTeaser');
        for (const name of setups) window[name]?.();
    }

    function insert(nodes) {
        list.prepend(...nodes);
        for (const node of nodes) {
            node.classList.add('bg-warning-subtle');
            setTimeout(() => node.classList.remove('bg-warning-subtle'), HIGHLIGHT_MS);
        }
        initTeasers(nodes);
        for (let extra = trimCount(list.children.length); extra > 0; extra--) list.lastElementChild.remove();
    }

    function flush() {
        pill.hidden = true;
        if (held.length === 0) return;
        const nodes = held;
        held = [];
        insert(nodes);
    }

    function receive(html) {
        const seen = Array.from(list.children, node => node.id).concat(held.map(node => node.id));
        const nodes = dedupeNodes(seen, parse(html));
        if (nodes.length === 0) return;
        if (shouldHold(window.scrollY)) {
            held = nodes.concat(held).slice(0, MAX_TEASERS);
            pill.textContent = config.strings.newPosts.replace('%d', held.length);
            pill.hidden = false;
        } else {
            insert(nodes);
        }
    }

    async function fetchNow() {
        if (inFlight) return;
        inFlight = true;
        lastFetch = now();
        try {
            const response = await fetch(`${config.postsUrl}?after=${cursor}`, { credentials: 'same-origin' });
            if (response.redirected || response.status === 403 || response.status === 404) {
                stop();           // logged out (login_required redirects) or Live is gone
            } else if (response.status === 200) {
                receive(await response.text());
                cursor = Number(response.headers.get('X-Live-Cursor')) || cursor;
                interval = POLL_MS;
            } else if (response.status === 204) {
                interval = POLL_MS;
            } else {
                interval = nextBackoff(interval);
            }
        } catch (error) {
            interval = nextBackoff(interval);
        } finally {
            inFlight = false;
        }
        if (!paused) schedule(source ? SAFETY_POLL_MS : interval);
    }

    // How long until a fetch is allowed. While backing off (429, errors) a wake-up must not fetch
    // sooner than the backoff allows.
    function fetchWait() {
        const gap = interval > POLL_MS ? interval : COALESCE_MS;
        return Math.max(0, lastFetch + gap - now());
    }

    function requestFetch() {
        if (coalesceTimer !== null) return;
        coalesceTimer = setTimeout(() => { coalesceTimer = null; fetchNow(); }, fetchWait());
    }

    function closeSse() {
        if (source) source.close();
        source = null;
    }

    function openSse() {
        source = new EventSource(config.sseUrl);
        source.onopen = () => { sseErrors = 0; };
        source.onmessage = () => { sseErrors = 0; requestFetch(); };
        source.onerror = () => {
            sseErrors += 1;
            // readyState 2 (CLOSED): the browser gave up for good (non-200 or wrong content type)
            // and will not retry, so waiting for more errors would leave us on the safety poll.
            if (source.readyState === 2 || sseErrors >= SSE_ERROR_LIMIT) {
                sseFailed = true;
                closeSse();
                schedule(interval);
            }
        };
    }

    function pause() {
        paused = true;
        clearTimeout(timer);
        clearTimeout(coalesceTimer);
        coalesceTimer = null;
        closeSse();
        setStatus(false);
    }

    function resume() {
        paused = false;
        setStatus(true);
        if (config.sseUrl && EventSource && !sseFailed) openSse();
        if (fetchWait() === 0) fetchNow();
        else requestFetch();
    }

    function stop() {
        stopped = true;
        pause();
    }

    function start() {
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) pause();
            else if (!stopped) resume();
        });
        window.addEventListener('scroll', () => {
            if (held.length > 0 && !shouldHold(window.scrollY)) flush();
        }, { passive: true });
        pill.addEventListener('click', () => {
            window.scrollTo({ top: 0 });
            flush();
        });
        if (document.hidden) pause();
        else resume();
    }

    return { start };
}

/* node:coverage disable */
if (typeof window !== 'undefined') {
    const list = window.document.getElementById('live_feed');
    if (list) {
        createLiveFeed({
            list,
            pill: window.document.getElementById('live_pill'),
            status: window.document.getElementById('live_status'),
            fetch: window.fetch.bind(window),
            EventSource: window.EventSource,
            document: window.document,
            window,
            setTimeout: window.setTimeout.bind(window),
            clearTimeout: window.clearTimeout.bind(window),
            now: () => Date.now(),
            parse: html => {
                const template = window.document.createElement('template');
                template.innerHTML = html;
                return Array.from(template.content.querySelectorAll(':scope > .post_teaser'));
            },
            config: {
                postsUrl: list.dataset.postsUrl,
                cursor: Number(list.dataset.cursor),
                sseUrl: list.dataset.sseUrl,
                lowBandwidth: window.document.body.classList.contains('low_bandwidth'),
                strings: { live: list.dataset.strLive, paused: list.dataset.strPaused,
                           newPosts: list.dataset.strNewPosts },
            },
        }).start();
    }
}
/* node:coverage enable */
