// Auto-reply module - Indonesia Tokopedia customer service
import { findTab, sendToTab, sleep } from './tab.js';
import { callClaude } from './claude-api.js';
import knowledge from './knowledge.json' with { type: 'json' };

let autoReplyState = { status: 'stopped' };
let pollTimer = null;
let repliedSet = new Set();
let claudeConfig = {};
let systemPrompt = '';

const DEFAULT_SYSTEM_PROMPT = `你是 ${knowledge.brand} (${knowledge.shop_name}) 的电商客服代表，负责回复Tokopedia平台上印尼买家的咨询。

## 产品知识
${knowledge.products.map(p => `### ${p.category} - ${p.name}
材质: ${p.material}
特点: ${p.features.join('、')}
规格: ${p.variants_note}
适用场景: ${p.use_cases}`).join('\n\n')}

## 店铺政策
- 发货: ${knowledge.policies.shipping}
- 退换: ${knowledge.policies.return}
- 保障: ${knowledge.policies.warranty}

## 专业知识
${knowledge.expertise}

## 回复规则
- 用印尼语回复，态度友好、专业、简洁
- 根据产品知识准确回答，不要编造不存在的规格
- 保持回复简短，一般1-3句话
- 如果不确定，说会进一步核实后回复`;

function log(msg) {
  console.log(`[AutoReply-ID] ${msg}`);
}

// Match FAQ by keywords
function matchFAQ(message) {
  const lower = message.toLowerCase();
  return knowledge.faq
    .filter(faq => faq.keywords.some(kw => lower.includes(kw.toLowerCase())))
    .map(faq => faq.answer);
}

// ===== Tokopedia Chat Page Selectors =====
const SEL = {
  chatItem:       '[id^="chat-room-conversation-list-item"]',
  chatItemBadge:  'span.p-badge',
  buyerBubble:    'div.chatd-bubble.chatd-bubble--left',
  agentBubble:    'div.chatd-bubble.chatd-bubble--right',
  textarea:       '#chat-input-textarea > textarea',
  sendBtn:        '#chat-input-send-button',
};

// Find unread chats (badge with non-empty text = number)
async function findUnreadChats(tabId) {
  const res = await sendToTab(tabId, {
    type: 'execute',
    actions: [{ type: 'query', selector: SEL.chatItem }]
  });
  const items = res?.results?.[0]?.items || [];
  // filter: items that have a badge with non-empty text (number)
  // badge selector inside each item: > div > div > span.p-badge
  // but we can't easily query nested - instead check if item has badge text
  // simpler: query all badges separately
  const badgeRes = await sendToTab(tabId, {
    type: 'execute',
    actions: [{ type: 'query', selector: SEL.chatItem + ' > div > div > ' + SEL.chatItemBadge }]
  });
  const badges = badgeRes?.results?.[0]?.items || [];
  const unreadItems = [];
  for (let i = 0; i < items.length && i < badges.length; i++) {
    const count = parseInt(badges[i]?.text);
    if (count > 0) {
      unreadItems.push({ selector: items[i].selector, text: items[i].text, count });
    }
  }
  return unreadItems;
}

// Click on a chat item in the sidebar
async function clickChatItem(tabId, itemSelector) {
  await sendToTab(tabId, {
    type: 'execute',
    actions: [
      { type: 'click', selector: itemSelector },
      { type: 'wait', value: 1500 }
    ]
  });
}

// Get recent messages with context (buyer + agent, last N)
// Returns { context: string, lastBuyerMsg: string }
async function getRecentMessages(tabId, count = 10) {
  const res = await sendToTab(tabId, {
    type: 'execute',
    actions: [
      { type: 'query', selector: SEL.buyerBubble },
      { type: 'query', selector: SEL.agentBubble }
    ]
  });
  const buyerItems = res?.results?.[0]?.items || [];
  const agentItems = res?.results?.[1]?.items || [];

  // merge and sort by DOM index (bubble order = conversation order)
  const all = [
    ...buyerItems.map(i => ({ ...i, role: '买家' })),
    ...agentItems.map(i => ({ ...i, role: '客服' }))
  ].sort((a, b) => a.index - b.index);

  // take last N
  const recent = all.slice(-count);
  if (recent.length === 0) return { context: '', lastBuyerMsg: null };

  const context = recent.map(m => `[${m.role}] ${m.text}`).join('\n');
  const lastBuyer = [...recent].reverse().find(m => m.role === '买家');

  return { context, lastBuyerMsg: lastBuyer?.text || null };
}

// Type a reply and click send
async function sendReply(tabId, text) {
  await sendToTab(tabId, {
    type: 'execute',
    actions: [
      { type: 'click', selector: SEL.textarea },
      { type: 'wait', value: 300 },
      { type: 'type', selector: SEL.textarea, value: text },
      { type: 'wait', value: 300 },
      { type: 'click', selector: SEL.sendBtn }
    ]
  });
}

// Main poll cycle
async function pollOnce() {
  try {
    const chatTab = await findTab(/tokopedia.*chat/);
    if (!chatTab) {
      log('no tokopedia chat tab');
      return;
    }

    // 1. find unread chats
    const unread = await findUnreadChats(chatTab.id);
    if (unread.length === 0) {
      autoReplyState = { ...autoReplyState, lastPoll: new Date().toISOString(), pending: 0 };
      return;
    }

    log(`found ${unread.length} unread chats`);

    // 2. process each unread chat
    for (const chat of unread) {
      // click on the chat item
      await clickChatItem(chatTab.id, chat.selector);
      log('opened: ' + chat.text.slice(0, 40));

      // get recent messages with context
      const { context, lastBuyerMsg } = await getRecentMessages(chatTab.id);
      if (!lastBuyerMsg) { log('no buyer message, skip'); continue; }
      if (repliedSet.has(lastBuyerMsg)) { log('already replied, skip'); continue; }

      log('context:\n' + context.slice(0, 200));

      // generate AI reply via Claude with conversation context + FAQ match
      const faqAnswers = matchFAQ(lastBuyerMsg);
      let userPrompt;
      if (context) {
        userPrompt = `以下是最近的对话记录:\n${context}\n\n请根据上下文回复最新的买家消息。`;
      } else {
        userPrompt = lastBuyerMsg;
      }
      if (faqAnswers.length > 0) {
        userPrompt += `\n\n[参考FAQ] ${faqAnswers.join('\n')}`;
      }
      const reply = await callClaude(claudeConfig, systemPrompt, userPrompt, { maxTokens: 300 });
      log('AI reply: ' + reply.slice(0, 80));

      // send the reply
      await sendReply(chatTab.id, reply);
      repliedSet.add(lastBuyerMsg);
      log('reply sent');

      // keep repliedSet from growing too large
      if (repliedSet.size > 200) {
        const arr = [...repliedSet];
        repliedSet = new Set(arr.slice(-100));
      }
    }

    autoReplyState = { ...autoReplyState, lastPoll: new Date().toISOString(), pending: unread.length, repliedCount: repliedSet.size };

  } catch (err) {
    log('poll error: ' + err.message);
    autoReplyState = { ...autoReplyState, lastError: err.message };
  }
}

export function startAutoReply(config, prompt, intervalSec = 10) {
  if (pollTimer) clearInterval(pollTimer);

  claudeConfig = {
    apiKey: config.apiKey,
    baseURL: config.baseURL || 'https://api.anthropic.com',
    model: config.model || 'claude-sonnet-4-20250514',
  };
  systemPrompt = prompt || DEFAULT_SYSTEM_PROMPT;
  autoReplyState = { status: 'running', interval: intervalSec };

  const poll = () => {
    pollOnce().catch(err => {
      log('fatal: ' + err.message);
    });
  };

  poll();
  pollTimer = setInterval(poll, intervalSec * 1000);

  log('started, interval: ' + intervalSec + 's, model: ' + claudeConfig.model);
  return { status: 'started' };
}

export function stopAutoReply() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  autoReplyState = { status: 'stopped' };
  repliedSet.clear();
  log('stopped');
  return { status: 'stopped' };
}

export function getAutoReplyState() {
  return { ...autoReplyState };
}
