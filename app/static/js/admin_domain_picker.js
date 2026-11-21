// The admin domain picker (app/templates/admin/_domain_picker.html): picking a domain applies at once, by submitting
// the picker's form, so the page reloads for the domain picked. Without scripts the form's own button does it.
export function setupDomainPicker(document) {
    const select = document.getElementById('domain-picker-select');
    if (!select || !select.form) {
        return false;
    }
    select.addEventListener('change', () => select.form.submit());
    return true;
}

/* node:coverage disable */
if (typeof window !== 'undefined') {
    setupDomainPicker(window.document);
}
/* node:coverage enable */
