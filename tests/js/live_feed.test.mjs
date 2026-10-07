// Tests of app/static/js/live_feed.js, driven entirely through injected fakes.
// Run: npm run test:js (fails below 100% line, branch or function coverage).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
    createLiveFeed, shouldHold, nextBackoff, dedupeNodes, trimCount,
    POLL_MS, SAFETY_POLL_MS, COALESCE_MS, MAX_BACKOFF_MS, HIGHLIGHT_MS, MAX_TEASERS, TEASER_SETUPS,
} from '../../app/static/js/live_feed.js';

const settle = () => new Promise(resolve => setImmediate(resolve));

class FakeClassList {
    constructor() { this.names = new Set(); }
    add(name) { this.names.add(name); }
    remove(name) { this.names.delete(name); }
    toggle(name, on) { if (on) this.add(name); else this.remove(name); }
    contains(name) { return this.names.has(name); }
}

class FakeNode {
    constructor(id) { this.id = id; this.classList = new FakeClassList(); this.parent = null; }
    remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; }
}

class FakeList {
    constructor(ids = []) { this.children = []; for (const id of ids) this.adopt(new FakeNode(id), false); }
    adopt(node, atFront) { node.parent = this; if (atFront) this.children.unshift(node); else this.children.push(node); }
    prepend(...nodes) { for (const node of [...nodes].reverse()) this.adopt(node, true); }
    get lastElementChild() { return this.children[this.children.length - 1]; }
    ids() { return this.children.map(node => node.id); }
}

class FakeTarget {
    constructor() { this.listeners = {}; }
    addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }
    fire(type) { for (const fn of this.listeners[type] ?? []) fn(); }
}

function fakeClock() {
    let time = 0;
    let sequence = 0;
    const timers = new Map();
    return {
        now: () => time,
        setTimeout: (fn, ms) => { const id = ++sequence; timers.set(id, { at: time + ms, fn }); return id; },
        clearTimeout: id => { timers.delete(id); },
        async advance(ms) {
            const end = time + ms;
            for (;;) {
                let next = null;
                for (const [id, timer] of timers) {
                    if (timer.at <= end && (next === null || timer.at < next[1].at)) next = [id, timer];
                }
                if (next === null) break;
                timers.delete(next[0]);
                time = next[1].at;
                next[1].fn();
                await settle();
            }
            time = end;
            await settle();
        },
    };
}

function response(status, { body = '', cursor = null, redirected = false } = {}) {
    return { status, redirected, headers: { get: name => (name === 'X-Live-Cursor' ? cursor : null) },
             text: async () => body };
}

function makeEventSource() {
    const instances = [];
    class FakeEventSource {
        constructor(url) { this.url = url; this.closed = false; instances.push(this); }
        close() { this.closed = true; }
    }
    return { FakeEventSource, instances };
}

// Builds a feed. `replies` is consumed one per fetch; once empty, every fetch answers 204.
// A reply that is an Error makes the fetch reject.
function setup({ replies = [], ids = ['post_1'], sseUrl = '', EventSource, lowBandwidth = false,
                 hidden = false, scrollY = 0, withHelpers = true } = {}) {
    const clock = fakeClock();
    const list = new FakeList(ids);
    const pill = Object.assign(new FakeTarget(), { hidden: true, textContent: '' });
    const status = { textContent: '', classList: new FakeClassList() };
    const document = Object.assign(new FakeTarget(), { hidden });
    const calls = [];
    const window = Object.assign(new FakeTarget(), { scrollY, scrolledTo: [] });
    window.scrollTo = options => window.scrolledTo.push(options.top);
    if (withHelpers) {
        window.htmx = { process: node => calls.push(`htmx:${node.id}`) };
        for (const name of [...TEASER_SETUPS, 'setupLightboxTeaser']) window[name] = () => calls.push(name);
    }
    const urls = [];
    const queue = [...replies];
    const fetch = url => {
        urls.push(url);
        const reply = queue.length ? queue.shift() : response(204);
        if (typeof reply === 'function') return reply();
        return reply instanceof Error ? Promise.reject(reply) : Promise.resolve(reply);
    };
    const parse = html => (html ? html.split(',').map(id => new FakeNode(id)) : []);
    const feed = createLiveFeed({
        list, pill, status, fetch, EventSource, document, window, parse,
        setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, now: clock.now,
        config: { postsUrl: '/live/posts', cursor: 1, sseUrl, lowBandwidth,
                  strings: { live: 'Live', paused: 'Paused', newPosts: 'New posts: %d' } },
    });
    return { feed, clock, list, pill, status, document, window, urls, calls };
}

test('pure helpers', () => {
    assert.equal(shouldHold(100), false);
    assert.equal(shouldHold(101), true);
    assert.equal(nextBackoff(POLL_MS), POLL_MS * 2);
    assert.equal(nextBackoff(MAX_BACKOFF_MS), MAX_BACKOFF_MS);
    const kept = dedupeNodes(['post_1'], [new FakeNode('post_1'), new FakeNode('post_2'),
                                          new FakeNode('post_2'), new FakeNode('')]);
    assert.deepEqual(kept.map(node => node.id), ['post_2']);
    assert.equal(trimCount(MAX_TEASERS), 0);
    assert.equal(trimCount(MAX_TEASERS + 3), 3);
});

test('starting visible fetches at once from the cursor, then polls every 15 s', async () => {
    const { feed, clock, urls, status } = setup();
    feed.start();
    await settle();
    assert.deepEqual(urls, ['/live/posts?after=1']);
    assert.equal(status.textContent, 'Live');
    assert.ok(status.classList.contains('text-bg-success'));
    await clock.advance(POLL_MS - 1);
    assert.equal(urls.length, 1);
    await clock.advance(1);
    assert.equal(urls.length, 2);
});

test('new posts at the top are inserted newest first, highlighted, initialised; the cursor moves', async () => {
    const { feed, clock, list, urls, calls } = setup({ replies: [response(200, { body: 'post_3,post_2', cursor: '3' })] });
    feed.start();
    await settle();
    assert.deepEqual(list.ids(), ['post_3', 'post_2', 'post_1']);
    assert.ok(list.children[0].classList.contains('bg-warning-subtle'));
    assert.deepEqual(calls, ['htmx:post_3', 'htmx:post_2', ...TEASER_SETUPS, 'setupLightboxTeaser']);
    await clock.advance(HIGHLIGHT_MS);
    assert.ok(!list.children[0].classList.contains('bg-warning-subtle'));
    await clock.advance(POLL_MS);
    assert.equal(urls.at(-1), '/live/posts?after=3');
});

test('low bandwidth skips the lightbox', async () => {
    const { feed, calls } = setup({ lowBandwidth: true, replies: [response(200, { body: 'post_2', cursor: '2' })] });
    feed.start();
    await settle();
    assert.ok(!calls.includes('setupLightboxTeaser'));
});

test('missing page helpers are skipped, not fatal', async () => {
    const { feed, list } = setup({ withHelpers: false, replies: [response(200, { body: 'post_2', cursor: '2' })] });
    feed.start();
    await settle();
    assert.deepEqual(list.ids(), ['post_2', 'post_1']);
});

test('a 200 with no cursor header keeps the old cursor; one with no teasers still moves it', async () => {
    const { feed, clock, urls, list } = setup({ replies: [response(200, { body: 'post_2' }),
                                                          response(200, { body: '', cursor: '9' })] });
    feed.start();
    await settle();
    await clock.advance(POLL_MS);
    assert.equal(urls[1], '/live/posts?after=1');
    await clock.advance(POLL_MS);
    assert.equal(urls[2], '/live/posts?after=9');
    assert.deepEqual(list.ids(), ['post_2', 'post_1']);
});

test('a reader scrolled down gets a pill, not a jump; the pill flushes to the top', async () => {
    const { feed, clock, list, pill, window } = setup({
        scrollY: 500,
        replies: [response(200, { body: 'post_2', cursor: '2' }), response(200, { body: 'post_4,post_3', cursor: '4' })],
    });
    feed.start();
    await settle();
    assert.deepEqual(list.ids(), ['post_1']);
    assert.equal(pill.hidden, false);
    assert.equal(pill.textContent, 'New posts: 1');
    await clock.advance(POLL_MS);
    assert.equal(pill.textContent, 'New posts: 3');
    pill.fire('click');
    assert.deepEqual(window.scrolledTo, [0]);
    assert.deepEqual(list.ids(), ['post_4', 'post_3', 'post_2', 'post_1']);
    assert.equal(pill.hidden, true);
});

test('scrolling back to the top flushes held posts; other scrolls do nothing', async () => {
    const { feed, list, window } = setup({ scrollY: 500, replies: [response(200, { body: 'post_2', cursor: '2' })] });
    feed.start();
    await settle();
    window.fire('scroll');                 // still scrolled down: nothing
    assert.deepEqual(list.ids(), ['post_1']);
    window.scrollY = 0;
    window.fire('scroll');
    assert.deepEqual(list.ids(), ['post_2', 'post_1']);
    window.fire('scroll');                 // nothing held: nothing
    assert.deepEqual(list.ids(), ['post_2', 'post_1']);
});

test('a pill click with nothing held only hides the pill', () => {
    const { feed, pill, list } = setup();
    feed.start();
    pill.hidden = false;
    pill.fire('click');
    assert.equal(pill.hidden, true);
    assert.deepEqual(list.ids(), ['post_1']);
});

test('a post already listed or already held is never added twice', async () => {
    const { feed, clock, list, pill, window } = setup({
        replies: [response(200, { body: 'post_1', cursor: '1' }), response(200, { body: 'post_2', cursor: '2' }),
                  response(200, { body: 'post_2', cursor: '2' })],
    });
    feed.start();
    await settle();
    assert.deepEqual(list.ids(), ['post_1']);
    assert.equal(pill.hidden, true);
    window.scrollY = 500;
    await clock.advance(POLL_MS);
    await clock.advance(POLL_MS);
    assert.equal(pill.textContent, 'New posts: 1');
});

test('the list never grows past the cap; the oldest teasers go', async () => {
    const ids = Array.from({ length: MAX_TEASERS - 1 }, (_, i) => `post_${i + 10}`);
    const { feed, list } = setup({ ids, replies: [response(200, { body: 'post_a,post_b,post_c', cursor: '9999' })] });
    feed.start();
    await settle();
    assert.equal(list.children.length, MAX_TEASERS);
    assert.deepEqual(list.ids().slice(0, 3), ['post_a', 'post_b', 'post_c']);
    assert.ok(!list.ids().includes(`post_${MAX_TEASERS - 1 + 9}`));
});

test('429, server errors and network errors back off to a cap; a 204 resets', async () => {
    const replies = [response(429), response(500), new Error('offline'), response(429), response(429), response(204)];
    const { feed, clock, urls } = setup({ replies });
    feed.start();
    await settle();                                         // 429 -> next in 30 s
    await clock.advance(POLL_MS * 2);                        // 500 -> 60 s
    assert.equal(urls.length, 2);
    await clock.advance(POLL_MS * 4);                        // network error -> 120 s
    await clock.advance(MAX_BACKOFF_MS);                     // 429 -> stays 120 s
    await clock.advance(MAX_BACKOFF_MS);                     // 429 -> 120 s
    await clock.advance(MAX_BACKOFF_MS);                     // 204 -> back to 15 s
    assert.equal(urls.length, 6);
    await clock.advance(POLL_MS);
    assert.equal(urls.length, 7);
});

for (const [label, reply] of [['a login redirect', response(200, { body: 'post_9', redirected: true })],
                              ['403', response(403)], ['404', response(404)]]) {
    test(`${label} stops the feed for good`, async () => {
        const { feed, clock, urls, status, list, document } = setup({ replies: [reply] });
        feed.start();
        await settle();
        assert.equal(status.textContent, 'Paused');
        assert.ok(status.classList.contains('text-bg-secondary'));
        assert.deepEqual(list.ids(), ['post_1']);
        document.hidden = false;
        document.fire('visibilitychange');
        await clock.advance(MAX_BACKOFF_MS * 2);
        assert.equal(urls.length, 1);
    });
}

test('with SSE, wake-ups are coalesced to one fetch per 5 s and a 60 s safety poll runs', async () => {
    const { FakeEventSource, instances } = makeEventSource();
    const { feed, clock, urls } = setup({ sseUrl: 'https://n.example/live/stream?feed=microblogs',
                                          EventSource: FakeEventSource });
    feed.start();
    await settle();
    assert.equal(instances[0].url, 'https://n.example/live/stream?feed=microblogs');
    assert.equal(urls.length, 1);
    instances[0].onmessage();
    instances[0].onmessage();
    await clock.advance(COALESCE_MS - 1);
    assert.equal(urls.length, 1);
    await clock.advance(1);
    assert.equal(urls.length, 2);
    await clock.advance(COALESCE_MS + 1);
    instances[0].onmessage();                                // long after the last fetch: no wait
    await clock.advance(0);
    assert.equal(urls.length, 3);
    await clock.advance(SAFETY_POLL_MS);
    assert.equal(urls.length, 4);
});

test('three SSE errors in a row fall back to polling; a message in between resets the count', async () => {
    const { FakeEventSource, instances } = makeEventSource();
    const { feed, clock, urls, document } = setup({ sseUrl: '/s', EventSource: FakeEventSource });
    feed.start();
    await settle();
    const source = instances[0];
    source.onerror(); source.onerror();
    source.onmessage();
    source.onerror(); source.onerror();
    assert.equal(source.closed, false);
    source.onerror();
    assert.equal(source.closed, true);
    const before = urls.length;
    await clock.advance(POLL_MS);
    assert.equal(urls.length, before + 1);
    document.hidden = true; document.fire('visibilitychange');
    document.hidden = false; document.fire('visibilitychange');
    assert.equal(instances.length, 1);                       // SSE is not retried after it failed
});

test('an EventSource the browser closed for good falls back to polling at once', async () => {
    const { FakeEventSource, instances } = makeEventSource();
    const { feed, clock, urls, document } = setup({ sseUrl: '/s', EventSource: FakeEventSource });
    feed.start();
    await settle();
    const source = instances[0];
    source.readyState = 2;
    source.onerror();
    assert.equal(source.closed, true);
    const before = urls.length;
    await clock.advance(POLL_MS);
    assert.equal(urls.length, before + 1);
    document.hidden = true; document.fire('visibilitychange');
    document.hidden = false; document.fire('visibilitychange');
    assert.equal(instances.length, 1);
});

test('an SSE open resets the error count', async () => {
    const { FakeEventSource, instances } = makeEventSource();
    const { feed } = setup({ sseUrl: '/s', EventSource: FakeEventSource });
    feed.start();
    await settle();
    const source = instances[0];
    source.onerror(); source.onerror();
    source.onopen();
    source.onerror(); source.onerror();
    assert.equal(source.closed, false);
});

test('showing the tab again within the coalesce gap waits for the gap to end', async () => {
    const { FakeEventSource } = makeEventSource();
    const { feed, clock, urls, document } = setup({ sseUrl: '/s', EventSource: FakeEventSource });
    feed.start();
    await settle();
    await clock.advance(1000);
    document.hidden = true; document.fire('visibilitychange');
    document.hidden = false; document.fire('visibilitychange');
    await settle();
    assert.equal(urls.length, 1);
    await clock.advance(COALESCE_MS - 1000 - 1);
    assert.equal(urls.length, 1);
    await clock.advance(1);
    assert.equal(urls.length, 2);
});

test('an SSE URL without EventSource support polls', async () => {
    const { feed, clock, urls } = setup({ sseUrl: '/s', EventSource: undefined });
    feed.start();
    await settle();
    await clock.advance(POLL_MS);
    assert.equal(urls.length, 2);
});

test('a hidden tab pauses everything; showing it again catches up at once', async () => {
    const { FakeEventSource, instances } = makeEventSource();
    const { feed, clock, urls, status, document } = setup({ sseUrl: '/s', EventSource: FakeEventSource });
    feed.start();
    await settle();
    instances[0].onmessage();
    document.hidden = true;
    document.fire('visibilitychange');
    assert.equal(status.textContent, 'Paused');
    assert.equal(instances[0].closed, true);
    await clock.advance(SAFETY_POLL_MS * 2);
    assert.equal(urls.length, 1);
    document.hidden = false;
    document.fire('visibilitychange');
    await settle();
    assert.equal(urls.length, 2);
    assert.equal(instances.length, 2);
    assert.equal(status.textContent, 'Live');
});

test('a polling tab pauses and resumes too', async () => {
    const { feed, clock, urls, document } = setup();
    feed.start();
    await settle();
    document.hidden = true;
    document.fire('visibilitychange');
    await clock.advance(POLL_MS * 2);
    assert.equal(urls.length, 1);
});

test('starting in a hidden tab fetches nothing until it is shown', async () => {
    const { feed, urls, status } = setup({ hidden: true });
    feed.start();
    await settle();
    assert.equal(urls.length, 0);
    assert.equal(status.textContent, 'Paused');
});

test('a fetch already in flight is not doubled, and a pause during it schedules nothing', async () => {
    const { FakeEventSource, instances } = makeEventSource();
    let release;
    const slow = () => new Promise(resolve => { release = () => resolve(response(204)); });
    const { feed, clock, urls, document } = setup({ sseUrl: '/s', EventSource: FakeEventSource, replies: [slow] });
    feed.start();
    await clock.advance(COALESCE_MS);
    instances[0].onmessage();
    await clock.advance(COALESCE_MS);
    assert.equal(urls.length, 1);
    document.hidden = true;
    document.fire('visibilitychange');
    release();
    await settle();
    await clock.advance(SAFETY_POLL_MS * 2);
    assert.equal(urls.length, 1);
});

test('with SSE, a wake-up during backoff waits out the backoff; after a 204 it coalesces at 5 s again', async () => {
    const { FakeEventSource, instances } = makeEventSource();
    const { feed, clock, urls } = setup({ sseUrl: '/s', EventSource: FakeEventSource,
                                          replies: [response(429), response(204)] });
    feed.start();
    await settle();
    assert.equal(urls.length, 1);                            // 429 -> interval 30 s
    instances[0].onmessage();
    await clock.advance(POLL_MS * 2 - 1);
    assert.equal(urls.length, 1);
    await clock.advance(1);
    assert.equal(urls.length, 2);                            // 204 -> interval back to 15 s
    instances[0].onmessage();
    await clock.advance(COALESCE_MS - 1);
    assert.equal(urls.length, 2);
    await clock.advance(1);
    assert.equal(urls.length, 3);
});

test('the held buffer is capped at MAX_TEASERS, keeping the newest', async () => {
    const body = Array.from({ length: MAX_TEASERS + 5 }, (_, i) => `n${i}`).join(',');
    const { feed, list, pill } = setup({ scrollY: 500, replies: [response(200, { body, cursor: '9' })] });
    feed.start();
    await settle();
    assert.equal(pill.textContent, `New posts: ${MAX_TEASERS}`);
    pill.fire('click');
    assert.equal(list.children.length, MAX_TEASERS);
    assert.equal(list.ids()[0], 'n0');
    assert.equal(list.ids()[MAX_TEASERS - 1], `n${MAX_TEASERS - 1}`);
});
