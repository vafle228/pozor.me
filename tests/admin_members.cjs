const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element() { return {children: [], value: '', textContent: '', hidden: false,
  append(...items) { this.children.push(...items); },
  replaceChildren(...items) { this.children = items; },
  setAttribute() {}, classList: {toggle() {}}}; }
const document = {getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
  createElement: element, querySelectorAll() { return []; }};
const context = vm.createContext({document, URLSearchParams, setTimeout, clearTimeout, console});
const source = fs.readFileSync('src/pozor/admin_static/app.js', 'utf8');
vm.runInContext(source.slice(0, source.lastIndexOf('boot().catch')), context);
(async () => {
  await vm.runInContext(`
    state.page = 'members'; state.chats = [{chat_id: -1, allowed: 1, enabled: 1}];
    $('members-chat').value = '-1';
    let enabled = false, mutations = 0;
    api = async (path, options) => {
      if (options) {
        const body = JSON.parse(options.body);
        if (body.chat !== -1 || body.user !== 42 || body.enabled !== !enabled) throw Error('Wrong scope or toggle');
        enabled = body.enabled; mutations++; return {};
      }
      return {members: [
        {user_id: 42, display_name: '<script>Alice</script>', consent: enabled, opt_out: !enabled},
        {user_id: 43, forgotten: 1, consent: 0} ]};
    };
    members();
  `, context);
  const rows = () => elements.get('members').children;
  assert.match(rows()[0].children[0].textContent, /<script>Alice<\/script>/);
  assert.equal(rows()[1].children[2].children[0].disabled, true);
  await rows()[0].children[2].children[0].onclick();
  assert.equal(rows()[0].children[1].textContent, 'Включён');
  await rows()[0].children[2].children[0].onclick();
  assert.equal(rows()[0].children[1].textContent, 'Выключен');
  assert.equal(vm.runInContext('mutations', context), 2);
  elements.get('members-search').value = 'missing';
  await vm.runInContext('members()', context);
  assert.match(rows()[0].children[0].textContent, /Участники не найдены/);
  console.log('Admin member controls: passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
