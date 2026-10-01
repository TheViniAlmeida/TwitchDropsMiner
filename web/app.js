'use strict';

const $ = (id) => document.getElementById(id);
let token = sessionStorage.getItem('dashboardToken') || '';
let socket = null;
let reconnectTimer = null;
let reconnectDelay = 1000;
let readonly = false;
let latestState = null;
let dropDeadline = 0;
let dropMinutes = 0;
let lastInventory = 0;
let inventoryBusy = false;
let inventoryTimer = null;
let showFinished = false;
let messageTimer = null;

function node(tag, value, className) {
  const element = document.createElement(tag);
  if (value !== undefined && value !== null) element.textContent = String(value);
  if (className) element.className = className;
  return element;
}

function setMessage(value) {
  const area = $('message');
  area.textContent = String(value);
  area.hidden = !value;
  clearTimeout(messageTimer);
  if (value) messageTimer = setTimeout(() => { area.hidden = true; }, 8000);
}

function setConnection(value) {
  const indicator = $('ws-status');
  indicator.textContent = value;
  indicator.className = 'indicator ' + value.toLowerCase();
}

function stopSocket() {
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  if (socket) {
    const old = socket;
    socket = null;
    old.close();
  }
}

function showAuth() {
  token = '';
  sessionStorage.removeItem('dashboardToken');
  stopSocket();
  setConnection('Offline');
  $('dashboard').hidden = true;
  $('auth-panel').hidden = false;
  $('auth-token').value = '';
  $('auth-token').focus();
}

async function api(path, body) {
  const options = { headers: { Authorization: 'Bearer ' + token } };
  if (body !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    throw new Error('Connection failed');
  }
  if (response.status === 401) {
    showAuth();
    throw new Error('Token required or expired');
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

async function run(action) {
  try { return await action(); }
  catch (error) { setMessage(error.message || 'Request failed'); return null; }
}

function safeImage(url) {
  if (typeof url !== 'string' || !url.startsWith('https://static-cdn.jtvnw.net/')) return null;
  const img = node('img');
  img.src = url;
  img.alt = '';
  img.loading = 'lazy';
  return img;
}

function percent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.min(100, number)) : 0;
}

function localDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '—' : date.toLocaleString();
}

function displayCountdown() {
  if (!latestState || !latestState.current_drop) return;
  const seconds = Math.max(0, Math.ceil((dropDeadline - Date.now()) / 1000));
  $('drop-remaining').textContent = `${dropMinutes} min ${seconds} s`;
}

function renderState(data) {
  if (!data || typeof data !== 'object') return;
  latestState = data;
  readonly = Boolean(data.readonly);
  $('dashboard').hidden = false;
  $('auth-panel').hidden = true;
  $('readonly-badge').hidden = !readonly;
  document.querySelectorAll('.writes').forEach((control) => { control.hidden = readonly; });
  $('miner-state').textContent = data.state || 'Unknown';
  $('watched-channel').textContent = data.watched_channel || 'None';
  const drop = data.current_drop;
  $('drop-game').textContent = drop ? drop.game || '—' : '—';
  $('drop-reward').textContent = drop ? drop.reward || '—' : 'No active drop';
  const progress = percent(drop && drop.progress);
  $('drop-progress').value = progress;
  $('drop-percent').textContent = `${Math.round(progress)}%`;
  dropMinutes = drop ? Math.max(0, Number(drop.remaining_minutes) || 0) : 0;
  dropDeadline = Date.now() + Math.max(0, Number(drop && drop.timer_seconds) || 0) * 1000;
  $('drop-remaining').textContent = drop ? '' : '—';
  displayCountdown();
  refreshInventory(false);
  refreshChannels();
}

function renderChannels(channels) {
  const body = $('channels-body');
  body.replaceChildren();
  for (const channel of channels) {
    const row = node('tr');
    for (const value of [channel.name, channel.online ? 'Online' : 'Offline', channel.game || '—',
      channel.viewers ?? '—', channel.drops_enabled ? 'Yes' : 'No', channel.acl_based ? 'Yes' : 'No']) {
      row.append(node('td', value));
    }
    const action = node('td');
    if (channel.watching) action.textContent = 'Watching';
    else if (!readonly) {
      const button = node('button', 'Watch', 'writes');
      button.type = 'button';
      button.addEventListener('click', () => run(async () => {
        await api('/api/switch', { channel: channel.name });
        await refreshChannels();
      }));
      action.append(button);
    }
    row.append(action);
    body.append(row);
  }
  if (!channels.length) {
    const row = node('tr');
    const cell = node('td', 'No channels available');
    cell.colSpan = 7;
    row.append(cell);
    body.append(row);
  }
}

async function refreshChannels() {
  const channels = await run(() => api('/api/channels'));
  if (channels) renderChannels(channels);
}

function renderInventory(campaigns) {
  const list = $('inventory-list');
  list.replaceChildren();
  if (!campaigns.length) { list.append(node('p', 'No campaigns available.')); return; }
  for (const campaign of campaigns) {
    const article = node('article', undefined, 'campaign');
    const img = safeImage(campaign.image_url);
    if (img) article.append(img);
    const content = node('div', undefined, 'campaign-content');
    content.append(node('strong', `${campaign.game || '—'} · ${campaign.name || '—'}`));
    const status = campaign.expired ? 'Expired' : campaign.finished ? 'Finished' : 'Active';
    content.append(node('p', `${status} · ${campaign.claimed_drops ?? 0}/${campaign.total_drops ?? 0} claimed · Ends ${localDate(campaign.ends_at)}`));
    const bar = node('progress');
    bar.max = 100;
    bar.value = percent(campaign.progress);
    bar.setAttribute('aria-label', `${campaign.name || 'Campaign'} progress`);
    content.append(bar, node('small', `${Math.round(percent(campaign.progress))}%`));
    if (Array.isArray(campaign.drops) && campaign.drops.length) {
      const details = node('details');
      details.append(node('summary', `${campaign.drops.length} drops`));
      const drops = node('ul');
      for (const drop of campaign.drops) {
        drops.append(node('li', `${drop.name || 'Drop'} · ${drop.reward || '—'} · ${Math.round(percent(drop.progress))}%${drop.claimed ? ' · Claimed' : ''}`));
      }
      details.append(drops);
      content.append(details);
    }
    article.append(content);
    list.append(article);
  }
}

async function refreshInventory(force = true) {
  if (force && inventoryTimer) {
    clearTimeout(inventoryTimer);
    inventoryTimer = null;
  }
  const wait = inventoryBusy ? 1000 : Math.max(0, 30000 - (Date.now() - lastInventory));
  if (inventoryBusy || (!force && wait > 0)) {
    if (!inventoryTimer) inventoryTimer = setTimeout(() => {
      inventoryTimer = null;
      refreshInventory(false);
    }, wait);
    return;
  }
  inventoryBusy = true;
  try {
    const campaigns = await api(`/api/inventory?all=${showFinished ? 1 : 0}`);
    renderInventory(campaigns);
    lastInventory = Date.now();
  } catch (error) { setMessage(error.message || 'Inventory refresh failed'); }
  finally { inventoryBusy = false; }
}

function actionButton(label, callback, disabled = false) {
  const button = node('button', label, 'secondary writes');
  button.type = 'button';
  button.disabled = disabled;
  button.addEventListener('click', () => run(callback));
  return button;
}

function renderPriority(games) {
  const list = $('priority-list');
  list.replaceChildren();
  games.forEach((game, index) => {
    const item = node('li', game + ' ');
    if (!readonly) {
      item.append(actionButton('↑', async () => {
        await api('/api/priority', { op: 'move', game, pos: index });
        await refreshFilters();
      }, index === 0));
      item.append(actionButton('↓', async () => {
        await api('/api/priority', { op: 'move', game, pos: index + 2 });
        await refreshFilters();
      }, index === games.length - 1));
      item.append(actionButton('Remove', async () => {
        await api('/api/priority', { op: 'remove', game });
        await refreshFilters();
      }));
    }
    list.append(item);
  });
  if (!games.length) list.append(node('li', 'No priority games'));
}

function renderExclude(games) {
  const list = $('exclude-list');
  list.replaceChildren();
  for (const game of games) {
    const item = node('li', game + ' ');
    if (!readonly) item.append(actionButton('Remove', async () => {
      await api('/api/exclude', { op: 'remove', game });
      await refreshFilters();
    }));
    list.append(item);
  }
  if (!games.length) list.append(node('li', 'No excluded games'));
}

async function refreshFilters() {
  const values = await run(() => Promise.all([
    api('/api/priority'), api('/api/exclude')
  ]));
  if (values) {
    renderPriority(values[0].priority || []);
    renderExclude(values[1].exclude || []);
  }
}

function renderSettings(settings) {
  const list = $('settings-list');
  list.replaceChildren();
  for (const [key, value] of Object.entries(settings)) {
    const form = node('form', undefined, 'setting-row');
    const label = node('label', key.replaceAll('_', ' '));
    const input = node('input');
    input.id = `setting-${key}`;
    input.value = value;
    input.required = true;
    label.htmlFor = input.id;
    form.append(label, input);
    if (!readonly) {
      const button = node('button', 'Save', 'writes');
      button.type = 'submit';
      form.append(button);
      form.addEventListener('submit', (event) => {
        event.preventDefault();
        run(async () => {
          const result = await api('/api/settings', { key, value: input.value });
          input.value = result.value;
          setMessage(result.warning || `${key} saved`);
        });
      });
    } else input.readOnly = true;
    list.append(form);
  }
}

async function refreshSettings() {
  const settings = await run(() => api('/api/settings'));
  if (settings) renderSettings(settings);
}

function appendLog(line) {
  const area = $('log-lines');
  const follow = area.scrollTop + area.clientHeight >= area.scrollHeight - 16;
  area.append(node('div', line));
  while (area.childElementCount > 1000) area.firstElementChild.remove();
  if (follow) area.scrollTop = area.scrollHeight;
}

async function loadInitial() {
  const games = await run(() => api('/api/games'));
  if (games) {
    const list = $('games');
    list.replaceChildren();
    for (const game of games) {
      const option = node('option');
      option.value = game;
      list.append(option);
    }
  }
  await Promise.all([refreshFilters(), refreshSettings()]);
  const logs = await run(() => api('/api/logs?tail=100'));
  if (logs) {
    $('log-lines').replaceChildren();
    logs.forEach(appendLog);
  }
}

function connectSocket() {
  if (socket || $('auth-panel').hidden === false) return;
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${protocol}//${location.host}/api/ws`);
  socket = ws;
  setConnection('Reconnecting');
  ws.addEventListener('open', () => {
    if (socket !== ws) return;
    reconnectDelay = 1000;
    setConnection('Connected');
    if (token) ws.send(JSON.stringify({ auth: token }));
  });
  ws.addEventListener('message', (event) => {
    if (socket !== ws) return;
    try {
      const data = JSON.parse(event.data);
      if (data.type === 'state') renderState(data);
      else if (data.type === 'log') appendLog(data.line);
    } catch (error) { setMessage('Invalid WebSocket message'); }
  });
  ws.addEventListener('close', (event) => {
    if (socket !== ws) return;
    socket = null;
    if (event.code === 1008) { showAuth(); return; }
    setConnection('Reconnecting');
    reconnectTimer = setTimeout(connectSocket, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 30000);
  });
  ws.addEventListener('error', () => { if (socket === ws) setConnection('Offline'); });
}

$('auth-form').addEventListener('submit', (event) => {
  event.preventDefault();
  token = $('auth-token').value.trim();
  sessionStorage.setItem('dashboardToken', token);
  run(async () => {
    const state = await api('/api/state');
    renderState(state);
    await loadInitial();
    connectSocket();
  });
});
$('show-finished').addEventListener('change', (event) => {
  showFinished = event.target.checked;
  refreshInventory();
});
$('refresh-inventory').addEventListener('click', () => refreshInventory());
$('priority-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const input = $('priority-game');
  run(async () => { await api('/api/priority', { op: 'add', game: input.value.trim() }); input.value = ''; await refreshFilters(); });
});
$('exclude-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const input = $('exclude-game');
  run(async () => { await api('/api/exclude', { op: 'add', game: input.value.trim() }); input.value = ''; await refreshFilters(); });
});
$('reload').addEventListener('click', () => {
  if (confirm('Reload inventory?')) run(async () => { await api('/api/reload', {}); await refreshInventory(); });
});
$('logout').addEventListener('click', () => {
  if (confirm('Logout from Twitch?')) run(async () => { await api('/api/logout', { confirm: true }); setMessage('Logged out'); });
});
setInterval(displayCountdown, 1000);
run(async () => {
  const state = await api('/api/state');
  renderState(state);
  await loadInitial();
  connectSocket();
});
