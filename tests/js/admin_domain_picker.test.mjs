// Tests of app/static/js/admin_domain_picker.js. Run: npm run test:js.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { setupDomainPicker } from '../../app/static/js/admin_domain_picker.js';

function page({ withSelect = true, withForm = true } = {}) {
    const form = { submitted: 0, submit() { this.submitted += 1; } };
    const select = { form: withForm ? form : null, listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; } };
    const document = { getElementById: id => (id === 'domain-picker-select' && withSelect ? select : null) };
    return { document, select, form };
}

test('picking a domain submits the picker', () => {
    const { document, select, form } = page();
    assert.equal(setupDomainPicker(document), true);
    assert.equal(form.submitted, 0);
    select.listeners.change();
    assert.equal(form.submitted, 1);
});

test('a page with no picker, or one named but not offered, is left alone', () => {
    assert.equal(setupDomainPicker(page({ withSelect: false }).document), false);
    assert.equal(setupDomainPicker(page({ withForm: false }).document), false);
});
