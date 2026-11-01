// Run with: node tests/admin_selector.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const elements = new Map();
function $(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', children: [], dataset: {}, hidden: false, disabled: false,
    replaceChildren(...children) {
      this.children = children;
      if (!children.some(child => String(child.value) === this.value)) this.value = String(children[0]?.value ?? '');
    },
    append(...children) { this.children.push(...children); },
    querySelectorAll(selector) {
      const inputs = this.children.flatMap(child => child.children || []).filter(child => child.type === 'checkbox');
      return selector === 'input:checked' ? inputs.filter(input => input.checked) : inputs;
    },
    get selectedOptions() { return this.children.filter(child => String(child.value) === this.value); },
  });
  return elements.get(id);
}
let enabled = false, membersRequests = 0, activationRequests = 0, members = [];
const context = vm.createContext({
  $, state: {page: 'history'}, setTimeout, clearTimeout, URLSearchParams,
  node: (tag, text) => ({tag, text, dataset: {}, children: [], append(...children) { this.children.push(...children); }}),
  failure: error => { throw error; },
  api: async (path, options) => {
    if (path === '/api/telegram/groups') return {groups: [{id: -2, title: 'New group', enabled}]};
    if (path === '/api/telegram/add_chat') {
      assert.equal(JSON.parse(options.body).chat, -2);
      enabled = true; activationRequests++;
      return {chat: -2};
    }
    if (path.startsWith('/api/telegram/members?')) { membersRequests++; return {members}; }
    throw new Error(`Unexpected request: ${path}`);
  },
});
vm.runInContext(fs.readFileSync('src/pozor/admin_static/pipeline.js', 'utf8'), context);
(async () => {
  await vm.runInContext('historyGroups()', context);
  assert.equal($('history-group').value, '-2');
  assert.notEqual($('history-group').children[0].disabled, true);
  assert.equal($('history-enable').hidden, false);
  assert.equal($('history-start').disabled, true);
  assert.equal(membersRequests, 0);
  await $('history-enable').onclick();
  assert.equal(activationRequests, 1);
  assert.equal($('history-group').value, '-2');
  assert.equal($('history-enable').hidden, true);
  assert.equal(membersRequests, 1);
  assert.equal($('history-start').disabled, true); // Consent is still required.
  members = [{user_id: 42, display_name: 'Alice'}];
  await vm.runInContext('historyMembers()', context);
  assert.equal($('history-start').disabled, true); // Having members does not select them.
  $('history-members').querySelectorAll('input')[0].checked = true;
  $('history-members').onchange();
  assert.equal($('history-start').disabled, false);
  await vm.runInContext('historyGroups()', context);
  assert.equal($('history-members').querySelectorAll('input:checked').length, 1);
  assert.equal($('history-start').disabled, false); // Refresh keeps the selected participant.
  vm.runInContext('historyRunning = true; updateHistoryStart()', context);
  assert.equal($('history-start').disabled, true);
  vm.runInContext('historyRunning = false', context);
  members = Array.from({length: 11}, (_, i) => ({user_id: i + 1}));
  await vm.runInContext('historyMembers()', context);
  $('history-members').querySelectorAll('input').forEach(input => input.checked = true);
  $('history-members').onchange();
  assert.equal($('history-start').disabled, true);
  $('history-group').value = '-3';
  await vm.runInContext('historyMembers()', context);
  assert.equal($('history-members').querySelectorAll('input:checked').length, 0);
  console.log('Chat selection and activation: passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
