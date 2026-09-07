// ブラウザーやネットワークを使わず、編集内容と非同期応答の扱いを検証する。
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function setup() {
  const nodes = new Map();
  const node = () => ({
    children: [], value: '', textContent: '', disabled: false, className: '',
    append(...items) { this.children.push(...items); },
    setAttribute() {}, addEventListener() {}, querySelectorAll() { return []; },
    classList: {toggle() {}, add() {}, remove() {}},
  });
  const context = vm.createContext({
    TAB_LABELS: {}, Map, console, Date, Number, Object, String, JSON, Promise,
    document: {getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); }, querySelectorAll() { return []; }},
    el(tag, options = {}, children = []) { return Object.assign(node(), {tag, textContent: options.text || '', children}); },
    clear(target) { target.children = []; },
    toast() {}, api: async () => { throw new Error('API not configured'); },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../ui/workspace.js'), 'utf8'), context);
  return {context, nodes, run: code => vm.runInContext(code, context)};
}

test('容量の不明値を0として表示しない', () => {
  const {run} = setup();
  assert.equal(run('formatBytes(null)'), '–');
  assert.equal(run('formatBytes(0)'), '0 B');
  assert.equal(run('formatBytes(1536)'), '1.5 KiB');
});

test('カスタム対象の空行を除去し、順番を保つ', () => {
  const {run} = setup();
  assert.equal(run('JSON.stringify(selectedBackupPaths("custom", "config/Engine.ini\\n\\n saves/world.sav "))'), '["config/Engine.ini","saves/world.sav"]');
});

test('タブ切替で未保存のエディター内容を失わない', () => {
  const {run, nodes} = setup();
  run(`workspace.editors.set('config/Engine.ini', {path:'config/Engine.ini',content:'FPS=60',original:'FPS=60',revision:'abc',dirty:false}); workspace.activeEditor='config/Engine.ini'; renderEditor();`);
  nodes.get('editor-content').value = 'FPS=90';
  run('editorChanged()');
  run(`workspace.editors.set('config/Game.ini', {path:'config/Game.ini',content:'Players=32',original:'Players=32',dirty:false}); workspace.activeEditor='config/Game.ini'; renderEditor(); workspace.activeEditor='config/Engine.ini'; renderEditor();`);
  assert.equal(nodes.get('editor-content').value, 'FPS=90');
  assert.equal(run(`workspace.editors.get('config/Engine.ini').dirty`), true);
});

test('保存競合時は編集内容と元リビジョンを維持する', async () => {
  const {run, context} = setup();
  context.api = async () => { throw new Error('conflict'); };
  run(`workspace.editors.set('config/Engine.ini', {path:'config/Engine.ini',content:'FPS=90',original:'FPS=60',revision:'old',dirty:true}); workspace.activeEditor='config/Engine.ini';`);
  await run('saveWorkspaceFile()');
  assert.equal(run(`workspace.editors.get(workspace.activeEditor).content`), 'FPS=90');
  assert.equal(run(`workspace.editors.get(workspace.activeEditor).revision`), 'old');
  assert.equal(run(`workspace.editors.get(workspace.activeEditor).dirty`), true);
});

test('保存成功時だけ未保存マークを消してリビジョンを更新する', async () => {
  const {run, context} = setup();
  let calls = 0;
  context.api = async (method, route, body) => { calls++; assert.equal(body.content, 'FPS=90'); return {revision:'new'}; };
  run(`workspace.editors.set('config/Engine.ini', {path:'config/Engine.ini',content:'FPS=90',original:'FPS=60',revision:'old',dirty:true}); workspace.activeEditor='config/Engine.ini';`);
  await run('saveWorkspaceFile()');
  await run('saveWorkspaceFile()');
  assert.equal(calls, 1);
  assert.equal(run(`workspace.editors.get(workspace.activeEditor).revision`), 'new');
  assert.equal(run(`workspace.editors.get(workspace.activeEditor).dirty`), false);
});

test('保存処理中の再送信を拒否する', async () => {
  const {run, context} = setup(); let resolve, calls = 0;
  context.api = () => { calls++; return new Promise(done => { resolve = done; }); };
  run(`workspace.editors.set('config/Engine.ini', {path:'config/Engine.ini',content:'x',original:'y',revision:'old',dirty:true}); workspace.activeEditor='config/Engine.ini';`);
  const first = run('saveWorkspaceFile()');
  await run('saveWorkspaceFile()');
  assert.equal(calls, 1); resolve({revision: 'new'}); await first;
});

test('遅れて返ったフォルダ一覧で最新の表示を上書きしない', async () => {
  const {run, context, nodes} = setup(); const responses = [];
  context.api = () => new Promise(resolve => responses.push(resolve));
  const old = run(`loadWorkspaceFiles('config')`);
  const recent = run(`loadWorkspaceFiles('saves')`);
  responses[1]({entries: []}); await recent;
  const row = nodes.get('files-tbody').children[0];
  responses[0]({entries: [{name:'old.ini',path:'config/old.ini',modified_at:0,editable:true}]}); await old;
  assert.equal(nodes.get('files-tbody').children[0], row);
  assert.equal(nodes.get('files-location').textContent, 'saves');
});

test('CRLFファイルをブラウザーで開いても改行だけで未保存扱いにしない', async () => {
  const {run, context, nodes} = setup();
  context.api = async () => ({path:'config/Engine.ini', content:'[Engine]\r\nFPS=60\r\n', revision:'original'});
  await run(`openWorkspaceFile('config/Engine.ini')`);
  nodes.get('editor-content').value = '[Engine]\nFPS=60\n';
  run('editorChanged()');
  assert.equal(run(`workspace.editors.get(workspace.activeEditor).dirty`), false);
});

test('遅れて開いたファイルが最後に選んだタブを奪わない', async () => {
  const {run, context} = setup(); const responses = [];
  context.api = () => new Promise(resolve => responses.push(resolve));
  const older = run(`openWorkspaceFile('config/Engine.ini')`);
  const newer = run(`openWorkspaceFile('config/Game.ini')`);
  responses[1]({path:'config/Game.ini',content:'game',revision:'game'}); await newer;
  responses[0]({path:'config/Engine.ini',content:'engine',revision:'engine'}); await older;
  assert.equal(run('workspace.activeEditor'), 'config/Game.ini');
  assert.equal(run('workspace.editors.size'), 2);
});
