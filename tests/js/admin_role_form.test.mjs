// Tests of app/static/js/admin_role_form.js. Run: npm run test:js.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { permissionRow, setupRoleForm } from '../../app/static/js/admin_role_form.js';

// A fake element: classes, children, listeners, and the few properties the script reads.
class El {
    constructor(classes = '', props = {}, children = []) {
        this.classes = new Set(classes.split(' ').filter(Boolean));
        this.children = children;
        this.listeners = {};
        this.dataset = {};
        Object.assign(this, props);
        const self = this;
        this.classList = {
            toggle(name, on) { if (on) { self.classes.add(name); } else { self.classes.delete(name); } },
            remove(name) { self.classes.delete(name); },
            contains(name) { return self.classes.has(name); },
        };
    }
    addEventListener(type, fn) { this.listeners[type] = fn; }
    fire(type) { this.listeners[type](); }
    querySelectorAll(selector) {
        const name = selector.slice(1);
        const found = [];
        const walk = element => element.children.forEach(child => {
            if (child.classes.has(name)) { found.push(child); }
            walk(child);
        });
        walk(this);
        return found;
    }
    get hidden() { return this.classes.has('d-none'); }
}

const toggle = (value, checked = false) => new El('perm-domain', { value, checked });

// A permission row: limitable to domains (with toggles for domains 1 and 2) unless `scopable` is false.
function row(search, { checked = false, scopable = true, mode = 'all', chosen = [] } = {}) {
    const box = new El('perm-box', { checked });
    const children = [box];
    if (scopable) {
        const select = new El('perm-scope-select', { value: mode });
        const domains = new El('perm-domains', {}, [toggle('1', chosen.includes('1')), toggle('2', chosen.includes('2'))]);
        children.push(new El('perm-scope', {}, [select, domains]));
    }
    const element = new El('role-permission', {}, children);
    if (search !== undefined) { element.dataset.search = search; }
    return element;
}

function group(rows, open = false) {
    return new El('role-permission-group', { open }, [
        new El('group-count', { textContent: '' }),
        new El('role-js d-none', {}, [new El('group-grant', { dataset: { grant: 'all' } }),
                                      new El('group-grant', { dataset: { grant: 'none' } })]),
        ...rows]);
}

function page({ withReach = true } = {}) {
    const users = group([row('ban users Ban accounts', { checked: true }), row('masquerade Act as', { scopable: false })]);
    const posts = group([row('edit posts Change posts', { mode: 'selected', chosen: ['2'] }), row(undefined)]);
    const reachSelect = new El('perm-scope-select', { value: 'all' });
    const reachDomains = new El('perm-domains', {}, [toggle('1'), toggle('2')]);
    const apply = new El('role-reach-apply');
    const reach = new El('role-js d-none', {}, [reachSelect, reachDomains, apply]);
    const tools = new El('role-js d-none');
    const form = new El('', {}, [tools, ...(withReach ? [reach] : []), users, posts]);
    const ids = {
        'role-form': form,
        'role-filter': new El('', { value: '' }),
        'role-granted-only': new El('', { checked: false }),
        'role-expand': new El(),
        'role-collapse': new El(),
        'role-reach': withReach ? reach : null,
    };
    const document = { getElementById: id => ids[id] ?? null };
    return { document, ids, users, posts, tools, reach, reachSelect, reachDomains, apply };
}

const rows = g => g.querySelectorAll('.role-permission').map(permissionRow);
const count = g => g.querySelectorAll('.group-count')[0].textContent;

test('a page with no role form is left alone', () => {
    assert.equal(setupRoleForm({ getElementById: () => null }), false);
});

test('the tools show, and each group counts what it grants', () => {
    const { document, users, posts, tools, reach } = page();
    assert.equal(setupRoleForm(document), true);
    assert.equal(tools.hidden, false);
    assert.equal(reach.hidden, false);
    assert.equal(count(users), '1 / 2');
    assert.equal(count(posts), '0 / 2');
});

test('a permission shows its domains while granted, its toggles only for selected domains', () => {
    const { document, users, posts } = page();
    setupRoleForm(document);
    const [ban] = rows(users);
    const scope = ban.row.querySelectorAll('.perm-scope')[0];
    const domains = ban.row.querySelectorAll('.perm-domains')[0];
    assert.equal(scope.hidden, false);
    assert.equal(domains.hidden, true);   // all domains
    ban.select.value = 'selected';
    ban.select.fire('change');
    assert.equal(domains.hidden, false);
    ban.box.checked = false;
    ban.box.fire('change');
    assert.equal(scope.hidden, true);
    assert.equal(count(users), '0 / 2');
    const [edit] = rows(posts);
    assert.equal(edit.row.querySelectorAll('.perm-scope')[0].hidden, true);   // not granted yet
});

test('a group is granted all or none at once', () => {
    const { document, posts } = page();
    setupRoleForm(document);
    const [grantAll, grantNone] = posts.querySelectorAll('.group-grant');
    grantAll.fire('click');
    assert.deepEqual(rows(posts).map(r => r.box.checked), [true, true]);
    assert.equal(count(posts), '2 / 2');
    grantNone.fire('click');
    assert.deepEqual(rows(posts).map(r => r.box.checked), [false, false]);
    assert.equal(count(posts), '0 / 2');
});

test('the search narrows the list and opens the groups that still show something', () => {
    const { document, ids, users, posts } = page();
    setupRoleForm(document);
    ids['role-filter'].value = '  BAN ';
    ids['role-filter'].fire('input');
    assert.deepEqual(rows(users).map(r => r.row.hidden), [false, true]);
    assert.equal(users.open, true);
    assert.equal(posts.hidden, true);
    assert.equal(posts.open, false);
    ids['role-filter'].value = '';
    ids['role-filter'].fire('input');
    assert.equal(posts.hidden, false);
    assert.equal(posts.open, false);   // an empty search opens nothing
});

test('granted only shows what the role grants', () => {
    const { document, ids, users, posts } = page();
    setupRoleForm(document);
    ids['role-granted-only'].checked = true;
    ids['role-granted-only'].fire('change');
    assert.deepEqual(rows(users).map(r => r.row.hidden), [false, true]);
    assert.equal(posts.hidden, true);
});

test('expand and collapse all', () => {
    const { document, ids, users, posts } = page();
    setupRoleForm(document);
    ids['role-expand'].fire('click');
    assert.deepEqual([users.open, posts.open], [true, true]);
    ids['role-collapse'].fire('click');
    assert.deepEqual([users.open, posts.open], [false, false]);
});

test('the role-wide domains go to every granted permission that can be limited', () => {
    const { document, posts, users, reachSelect, reachDomains, apply } = page();
    setupRoleForm(document);
    assert.equal(reachDomains.hidden, true);
    reachSelect.value = 'selected';
    reachSelect.fire('change');
    assert.equal(reachDomains.hidden, false);
    reachDomains.querySelectorAll('.perm-domain')[0].checked = true;   // domain 1
    apply.fire('click');
    const [ban] = rows(users);
    assert.equal(ban.select.value, 'selected');
    assert.deepEqual(ban.row.querySelectorAll('.perm-domain').map(t => t.checked), [true, false]);
    assert.equal(ban.row.querySelectorAll('.perm-domains')[0].hidden, false);
    const [edit] = rows(posts);   // not granted: left as it was
    assert.equal(edit.select.value, 'selected');
    assert.deepEqual(edit.row.querySelectorAll('.perm-domain').map(t => t.checked), [false, true]);
});

test('a site with no domains to choose has no role-wide domains', () => {
    const { document, users } = page({ withReach: false });
    assert.equal(setupRoleForm(document), true);
    assert.equal(count(users), '1 / 2');
});

test('a permission with no search text matches only an empty search', () => {
    const { document, ids, posts } = page();
    setupRoleForm(document);
    ids['role-filter'].value = 'x';
    ids['role-filter'].fire('input');
    assert.equal(rows(posts)[1].row.hidden, true);
});
