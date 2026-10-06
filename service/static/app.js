import {$, renderRuns, renderCoverage, renderSources, updateFilters, renderDetail} from './view.js';
const state = {runs: [], run: null, sources: [], detail: null, busy: false, timer: null};
let selection = 0;
async function api(path, body) {
  const response = await fetch(`/research${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let error;
    try { error = await response.json(); } catch { error = {detail: response.statusText}; }
    const detail = typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail || error);
    throw new Error(`${response.status}: ${detail}`);
  }
  return response.json();
}
function notice(message, error = false) {
  $('notice').textContent = message; $('notice').dataset.error = String(error); $('notice').hidden = !message;
  $('detail-notice').textContent = message; $('detail-notice').hidden = !message;
}
function setBusy(busy, button, label) {
  state.busy = busy;
  for (const id of ['discover', 'collect', 'refresh', 'refresh-runs', 'new-run', 'retry-source', 'skip-source']) $(id).disabled = busy;
  for (const submit of document.querySelectorAll('button[type="submit"]')) submit.disabled = busy;
  $('main').setAttribute('aria-busy', String(busy));
  if (button) button.textContent = label;
  if (!busy && state.detail) {
    $('retry-source').disabled = state.detail.status === 'reading';
    $('skip-source').disabled = ['reading', 'skipped'].includes(state.detail.status);
  }
}
async function action(button, pending, operation, success) {
  if (state.busy) return;
  const label = button?.textContent;
  setBusy(true, button, pending); notice(pending);
  try { await operation(); notice(success); }
  catch (error) { notice(error.message, true); }
  finally { setBusy(false, button, label); scheduleRefresh(); }
}
async function loadRuns() {
  const result = await api('/runs');
  state.runs = Array.isArray(result) ? result : result.runs;
  renderRuns(state.runs, state.run?.id, selectRun);
}
async function selectRun(id) {
  if (state.busy) return;
  const token = ++selection;
  clearTimeout(state.timer);
  notice('Loading research…');
  try {
    const [run, sourceResult, coverage] = await Promise.all([api(`/runs/${encodeURIComponent(id)}`), api(`/runs/${encodeURIComponent(id)}/sources`), api(`/runs/${encodeURIComponent(id)}/coverage`)]);
    if (token !== selection) return;
    state.run = run; state.sources = Array.isArray(sourceResult) ? sourceResult : sourceResult.sources;
    showRun(coverage); notice(''); scheduleRefresh();
  } catch (error) { if (token === selection) notice(error.message, true); }
}
function showRun(coverage) {
  $('create-panel').hidden = true; $('run-panel').hidden = false;
  $('run-title').textContent = state.run.title; $('run-query').textContent = state.run.query;
  $('run-status').textContent = state.run.status || 'active';
  $('export-markdown').href = `/research/runs/${encodeURIComponent(state.run.id)}/export?format=markdown`;
  $('export-json').href = `/research/runs/${encodeURIComponent(state.run.id)}/export?format=json`;
  renderRuns(state.runs, state.run.id, selectRun); renderCoverage(coverage, state.sources);
  updateFilters(state.sources); renderSources(state.sources, openSource);
}
async function refreshCurrent() {
  if (!state.run) return;
  const id = state.run.id;
  const [run, sourceResult, coverage] = await Promise.all([api(`/runs/${id}`), api(`/runs/${id}/sources`), api(`/runs/${id}/coverage`)]);
  if (state.run?.id !== id) return;
  state.run = run; state.sources = Array.isArray(sourceResult) ? sourceResult : sourceResult.sources;
  await loadRuns(); showRun(coverage);
}
function scheduleRefresh() {
  clearTimeout(state.timer);
  if (state.sources.some(source => source.status === 'reading') && !document.hidden) {
    state.timer = setTimeout(async () => {
      if (!state.busy) { try { await refreshCurrent(); } catch (error) { notice(error.message, true); } }
      scheduleRefresh();
    }, 4000);
  }
}
async function openSource(id) {
  try {
    const source = await api(`/sources/${encodeURIComponent(id)}`);
    state.detail = source; renderDetail(source); $('source-dialog').showModal();
  } catch (error) { notice(error.message, true); }
}
function lines(id) { return $(id).value.split('\n').map(value => value.trim()).filter(Boolean); }
$('run-form').addEventListener('submit', event => {
  event.preventDefault();
  if (!$('title').value.trim() || !$('query').value.trim()) { notice('Enter a title and a research question.', true); return; }
  action(event.submitter, 'Creating research…', async () => {
    const run = await api('/runs', {title: $('title').value.trim(), query: $('query').value.trim(), variants: lines('variants'), subtopics: lines('subtopics'), domains: $('domains').value.split(',').map(value => value.trim()).filter(Boolean), language: $('language').value, max_sources: Number($('budget').value)});
    await loadRuns(); state.run = run; state.sources = []; showRun(); $('run-title').focus();
  }, 'Research created. Discover sources to begin.');
});
$('new-run').addEventListener('click', () => {
  selection++; clearTimeout(state.timer); state.run = null; state.sources = [];
  $('create-panel').hidden = false; $('run-panel').hidden = true; notice('');
  renderRuns(state.runs, null, selectRun); $('title').focus();
});
$('discover').addEventListener('click', event => action(event.currentTarget, 'Discovering…', async () => {
  await api(`/runs/${state.run.id}/discover`, {}); await refreshCurrent();
}, 'Discovery finished. Review the source library and collect the next batch.'));
$('collect').addEventListener('click', event => action(event.currentTarget, 'Collecting…', async () => {
  await api(`/runs/${state.run.id}/collect`, {batch_size: 5}); await refreshCurrent();
}, 'Collection batch finished. Sources needing a browser remain pending until a result is imported.'));
$('refresh').addEventListener('click', event => action(event.currentTarget, 'Refreshing…', refreshCurrent, 'Research refreshed.'));
$('refresh-runs').addEventListener('click', event => action(event.currentTarget, '…', loadRuns, 'Research list refreshed.'));
for (const id of ['source-search', 'source-filter']) $(id).addEventListener('input', () => renderSources(state.sources, openSource));
$('close-detail').addEventListener('click', () => $('source-dialog').close());
for (const [id, endpoint, message] of [['retry-source', 'retry', 'Source queued for retry.'], ['skip-source', 'skip', 'Source skipped.']]) {
  $(id).addEventListener('click', event => action(event.currentTarget, 'Saving…', async () => {
    const source = await api(`/sources/${state.detail.id}/${endpoint}`, {}); state.detail = source;
    renderDetail(source); await refreshCurrent();
  }, message));
}
$('browser-form').addEventListener('submit', event => {
  event.preventDefault();
  if (!$('browser-markdown').value.trim()) { notice('Paste the content collected in your browser.', true); return; }
  action(event.submitter, 'Saving content…', async () => {
    const source = await api(`/sources/${state.detail.id}/browser-result`, {url: $('browser-url').value, markdown: $('browser-markdown').value, method: $('browser-method').value, title: $('browser-title').value.trim()});
    state.detail = source; renderDetail(source); await refreshCurrent();
  }, 'Browser result saved and evaluated. Review its status and reasons.');
});
document.addEventListener('visibilitychange', scheduleRefresh);
loadRuns().catch(error => notice(`Cannot load research. ${error.message} Use refresh to try again.`, true));

async function loadBrowserStatus() {
  try {
    const response = await fetch('/browser/status');
    if (!response.ok) throw new Error('Unavailable');
    const status = await response.json();
    $('browser-status').textContent = status.configured
      ? `Browser collection configured · ${status.owned_tabs || 0} workspace-owned tabs. Review collection outcomes per source.`
      : 'Browser collection is not configured. Pending sources require Chrome DevTools, Playwright MCP, or agent-browser collection, then a browser-result import.';
  } catch {
    $('browser-status').textContent = 'Browser availability could not be checked. Inspect pending sources and import actual browser results when needed.';
  }
}
loadBrowserStatus();
