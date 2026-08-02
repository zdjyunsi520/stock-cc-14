// Content script - page automation engine
(() => {
  if (window.__pageInspectorLoaded) return;
  window.__pageInspectorLoaded = true;

  // ========== Utilities ==========
  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

  function find(s, idx) {
    if (idx !== undefined) {
      const all = document.querySelectorAll(s);
      if (!all[idx]) throw new Error(`not found: ${s}[${idx}]`);
      return all[idx];
    }
    const e = document.querySelector(s);
    if (!e) throw new Error('not found: ' + s);
    return e;
  }

  function sel(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.name) return `[name="${el.name}"]`;
    const parts = [];
    let cur = el;
    for (let i = 0; i < 5; i++) {
      if (!cur || cur === document.body) break;
      let s = cur.tagName.toLowerCase();
      if (cur.id) { parts.unshift('#' + CSS.escape(cur.id)); break; }
      if (cur.className && typeof cur.className === 'string') {
        const c = cur.className.trim().split(/\s+/).filter(x => x && !/\d/.test(x)).slice(0, 2).join('.');
        if (c) s += '.' + c;
      }
      parts.unshift(s);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  }

  // ========== Execute Actions ==========
  async function executeActions(actions) {
    const results = [];
    for (const a of actions) {
      try { results.push(await runOne(a)); }
      catch (e) { results.push({ action: a.type, success: false, error: e.message }); if (a.abortOnError) break; }
    }
    return results;
  }

  async function runOne(a) {
    if (a.delay) await sleep(a.delay);
    switch (a.type) {
      case 'click': { const e = find(a.selector, a.index); e.scrollIntoView({ block: 'center' }); await sleep(100); e.click(); return { action: 'click', selector: a.selector, index: a.index, success: true }; }
      case 'type': {
        const e = find(a.selector, a.index); e.scrollIntoView({ block: 'center' }); e.focus();
        await sleep(50);
        document.execCommand('selectAll', false, null);
        await sleep(30);
        document.execCommand('delete', false, null);
        await sleep(30);
        for (const ch of a.value) {
          e.dispatchEvent(new KeyboardEvent('keydown', { key: ch, bubbles: true }));
          e.dispatchEvent(new KeyboardEvent('keypress', { key: ch, bubbles: true }));
          document.execCommand('insertText', false, ch);
          e.dispatchEvent(new KeyboardEvent('keyup', { key: ch, bubbles: true }));
          await sleep(20 + Math.random() * 30);
        }
        e.dispatchEvent(new Event('input', { bubbles: true }));
        e.dispatchEvent(new Event('change', { bubbles: true }));
        return { action: 'type', selector: a.selector, success: true };
      }
      case 'press': {
        const e = a.selector ? find(a.selector, a.index) : document.activeElement;
        const key = a.key || 'Enter';
        e.dispatchEvent(new KeyboardEvent('keydown', { key, code: key === 'Enter' ? 'Enter' : key, keyCode: key === 'Enter' ? 13 : 0, bubbles: true }));
        e.dispatchEvent(new KeyboardEvent('keypress', { key, code: key === 'Enter' ? 'Enter' : key, keyCode: key === 'Enter' ? 13 : 0, bubbles: true }));
        e.dispatchEvent(new KeyboardEvent('keyup', { key, code: key === 'Enter' ? 'Enter' : key, keyCode: key === 'Enter' ? 13 : 0, bubbles: true }));
        return { action: 'press', key, success: true };
      }
      case 'select': { const e = find(a.selector); e.value = a.value; e.dispatchEvent(new Event('change', { bubbles: true })); return { action: 'select', selector: a.selector, success: true }; }
      case 'wait': await sleep(a.value || 1000); return { action: 'wait', ms: a.value || 1000, success: true };
      case 'waitFor': {
        const t = a.timeout || 10000, s = Date.now();
        while (Date.now() - s < t) { if (document.querySelector(a.selector)) return { action: 'waitFor', selector: a.selector, success: true }; await sleep(300); }
        throw new Error('waitFor timeout: ' + a.selector);
      }
      case 'scroll': window.scrollBy({ top: a.value || 500, behavior: 'instant' }); return { action: 'scroll', success: true };
      case 'clickOk': {
        // Look for confirm button inside the date picker popup
        const picker = document.querySelector('[class*="date-picker-trigger"]');
        if (picker) {
          const btn = picker.querySelector('button[class*="btn-primary"]');
          if (btn) { btn.click(); return { action: 'clickOk', source: 'picker', success: true }; }
        }
        throw new Error('OK button not found in picker');
      }
      case 'clickDateCell': {
        const picker = document.querySelector('[class*="date-picker-trigger"]');
        if (!picker) throw new Error('date picker not open');
        const dayStr = String(a.day);
        const tds = picker.querySelectorAll('td');
        for (const td of tds) {
          const text = td.textContent.trim();
          if (text === dayStr) {
            td.click();
            return { action: 'clickDateCell', day: a.day, success: true };
          }
        }
        throw new Error('date cell not found for day: ' + a.day);
      }
      case 'query': {
        const all = document.querySelectorAll(a.selector);
        const items = Array.from(all).map((el, i) => ({ index: i, text: (el.textContent || '').trim().slice(0, 200), tag: el.tagName, selector: sel(el) }));
        return { action: 'query', selector: a.selector, count: items.length, items };
      }
      case 'getSelector': {
        const el = a.selector ? find(a.selector, a.index) : document.activeElement;
        return { action: 'getSelector', selector: sel(el), tag: el.tagName, text: (el.textContent || '').trim().slice(0, 100) };
      }
      case 'eval': {
        // 在页面上下文执行任意 JS，返回值会被 JSON.stringify
        const fn = new Function('return (' + a.code + ')()');
        const result = await fn();
        return { action: 'eval', success: true, result };
      }
      case 'fetchUrl': {
        // content script 自己发 fetch（不受页面 CSP 限制，带页面 origin 的 cookie）
        const r = await fetch(a.url, {
          method: a.method || 'GET',
          headers: a.headers || {},
          credentials: 'include',  // 带页面的 cookie
        });
        const text = await r.text();
        return { action: 'fetchUrl', success: true, status: r.status, len: text.length, text: a.maxLen ? text.slice(0, a.maxLen) : text };
      }
      case 'captureNet': {
        // 注入脚本到页面（MAIN world），劫持 fetch/XHR 记录到 window.__captured
        // 用 script tag 注入绕过 CSP（script src 不被 inline CSP 限制，但 inline script 受限）
        // 所以这里改用 content script 自己 hook，但只能看到 content script 的 fetch
        // 折中：注入 data:text/javascript 形式的 script
        return { action: 'captureNet', success: false, error: 'not implemented, use fetchUrl instead' };
      }
      default: throw new Error('unknown: ' + a.type);
    }
  }

  // ========== Time Helpers ==========
  function parseDateTime(str) {
    const [datePart, timePart] = str.split(' ');
    const [month, day, year] = datePart.split('/').map(Number);
    const [hour, minute] = timePart.split(':').map(Number);
    return new Date(year, month - 1, day, hour, minute);
  }

  function fmtDate(d) {
    return `${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')}/${d.getFullYear()}`;
  }

  function fmtTime(d) {
    let h = d.getHours();
    const m = String(d.getMinutes()).padStart(2, '0');
    const ampm = h >= 12 ? 'PM' : 'AM';
    if (h === 0) h = 12; else if (h > 12) h -= 12;
    return `${h}:${m} ${ampm}`;
  }

  // ========== Indonesia: Create Promo ==========
  async function createPromoID(count) {
    let created = 0;
    for (let round = 0; round < count; round++) {
      // check we're on list page
      if (!location.href.includes('/management')) {
        throw new Error('请先打开促销列表页');
      }

      // 1. get end time from list
      const queryRes = await executeActions([{ type: 'query', selector: 'span.text-gray-500' }]);
      const times = queryRes[0].items;
      const endTimeStr = times[1].text; // second item is end time of first row

      // 2. calculate new times
      const endMoment = parseDateTime(endTimeStr);
      const newStart = new Date(endMoment.getTime() + 60000);
      const newEnd = new Date(newStart.getTime() + 3 * 86400000);

      // 3. click Duplicate
      await executeActions([
        { type: 'click', selector: 'td.core-table-td.core-table-col-fixed-right > div.core-table-cell > span.core-table-cell-wrap-value > div.flex.w-min > button.core-btn.core-btn-secondary' },
        { type: 'wait', value: 3000 }
      ]);

      // 4. wait for create page
      await sleep(5000);

      // 5-8. set dates and times
      await executeActions([
        { type: 'click', selector: 'input[placeholder="Start time"]' },
        { type: 'wait', value: 2000 },
        { type: 'type', selector: 'input[placeholder="Start time"]', value: fmtDate(newStart) },
        { type: 'wait', value: 500 },
        { type: 'press', selector: 'input[placeholder="Start time"]', key: 'Enter' },
        { type: 'wait', value: 1000 },

        { type: 'click', selector: 'input[placeholder="Select time"]', index: 0 },
        { type: 'wait', value: 2000 },
        { type: 'type', selector: 'input[placeholder="Select time"]', index: 0, value: fmtTime(newStart) },
        { type: 'wait', value: 500 },
        { type: 'press', selector: 'input[placeholder="Select time"]', index: 0, key: 'Enter' },
        { type: 'wait', value: 1000 },

        { type: 'click', selector: 'input[placeholder="End time"]' },
        { type: 'wait', value: 2000 },
        { type: 'type', selector: 'input[placeholder="End time"]', value: fmtDate(newEnd) },
        { type: 'wait', value: 500 },
        { type: 'press', selector: 'input[placeholder="End time"]', key: 'Enter' },
        { type: 'wait', value: 1000 },

        { type: 'click', selector: 'input[placeholder="Select time"]', index: 1 },
        { type: 'wait', value: 2000 },
        { type: 'type', selector: 'input[placeholder="Select time"]', index: 1, value: fmtTime(newEnd) },
        { type: 'wait', value: 500 },
        { type: 'press', selector: 'input[placeholder="Select time"]', index: 1, key: 'Enter' },
        { type: 'wait', value: 1000 },

        // submit
        { type: 'scroll', value: -99999 },
        { type: 'wait', value: 500 },
        { type: 'click', selector: 'div.flex.bg-white > div.flex.justify-between > div.flex.w-full > div > button.theme-arco-btn-primary' },
        { type: 'wait', value: 3000 },

        // confirm modal
        { type: 'click', selector: 'div.theme-arco-modal button.theme-arco-btn-primary' },
        { type: 'wait', value: 5000 }
      ]);

      created++;

      // wait for list page to reload
      if (round < count - 1) {
        await sleep(3000);
      }
    }
    return { created };
  }

  // ========== Philippines: Create Promo (TODO) ==========
  async function createPromoPH(count) {
    throw new Error('菲律宾版本待开发');
  }

  // ========== Task State ==========
  let taskState = { status: 'idle' };

  // ========== Message Handler ==========
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === 'ping') {
      sendResponse({ ok: true });
      return;
    }

    if (msg.type === 'startCreatePromoID') {
      taskState = { status: 'running', region: 'ID', count: msg.count };
      sendResponse({ status: 'started' });
      createPromoID(msg.count || 1)
        .then(result => { taskState = { status: 'completed', result }; })
        .catch(err => { taskState = { status: 'failed', error: err.message }; });
      return;
    }

    if (msg.type === 'startCreatePromoPH') {
      taskState = { status: 'running', region: 'PH', count: msg.count };
      sendResponse({ status: 'started' });
      createPromoPH(msg.count || 1)
        .then(result => { taskState = { status: 'completed', result }; })
        .catch(err => { taskState = { status: 'failed', error: err.message }; });
      return;
    }

    if (msg.type === 'taskState') {
      sendResponse(taskState);
      return;
    }

    // server commands (via background)
    if (msg.type === 'snapshot') {
      // simple snapshot for server
      const r = { url: location.href, title: document.title, inputs: [], buttons: [], tables: [] };
      document.querySelectorAll('input, textarea').forEach(el => {
        r.inputs.push({ placeholder: el.placeholder || '', value: el.value || '', selector: sel(el) });
      });
      document.querySelectorAll('button').forEach(el => {
        r.buttons.push({ text: (el.textContent || '').trim().slice(0, 60), selector: sel(el) });
      });
      sendResponse({ type: 'response', _reqId: msg._reqId, success: true, data: r });
      return;
    }

    if (msg.type === 'execute') {
      executeActions(msg.actions)
        .then(results => sendResponse({ type: 'response', _reqId: msg._reqId, success: true, results }))
        .catch(err => sendResponse({ type: 'response', _reqId: msg._reqId, success: false, error: err.message }));
      return true;
    }

    // register
    if (msg.type === 'register') {
      chrome.runtime.sendMessage({ type: 'register' });
      sendResponse({ ok: true });
    }
  });

  // register with background on load + 每隔30秒重新注册保持活跃
  chrome.runtime.sendMessage({ type: 'register' });
  setInterval(() => {
    chrome.runtime.sendMessage({ type: 'register' });
  }, 30000);
  console.log('[PageInspector] loaded');
})();
