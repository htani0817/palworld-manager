/* ワークスペースの表示状態。接続情報やパスワードはブラウザーへ保存しない。 */
'use strict';
let workspaceStatusAt = 0;
const workspace = {
  tabs: ['dashboard'], directory: 'config', editors: new Map(), activeEditor: null,
  backupId: null, backups: [], backupBusy: false, fileRequest: 0, detailRequest: 0,
  jobBusy: false, lastJob: null, scheduleLoaded: false, iniCategory: 'all',
  editorIntent: 0, editorRequests: new Map(),
};
const wsNode = id => document.getElementById(id);
const workspaceLabels = { ...TAB_LABELS, logs: 'コンソール' };
const workspaceSymbols = { dashboard: '▦', players: '♙', world: '◎', files: '▱', ini: '⚙', backups: '▤', logs: '›_', schedule: '◷', operations: '◈', history: '↗', ranking: '♜', commands: '›_' };

function workspaceButton(label, action, cls = 'btn btn-ghost btn-sm') {
  return el('button', {text: label, class: cls, attrs: {type: 'button'}, on: {click: action}});
}

function initWorkspace() {
  document.querySelectorAll('#sidebar .nav-item').forEach(button => {
    const label = workspaceLabels[button.dataset.tab];
    button.title = label;
    button.setAttribute('aria-label', label);
  });
  document.querySelectorAll('[data-open]').forEach(button => button.addEventListener('click', () => navTo(button.dataset.open)));
  const shortcuts = wsNode('explorer-shortcuts');
  for (const tab of ['dashboard', 'players', 'world', 'backups', 'ini', 'logs', 'schedule']) {
    shortcuts.append(el('button', {class: 'explorer-link', dataset: {view: tab}, attrs: {type: 'button'}, on: {click: () => navTo(tab)}}, [
      el('span', {text: workspaceSymbols[tab], attrs: {'aria-hidden': 'true'}}), el('span', {text: workspaceLabels[tab]}),
    ]));
  }
  wsNode('main-col').append(el('footer', {class: 'workspace-footer'}, [
    workspaceButton('›_ コンソール', () => navTo('logs'), ''),
    el('span', {class: 'footer-detail', text: 'Palworld Server Manager · Web workspace'}),
    el('time', {attrs: {id: 'workspace-clock'}}),
  ]));
  const clock = () => { wsNode('workspace-clock').textContent = new Date().toLocaleTimeString('ja-JP'); };
  clock(); setInterval(clock, 1000);
  wsNode('explorer-refresh').addEventListener('click', loadFileTree);
  wsNode('files-refresh').addEventListener('click', () => loadWorkspaceFiles(workspace.directory));
  wsNode('files-up').addEventListener('click', () => {
    const parts = workspace.directory.split('/'); parts.pop();
    loadWorkspaceFiles(parts.join('/') || 'config');
  });
  wsNode('editor-content').addEventListener('input', editorChanged);
  wsNode('editor-content').addEventListener('scroll', () => { wsNode('editor-lines').scrollTop = wsNode('editor-content').scrollTop; });
  wsNode('editor-content').addEventListener('keydown', event => {
    if (event.key === 'Tab') {
      event.preventDefault();
      const input = event.currentTarget;
      input.setRangeText('    ', input.selectionStart, input.selectionEnd, 'end');
      editorChanged();
    }
  });
  wsNode('editor-save').addEventListener('click', saveWorkspaceFile);
  wsNode('editor-reload').addEventListener('click', () => {
    const file = workspace.editors.get(workspace.activeEditor);
    if (!file) return;
    if (file.dirty) openModal('変更を破棄して再読込', '未保存の変更が失われます。ディスク上の内容を読み込みますか？', () => openWorkspaceFile(file.path, true));
    else openWorkspaceFile(file.path, true);
  });
  window.addEventListener('beforeunload', event => {
    if ([...workspace.editors.values()].some(file => file.dirty)) { event.preventDefault(); event.returnValue = ''; }
  });
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's' && currentTab() === 'files' && workspace.activeEditor) {
      event.preventDefault(); saveWorkspaceFile();
    }
  });
  wsNode('backup-new').addEventListener('click', () => {
    wsNode('backup-label').value = '手動バックアップ ' + new Date().toLocaleDateString('ja-JP');
    wsNode('backup-modal').classList.remove('hidden'); wsNode('backup-label').focus();
  });
  wsNode('backup-cancel').addEventListener('click', () => wsNode('backup-modal').classList.add('hidden'));
  wsNode('backup-scope').addEventListener('change', () => wsNode('backup-paths-field').classList.toggle('hidden', wsNode('backup-scope').value !== 'custom'));
  wsNode('backup-form').addEventListener('submit', createWorkspaceBackup);
  wsNode('backup-refresh').addEventListener('click', () => { loadBackups(); loadBackupSchedule(); refreshBackupJob(); });
  wsNode('backup-schedule-form').addEventListener('submit', saveBackupSchedule);
  wsNode('backup-frequency').addEventListener('change', () => {
    if (wsNode('backup-frequency').value !== 'custom') wsNode('backup-cron').value = wsNode('backup-frequency').value;
  });
  wsNode('backup-cron').addEventListener('input', () => { wsNode('backup-frequency').value = 'custom'; });
  installIniCategories();
  installConsole();
  installModalAccessibility();
  renderWorkspaceTabs();
  workspaceNavigate('dashboard');
  loadFileTree();
  setInterval(() => { if (!document.hidden && (workspace.backupBusy || currentTab() === 'backups')) refreshBackupJob(); }, 2000);
}

function workspaceNavigate(tab) {
  if (!workspace.tabs.includes(tab)) workspace.tabs.push(tab);
  renderWorkspaceTabs(tab);
  document.querySelectorAll('.explorer-link').forEach(link => link.classList.toggle('active', link.dataset.view === tab));
  wsNode('explorer-title').textContent = ['ini', 'files'].includes(tab) ? '設定・ファイル' : 'エクスプローラー';
  if (tab === 'files') loadWorkspaceFiles(workspace.directory);
  if (tab === 'backups') { loadBackups(); if (!workspace.scheduleLoaded) loadBackupSchedule(); refreshBackupJob(); }
  wsNode('content').scrollTop = 0;
}

function renderWorkspaceTabs(active = currentTab()) {
  const container = wsNode('workspace-tabs');
  clear(container);
  workspace.tabs.forEach(tab => {
    const selected = tab === active;
    const button = workspaceButton((workspaceSymbols[tab] || '') + '  ' + workspaceLabels[tab], () => navTo(tab), '');
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-selected', String(selected));
    button.setAttribute('aria-controls', 'tab-' + tab);
    button.tabIndex = selected ? 0 : -1;
    button.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      let index = workspace.tabs.indexOf(tab);
      index = event.key === 'Home' ? 0 : event.key === 'End' ? workspace.tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + workspace.tabs.length) % workspace.tabs.length;
      navTo(workspace.tabs[index]);
      wsNode('workspace-tabs').querySelector('[aria-selected="true"]').focus();
    });
    const children = [button];
    if (tab !== 'dashboard') {
      const close = workspaceButton('×', () => {
        workspace.tabs = workspace.tabs.filter(item => item !== tab);
        if (currentTab() === tab) navTo(workspace.tabs[workspace.tabs.length - 1]);
        else renderWorkspaceTabs();
      }, '');
      close.setAttribute('aria-label', workspaceLabels[tab] + 'のタブを閉じる'); children.push(close);
    }
    container.append(el('div', {class: 'workspace-tab' + (selected ? ' selected' : '')}, children));
  });
}

function updateWorkspaceStatus({info, sys, svc}) {
  workspaceStatusAt = Date.now();
  const running = svc?.query_ok === true && svc?.status === 'active';
  const stopped = svc?.query_ok === true && ['inactive', 'failed'].includes(svc?.status);
  const label = running ? '稼働中' : stopped ? '停止中' : '状態不明';
  wsNode('header-status').textContent = label;
  wsNode('header-status').classList.toggle('running', running);
  wsNode('explorer-status').textContent = label;
  for (const id of ['tree-dot', 'explorer-dot']) wsNode(id).className = 'dot ' + (running ? 'dot-on' : stopped ? 'dot-off' : 'dot-idle');
  if (!svc) { wsNode('svc-label').textContent = '状態不明'; wsNode('svc-dot').className = 'dot dot-idle'; }
  if (!sys) {
    for (const key of ['cpu', 'mem', 'disk']) setKpiPercent(key, null, '取得できません');
  }
  if (info) {
    wsNode('workspace-server-name').textContent = info.servername || 'Palworld Server';
    wsNode('tree-name').textContent = info.servername || 'PALWORLD SERVER';
    wsNode('workspace-version').textContent = 'Palworld ' + (info.version || '–');
  }
  wsNode('workspace-address').textContent = wsNode('i-address').textContent;
  if (sys?.host) wsNode('workspace-os').textContent = sys.host.os_pretty_name || 'ホスト情報なし';
}

function renderWorkspaceCharts(rows) {
  renderLineChart('workspace-cpu-chart', rows, [{keys: ['system_cpu_percent', 'cpu_percent'], color: themeColor('--chart-2', '#ef947f'), label: 'CPU %', digits: 1}], {floor: 0, ceiling: 100});
  renderLineChart('workspace-memory-chart', rows, [{keys: ['system_memory_percent', 'system_mem_percent', 'mem_percent'], color: themeColor('--chart-1', '#52cf8a'), label: 'メモリ %', digits: 1}], {floor: 0, ceiling: 100});
}

function formatBytes(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '–';
  const units = ['B', 'KiB', 'MiB', 'GiB']; let number = Number(value), index = 0;
  while (number >= 1024 && index < units.length - 1) { number /= 1024; index++; }
  return number.toFixed(index ? 1 : 0) + ' ' + units[index];
}

async function loadFileTree() {
  const tree = wsNode('file-tree'); clear(tree);
  for (const [key, label] of [['config', '設定ファイル'], ['saves', 'セーブデータ']]) {
    const root = el('div');
    root.append(el('button', {class: 'file-tree-button', attrs: {type: 'button'}, on: {click: () => { workspace.directory = key; navTo('files'); }}}, [el('span', {text: '⌄'}), el('span', {text: label})]));
    tree.append(root);
    try {
      const data = await api('GET', '/api/files?path=' + key);
      for (const file of data.entries.slice(0, 50)) {
        root.append(el('button', {class: 'file-tree-button file', attrs: {type: 'button'}, on: {click: () => openFileEntry(file)}}, [
          el('span', {text: file.directory ? '▱' : '♧'}), el('span', {text: file.name}),
        ]));
      }
      if (!data.entries.length) root.append(el('p', {class: 'note', text: 'ファイルがありません'}));
    } catch (error) { root.append(el('p', {class: 'note', text: 'このサーバーでは領域を取得できません'})); }
  }
}

async function loadWorkspaceFiles(path) {
  const request = ++workspace.fileRequest;
  workspace.directory = path;
  wsNode('files-location').textContent = path;
  wsNode('files-up').disabled = !path.includes('/');
  const tbody = wsNode('files-tbody');
  clear(tbody); tbody.append(el('tr', {}, [el('td', {text: '読み込み中…', attrs: {colspan: 4}})]));
  try {
    const data = await api('GET', '/api/files?path=' + encodeURIComponent(path));
    if (request !== workspace.fileRequest) return;
    clear(tbody);
    for (const file of data.entries) {
      const button = el('button', {class: 'file-entry', attrs: {type: 'button'}, on: {click: () => openFileEntry(file)}}, [el('span', {text: file.directory ? '▱' : '♧', attrs: {'aria-hidden': 'true'}}), el('span', {text: file.name})]);
      tbody.append(el('tr', {}, [el('td', {}, [button]), el('td', {text: formatBytes(file.size)}), el('td', {text: new Date(file.modified_at * 1000).toLocaleString('ja-JP')}), el('td', {text: file.directory ? 'フォルダ' : file.settings ? 'ゲーム設定' : file.editable ? '編集可能' : 'セーブデータ'})]));
    }
    if (!data.entries.length) tbody.append(el('tr', {}, [el('td', {text: 'ファイルがありません', attrs: {colspan: 4}})]));
  } catch (error) {
    if (request !== workspace.fileRequest) return;
    clear(tbody); tbody.append(el('tr', {}, [el('td', {text: error.message, attrs: {colspan: 4}})]));
  }
}

function openFileEntry(file) {
  if (file.settings) { navTo('ini'); return; }
  if (file.directory) { workspace.directory = file.path; navTo('files'); return; }
  if (file.editable) { navTo('files'); openWorkspaceFile(file.path); return; }
  openModal('セーブデータ', file.name + ' はバイナリ形式です。バックアップ画面で、このファイルを対象に選択できます。', () => {
    navTo('backups'); wsNode('backup-new').click(); wsNode('backup-scope').value = 'custom';
    wsNode('backup-paths-field').classList.remove('hidden'); wsNode('backup-paths').value = file.path;
  });
}

async function openWorkspaceFile(path, reload = false) {
  const intent = ++workspace.editorIntent;
  if (workspace.editors.has(path) && !reload) { workspace.activeEditor = path; renderEditor(); return; }
  workspace.editorRequests.set(path, intent);
  const previous = workspace.editors.get(path);
  if (previous) { previous.reloading = true; renderEditor(); }
  try {
    const data = await api('GET', '/api/files/content?path=' + encodeURIComponent(path));
    if (workspace.editorRequests.get(path) !== intent) return;
    const content = data.content.replace(/\r\n?/g, '\n');
    workspace.editors.set(path, {...data, content, original: content, dirty: false, saving: false});
    if (intent === workspace.editorIntent) workspace.activeEditor = path;
    renderEditor();
  } catch (error) { toast(error.message, 'error'); }
  finally {
    if (workspace.editorRequests.get(path) === intent) {
      workspace.editorRequests.delete(path);
      const file = workspace.editors.get(path);
      if (file) file.reloading = false;
      renderEditor();
    }
  }
}

function renderEditor() {
  wsNode('editor-workspace').classList.toggle('hidden', !workspace.activeEditor);
  const file = workspace.editors.get(workspace.activeEditor);
  if (!file) return;
  const tabs = wsNode('editor-tabs'); clear(tabs);
  for (const [path, doc] of workspace.editors) {
    const button = workspaceButton((doc.dirty ? '● ' : '⚙ ') + path.split('/').pop(), () => { workspace.activeEditor = path; renderEditor(); }, '');
    button.setAttribute('role', 'tab'); button.setAttribute('aria-selected', String(path === file.path));
    const remove = () => {
      workspace.editors.delete(path);
      if (workspace.activeEditor === path) workspace.activeEditor = [...workspace.editors.keys()].pop() || null;
      renderEditor();
    };
    const close = workspaceButton('×', () => {
      if (doc.saving || doc.reloading) return;
      if (doc.dirty) openModal('変更を破棄', path + ' の未保存の変更を破棄しますか？', remove);
      else remove();
    }, '');
    close.setAttribute('aria-label', path + 'を閉じる');
    tabs.append(el('div', {class: 'editor-tab' + (path === file.path ? ' selected' : '')}, [button, close]));
  }
  wsNode('editor-path').textContent = file.path;
  if (wsNode('editor-content').value !== file.content) wsNode('editor-content').value = file.content;
  wsNode('editor-content').readOnly = file.saving || file.reloading;
  wsNode('editor-reload').disabled = file.saving || file.reloading;
  wsNode('editor-save').disabled = !file.dirty || file.saving || file.reloading;
  wsNode('editor-state').textContent = file.reloading ? '読み込み中…' : file.saving ? '保存中…' : file.dirty ? '● 未保存の変更' : '保存済み';
  wsNode('editor-lines').textContent = file.content.split('\n').map((_, i) => i + 1).join('\n');
}

function editorChanged() {
  const file = workspace.editors.get(workspace.activeEditor);
  if (!file) return;
  file.content = wsNode('editor-content').value; file.dirty = file.content !== file.original;
  renderEditor();
}

async function saveWorkspaceFile() {
  const file = workspace.editors.get(workspace.activeEditor);
  if (!file || !file.dirty || file.saving || file.reloading) return;
  file.saving = true; renderEditor();
  try {
    const result = await api('PUT', '/api/files/content', {path: file.path, content: file.content, revision: file.revision});
    file.original = file.content; file.revision = result.revision; file.dirty = false;
    toast('保存しました。サーバー再起動で反映されます。', 'success');
  } catch (error) { toast(error.message, 'error'); }
  finally { file.saving = false; renderEditor(); }
}

async function loadBackups() {
  try {
    const data = await api('GET', '/api/backups'); workspace.backups = data.backups;
    renderBackups();
    if (workspace.backupId && workspace.backups.some(item => item.id === workspace.backupId)) showBackup(workspace.backupId);
    else if (workspace.backups.length && !workspace.backups[0].invalid) showBackup(workspace.backups[0].id);
    else {
      workspace.backupId = null;
      clear(wsNode('backup-detail'));
      wsNode('backup-detail').append(el('div', {class: 'empty-state'}, [el('span', {class: 'empty-symbol', text: '▤'}), el('h3', {text: '最初のバックアップを作成しましょう'}), el('p', {text: '設定やワールドを保存しておくと、アップデート前後の復旧に使えます。'})]));
    }
  } catch (error) { clear(wsNode('backup-list')); wsNode('backup-list').append(el('p', {class: 'note', text: error.message})); }
}

function renderBackups() {
  const list = wsNode('backup-list'); clear(list);
  workspace.backups.forEach(backup => {
    const button = el('button', {class: 'backup-item' + (backup.id === workspace.backupId ? ' selected' : ''), attrs: {type: 'button'}, on: {click: () => showBackup(backup.id)}}, [
      el('strong', {text: backup.label}), el('small', {text: backup.invalid ? 'ファイルを確認してください' : formatBytes(backup.archive_bytes) + ' · ' + backup.file_count + ' ファイル'}),
      backup.created_at ? el('small', {text: new Date(backup.created_at).toLocaleString('ja-JP')}) : null,
    ]);
    button.disabled = !!backup.invalid; list.append(button);
  });
  if (!workspace.backups.length) list.append(el('p', {class: 'note', text: 'バックアップはまだありません。'}));
}

async function showBackup(id) {
  workspace.backupId = id; renderBackups(); const request = ++workspace.detailRequest;
  try {
    const backup = await api('GET', '/api/backups/' + encodeURIComponent(id));
    if (request !== workspace.detailRequest) return;
    const detail = wsNode('backup-detail'); clear(detail);
    const restore = workspaceButton('↶ 復元', () => openModal('バックアップを復元', 'サーバー停止中のみ実行できます。含まれるファイルを上書きし、変更前のファイルを自動退避します。バックアップに含まれないファイルは残ります。「' + backup.label + '」を復元しますか？', () => submitBackupRestore(id)), 'btn btn-primary btn-sm');
    const remove = workspaceButton('削除', () => openModal('バックアップを削除', '「' + backup.label + '」をバックアップ用ごみ箱に移動しますか？', async () => {
      try { await api('DELETE', '/api/backups/' + encodeURIComponent(id)); workspace.backupId = null; await loadBackups(); toast('ごみ箱へ移動しました', 'success'); }
      catch (error) { toast(error.message, 'error'); }
    }), 'btn btn-err btn-sm');
    restore.disabled = remove.disabled = workspace.backupBusy;
    detail.append(el('span', {class: 'empty-symbol', text: '▤'}), el('h3', {text: backup.label, style: 'margin-top:18px;'}),
      el('p', {class: 'note', text: formatBytes(backup.archive_bytes) + ' · ' + new Date(backup.created_at).toLocaleString('ja-JP')}),
      el('div', {class: 'backup-actions'}, [restore, remove]),
      el('h4', {text: 'バックアップの説明'}), el('p', {class: 'note', text: backup.description || '説明はありません。'}),
      el('p', {class: 'note', text: backup.consistency === 'live' ? '稼働中に保存要求後コピーしたバックアップです。全ファイルが同一時点とは限りません。' : 'サーバー停止中に作成したバックアップです。'}),
      el('h4', {text: '含まれるファイル · ' + backup.files.length}));
    const fileList = el('div', {class: 'backup-files'});
    backup.files.slice(0, 1000).forEach(file => fileList.append(el('div', {}, [el('span', {text: file.path}), el('span', {text: formatBytes(file.size)})])));
    if (backup.files.length > 1000) fileList.append(el('p', {class: 'note', text: '先頭1,000件を表示しています。復元時は全件が対象です。'}));
    detail.append(fileList);
  } catch (error) { toast(error.message, 'error'); }
}

function selectedBackupPaths(scope, custom = '') {
  return scope === 'all' ? ['config', 'saves'] : scope === 'custom' ? custom.split('\n').map(item => item.trim()).filter(Boolean) : [scope];
}

async function createWorkspaceBackup(event) {
  event.preventDefault();
  if (workspace.backupBusy) return;
  wsNode('backup-submit').disabled = true;
  try {
    const data = await api('POST', '/api/backups', {label: wsNode('backup-label').value.trim(), description: wsNode('backup-description').value,
      paths: selectedBackupPaths(wsNode('backup-scope').value, wsNode('backup-paths').value)});
    wsNode('backup-modal').classList.add('hidden'); renderBackupJob(data.job, true);
  } catch (error) { toast(error.message, 'error'); }
  finally { wsNode('backup-submit').disabled = false; }
}

async function submitBackupRestore(id) {
  if (workspace.backupBusy) return;
  try { const data = await api('POST', '/api/backups/' + encodeURIComponent(id) + '/restore', {confirm: 'RESTORE'}); renderBackupJob(data.job, true); }
  catch (error) { toast(error.message, 'error'); }
}

function renderBackupJob(job, submitted = false) {
  const previous = workspace.backupBusy;
  workspace.backupBusy = !!job && ['queued', 'running'].includes(job.state);
  wsNode('backup-new').disabled = workspace.backupBusy;
  document.querySelectorAll('#backup-detail .backup-actions button').forEach(button => { button.disabled = workspace.backupBusy; });
  wsNode('backup-job').className = 'backup-job' + (!job ? ' hidden' : job.state === 'failed' ? ' failed' : '');
  wsNode('backup-job').textContent = job ? (workspace.backupBusy ? '◌ ' : job.state === 'completed' ? '✓ ' : '！ ') + job.message : '';
  if (job && (previous || submitted) && !workspace.backupBusy) {
    if (job.result?.backup_id) workspace.backupId = job.result.backup_id;
    loadBackups(); loadBackupSchedule(); loadFileTree();
    toast(job.message, job.state === 'completed' ? 'success' : 'error');
  }
  workspace.lastJob = job?.id;
}

async function refreshBackupJob() {
  if (workspace.jobBusy) return;
  workspace.jobBusy = true;
  try { const data = await api('GET', '/api/backups/jobs/current'); renderBackupJob(data.job); }
  catch (error) { if (workspace.backupBusy) wsNode('backup-job').textContent = '進捗に再接続しています。結果が確認できるまで再実行しないでください。'; }
  finally { workspace.jobBusy = false; }
}

async function loadBackupSchedule() {
  try {
    const data = await api('GET', '/api/backups/schedule');
    wsNode('backup-schedule-enabled').checked = data.enabled;
    wsNode('backup-cron').value = data.cron;
    wsNode('backup-frequency').value = ['0 3 * * *', '0 */6 * * *', '0 * * * *'].includes(data.cron) ? data.cron : 'custom';
    wsNode('backup-schedule-scope').value = data.paths.length > 1 ? 'all' : data.paths[0];
    wsNode('backup-next').textContent = (data.enabled ? '次回: ' + (data.next_run ? new Date(data.next_run).toLocaleString('ja-JP', {timeZone: data.timezone}) : '確認中') : '予約は無効です') + ' · ' + data.timezone +
      (data.last_run ? ' ｜ 前回: ' + data.last_run.message : '');
    workspace.scheduleLoaded = true;
  } catch (error) { wsNode('backup-next').textContent = error.message; }
}

async function saveBackupSchedule(event) {
  event.preventDefault(); const submit = event.submitter; if (submit) submit.disabled = true;
  try {
    await api('PUT', '/api/backups/schedule', {enabled: wsNode('backup-schedule-enabled').checked, cron: wsNode('backup-cron').value.trim(), paths: selectedBackupPaths(wsNode('backup-schedule-scope').value)});
    await loadBackupSchedule(); toast('バックアップ予約を保存しました', 'success');
  } catch (error) { toast(error.message, 'error'); }
  finally { if (submit) submit.disabled = false; }
}

const iniGroups = {
  all: ['すべて', /./], server: ['サーバー', /Server|Public|Admin|RCON|REST|Coop|BanList|bUseAuth/i],
  world: ['ワールド', /Day|Night|Death|Drop|Build|BaseCamp|Work|Collection|Item|Guild/i],
  players: ['プレイヤー', /Player|Exp|PvP|Friendly|Technology/i], pals: ['パル', /Pal|Enemy|Capture|Raid|Monster/i],
};
function installIniCategories() {
  const filter = wsNode('ini-filter');
  const bar = el('div', {class: 'ini-categories', attrs: {'aria-label': '設定のカテゴリ'}});
  Object.entries(iniGroups).forEach(([key, [label]]) => bar.append(workspaceButton(label, () => {
    workspace.iniCategory = key; bar.querySelectorAll('button').forEach(button => button.classList.toggle('selected', button.textContent === label));
    filterIni(filter.value);
  }, key === 'all' ? 'selected' : '')));
  filter.before(bar);
  const original = filterIni;
  filterIni = q => {
    const source = iniValues;
    iniValues = Object.fromEntries(Object.entries(source).filter(([key]) => iniGroups[workspace.iniCategory][1].test(key)));
    original(q); iniValues = source;
  };
  const render = renderIni;
  renderIni = values => render(Object.fromEntries(Object.entries(values).filter(([key]) => iniGroups[workspace.iniCategory][1].test(key))));
}

function installConsole() {
  wsNode('tab-logs').querySelector('h2').textContent = 'コンソール';
  const form = el('form', {class: 'console-input'}, [el('span', {text: '›_'}), el('input', {attrs: {id: 'console-command', placeholder: '/Info、/ShowPlayers、/Save、/Broadcast メッセージ', 'aria-label': 'Palworld管理コマンド', autocomplete: 'off'}}), el('button', {text: '実行', class: 'btn btn-primary btn-sm', attrs: {type: 'submit'}})]);
  wsNode('tab-logs').append(form, el('p', {class: 'note', text: 'Palworld の管理コマンドを実行できます。利用できる操作は「コマンド」画面でも確認できます。'}));
  let history = [], index = 0;
  wsNode('console-command').addEventListener('keydown', event => {
    if (!['ArrowUp', 'ArrowDown'].includes(event.key)) return;
    event.preventDefault(); index = Math.min(history.length, Math.max(0, index + (event.key === 'ArrowUp' ? -1 : 1)));
    event.currentTarget.value = history[index] || '';
  });
  form.addEventListener('submit', async event => {
    event.preventDefault(); const input = wsNode('console-command'); const value = input.value.trim();
    if (!value) return;
    const [command, ...parts] = value.replace(/^\//, '').split(/\s+/); const message = parts.join(' ');
    const routes = {info: ['GET', '/api/server/info'], showplayers: ['GET', '/api/server/players'], save: ['POST', '/api/server/save'], broadcast: ['POST', '/api/server/announce', {message}]};
    const operation = routes[command.toLowerCase()];
    if (!operation) { toast('このコマンドは入力欄では実行できません。「コマンド」画面から操作してください。', 'error'); return; }
    if (command.toLowerCase() === 'broadcast' && !message) { toast('配信するメッセージを入力してください', 'error'); return; }
    const execute = async () => {
      form.querySelector('button').disabled = true;
      try { const result = await api(...operation); appendLog('[INFO] > ' + value + '\n' + JSON.stringify(result)); input.value = ''; history.push(value); history = history.slice(-50); index = history.length; }
      catch (error) { appendLog('[ERROR] ' + error.message); toast(error.message, 'error'); }
      finally { form.querySelector('button').disabled = false; input.focus(); }
    };
    if (operation[0] === 'POST') openModal('コマンドを実行', value + ' を実行しますか？', execute);
    else execute();
  });
}

function installModalAccessibility() {
  let previousFocus = null;
  document.querySelectorAll('.overlay').forEach(overlay => {
    const modal = overlay.querySelector('.modal');
    modal.setAttribute('role', 'dialog'); modal.setAttribute('aria-modal', 'true');
    if (!modal.hasAttribute('aria-labelledby')) {
      const title = modal.querySelector('h3');
      if (title) { title.id ||= overlay.id + '-heading'; modal.setAttribute('aria-labelledby', title.id); }
    }
    new MutationObserver(() => {
      if (!overlay.classList.contains('hidden')) {
        previousFocus = document.activeElement;
        wsNode('app').inert = true;
        const target = modal.querySelector('input:not([disabled]), textarea:not([disabled]), button:not([disabled])');
        if (target) target.focus();
      } else if (!document.querySelector('.overlay:not(.hidden)')) {
        wsNode('app').inert = false;
        if (previousFocus?.isConnected) previousFocus.focus();
      }
    }).observe(overlay, {attributes: true, attributeFilter: ['class']});
    overlay.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        const cancel = modal.querySelector('[id$="cancel"]'); if (cancel) cancel.click();
      }
      if (event.key !== 'Tab') return;
      const items = [...modal.querySelectorAll('button, input, select, textarea')].filter(item => !item.disabled && item.getClientRects().length);
      if (!items.length) return;
      if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items[items.length - 1].focus(); }
      else if (!event.shiftKey && document.activeElement === items[items.length - 1]) { event.preventDefault(); items[0].focus(); }
    });
  });
}
