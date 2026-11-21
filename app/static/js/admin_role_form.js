// Admin > Roles > the role form (app/templates/admin/role_form.html). The form works without scripts: every
// permission's domain controls are shown. With scripts:
// - a permission's domain controls show only while it is granted, and its domain toggles only for "Selected domains";
// - each group's heading counts what it grants, and grants all or none of it at once;
// - a search box and a "granted only" switch narrow the list, opening the groups that still show something;
// - the role-wide domains are applied to every granted permission that can be limited to domains.
// It finds what it works on by class name alone.

const HIDDEN = 'd-none';

function all(root, name) {
    return Array.from(root.querySelectorAll('.' + name));
}

function first(root, name) {
    return all(root, name)[0] || null;
}

export function permissionRow(row) {
    const box = first(row, 'perm-box');
    const scope = first(row, 'perm-scope');
    const select = scope && first(scope, 'perm-scope-select');
    const domains = scope && first(scope, 'perm-domains');
    function sync() {
        if (scope) {
            scope.classList.toggle(HIDDEN, !box.checked);
            domains.classList.toggle(HIDDEN, select.value !== 'selected');
        }
    }
    function reach(value, chosen) {
        if (select && box.checked) {
            select.value = value;
            all(domains, 'perm-domain').forEach(toggle => { toggle.checked = chosen.has(toggle.value); });
            sync();
        }
    }
    return { row, box, select, sync, reach, text: (row.dataset.search || '').toLowerCase() };
}

export function setupRoleForm(document) {
    const form = document.getElementById('role-form');
    if (!form) {
        return false;
    }
    const groups = all(form, 'role-permission-group').map(group => ({
        group, count: first(group, 'group-count'), rows: all(group, 'role-permission').map(permissionRow),
    }));
    const rows = groups.flatMap(group => group.rows);
    const filter = document.getElementById('role-filter');
    const grantedOnly = document.getElementById('role-granted-only');

    function counts() {
        groups.forEach(({ count, rows: groupRows }) => {
            count.textContent = `${groupRows.filter(row => row.box.checked).length} / ${groupRows.length}`;
        });
    }

    function narrow() {
        const text = filter.value.trim().toLowerCase();
        const onlyGranted = grantedOnly.checked;
        groups.forEach(({ group, rows: groupRows }) => {
            let shown = 0;
            groupRows.forEach(row => {
                const show = row.text.includes(text) && (!onlyGranted || row.box.checked);
                row.row.classList.toggle(HIDDEN, !show);
                shown += show ? 1 : 0;
            });
            group.classList.toggle(HIDDEN, shown === 0);
            if ((text || onlyGranted) && shown) {
                group.open = true;
            }
        });
    }

    rows.forEach(row => {
        row.box.addEventListener('change', () => { row.sync(); counts(); });
        if (row.select) {
            row.select.addEventListener('change', row.sync);
        }
        row.sync();
    });
    groups.forEach(({ group, rows: groupRows }) => {
        all(group, 'group-grant').forEach(button => button.addEventListener('click', () => {
            groupRows.forEach(row => { row.box.checked = button.dataset.grant === 'all'; row.sync(); });
            counts();
        }));
    });
    filter.addEventListener('input', narrow);
    grantedOnly.addEventListener('change', narrow);
    document.getElementById('role-expand').addEventListener('click', () => groups.forEach(({ group }) => { group.open = true; }));
    document.getElementById('role-collapse').addEventListener('click', () => groups.forEach(({ group }) => { group.open = false; }));

    const reach = document.getElementById('role-reach');
    if (reach) {
        const select = first(reach, 'perm-scope-select');
        const domains = first(reach, 'perm-domains');
        const sync = () => domains.classList.toggle(HIDDEN, select.value !== 'selected');
        select.addEventListener('change', sync);
        sync();
        first(reach, 'role-reach-apply').addEventListener('click', () => {
            const chosen = new Set(all(domains, 'perm-domain').filter(toggle => toggle.checked).map(toggle => toggle.value));
            rows.forEach(row => row.reach(select.value, chosen));
        });
    }
    all(form, 'role-js').forEach(element => element.classList.remove(HIDDEN));   // the tools need a script
    counts();
    return true;
}

/* node:coverage disable */
if (typeof window !== 'undefined') {
    setupRoleForm(window.document);
}
/* node:coverage enable */
