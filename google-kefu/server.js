// Page Inspector Server
// HTTP API + WebSocket + 自动化任务

const WebSocket = require('ws');
const { execSync } = require('child_process');
const http = require('http');

const PORT = 3123;

const clients = new Map();
const pendingCallbacks = new Map();
let reqCounter = 0;

// ===== 启动前杀掉占用端口的旧进程 =====
try {
  const result = execSync(`netstat -ano | findstr :${PORT} | findstr LISTENING`, { encoding: 'utf-8' }).trim();
  if (result) {
    const lines = result.split('\n');
    for (const line of lines) {
      const parts = line.trim().split(/\s+/);
      const pid = parts[parts.length - 1];
      if (pid && pid !== '0') {
        try {
          execSync(`taskkill /F /PID ${pid}`, { encoding: 'utf-8' });
          console.log(`[BOOT] killed old process PID=${pid} on port ${PORT}`);
        } catch (_) {}
      }
    }
  }
} catch (_) {}

// ===== HTTP Server =====
const httpServer = http.createServer(async (req, res) => {
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Access-Control-Allow-Origin', '*');
  if (req.method === 'OPTIONS') { res.writeHead(200); res.end(); return; }

  const url = new URL(req.url, `http://${req.headers.host}`);
  const path = url.pathname;

  try {
    if (path === '/tabs' && req.method === 'GET') {
      const tabs = Array.from(clients.entries()).map(([id, c]) => ({ tabId: id, url: c.url }));
      res.end(JSON.stringify({ tabs }));
      return;
    }

    if (path === '/snapshot' && req.method === 'GET') {
      const tabId = url.searchParams.get('tabId') || Array.from(clients.keys())[1];
      const result = await sendToPlugin(tabId, { type: 'snapshot' });
      res.end(JSON.stringify(result));
      return;
    }

    if (path === '/execute' && req.method === 'POST') {
      const body = await readBody(req);
      const cmd = JSON.parse(body);
      const tabId = cmd.tabId || Array.from(clients.keys())[1];
      const result = await sendToPlugin(tabId, { type: 'execute', actions: cmd.actions }, cmd.timeout || 30000);
      res.end(JSON.stringify(result));
      return;
    }

    // POST /create-promo-id - start Indonesia promotion automation
    if (path === '/create-promo-id' && req.method === 'POST') {
      const body = await readBody(req);
      const cmd = body ? JSON.parse(body) : {};
      const count = cmd.count || 1;
      const tabId = cmd.tabId || findTabByUrl(/tokopedia\.com.*management/) || Array.from(clients.keys())[1];

      if (!tabId) {
        res.writeHead(400);
        res.end(JSON.stringify({ error: 'No tab found. Open the promotion list page first.' }));
        return;
      }

      const taskId = 'promo-id-' + Date.now();
      res.end(JSON.stringify({ status: 'started', taskId, tabId, count }));

      // run in background
      activeTasks.set(taskId, { status: 'running', tabId, count });
      createPromoID(tabId, count)
        .then(result => {
          activeTasks.set(taskId, { ...activeTasks.get(taskId), status: 'completed', result });
          console.log(`[Task] ${taskId} completed:`, result);
        })
        .catch(err => {
          activeTasks.set(taskId, { ...activeTasks.get(taskId), status: 'failed', error: err.message });
          console.error(`[Task] ${taskId} failed:`, err.message);
        });
      return;
    }

    // POST /create-promo-ph - Philippines promotion automation (TODO)
    if (path === '/create-promo-ph' && req.method === 'POST') {
      const body = await readBody(req);
      const cmd = body ? JSON.parse(body) : {};
      const count = cmd.count || 1;
      res.end(JSON.stringify({ error: '菲律宾版本待开发', status: 'todo' }));
      return;
    }

    // GET /task-status?taskId=xxx
    if (path === '/task-status' && req.method === 'GET') {
      const taskId = url.searchParams.get('taskId');
      const task = taskId ? activeTasks.get(taskId) : null;
      res.end(JSON.stringify({ tasks: Object.fromEntries(activeTasks), task }));
      return;
    }

    res.writeHead(404);
    res.end(JSON.stringify({ error: 'not found' }));
  } catch (e) {
    res.writeHead(500);
    res.end(JSON.stringify({ error: e.message }));
  }
});

// ===== WebSocket Server (挂载到 HTTP Server，共用端口) =====
const wss = new WebSocket.Server({ server: httpServer });

wss.on('connection', (ws) => {
  let tabId = null;
  let alive = true;

  // 心跳：30秒无消息则断开
  const heartbeat = setInterval(() => {
    if (!alive) { ws.terminate(); return; }
    alive = false;
  }, 30000);

  ws.on('message', (raw) => {
    alive = true;
    try {
      const msg = JSON.parse(raw.toString());
      if (msg.type === 'ping') return; // 应用层心跳，忽略
      if (msg.type === 'register') {
        tabId = msg.tabId;
        clients.set(tabId, { ws, url: msg.url || '' });
        console.log(`[WS] registered tab ${tabId}`);
        return;
      }
      if (msg.type === 'response' && msg._reqId && pendingCallbacks.has(msg._reqId)) {
        const { resolve } = pendingCallbacks.get(msg._reqId);
        pendingCallbacks.delete(msg._reqId);
        resolve(msg);
        return;
      }
    } catch (e) {
      console.error('[WS] error:', e.message);
    }
  });

  ws.on('close', () => {
    clearInterval(heartbeat);
    if (tabId) { clients.delete(tabId); console.log(`[WS] tab ${tabId} disconnected`); }
  });
});

// ===== Start =====
httpServer.listen(PORT, () => {
  console.log(`[HTTP+WS] listening on http://127.0.0.1:${PORT}`);
  console.log('');
  console.log('API:');
  console.log('  GET  /tabs');
  console.log('  GET  /snapshot?tabId=X');
  console.log('  POST /execute  {actions:[...]}');
  console.log('  POST /create-promo-id  {count:N}');
  console.log('  GET  /task-status?taskId=xxx');
});

// ===== Plugin Communication =====
function sendToPlugin(tabId, msg, timeout = 30000) {
  return new Promise((resolve, reject) => {
    const client = clients.get(Number(tabId));
    if (!client) return reject(new Error(`tab ${tabId} not connected`));
    const reqId = ++reqCounter;
    msg._reqId = reqId;
    pendingCallbacks.set(reqId, { resolve, reject });
    client.ws.send(JSON.stringify(msg));
    setTimeout(() => {
      if (pendingCallbacks.has(reqId)) {
        pendingCallbacks.delete(reqId);
        reject(new Error('timeout'));
      }
    }, timeout);
  });
}

async function exec(tabId, actions, timeout) {
  const res = await sendToPlugin(tabId, { type: 'execute', actions }, timeout || 60000);
  if (!res.success) throw new Error(res.error || 'execute failed');
  return res.results;
}

function findTabByUrl(pattern) {
  for (const [id, c] of clients) {
    if (c.url && c.url.match(pattern)) return id;
  }
  return null;
}

// ===== Indonesia Promotion Automation =====
async function createPromoID(tabId, count = 1) {
  const log = (msg) => console.log(`[ID-Promo] ${msg}`);
  let created = 0;

  for (let round = 0; round < count; round++) {
    log(`=== Round ${round + 1}/${count} ===`);

    // 1. make sure we're on list page
    const snap = await sendToPlugin(tabId, { type: 'snapshot' });
    const currentUrl = snap.data?.url || '';
    log(`current URL: ${currentUrl}`);

    if (!currentUrl.includes('/management')) {
      log('Not on list page, waiting for manual navigation...');
      throw new Error('Please open the promotion list page first');
    }

    // 2. query end times from list
    const queryRes = await exec(tabId, [{ type: 'query', selector: 'span.text-gray-500' }]);
    const times = queryRes[0].items;
    const endTimeStr = times[1].text;
    log(`Latest promo end time: ${endTimeStr}`);

    // 3. parse end time and calculate new times
    const endMoment = parseDateTime(endTimeStr);
    const newStart = new Date(endMoment.getTime() + 60 * 1000);
    const newEnd = new Date(newStart.getTime() + 3 * 24 * 60 * 60 * 1000);
    log(`New start: ${formatDate(newStart)} ${formatTime(newStart)}`);
    log(`New end:   ${formatDate(newEnd)} ${formatTime(newEnd)}`);

    // 4. click first Duplicate button
    await exec(tabId, [
      { type: 'click', selector: 'td.core-table-td.core-table-col-fixed-right > div.core-table-cell > span.core-table-cell-wrap-value > div.flex.w-min > button.core-btn.core-btn-secondary' },
      { type: 'wait', value: 3000 }
    ]);
    log('Clicked Duplicate');

    // 5. wait for create page to load
    await sleep(5000);
    log('Waiting for create page...');

    // 6. set start date
    await exec(tabId, [
      { type: 'click', selector: 'input[placeholder="Start time"]' },
      { type: 'wait', value: 2000 },
      { type: 'type', selector: 'input[placeholder="Start time"]', value: formatDate(newStart) },
      { type: 'wait', value: 500 },
      { type: 'press', selector: 'input[placeholder="Start time"]', key: 'Enter' },
      { type: 'wait', value: 1000 }
    ]);
    log(`Set start date: ${formatDate(newStart)}`);

    // 7. set start time
    await exec(tabId, [
      { type: 'click', selector: 'input[placeholder="Select time"]', index: 0 },
      { type: 'wait', value: 2000 },
      { type: 'type', selector: 'input[placeholder="Select time"]', index: 0, value: formatTime(newStart) },
      { type: 'wait', value: 500 },
      { type: 'press', selector: 'input[placeholder="Select time"]', index: 0, key: 'Enter' },
      { type: 'wait', value: 1000 }
    ]);
    log(`Set start time: ${formatTime(newStart)}`);

    // 8. set end date
    await exec(tabId, [
      { type: 'click', selector: 'input[placeholder="End time"]' },
      { type: 'wait', value: 2000 },
      { type: 'type', selector: 'input[placeholder="End time"]', value: formatDate(newEnd) },
      { type: 'wait', value: 500 },
      { type: 'press', selector: 'input[placeholder="End time"]', key: 'Enter' },
      { type: 'wait', value: 1000 }
    ]);
    log(`Set end date: ${formatDate(newEnd)}`);

    // 9. set end time
    await exec(tabId, [
      { type: 'click', selector: 'input[placeholder="Select time"]', index: 1 },
      { type: 'wait', value: 2000 },
      { type: 'type', selector: 'input[placeholder="Select time"]', index: 1, value: formatTime(newEnd) },
      { type: 'wait', value: 500 },
      { type: 'press', selector: 'input[placeholder="Select time"]', index: 1, key: 'Enter' },
      { type: 'wait', value: 1000 }
    ]);
    log(`Set end time: ${formatTime(newEnd)}`);

    // 10. scroll down and click Agree and publish
    await exec(tabId, [
      { type: 'scroll', value: -99999 },
      { type: 'wait', value: 500 },
      { type: 'click', selector: 'div.flex.bg-white > div.flex.justify-between > div.flex.w-full > div > button.theme-arco-btn-primary' },
      { type: 'wait', value: 3000 }
    ]);
    log('Clicked Agree and publish');

    // 11. click confirm in modal
    await exec(tabId, [
      { type: 'click', selector: 'div.theme-arco-modal button.theme-arco-btn-primary' },
      { type: 'wait', value: 5000 }
    ]);
    log('Confirmed in modal');

    created++;
    log(`Created ${created}/${count}`);

    // 12. wait for page to go back to list
    if (round < count - 1) {
      await sleep(3000);
      log('Waiting for list page...');
    }
  }

  return { success: true, created };
}

// ===== Time Helpers =====
function parseDateTime(str) {
  const [datePart, timePart] = str.split(' ');
  const [month, day, year] = datePart.split('/').map(Number);
  const [hour, minute] = timePart.split(':').map(Number);
  return new Date(year, month - 1, day, hour, minute);
}

function formatDate(d) {
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  const yyyy = d.getFullYear();
  return `${mm}/${dd}/${yyyy}`;
}

function formatTime(d) {
  let h = d.getHours();
  const m = String(d.getMinutes()).padStart(2, '0');
  const ampm = h >= 12 ? 'PM' : 'AM';
  if (h === 0) h = 12;
  else if (h > 12) h -= 12;
  return `${h}:${m} ${ampm}`;
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function readBody(req) {
  return new Promise((resolve) => {
    let data = '';
    req.on('data', (c) => data += c);
    req.on('end', () => resolve(data));
  });
}

// ===== Active tasks =====
const activeTasks = new Map();
