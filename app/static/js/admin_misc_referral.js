// Admin > Misc: choosing the "Referral code" registration mode fills the question with the referral-code prompt
// (spec 2026-10-31). The admin can still edit it, and it is not forced on save. The prompt is the translated text
// on #registration_mode's data-referral-prompt.
export const REFERRAL_MODE = 'ReferralCode';

export function setupReferralPrompt(document) {
    const mode = document.getElementById('registration_mode');
    const question = document.getElementById('application_question');
    const prompt = mode && mode.dataset.referralPrompt;
    if (!prompt || !question) {
        return false;
    }
    mode.addEventListener('change', () => {
        if (mode.value === REFERRAL_MODE) {
            question.value = prompt;
        }
    });
    return true;
}

/* node:coverage disable */
if (typeof window !== 'undefined') {
    setupReferralPrompt(window.document);
}
/* node:coverage enable */
