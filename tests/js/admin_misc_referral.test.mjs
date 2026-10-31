// Tests of app/static/js/admin_misc_referral.js. Run: npm run test:js.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { REFERRAL_MODE, setupReferralPrompt } from '../../app/static/js/admin_misc_referral.js';

class FakeElement {
    constructor(attributes = {}) { Object.assign(this, attributes); this.listeners = {}; }
    addEventListener(type, fn) { this.listeners[type] = fn; }
}

function page({ prompt = 'Enter your referral code', withQuestion = true, withMode = true } = {}) {
    const mode = new FakeElement({ value: 'Open', dataset: prompt ? { referralPrompt: prompt } : {} });
    const question = new FakeElement({ value: 'Why would you like to join?' });
    const document = {
        getElementById: id => ({ registration_mode: withMode ? mode : null,
                                 application_question: withQuestion ? question : null })[id] ?? null,
    };
    return { document, mode, question };
}

test('choosing referral code fills the question with the prompt', () => {
    const { document, mode, question } = page();
    assert.equal(setupReferralPrompt(document), true);
    mode.value = REFERRAL_MODE;
    mode.listeners.change();
    assert.equal(question.value, 'Enter your referral code');
});

test('other modes leave the question alone, and it stays editable afterwards', () => {
    const { document, mode, question } = page();
    setupReferralPrompt(document);
    mode.value = 'Closed';
    mode.listeners.change();
    assert.equal(question.value, 'Why would you like to join?');
    mode.value = REFERRAL_MODE;
    mode.listeners.change();
    question.value = 'Your invite code, please';
    assert.equal(question.value, 'Your invite code, please');
});

test('a page without the fields or the prompt is left alone', () => {
    assert.equal(setupReferralPrompt(page({ withMode: false }).document), false);
    assert.equal(setupReferralPrompt(page({ withQuestion: false }).document), false);
    assert.equal(setupReferralPrompt(page({ prompt: '' }).document), false);
});
