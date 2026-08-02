// Background service worker - entry point + message routing
import { connectWS, getWS } from './libs/ws.js';
import { createPromoID } from './libs/promo-id.js';
import { createPromoPH } from './libs/promo-ph.js';
import { startAutoReply, stopAutoReply, getAutoReplyState } from './libs/auto-reply.js';

let taskState = { status: 'idle' };

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'register' && sender.tab) {
    const ws = getWS();
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'register', tabId: sender.tab.id, url: sender.tab.url }));
    }
    sendResponse({ ok: true });
    return;
  }

  if (msg.type === 'startCreatePromoID') {
    taskState = { status: 'running', region: 'ID', count: msg.count };
    sendResponse({ status: 'started' });
    createPromoID(msg.count || 1)
      .then(result => { taskState = { status: 'completed', result }; console.log('[ID-Promo] done:', result); })
      .catch(err => { taskState = { status: 'failed', error: err.message }; console.error('[ID-Promo] failed:', err.message); });
    return;
  }

  if (msg.type === 'startCreatePromoPH') {
    taskState = { status: 'running', region: 'PH', count: msg.count };
    sendResponse({ status: 'started' });
    createPromoPH(msg.count || 1)
      .then(result => { taskState = { status: 'completed', result }; console.log('[PH-Promo] done:', result); })
      .catch(err => { taskState = { status: 'failed', error: err.message }; console.error('[PH-Promo] failed:', err.message); });
    return;
  }

  if (msg.type === 'taskState') {
    sendResponse(taskState);
    return;
  }

  // Auto-reply controls
  if (msg.type === 'startAutoReply') {
    const { apiKey, baseURL, model, systemPrompt, intervalSec } = msg;
    if (!apiKey) { sendResponse({ error: 'API key required' }); return; }
    const config = { apiKey, baseURL, model };
    chrome.storage.local.set({ autoReplyConfig: { ...config, systemPrompt, intervalSec } });
    const result = startAutoReply(config, systemPrompt, intervalSec);
    sendResponse(result);
    return;
  }

  if (msg.type === 'stopAutoReply') {
    const result = stopAutoReply();
    sendResponse(result);
    return;
  }

  if (msg.type === 'autoReplyState') {
    sendResponse(getAutoReplyState());
    return;
  }
});

chrome.runtime.onInstalled.addListener(() => connectWS());
chrome.runtime.onStartup.addListener(() => connectWS());
connectWS();

// 定期重新注册所有标签页（防止连接断开后丢失）
setInterval(() => {
  const ws = getWS();
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  chrome.tabs.query({}, (tabs) => {
    for (const tab of tabs) {
      if (!tab.url || tab.url.startsWith('chrome://') || tab.url.startsWith('chrome-extension://')) continue;
      ws.send(JSON.stringify({ type: 'register', tabId: tab.id, url: tab.url }));
    }
  });
}, 15000);
