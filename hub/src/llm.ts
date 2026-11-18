/**
 * One chat client for every AI job in the hub: reply drafts, photo descriptions and caregiver briefings.
 *
 * It speaks the OpenAI-compatible chat-completions API, so the same code works with
 *   - Azure AI Foundry (the default):  AI_BASE_URL=https://<resource>.services.ai.azure.com/openai/v1
 *   - a model on this machine (Ollama, vLLM, LM Studio):  AI_BASE_URL=http://localhost:11434/v1
 *
 *   POST {AI_BASE_URL}/chat/completions   { model: AI_MODEL, messages, tools?, max_completion_tokens }
 *
 * With no AI_BASE_URL the hub still works: callers fall back to built-in replies and a plain summary.
 * Newer chat models reject sampling parameters, so the request carries only model, messages, tools and
 * max_completion_tokens. If a server rejects max_completion_tokens the call is retried once with max_tokens.
 */
import { settings } from './env.ts';

export type Fetch = typeof fetch;
export type ToolCall = { id: string; type: 'function'; function: { name: string; arguments: string } };

export interface ChatRequest {
  messages: unknown[];
  tools?: readonly unknown[];
  maxTokens: number;
  vision?: boolean;              // uses AI_VISION_MODEL when set (otherwise AI_MODEL)
}

export interface ChatReply { content: string | null; tool_calls?: ToolCall[] }

const isLocal = (url: string) => /\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:|\/|$)/.test(url);

/** True when a model is configured: a base URL plus a key (a local server needs no key). */
export function aiConfigured(): boolean {
  return Boolean(settings.aiBaseUrl && settings.aiModel && (settings.aiApiKey || isLocal(settings.aiBaseUrl)));
}

/** Name stored with briefings and shown in logs: the model, or "rules" with no model. */
export function modelLabel(): string {
  return aiConfigured() ? settings.aiModel : 'rules';
}

export class LlmError extends Error {
  constructor(message: string, public status?: number) { super(message); }
}

async function post(fetchFn: Fetch, url: string, headers: Record<string, string>, body: unknown): Promise<Response> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), settings.aiTimeoutMs);
  try {
    return await fetchFn(url, { method: 'POST', headers, body: JSON.stringify(body), signal: ctl.signal });
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') throw new LlmError(`no answer within ${settings.aiTimeoutMs / 1000}s`);
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

/** One chat-completions call. Throws LlmError on failure. */
export async function chatCompletion(fetchFn: Fetch, req: ChatRequest): Promise<ChatReply> {
  if (!aiConfigured()) throw new LlmError('no AI model configured (set AI_BASE_URL, AI_MODEL and AI_API_KEY)');
  const url = `${settings.aiBaseUrl}/chat/completions`;
  const headers: Record<string, string> = { 'Content-Type': 'application/json', Accept: 'application/json' };
  if (settings.aiApiKey) {
    headers.Authorization = `Bearer ${settings.aiApiKey}`;
    headers['api-key'] = settings.aiApiKey;              // Azure's header; other servers ignore it
  }
  const body: Record<string, unknown> = {
    model: req.vision && settings.aiVisionModel ? settings.aiVisionModel : settings.aiModel,
    messages: req.messages,
    max_completion_tokens: req.maxTokens,
  };
  if (req.tools) { body.tools = req.tools; body.tool_choice = 'auto'; }

  let res = await post(fetchFn, url, headers, body);
  if (res.status === 400) {
    const txt = await res.text();
    if (!/max_completion_tokens/.test(txt)) throw new LlmError(`AI HTTP 400: ${txt.slice(0, 300)}`, 400);
    delete body.max_completion_tokens;                   // older servers only know max_tokens
    body.max_tokens = req.maxTokens;
    res = await post(fetchFn, url, headers, body);
  }
  if (!res.ok) throw new LlmError(`AI HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`, res.status);
  const data = (await res.json()) as { choices?: Array<{ message: ChatReply }> };
  const msg = data.choices?.[0]?.message;
  if (!msg) throw new LlmError('AI returned no choices');
  return msg;
}
