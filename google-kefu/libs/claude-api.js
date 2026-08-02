// Claude API wrapper - Anthropic Messages API direct call
// Pattern adapted from daily_stock_analysis/anthropic_direct_client.py

const DEFAULT_BASE_URL = 'https://api.anthropic.com';
const API_VERSION = '2023-06-01';
const MAX_RETRIES = 3;

function buildMessagesUrl(baseURL) {
  const base = baseURL.replace(/\/+$/, '');
  if (base.endsWith('/v1/messages')) return base;
  if (base.endsWith('/v1')) return base + '/messages';
  return base + '/v1/messages';
}

function extractText(data) {
  const parts = [];
  for (const block of (data.content || [])) {
    if (block.type === 'text' && typeof block.text === 'string') {
      parts.push(block.text);
    }
  }
  return parts.join('\n').trim();
}

/**
 * Call Claude Messages API with retry logic.
 * @param {Object} config - { apiKey, baseURL, model }
 * @param {string} systemPrompt
 * @param {string} userMessage
 * @param {Object} [options] - { maxTokens, temperature }
 * @returns {Promise<string>} response text
 */
export async function callClaude(config, systemPrompt, userMessage, options = {}) {
  const apiKey = config.apiKey;
  const baseURL = config.baseURL || DEFAULT_BASE_URL;
  const model = config.model || 'claude-sonnet-4-20250514';
  const maxTokens = options.maxTokens || 500;
  const temperature = options.temperature ?? 0.3;
  const url = buildMessagesUrl(baseURL);

  const payload = {
    model,
    max_tokens: maxTokens,
    temperature,
    messages: [{ role: 'user', content: userMessage }],
  };
  if (systemPrompt && systemPrompt.trim()) {
    payload.system = systemPrompt.trim();
  }

  let lastError = null;
  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    try {
      const controller = new AbortController();
      const timeout = (attempt + 1) * 30000; // 30s, 60s, 90s
      const timer = setTimeout(() => controller.abort(), timeout);

      const res = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-api-key': apiKey,
          'anthropic-version': API_VERSION,
          'anthropic-dangerous-direct-browser-access': 'true',
        },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      clearTimeout(timer);

      if (!res.ok) {
        const errText = await res.text();
        throw new Error(`HTTP ${res.status}: ${errText.slice(0, 500)}`);
      }

      const data = await res.json();
      return extractText(data);
    } catch (err) {
      lastError = err;
      if (attempt < MAX_RETRIES - 1) {
        await new Promise(r => setTimeout(r, 2000));
      }
    }
  }
  throw lastError;
}
