// Tests of app/static/js/onion_pow.js, driven through injected fakes and node's own crypto.subtle.
// Run: npm run test:js (fails below 100% line, branch or function coverage).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { BATCH, leadingZeroBits, nonceAt, solve, setupOnionPow } from '../../app/static/js/onion_pow.js';

// The shared vector: tests/test_onion_pow.py::test_the_shared_vector finds the same nonce, and verify_pow accepts it.
const SHARED_SALT = '00112233445566778899aabbccddeeff';
const SHARED_BITS = 12;
const SHARED_NONCE = '11y';

const settle = () => new Promise(resolve => setImmediate(resolve));
const digest = data => webcrypto.subtle.digest('SHA-256', data);

test('leadingZeroBits counts exactly', () => {
    const bytes = (...values) => Uint8Array.from([...values, ...new Array(32 - values.length).fill(0xff)]);
    assert.equal(leadingZeroBits(bytes(0x80)), 0);
    assert.equal(leadingZeroBits(bytes(0x01)), 7);
    assert.equal(leadingZeroBits(Uint8Array.from([0, 0x80, ...new Array(30).fill(0)])), 8);
    assert.equal(leadingZeroBits(bytes(0, 0, 0x0f)), 20);
    assert.equal(leadingZeroBits(new Uint8Array(32)), 256);
});

test('nonces count up in base 36', () => {
    assert.deepEqual([0, 9, 10, 35, 36, 1366].map(nonceAt), ['0', '9', 'a', 'z', '10', '11y']);
});

test('solve finds the shared vector, as verify_pow checks it', async () => {
    assert.equal(await solve(SHARED_SALT, SHARED_BITS, digest), SHARED_NONCE);
    const hash = new Uint8Array(await digest(new TextEncoder().encode(`${SHARED_SALT}:${SHARED_NONCE}`)));
    assert.ok(leadingZeroBits(hash) >= SHARED_BITS);
});

test('solve returns the first nonce that meets the difficulty, whatever the batch', async () => {
    assert.equal(await solve(SHARED_SALT, SHARED_BITS, digest, 1), SHARED_NONCE);
    assert.equal(await solve(SHARED_SALT, SHARED_BITS, digest, 7), SHARED_NONCE);
    assert.equal(await solve(SHARED_SALT, 0, digest), '0');
    assert.ok(BATCH >= 1);
});

class FakeElement {
    constructor(attributes = {}) { Object.assign(this, attributes); this.listeners = {}; }
    addEventListener(type, fn) { this.listeners[type] = fn; }
}

function page({ withNonce = true, solved = '' } = {}) {
    const fallback = new FakeElement({ hidden: true });
    const submits = [];
    const form = new FakeElement({ requestSubmit: submitter => submits.push(submitter) });
    const nonce = new FakeElement({ form, value: solved, dataset: { salt: SHARED_SALT, bits: String(SHARED_BITS) } });
    const button = new FakeElement({ disabled: false });
    const view = new FakeElement();                 // the window: document.defaultView
    const document = {
        defaultView: view,
        querySelector: selector => (selector === 'input[name="pow_nonce"]' && withNonce ? nonce : null),
        getElementById: id => (id === 'onion_pow_fallback' ? fallback : null),
    };
    const submit = (submitter = button) => {
        const event = { submitter, prevented: false, preventDefault() { this.prevented = true; } };
        const done = form.listeners.submit(event);
        return { event, done };
    };
    return { document, form, nonce, fallback, submits, submit, button, view };
}

test('a page with no challenge is left alone', () => {
    const { document } = page({ withNonce: false });
    assert.equal(setupOnionPow({ document, subtle: webcrypto.subtle }), false);
});

test('without crypto.subtle the wait step is offered and the form is not held', () => {
    const { document, form, fallback } = page();
    assert.equal(setupOnionPow({ document, subtle: undefined }), false);
    assert.equal(fallback.hidden, false);
    assert.equal(form.listeners.submit, undefined);
});

test('a submit is held, solved, then sent with the same button', async () => {
    const { document, nonce, submits, submit, button } = page();
    assert.equal(setupOnionPow({ document, subtle: webcrypto.subtle }), true);
    const { event, done } = submit();
    assert.equal(event.prevented, true);
    assert.equal(button.disabled, true);
    const second = submit();                       // a second click while solving: held, not solved twice
    assert.equal(second.event.prevented, true);
    await done;
    await second.done;
    assert.equal(nonce.value, SHARED_NONCE);
    assert.deepEqual(submits, [button]);
    assert.equal(button.disabled, false);
    const after = submit();                        // the resubmit: solved, so it goes through
    assert.equal(after.event.prevented, false);
});

test('a nonce the browser restored is cleared and solved again, never sent as it was', async () => {
    const { document, nonce, submits, submit, button } = page({ solved: 'stale' });
    setupOnionPow({ document, subtle: webcrypto.subtle });
    assert.equal(nonce.value, '');
    nonce.value = 'stale';                         // restored again after setup: still not trusted
    const { event, done } = submit();
    assert.equal(event.prevented, true);
    await done;
    assert.equal(nonce.value, SHARED_NONCE);
    assert.deepEqual(submits, [button]);
});

test('a submit with no button is sent with none', async () => {
    const { document, submits, submit } = page();
    setupOnionPow({ document, subtle: webcrypto.subtle });
    await submit(null).done;
    assert.deepEqual(submits, [undefined]);
});

test('a hash that fails offers the wait step and lets the visitor try again', async () => {
    const { document, nonce, submits, submit, fallback, button } = page();
    let calls = 0;
    const subtle = { digest: async () => { calls += 1; throw new Error('no'); } };
    setupOnionPow({ document, subtle });
    await submit().done;
    assert.equal(nonce.value, '');
    assert.deepEqual(submits, []);
    assert.equal(fallback.hidden, false);
    assert.equal(button.disabled, false);
    await submit().done;                           // not stuck: a new click tries again
    assert.ok(calls >= 2);
    await settle();
});

test('a page without a fallback element still copes', async () => {
    const { document, submit } = page();
    document.getElementById = () => null;
    assert.equal(setupOnionPow({ document, subtle: undefined }), false);
    setupOnionPow({ document, subtle: { digest: async () => { throw new Error('no'); } } });
    await submit().done;
});

test('a page the browser restores from its back-forward cache solves again', async () => {
    const { document, nonce, submits, submit, button, view } = page();
    setupOnionPow({ document, subtle: webcrypto.subtle });
    await submit().done;
    assert.equal(nonce.value, SHARED_NONCE);
    view.listeners.pageshow({ persisted: false });  // an ordinary load: nothing to forget
    assert.equal(nonce.value, SHARED_NONCE);
    view.listeners.pageshow({ persisted: true });   // Back to the page: its proof was spent with the submit
    assert.equal(nonce.value, '');
    const { event, done } = submit();
    assert.equal(event.prevented, true);            // held and solved again, not sent with the spent proof
    await done;
    assert.equal(nonce.value, SHARED_NONCE);
    assert.deepEqual(submits, [button, button]);
});
