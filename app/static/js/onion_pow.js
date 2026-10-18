// Onion login and registration: solve the form's proof-of-work before it is sent.
// Spec: docs/superpowers/specs/2026-10-17-onion-storefront-design.md section 3.7a A.
//
// The form's hidden pow_nonce input carries the challenge's salt and difficulty (data-salt, data-bits). The nonce
// sought is the first of 0, 1, 2 ... written in base 36 for which sha256("<salt>:<nonce>") starts with at least
// `bits` zero bits: app/onion_guard.py's verify_pow makes the same check (tests/js/onion_pow.test.mjs and
// tests/test_onion_pow.py share a vector). No library: crypto.subtle only. Where crypto.subtle is missing (it is
// only offered to secure contexts) or a hash fails, the page's hidden link to the no-JavaScript wait step is shown
// instead, and the form is not held.
//
// Every browser dependency is passed in, so the tests drive it all under node. Only the bootstrap at the bottom
// touches real globals.

export const BATCH = 64;    // hashes in flight at once: one await per hash would cost more than the hash

export function leadingZeroBits(bytes) {
    let bits = 0;
    for (const byte of bytes) {
        if (byte === 0) {
            bits += 8;
            continue;
        }
        return bits + Math.clz32(byte) - 24;
    }
    return bits;
}

export function nonceAt(index) {
    return index.toString(36);
}

// The first nonce that meets `bits`, in counting order whatever `batch` is, so it is the one the Python reference
// finds. `digest(bytes)` resolves to an ArrayBuffer.
export async function solve(salt, bits, digest, batch = BATCH) {
    const encoder = new TextEncoder();
    for (let start = 0; ; start += batch) {
        const nonces = Array.from({ length: batch }, (_, offset) => nonceAt(start + offset));
        const hashes = await Promise.all(nonces.map(nonce => digest(encoder.encode(`${salt}:${nonce}`))));
        const found = hashes.findIndex(hash => leadingZeroBits(new Uint8Array(hash)) >= bits);
        if (found >= 0) return nonces[found];
    }
}

export function setupOnionPow({ document, subtle }) {
    const nonce = document.querySelector('input[name="pow_nonce"]');
    if (!nonce) return false;
    const offerWait = () => {
        const fallback = document.getElementById('onion_pow_fallback');
        if (fallback) fallback.hidden = false;
    };
    if (!subtle) {
        offerWait();
        return false;
    }
    const form = nonce.form;
    let solving = false;
    form.addEventListener('submit', async event => {
        if (nonce.value) return;              // solved: this is the resubmit, let it go
        event.preventDefault();
        if (solving) return;
        solving = true;
        const submitter = event.submitter ?? undefined;
        if (submitter) submitter.disabled = true;
        try {
            nonce.value = await solve(nonce.dataset.salt, Number(nonce.dataset.bits),
                                      data => subtle.digest('SHA-256', data));
        } catch {
            offerWait();
        } finally {
            solving = false;
            if (submitter) submitter.disabled = false;
        }
        if (nonce.value) form.requestSubmit(submitter);
    });
    return true;
}

/* node:coverage disable */
if (typeof window !== 'undefined') {
    setupOnionPow({ document: window.document, subtle: window.crypto?.subtle });
}
/* node:coverage enable */
