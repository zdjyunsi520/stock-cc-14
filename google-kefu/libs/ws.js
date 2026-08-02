// WebSocket connection to debug server — 带心跳 + 自动重连

const WS_URL = 'ws://127.0.0.1:3123';
let ws = null;
let reconnectTimer = null;
let pingTimer = null;

export function getWS() { return ws; }

export function connectWS() {
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }

  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    console.log('[WS] connected');
    // 重新注册所有标签页
    chrome.tabs.query({}, (tabs) => {
      tabs.forEach(t => {
        if (ws?.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'register', tabId: t.id, url: t.url }));
        }
      });
    });
    // 心跳：每25秒发一次 ping 消息保持连接
    if (pingTimer) clearInterval(pingTimer);
    pingTimer = setInterval(() => {
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping' }));
      }
    }, 25000);
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg._reqId && msg.type) {
        const tabId = msg.tabId ? Number(msg.tabId) : null;
        if (tabId) forwardToTab(tabId, msg);
        else {
          chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
            if (tabs[0]) forwardToTab(tabs[0].id, msg);
          });
        }
      }
    } catch (e) {}
  };

  ws.onclose = () => {
    console.log('[WS] disconnected, reconnect in 2s');
    ws = null;
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    reconnectTimer = setTimeout(connectWS, 2000);
  };

  ws.onerror = () => {};
}

function forwardToTab(tabId, msg) {
  chrome.tabs.sendMessage(tabId, msg, (res) => {
    if (chrome.runtime.lastError) {
      if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'response', _reqId: msg._reqId, success: false, error: chrome.runtime.lastError.message }));
      return;
    }
    if (ws?.readyState === WebSocket.OPEN && res) ws.send(JSON.stringify(res));
  });
}
