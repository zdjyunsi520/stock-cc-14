const logEl = document.getElementById('log');
function log(t) {
  const time = new Date().toLocaleTimeString();
  logEl.textContent += '\n[' + time + '] ' + t;
  logEl.scrollTop = logEl.scrollHeight;
}

log('popup.js loaded');

// ===== Load saved config =====
chrome.storage.local.get('autoReplyConfig', (data) => {
  if (data?.autoReplyConfig) {
    const c = data.autoReplyConfig;
    if (c.apiKey) document.getElementById('apiKey').value = c.apiKey;
    if (c.baseURL) document.getElementById('arBaseURL').value = c.baseURL;
    if (c.model) document.getElementById('arModel').value = c.model;
    if (c.intervalSec) document.getElementById('arInterval').value = c.intervalSec;
  }
});

// ===== Promo: Indonesia =====
document.getElementById('btnID').addEventListener('click', async () => {
  const count = parseInt(document.getElementById('countID').value) || 1;
  logEl.textContent = '';
  log('启动印尼促销, count=' + count);

  chrome.runtime.sendMessage({ type: 'startCreatePromoID', count }, (res) => {
    if (chrome.runtime.lastError) { log('错误: ' + chrome.runtime.lastError.message); return; }
    if (res?.error) { log('错误: ' + res.error); return; }
    log('已启动, 轮询状态...');

    const poll = setInterval(() => {
      chrome.runtime.sendMessage({ type: 'taskState' }, (d) => {
        if (chrome.runtime.lastError) { clearInterval(poll); return; }
        log('状态: ' + d?.status);
        if (d?.status === 'completed') { log('完成! 创建了 ' + (d.result?.created || 0) + ' 个'); clearInterval(poll); }
        else if (d?.status === 'failed') { log('失败: ' + d.error); clearInterval(poll); }
      });
    }, 2000);
  });
});

// ===== Promo: Philippines =====
document.getElementById('btnPH').addEventListener('click', async () => {
  const count = parseInt(document.getElementById('countPH').value) || 1;
  logEl.textContent = '';
  log('启动菲律宾促销, count=' + count);

  chrome.runtime.sendMessage({ type: 'startCreatePromoPH', count }, (res) => {
    if (chrome.runtime.lastError) { log('错误: ' + chrome.runtime.lastError.message); return; }
    if (res?.error) { log('错误: ' + res.error); return; }
    log('已启动, 轮询状态...');

    const poll = setInterval(() => {
      chrome.runtime.sendMessage({ type: 'taskState' }, (d) => {
        if (chrome.runtime.lastError) { clearInterval(poll); return; }
        log('状态: ' + d?.status);
        if (d?.status === 'completed') { log('完成! 创建了 ' + (d.result?.created || 0) + ' 个'); clearInterval(poll); }
        else if (d?.status === 'failed') { log('失败: ' + d.error); clearInterval(poll); }
      });
    }, 2000);
  });
});

// ===== Auto-Reply =====
const btnStart = document.getElementById('btnStartAR');
const btnStop = document.getElementById('btnStopAR');
const arStatusEl = document.getElementById('arStatus');

function updateARUI(state) {
  const running = state?.status === 'running';
  arStatusEl.textContent = running ? '运行中' : '已停止';
  arStatusEl.className = 'status ' + (running ? 'running' : 'stopped');
  btnStart.disabled = running;
  btnStop.disabled = !running;
}

let arPollTimer = null;
function startARPoll() {
  if (arPollTimer) clearInterval(arPollTimer);
  arPollTimer = setInterval(() => {
    chrome.runtime.sendMessage({ type: 'autoReplyState' }, (d) => {
      if (!chrome.runtime.lastError && d) updateARUI(d);
    });
  }, 3000);
}

btnStart.addEventListener('click', () => {
  const apiKey = document.getElementById('apiKey').value.trim();
  if (!apiKey) { log('请输入API Key'); return; }
  const baseURL = document.getElementById('arBaseURL').value.trim() || undefined;
  const model = document.getElementById('arModel').value.trim() || undefined;
  const intervalSec = parseInt(document.getElementById('arInterval').value) || 10;

  log('启动自动回复, 间隔: ' + intervalSec + 's, model: ' + (model || 'default'));
  chrome.runtime.sendMessage({ type: 'startAutoReply', apiKey, baseURL, model, intervalSec }, (res) => {
    if (chrome.runtime.lastError) { log('错误: ' + chrome.runtime.lastError.message); return; }
    if (res?.error) { log('错误: ' + res.error); return; }
    log('自动回复已启动');
    updateARUI({ status: 'running' });
    startARPoll();
  });
});

btnStop.addEventListener('click', () => {
  log('停止自动回复');
  chrome.runtime.sendMessage({ type: 'stopAutoReply' }, (res) => {
    if (chrome.runtime.lastError) { log('错误: ' + chrome.runtime.lastError.message); return; }
    log('已停止');
    updateARUI({ status: 'stopped' });
    if (arPollTimer) { clearInterval(arPollTimer); arPollTimer = null; }
  });
});

// init: check current state
chrome.runtime.sendMessage({ type: 'autoReplyState' }, (d) => {
  if (!chrome.runtime.lastError && d) updateARUI(d);
});
