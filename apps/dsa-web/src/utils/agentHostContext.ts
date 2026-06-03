export type AgentHostSurface = 'home' | 'chat' | 'portfolio';

export interface AgentHostContext {
  surface: AgentHostSurface;
  intent?: string;
  pageState?: Record<string, unknown>;
  selectedReportId?: string;
  selectedSymbols?: string[];
  portfolioScope?: {
    accountId?: number | 'all';
    costMethod?: string;
  };
}

export interface AgentHostHandoff {
  prompt: string;
  context: AgentHostContext;
  sourceLabel: string;
  createdAt: number;
}

const HANDOFF_STORAGE_KEY = 'dsa_agent_host_handoff';
const HANDOFF_MAX_AGE_MS = 30 * 60 * 1000;

export const AGENT_HOST_HANDOFF_QUERY_KEY = 'agentHost';

export const AGENT_HOST_PROMPTS = {
  homeReview: '请基于首页当前报告和大盘信息，主持一段简短复盘：先给核心结论，再说明主要风险，最后列出下一步观察清单。',
  portfolioReview: '请基于当前持仓快照和风险数据，诊断组合风险：先讲收益与集中度，再讲价格/数据缺口，最后给只读观察清单。',
} as const;

export function getAgentHostSurfaceLabel(surface: AgentHostSurface): string {
  if (surface === 'home') return '来自首页复盘';
  if (surface === 'portfolio') return '来自持仓诊断';
  return '普通问股';
}

export function buildAgentHostChatUrl(): string {
  return `/chat?${AGENT_HOST_HANDOFF_QUERY_KEY}=1`;
}

export function compactAgentHostText(value: unknown, maxLength = 1200): string | undefined {
  if (value == null) return undefined;
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  const normalized = text.replace(/\s+/g, ' ').trim();
  if (!normalized) return undefined;
  return normalized.length > maxLength ? `${normalized.slice(0, maxLength)}...` : normalized;
}

export function saveAgentHostHandoff(prompt: string, context: AgentHostContext): void {
  const handoff: AgentHostHandoff = {
    prompt,
    context,
    sourceLabel: getAgentHostSurfaceLabel(context.surface),
    createdAt: Date.now(),
  };
  sessionStorage.setItem(HANDOFF_STORAGE_KEY, JSON.stringify(handoff));
}

export function consumeAgentHostHandoff(): AgentHostHandoff | null {
  const raw = sessionStorage.getItem(HANDOFF_STORAGE_KEY);
  if (!raw) return null;

  sessionStorage.removeItem(HANDOFF_STORAGE_KEY);

  try {
    const handoff = JSON.parse(raw) as AgentHostHandoff;
    if (!handoff?.prompt || !handoff.context?.surface) {
      return null;
    }
    if (Date.now() - Number(handoff.createdAt || 0) > HANDOFF_MAX_AGE_MS) {
      return null;
    }
    return handoff;
  } catch {
    return null;
  }
}
