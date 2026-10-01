'use strict';

const $ = (id) => document.getElementById(id);
const SVG = 'http://www.w3.org/2000/svg';
const pages = ['overview', 'inventory', 'games', 'channels', 'settings', 'logs'];
const filterNames = ['not_linked', 'upcoming', 'expired', 'excluded', 'finished'];
// same initial state as the GUI inventory filters
const filterState = Object.fromEntries(filterNames.map((name) => [name, name === 'upcoming']));
const cache = new Map();
let filtersChosen = false;
let auth = false;
let token = '';
let readonly = false;
let state = null;
let socket = null;
let reconnectTimer = null;
let reconnectDelay = 1000;
let routeVersion = 0;
let currentPage = 'overview';
let logs = [];
let logsLoaded = false;
let autoscroll = true;
let countdownEnd = 0;
let countdownMinutes = 0;

function el(tag, { class: className, text, attrs } = {}) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  for (const [name, value] of Object.entries(attrs || {})) element.setAttribute(name, String(value));
  return element;
}

function icon(name) {
  const svg = document.createElementNS(SVG, 'svg');
  const use = document.createElementNS(SVG, 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.setAttribute('aria-hidden', 'true');
  svg.append(use);
  return svg;
}

function mark(tag, className, attrs = {}, text) {
  const element = document.createElementNS(SVG, tag);
  if (className) element.classList.add(className);
  for (const [name, value] of Object.entries(attrs)) element.setAttribute(name, String(value));
  if (text !== undefined) element.textContent = String(text);
  return element;
}

function chart(title, description) {
  const svg = mark('svg', 'chart', { viewBox: '0 0 320 190', role: 'img', 'aria-label': description });
  svg.append(mark('title', '', {}, title));
  return svg;
}

function card(title, wide = false) {
  const container = el('section', { class: wide ? 'card wide' : 'card' });
  if (title) container.append(el('h2', { text: title }));
  return container;
}

function chip(text, status = text) {
  return el('span', { class: `chip ${String(status).replace(/[^a-z_]/g, '')}`, text: String(text).replaceAll('_', ' ') });
}

function button(text, action, className = '') {
  const control = el('button', { class: className, text, attrs: { type: 'button' } });
  control.addEventListener('click', () => run(action));
  return control;
}

function image(url, label) {
  if (typeof url !== 'string' || !url.startsWith('https://static-cdn.jtvnw.net/')) {
    return el('span', { class: 'placeholder', text: '✦', attrs: { 'aria-label': label || 'Image unavailable' } });
  }
  const picture = el('img', { attrs: { alt: label || '', loading: 'lazy' } });
  picture.addEventListener('error', () => picture.replaceWith(el('span', { class: 'placeholder', text: '✦', attrs: { 'aria-label': 'Image unavailable' } })));
  picture.src = url;
  return picture;
}

function bar(progress) {
  const fraction = Math.max(0, Math.min(1, Number(progress) || 0));
  // an SVG rect width attribute keeps the page free of inline styles
  const track = document.createElementNS(SVG, 'svg');
  for (const [name, value] of Object.entries({ class: 'bar', viewBox: '0 0 100 10', preserveAspectRatio: 'none', role: 'progressbar', 'aria-valuenow': Math.round(fraction * 100), 'aria-valuemin': 0, 'aria-valuemax': 100 })) track.setAttribute(name, value);
  const fill = document.createElementNS(SVG, 'rect');
  for (const [name, value] of Object.entries({ class: 'bar-fill', width: fraction * 100, height: 10 })) fill.setAttribute(name, value);
  track.append(fill);
  return track;
}

function localTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

function searchMatch(...values) {
  const query = $('search').value.trim().toLocaleLowerCase();
  return !query || values.some((value) => String(value || '').toLocaleLowerCase().includes(query));
}

function notify(message) {
  $('message').textContent = message;
  $('message').hidden = !message;
}

function stored(store, key) {
  try { return store.getItem(key); } catch { return null; }
}

function persist(store, key, value) {
  try { store.setItem(key, value); } catch { return; }
}

function forget(store, key) {
  try { store.removeItem(key); } catch { return; }
}

function stopSocket() {
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  if (socket) {
    const previous = socket;
    socket = null;
    previous.close();
  }
}

function showAuth() {
  if (!auth) return;
  token = '';
  forget(sessionStorage, 'dashboardToken');
  stopSocket();
  $('ws-status').textContent = 'Offline';
  $('dashboard').hidden = true;
  $('auth-panel').hidden = false;
  $('auth-token').value = '';
  $('auth-token').focus();
}

async function api(path, body) {
  const headers = {};
  if (auth && token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  if (response.status === 401 && auth) {
    showAuth();
    throw new Error('Token required or invalid');
  }
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}

async function run(action) {
  notify('');
  try { await action(); } catch (error) { notify(error.message || 'Request failed'); }
}

async function cached(key, path, age = 30000) {
  const previous = cache.get(key);
  if (previous && Date.now() - previous.time < age) return previous.value;
  if (previous && previous.pending) return previous.pending;
  const pending = api(path);
  cache.set(key, { value: previous?.value, time: previous?.time || 0, pending });
  try {
    const value = await pending;
    cache.set(key, { value, time: Date.now() });
    return value;
  } catch (error) {
    if (previous?.value !== undefined) cache.set(key, previous);
    else cache.delete(key);
    throw error;
  }
}

function campaignPath() {
  const params = new URLSearchParams();
  params.set('all', filterState.all ? '1' : '0');
  for (const name of filterNames) params.set(name, filterState[name] ? '1' : '0');
  return `/api/campaigns?${params}`;
}

async function update(action, path, body) {
  await api(path, body);
  if (['priority', 'exclude', 'settings', 'reload'].includes(action)) cache.clear();
  await renderPage();
}

function connect() {
  if (auth && !token) return;
  stopSocket();
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const connection = new WebSocket(`${protocol}//${location.host}/api/ws`);
  socket = connection;
  $('ws-status').textContent = 'Connecting';
  connection.addEventListener('open', () => {
    reconnectDelay = 1000;
    $('ws-status').textContent = 'Online';
    $('ws-status').className = 'chip online';
    if (auth) connection.send(JSON.stringify({ auth: token }));
  });
  connection.addEventListener('message', (event) => {
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (data.type === 'state') {
      const { type, ...nextState } = data;
      state = nextState;
      if (!filtersChosen && state.default_filters) {
        // same initial filters as the GUI until the viewer picks their own
        for (const name of filterNames) if (typeof state.default_filters[name] === 'boolean') filterState[name] = state.default_filters[name];
      }
      readonly = Boolean(state.readonly);
      $('miner-state').textContent = state.state || 'Unknown';
      $('readonly-badge').hidden = !readonly;
      renderPage();
    } else if (data.type === 'log') appendLog(data.line);
  });
  connection.addEventListener('close', () => {
    if (socket !== connection) return;
    socket = null;
    $('ws-status').textContent = 'Offline';
    $('ws-status').className = 'chip offline';
    if (currentPage === 'overview') renderPage();
    reconnectTimer = setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 30000);
  });
  connection.addEventListener('error', () => connection.close());
}

function appendLog(value) {
  logs.push(String(value));
  logs = logs.slice(-200);
  if (currentPage === 'logs') {
    const region = $('log-lines');
    if (region && searchMatch(value)) {
      region.append(el('div', { text: value }));
      while (region.childElementCount > 200) region.firstElementChild.remove();
      if (autoscroll) region.scrollTop = region.scrollHeight;
    }
  } else if (currentPage === 'overview') renderPage();
}

function kpi(iconName, number, caption) {
  const item = card();
  item.classList.add('kpi');
  item.append(icon(iconName), el('strong', { text: number }), el('span', { class: 'muted', text: caption }));
  return item;
}

function ring(value) {
  const fraction = Math.max(0, Math.min(1, Number(value) || 0));
  const svg = chart('Current drop progress', `${Math.round(fraction * 100)} percent complete`);
  const perimeter = 2 * Math.PI * 65;
  svg.append(mark('circle', 'ring-base', { cx: 160, cy: 95, r: 65 }),
    mark('circle', 'ring-mark', { cx: 160, cy: 95, r: 65, 'stroke-dasharray': perimeter, 'stroke-dashoffset': perimeter * (1 - fraction), transform: 'rotate(-90 160 95)' }),
    mark('text', 'ring-number', { x: 160, y: 101, 'text-anchor': 'middle' }, `${Math.round(fraction * 100)}%`));
  return svg;
}

function bars(campaigns) {
  const entries = campaigns.filter((item) => item.status === 'active').sort((left, right) => Number(right.remaining_minutes) - Number(left.remaining_minutes)).slice(0, 8);
  if (!entries.length) return el('p', { class: 'muted', text: 'No active campaigns.' });
  const svg = chart('Remaining minutes per campaign', entries.map((item) => `${item.name}: ${item.remaining_minutes} minutes`).join('; '));
  const max = Math.max(1, ...entries.map((item) => Number(item.remaining_minutes) || 0));
  entries.forEach((item, index) => {
    const top = index * 23 + 4;
    svg.append(mark('text', '', { x: 0, y: top + 12 }, String(item.name).slice(0, 14)),
      mark('rect', 'bar-mark', { x: 118, y: top, width: Math.max(0, (Number(item.remaining_minutes) || 0) / max * 145), height: 14, rx: 4 }),
      mark('text', '', { x: 270, y: top + 12 }, `${Math.round(Number(item.remaining_minutes) || 0)}m`));
  });
  return svg;
}

function donut(campaigns) {
  const counts = ['active', 'upcoming', 'expired'].map((status) => campaigns.filter((item) => item.status === status).length);
  if (!campaigns.length) return el('p', { class: 'muted', text: 'No campaigns yet.' });
  const svg = chart('Campaigns by status', `Active: ${counts[0]}, upcoming: ${counts[1]}, expired: ${counts[2]}`);
  const perimeter = 2 * Math.PI * 65;
  svg.append(mark('circle', 'ring-base', { cx: 160, cy: 95, r: 65 }));
  let offset = 0;
  counts.forEach((count, index) => {
    if (!count) return;
    const length = count / campaigns.length * perimeter;
    svg.append(mark('circle', `ring-${['active', 'upcoming', 'expired'][index]}`, {
      cx: 160, cy: 95, r: 65, 'stroke-dasharray': `${length} ${perimeter - length}`,
      'stroke-dashoffset': -offset, transform: 'rotate(-90 160 95)',
    }));
    offset += length;
  });
  svg.append(mark('text', 'ring-number', { x: 160, y: 101, 'text-anchor': 'middle' }, campaigns.length));
  return svg;
}

function area(samples) {
  if (samples.length < 2) return el('p', { class: 'muted', text: 'Not enough history yet (at least 2 samples needed).' });
  const svg = chart('Session progress over time', `${samples.length} samples, from ${Math.round(Number(samples[0].progress || 0) * 100)} to ${Math.round(Number(samples.at(-1).progress || 0) * 100)} percent`);
  const start = Number(samples[0].t);
  const duration = Math.max(1, Number(samples.at(-1).t) - start);
  const points = samples.map((sample) => [12 + (Number(sample.t) - start) / duration * 296, 175 - Math.max(0, Math.min(1, Number(sample.progress) || 0)) * 155]);
  const line = points.map(([x, y]) => `${x},${y}`).join(' ');
  svg.append(mark('line', 'grid-line', { x1: 12, y1: 175, x2: 308, y2: 175 }),
    mark('polygon', 'area-mark', { points: `12,175 ${line} 308,175` }), mark('polyline', 'line-mark', { points: line }));
  return svg;
}

async function overview() {
  const [campaigns, games, progress, history, recent] = await Promise.all([
    cached('all-campaigns', '/api/campaigns?all=1'), cached('games', '/api/games'),
    api('/api/progress'), cached('history', '/api/history', 60000), cached('recent-logs', '/api/logs?tail=8', 30000),
  ]);
  if (!logs.length) logs = recent.map(String);
  const total = campaigns.reduce((sum, item) => sum + item.total_drops, 0);
  const claimed = campaigns.reduce((sum, item) => sum + item.claimed_drops, 0);
  const kpis = el('div', { class: 'kpis wide' });
  const ws = state?.websockets || {};
  kpis.append(kpi('box', `${claimed}/${total}`, 'Drops claimed'),
    kpi('home', campaigns.filter((item) => item.status === 'active').length, 'Active campaigns'),
    kpi('gamepad', `${games.filter((item) => item.status === 'mining').length}/${games.filter((item) => item.priority_pos).length}`, 'Mining / priority'),
    kpi('tv', state?.watched_channel || '—', 'Watched channel'),
    kpi('wifi', `${ws.connected || 0}/${ws.total || 0}`, 'WebSockets'));
  const current = card('Current drop');
  current.classList.add('highlight');
  if (progress) {
    const benefit = progress.benefits?.[0];
    const media = el('div', { class: 'media' });
    media.append(image(benefit?.image_url, benefit?.name), el('div', { text: `${progress.game} · ${progress.name}` }));
    current.append(media, ring(progress.progress), el('p', { text: `${progress.remaining_minutes} minutes remaining` }), el('p', { attrs: { id: 'countdown' }, class: 'muted' }));
    countdownMinutes = Number(progress.remaining_minutes) || 0;
    const timer = Number(progress.timer_seconds) || 0;
    countdownEnd = Date.now() + Math.max(0, timer > 0 ? (countdownMinutes - 1) * 60 + timer : countdownMinutes * 60) * 1000;
    tickCountdown();
  } else current.append(el('p', { class: 'muted', text: 'No active drop.' }));
  const remaining = card('Remaining minutes · active campaigns');
  remaining.append(bars(campaigns));
  const status = card('Campaigns by status');
  status.append(donut(campaigns), el('p', { class: 'chart-key', text: `Active ${campaigns.filter((item) => item.status === 'active').length} · Upcoming ${campaigns.filter((item) => item.status === 'upcoming').length} · Expired ${campaigns.filter((item) => item.status === 'expired').length}` }));
  const trend = card('Progress over time');
  trend.append(area(history));
  const events = card('Recent events');
  const matchingLogs = logs.filter((value) => searchMatch(value)).slice(-8).reverse();
  for (const line of matchingLogs) events.append(el('p', { text: line }));
  if (!matchingLogs.length) events.append(el('p', { class: 'muted', text: logs.length ? 'No events match your search.' : 'No recent events.' }));
  return [kpis, current, remaining, status, trend, events];
}

function tickCountdown() {
  const target = $('countdown');
  if (!target) return;
  const seconds = Math.max(0, Math.ceil((countdownEnd - Date.now()) / 1000));
  target.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')} remaining`;
}

function campaignCard(campaign) {
  const container = card();
  const heading = el('div', { class: 'media' });
  const label = el('div');
  label.append(el('h3', { text: campaign.name }), chip(campaign.status));
  heading.append(image(campaign.image_url, campaign.name), label);
  container.append(heading, el('div', { class: 'chips' }));
  const chips = container.lastElementChild;
  chips.append(chip(campaign.linked ? 'Linked' : 'Not linked', campaign.linked ? 'linked' : 'not_linked'));
  if (!campaign.linked && campaign.link_url) {
    try {
      const target = new URL(campaign.link_url);
      if (target.protocol === 'https:') chips.append(el('a', { class: 'link-button', text: 'Link account ↗', attrs: { href: target.href, target: '_blank', rel: 'noopener noreferrer' } }));
    } catch {}
  }
  container.append(el('p', { text: `${campaign.claimed_drops}/${campaign.total_drops} claimed · ${Math.round(Number(campaign.progress || 0) * 100)}% · ${campaign.remaining_minutes} min remaining` }),
    bar(campaign.progress), el('p', { class: 'muted', text: `Ends ${localTime(campaign.ends_at)}` }));
  if (campaign.allowed_channels?.length) {
    const acl = el('details');
    acl.open = campaign.allowed_channels.length <= 3;
    acl.append(el('summary', { text: `Allowed channels (${campaign.allowed_channels.length})` }), el('p', { text: campaign.allowed_channels.join(', ') }));
    container.append(acl);
  }
  const details = el('details');
  details.append(el('summary', { text: `Drops (${campaign.drops.length})` }));
  for (const drop of campaign.drops) {
    const entry = el('div', { class: 'entry' });
    const head = el('div', { class: 'row-head' });
    head.append(el('strong', { text: drop.name }), chip(drop.status));
    entry.append(head, el('p', { text: `${drop.current_minutes}/${drop.required_minutes} min · ${drop.remaining_minutes} min remaining` }), bar(drop.progress));
    for (const benefit of drop.benefits || []) {
      const media = el('div', { class: 'media' });
      media.append(image(benefit.image_url, benefit.name), el('span', { text: benefit.name }));
      entry.append(media);
    }
    details.append(entry);
  }
  container.append(details);
  return container;
}

async function inventory() {
  const filters = card('Campaign filters', true);
  const controls = el('div', { class: 'filter-row' });
  for (const name of [...filterNames, 'all']) {
    const input = el('input', { attrs: { type: 'checkbox' } });
    input.checked = Boolean(filterState[name]);
    input.addEventListener('change', () => {
      filterState[name] = input.checked;
      filtersChosen = true;
      persist(localStorage, 'dashboardFilters', JSON.stringify(filterState));
      run(() => renderPage());
    });
    const label = el('label');
    label.append(input, el('span', { text: name === 'all' ? 'Show all' : name.replaceAll('_', ' ') }));
    controls.append(label);
  }
  filters.append(controls);
  const campaigns = await cached(`campaigns:${campaignPath()}`, campaignPath());
  const groups = new Map();
  for (const campaign of campaigns) {
    if (!searchMatch(campaign.game, campaign.name, ...campaign.drops.map((drop) => drop.name))) continue;
    if (!groups.has(campaign.game)) groups.set(campaign.game, []);
    groups.get(campaign.game).push(campaign);
  }
  const result = [filters];
  for (const [name, entries] of [...groups].sort(([left], [right]) => left.localeCompare(right))) {
    const group = card(name, true);
    const grid = el('div', { class: 'card-grid' });
    for (const campaign of entries) grid.append(campaignCard(campaign));
    group.append(grid);
    result.push(group);
  }
  if (!groups.size) result.push(card('No campaigns match these filters.'));
  return result;
}

function channelSelector(channels, callback) {
  if (!channels.length) return el('p', { class: 'muted', text: 'No online channels' });
  const select = el('select', { attrs: { 'aria-label': 'Choose an online channel' } });
  for (const channel of channels) select.append(el('option', { text: channel.name, attrs: { value: channel.name } }));
  const action = button('Watch a channel', () => callback(select.value), 'primary');
  const wrapper = el('div', { class: 'inline' });
  wrapper.append(select, action);
  return wrapper;
}

async function gamesPage() {
  const [games, priority] = await Promise.all([cached('games', '/api/games'), cached('priority', '/api/priority')]);
  const result = [];
  for (const game of games.filter((item) => searchMatch(item.name, item.status))) {
    const entry = card(game.name);
    entry.append(chip(game.status), el('p', { text: `Priority #${game.priority_pos || '—'} · Excluded: ${game.excluded ? 'Yes' : 'No'}` }),
      el('p', { text: `Campaigns ${game.active_campaigns} active / ${game.upcoming_campaigns} upcoming` }),
      el('p', { text: `Drops ${game.claimed_drops}/${game.total_drops} · Online channels ${game.online_channels.length}` }));
    if (!readonly) {
      const actions = el('div', { class: 'actions' });
      const change = (op, pos) => update('priority', '/api/priority', { op, game: game.name, ...(pos === undefined ? {} : { pos }) });
      if (!game.priority_pos) actions.append(button('Prioritize', () => change('add')));
      else {
        if (game.priority_pos > 1) actions.append(button('↑ Move up', () => change('move', game.priority_pos - 1)));
        if (game.priority_pos < priority.priority.length) actions.append(button('↓ Move down', () => change('move', game.priority_pos + 1)));
        actions.append(button('Remove priority', () => change('remove')));
      }
      actions.append(button(game.excluded ? 'Include' : 'Exclude', () => update('exclude', '/api/exclude', { op: game.excluded ? 'remove' : 'add', game: game.name })));
      entry.append(actions, channelSelector(game.online_channels, (channel) => update('switch', '/api/switch', { channel })));
    }
    result.push(entry);
  }
  return result.length ? result : [card('No games match your search.')];
}

async function channelsPage() {
  const channels = await api('/api/channels');
  const result = card('Channels', true);
  const online = channels.filter((channel) => channel.online);
  if (!readonly) result.append(channelSelector(online, (channel) => update('switch', '/api/switch', { channel })));
  const wrapper = el('div', { class: 'table-wrap' });
  const table = el('table');
  const header = el('tr');
  for (const title of ['Channel', 'Status', 'Game', 'Viewers', 'Drops', 'ACL', 'Action']) header.append(el('th', { text: title }));
  const head = el('thead');
  head.append(header);
  const body = el('tbody');
  for (const channel of channels.filter((item) => searchMatch(item.name, item.game, item.online ? 'online' : 'offline'))) {
    const row = el('tr');
    for (const value of [channel.name, channel.online ? 'Online' : 'Offline', channel.game || '—', channel.viewers ?? '—', channel.drops_enabled ? 'Yes' : 'No', channel.acl_based ? 'Yes' : 'No']) row.append(el('td', { text: value }));
    const action = el('td');
    if (channel.watching) action.textContent = 'Watching';
    else if (!readonly && channel.online) action.append(button('Watch', () => update('switch', '/api/switch', { channel: channel.name })));
    row.append(action);
    body.append(row);
  }
  table.append(head, body);
  wrapper.append(table);
  result.append(wrapper);
  return [result];
}

function optionSelect(options, label) {
  const select = el('select', { attrs: { 'aria-label': label } });
  for (const option of options) select.append(el('option', { text: option, attrs: { value: option } }));
  return select;
}

async function settingsPage() {
  const [schema, priority, exclude, choices] = await Promise.all([
    api('/api/settings/schema'), api('/api/priority'), api('/api/exclude'), api('/api/game_choices'),
  ]);
  const result = [];
  const groups = new Map();
  for (const [key, definition] of Object.entries(schema)) {
    if (!searchMatch(key)) continue;
    const entry = el('div', { class: 'entry' });
    const groupName = ['proxy', 'connection_quality'].includes(key) ? 'Connection' :
      ['priority_mode', 'available_drops_check'].includes(key) ? 'Mining' : 'Preferences';
    if (!groups.has(groupName)) groups.set(groupName, card(groupName));
    const field = el('label', { class: 'field' });
    let input;
    if (definition.choices) {
      input = optionSelect(definition.choices, key);
      input.value = String(definition.value);
    } else if (definition.type === 'boolean') {
      input = el('input', { attrs: { type: 'checkbox', role: 'switch' } });
      input.checked = Boolean(definition.value);
    } else {
      input = el('input', { attrs: { type: definition.type === 'integer' ? 'number' : 'text' } });
      if (definition.type === 'integer') { input.min = '1'; input.max = '6'; }
      input.value = String(definition.value ?? '');
    }
    input.disabled = readonly;
    field.append(el('span', { text: key.replaceAll('_', ' ') }), input);
    entry.append(field);
    if (!readonly) {
      const save = () => update('settings', '/api/settings', { key, value: definition.type === 'boolean' ? String(input.checked) : input.value });
      if (definition.type === 'boolean' || definition.choices) input.addEventListener('change', () => run(save));
      else entry.append(button('Save', save, 'primary'));
    }
    groups.get(groupName).append(entry);
  }
  result.push(...groups.values());
  const lists = [
    ['Priority', priority.priority, 'priority'], ['Exclusion', exclude.exclude, 'exclude'],
  ];
  for (const [title, names, action] of lists) {
    if (!searchMatch(title, ...names)) continue;
    const entry = card(title);
    if (!readonly) {
      const select = optionSelect(choices, `Add game to ${title}`);
      const add = el('div', { class: 'inline' });
      add.append(select, button('Add', () => update(action, `/api/${action}`, { op: 'add', game: select.value }), 'primary'));
      entry.append(add);
    }
    const chips = el('div', { class: 'chips' });
    names.forEach((name, index) => {
      const item = el('span', { class: 'chip-item', text: name });
      if (!readonly) {
        if (action === 'priority') {
          if (index > 0) {
            const up = button('↑', () => update(action, '/api/priority', { op: 'move', game: name, pos: index }), 'icon-button');
            up.setAttribute('aria-label', `Move ${name} up`);
            item.append(up);
          }
          if (index < names.length - 1) {
            const down = button('↓', () => update(action, '/api/priority', { op: 'move', game: name, pos: index + 2 }), 'icon-button');
            down.setAttribute('aria-label', `Move ${name} down`);
            item.append(down);
          }
        }
        const remove = button('✕', () => update(action, `/api/${action}`, { op: 'remove', game: name }), 'icon-button');
        remove.setAttribute('aria-label', `Remove ${name} from ${title}`);
        item.append(remove);
      }
      chips.append(item);
    });
    entry.append(chips);
    result.push(entry);
  }
  if (!readonly && searchMatch('danger reload logout')) {
    const danger = card('Danger zone');
    danger.append(button('Reload inventory', () => update('reload', '/api/reload', {})),
      button('Logout', async () => {
        if (window.confirm('Log out of the miner?')) await update('logout', '/api/logout', { confirm: true });
      }));
    result.push(danger);
  }
  return result.length ? result : [card('No settings match your search.')];
}

async function logsPage() {
  if (!logsLoaded) {
    logs = (await api('/api/logs?tail=100')).map(String);
    logsLoaded = true;
  }
  const result = card('Logs', true);
  const toggle = el('label');
  const input = el('input', { attrs: { type: 'checkbox' } });
  input.checked = autoscroll;
  input.addEventListener('change', () => { autoscroll = input.checked; });
  toggle.append(input, el('span', { text: ' Auto-scroll' }));
  const lines = el('div', { class: 'log', attrs: { id: 'log-lines', role: 'log', 'aria-live': 'polite' } });
  for (const line of logs.filter((value) => searchMatch(value))) lines.append(el('div', { text: line }));
  result.append(toggle, lines);
  if (autoscroll) requestAnimationFrame(() => { lines.scrollTop = lines.scrollHeight; });
  return [result];
}

async function renderPage() {
  if ($('dashboard').hidden || !state) return;
  const page = currentPage;
  const version = ++routeVersion;
  try {
    const sections = await ({ overview, inventory, games: gamesPage, channels: channelsPage, settings: settingsPage, logs: logsPage })[page]();
    if (version !== routeVersion || page !== currentPage) return;
    $('page-content').replaceChildren(...sections);
  } catch (error) {
    if (version === routeVersion) notify(error.message || 'Could not load page');
  }
}

function navigate() {
  const page = location.hash === '#/' || location.hash === '' ? 'overview' : location.hash.slice(2);
  currentPage = pages.includes(page) ? page : 'overview';
  for (const link of document.querySelectorAll('.sidebar a')) {
    if (link.dataset.page === currentPage) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  $('page-title').textContent = currentPage[0].toUpperCase() + currentPage.slice(1);
  $('search').value = '';
  $('page-title').focus();
  renderPage();
}

function setTheme(value) {
  document.documentElement.dataset.theme = value;
  $('theme-toggle').replaceChildren(icon(value === 'dark' ? 'sun' : 'moon'));
  $('theme-toggle').setAttribute('aria-label', `Switch to ${value === 'dark' ? 'light' : 'dark'} theme`);
}

async function start() {
  const savedTheme = stored(localStorage, 'dashboardTheme');
  if (savedTheme === 'dark' || savedTheme === 'light') setTheme(savedTheme);
  const savedFilters = stored(localStorage, 'dashboardFilters');
  try {
    const selected = JSON.parse(savedFilters);
    for (const name of [...filterNames, 'all']) if (typeof selected?.[name] === 'boolean') filterState[name] = selected[name];
    if (selected && typeof selected === 'object') filtersChosen = true;
  } catch {}
  $('theme-toggle').addEventListener('click', () => {
    const current = document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = current === 'dark' ? 'light' : 'dark';
    setTheme(next);
    persist(localStorage, 'dashboardTheme', next);
  });
  $('search').addEventListener('input', () => renderPage());
  $('auth-form').addEventListener('submit', (event) => {
    event.preventDefault();
    token = $('auth-token').value;
    persist(sessionStorage, 'dashboardToken', token);
    $('auth-panel').hidden = true;
    $('dashboard').hidden = false;
    run(async () => { state = await api('/api/state'); readonly = Boolean(state.readonly); $('readonly-badge').hidden = !readonly; $('miner-state').textContent = state.state || 'Unknown'; await renderPage(); connect(); });
  });
  window.addEventListener('hashchange', navigate);
  navigate();
  try {
    const meta = await api('/api/meta');
    auth = Boolean(meta.auth);
    readonly = Boolean(meta.readonly);
    $('readonly-badge').hidden = !readonly;
    if (auth) token = stored(sessionStorage, 'dashboardToken') || '';
    if (auth && !token) { showAuth(); return; }
    state = await api('/api/state');
    readonly = Boolean(state.readonly);
    $('miner-state').textContent = state.state || 'Unknown';
    $('readonly-badge').hidden = !readonly;
    $('dashboard').hidden = false;
    await renderPage();
    connect();
  } catch (error) { notify(error.message || 'Connection failed'); }
}

setInterval(tickCountdown, 1000);
setInterval(() => {
  if ($('dashboard').hidden) return;
  if (['overview', 'inventory', 'games'].includes(currentPage)) renderPage();
}, 30000);
setInterval(() => {
  if (currentPage === 'overview' && !$('dashboard').hidden) {
    run(async () => { await cached('history', '/api/history', 60000); await renderPage(); });
  }
}, 60000);
start();
