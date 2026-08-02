// Tab helpers

export function sendToTab(tabId, msg, timeout = 30000) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, msg, (res) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      resolve(res);
    });
    setTimeout(() => reject(new Error('tab timeout')), timeout);
  });
}

export function findTab(pattern) {
  return new Promise((resolve) => {
    chrome.tabs.query({}, (tabs) => {
      resolve(tabs.find(t => t.url && t.url.match(pattern)));
    });
  });
}

export function waitForTab(pattern, timeout = 30000) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const check = () => {
      chrome.tabs.query({}, (tabs) => {
        const found = tabs.find(t => t.url && t.url.match(pattern));
        if (found) return resolve(found);
        if (Date.now() - start > timeout) return reject(new Error('waitForTab timeout'));
        setTimeout(check, 500);
      });
    };
    check();
  });
}

export function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
