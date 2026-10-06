export const $ = id => document.getElementById(id);
export function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
export function safeURL(value) {
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : ''; }
  catch { return ''; }
}
export function badge(status = 'unknown') {
  const name = String(status);
  return node('span', name.replaceAll('_', ' '), `badge ${name.replace(/[^a-z_]/g, '')}`);
}
export function reason(source) {
  return [source.error, ...(source.reasons || []), source.next_action].filter(Boolean)
    .map(value => typeof value === 'string' ? value : JSON.stringify(value)).join(' · ');
}
export function renderRuns(runs, selected, selectRun) {
  $('run-list').replaceChildren();
  if (!runs.length) $('run-list').append(node('p', 'Your saved research will appear here.', 'muted'));
  for (const run of runs) {
    const button = node('button', run.title || run.query || 'Untitled research', 'run-item');
    button.type = 'button';
    button.setAttribute('aria-current', String(run.id === selected));
    button.dataset.runId = run.id;
    button.append(node('small', `${String(run.status || 'active').replaceAll('_', ' ')} · ${dateLabel(run.created_at)}`));
    button.addEventListener('click', () => selectRun(run.id));
    $('run-list').append(button);
  }
}
function dateLabel(value) {
  if (!value) return 'Saved';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? 'Saved' : date.toLocaleDateString();
}
export function renderCoverage(coverage, sources) {
  const count = status => sources.filter(source => source.status === status).length;
  const collected = count('read');
  const attention = sources.filter(source => ['browser_required', 'browser_pending', 'failed', 'blocked', 'partial'].includes(source.status)).length;
  const cards = [
    ['Discovered sources', sources.length, 'Unique sources in this run'],
    ['Collected', collected, sources.length ? `${Math.round(collected / sources.length * 100)}% of discovered sources` : 'No discovered sources yet', 'success'],
    ['Needs attention', attention, 'Review reasons and next steps'],
    ['Web universe', 'Unknown', 'No completeness estimate'],
  ];
  $('coverage').replaceChildren(...cards.map(([label, value, detail, className]) => {
    const card = node('div', undefined, `metric ${className || ''}`);
    card.append(node('small', label), node('strong', String(value)), node('p', detail));
    return card;
  }));
  if (coverage) {
    $('coverage').setAttribute('title', `Requests: ${coverage.requests_used || 0}/${coverage.max_requests || '—'} · Content characters: ${coverage.content_chars || 0}/${coverage.max_content_chars || '—'}`);
  }
}
export function renderSources(sources, openSource) {
  const search = $('source-search').value.trim().toLowerCase();
  const status = $('source-filter').value;
  const visible = sources.filter(source => (!status || source.status === status)
    && `${source.title} ${source.url} ${reason(source)}`.toLowerCase().includes(search));
  $('source-count').textContent = String(sources.length);
  $('sources').replaceChildren();
  $('source-empty').hidden = visible.length > 0;
  $('source-empty').querySelector('h3').textContent = sources.length ? 'No matching sources' : 'No sources yet';
  $('source-empty').querySelector('p').textContent = sources.length ? 'Try another search or status filter.' : 'Discover sources to begin building your evidence library.';
  for (const source of visible) {
    const row = node('tr');
    const identity = node('td');
    const link = node('a', source.title || source.url, 'source-title');
    const href = safeURL(source.url);
    if (href) { link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer'; }
    identity.append(link, node('span', source.url || '', 'source-url'));
    const state = node('td');
    state.append(badge(source.status));
    if (reason(source)) state.append(node('p', reason(source), 'source-reason'));
    const method = node('td', `${source.method || 'Pending'} · ${source.attempts || 0} attempts`);
    method.append(node('p', `${source.retry_count || 0} retries`, 'source-reason'));
    const trail = node('td', (source.provenance || []).map(item => typeof item === 'string' ? item : `${item.query || 'Search'}${item.page ? ` · page ${item.page}` : ''}${item.engine ? ` · ${item.engine}` : ''}`).join('\n'), 'provenance');
    const action = node('td');
    const button = node('button', 'Inspect →', 'row-action');
    button.type = 'button'; button.dataset.sourceId = source.id;
    button.addEventListener('click', () => openSource(source.id));
    action.append(button); row.append(identity, state, method, trail, action); $('sources').append(row);
  }
}
export function updateFilters(sources) {
  const current = $('source-filter').value;
  const options = [node('option', 'All statuses'), ...[...new Set(sources.map(source => source.status))].sort().map(status => {
    const option = node('option', status.replaceAll('_', ' ')); option.value = status; return option;
  })];
  options[0].value = '';
  $('source-filter').replaceChildren(...options);
  $('source-filter').value = options.some(option => option.value === current) ? current : '';
}
export function renderDetail(source) {
  $('detail-title').textContent = source.title || 'Untitled source';
  $('detail-url').textContent = source.url || '';
  const href = safeURL(source.url);
  if (href) $('detail-url').href = href; else $('detail-url').removeAttribute('href');
  $('detail-meta').replaceChildren(badge(source.status), node('span', `${source.method || 'Not collected'} · ${source.attempts || 0} attempts · ${source.retry_count || 0} retries`));
  $('detail-reason').textContent = reason(source);
  $('detail-content').textContent = source.markdown || 'No saved content. Collect this source or import a browser result.';
  $('detail-history').textContent = JSON.stringify({provenance: source.provenance || [], history: source.history || [], reasons: source.reasons || [], next_action: source.next_action || null}, null, 2);
  $('browser-url').value = source.url || '';
  $('browser-markdown').value = '';
  $('browser-title').value = '';
  $('retry-source').disabled = source.status === 'reading';
  $('skip-source').disabled = source.status === 'skipped' || source.status === 'reading';
  $('browser-import').open = ['browser_required', 'browser_pending', 'blocked'].includes(source.status);
}
