/**
 * One chat client for every AI job in the hub: reply drafts, photo descriptions and caregiver briefings.
 *
 * It speaks the OpenAI-compatible APIs, so the same code works with
 *   - Azure AI Foundry (the default):  AI_BASE_URL=https://<resource>.services.ai.azure.com/openai/v1
 *   - a model on this machine (Ollama, vLLM, LM Studio):  AI_BASE_URL=http://localhost:11434/v1
 *
 * Two request shapes, picked by AI_API:
 *   chat       POST {AI_BASE_URL}/chat/completions   { model, messages, tools?, max_completion_tokens }
 *   responses  POST {AI_BASE_URL}/responses          { model, instructions, input, tools?, max_output_tokens }
 *   auto       (default) chat first; a deployment that only serves the Responses API is switched over
 *              on the first refusal, and stays switched for the life of the hub
 *
 * AI_REASONING_EFFORT (default "low") keeps reasoning models quick; a model that does not take the
 * parameter is retried once without it, and it is then left off. A server that rejects
 * max_completion_tokens is retried with max_tokens. With no AI_BASE_URL the hub still works: callers
 * fall back to built-in replies and a plain summary.
 */
import { settings } from './env.ts';

export type Fetch = typeof fetch;
export type ToolCall = { id: string; type: 'function'; function: { name: string; arguments: string } };

export interface ChatRequest {
  messages: unknown[];
  tools?: readonly unknown[];
  toolChoice?: 'auto' | 'required';
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

// learned at run time, so each refusal costs one extra request at most once
const state = {
  api: settings.aiApi as 'auto' | 'chat' | 'responses',
  effort: settings.aiReasoningEffort,
  maxTokensKey: 'max_completion_tokens' as 'max_completion_tokens' | 'max_tokens',
};

/** Test hook: forget what was learned from earlier refusals. */
export function resetLlmState(): void {
  state.api = settings.aiApi as typeof state.api;
  state.effort = settings.aiReasoningEffort;
  state.maxTokensKey = 'max_completion_tokens';
}

async function post(fetchFn: Fetch, path: string, body: unknown): Promise<Response> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json', Accept: 'application/json' };
  if (settings.aiApiKey) {
    headers.Authorization = `Bearer ${settings.aiApiKey}`;
    headers['api-key'] = settings.aiApiKey;              // Azure's header; other servers ignore it
  }
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), settings.aiTimeoutMs);
  try {
    return await fetchFn(`${settings.aiBaseUrl}${path}`, { method: 'POST', headers, body: JSON.stringify(body), signal: ctl.signal });
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') throw new LlmError(`no answer within ${settings.aiTimeoutMs / 1000}s`);
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

const model = (req: ChatRequest) => (req.vision && settings.aiVisionModel ? settings.aiVisionModel : settings.aiModel);

// ------------------------------------------------------------------ chat completions
function chatBody(req: ChatRequest): Record<string, unknown> {
  const body: Record<string, unknown> = { model: model(req), messages: req.messages, [state.maxTokensKey]: req.maxTokens };
  if (req.tools) { body.tools = req.tools; body.tool_choice = req.toolChoice ?? 'auto'; }
  if (state.effort) body.reasoning_effort = state.effort;
  return body;
}

async function viaChat(fetchFn: Fetch, req: ChatRequest): Promise<ChatReply> {
  const res = await post(fetchFn, '/chat/completions', chatBody(req));
  if (!res.ok) throw new LlmError(`AI HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`, res.status);
  const data = (await res.json()) as { choices?: Array<{ message: ChatReply }> };
  const msg = data.choices?.[0]?.message;
  if (!msg) throw new LlmError('AI returned no choices');
  return msg;
}

// ------------------------------------------------------------------ responses
type AnyMsg = { role: string; content?: unknown; tool_calls?: ToolCall[]; tool_call_id?: string };

/** Chat-style messages → Responses `instructions` + `input` items. */
export function toResponsesInput(messages: unknown[]): { instructions?: string; input: unknown[] } {
  const instructions: string[] = [];
  const input: unknown[] = [];
  for (const m of messages as AnyMsg[]) {
    if (m.role === 'system') { instructions.push(String(m.content ?? '')); continue; }
    if (m.role === 'tool') { input.push({ type: 'function_call_output', call_id: m.tool_call_id, output: String(m.content ?? '') }); continue; }
    if (m.role === 'assistant') {
      if (m.content) input.push({ role: 'assistant', content: String(m.content) });
      for (const c of m.tool_calls ?? []) input.push({ type: 'function_call', call_id: c.id, name: c.function.name, arguments: c.function.arguments });
      continue;
    }
    const content = Array.isArray(m.content)
      ? (m.content as Array<{ type: string; text?: string; image_url?: { url: string } }>).map(p =>
        p.type === 'image_url' ? { type: 'input_image', image_url: p.image_url?.url } : { type: 'input_text', text: p.text ?? '' })
      : String(m.content ?? '');
    input.push({ role: 'user', content });
  }
  return { instructions: instructions.length ? instructions.join('\n\n') : undefined, input };
}

/** Chat-style function tools → Responses tools (name/parameters at the top level). */
export function toResponsesTools(tools: readonly unknown[]): unknown[] {
  return (tools as Array<{ function: { name: string; description?: string; parameters: unknown } }>).map(t => ({
    type: 'function', name: t.function.name, description: t.function.description, parameters: t.function.parameters,
  }));
}

async function viaResponses(fetchFn: Fetch, req: ChatRequest): Promise<ChatReply> {
  const { instructions, input } = toResponsesInput(req.messages);
  const body: Record<string, unknown> = { model: model(req), input, max_output_tokens: req.maxTokens };
  if (instructions) body.instructions = instructions;
  if (req.tools) { body.tools = toResponsesTools(req.tools); body.tool_choice = req.toolChoice ?? 'auto'; }
  if (state.effort) body.reasoning = { effort: state.effort };
  const res = await post(fetchFn, '/responses', body);
  if (!res.ok) throw new LlmError(`AI HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`, res.status);
  const data = (await res.json()) as {
    output?: Array<{ type: string; content?: Array<{ type: string; text?: string }>; call_id?: string; id?: string; name?: string; arguments?: string }>;
    output_text?: string;
  };
  const text: string[] = [];
  const calls: ToolCall[] = [];
  for (const item of data.output ?? []) {
    if (item.type === 'message') for (const c of item.content ?? []) if (c.text) text.push(c.text);
    if (item.type === 'function_call') calls.push({ id: item.call_id ?? item.id ?? 'call', type: 'function', function: { name: item.name ?? '', arguments: item.arguments ?? '{}' } });
  }
  if (!text.length && data.output_text) text.push(data.output_text);
  if (!text.length && !calls.length) throw new LlmError('AI returned no output');
  return { content: text.length ? text.join('') : null, ...(calls.length ? { tool_calls: calls } : {}) };
}

// ------------------------------------------------------------------ one call, learning from refusals
const refusesEffort = (e: LlmError) => e.status === 400 && /reasoning/i.test(e.message);
const refusesMaxTokens = (e: LlmError) => e.status === 400 && /max_completion_tokens/.test(e.message);
const wantsResponses = (e: LlmError) =>
  (e.status === 400 || e.status === 404) && /OperationNotSupported|chat ?completions? operation|responses api|use the responses|\/v1\/responses/i.test(e.message);

/** One model call. Throws LlmError on failure. */
export async function chatCompletion(fetchFn: Fetch, req: ChatRequest): Promise<ChatReply> {
  if (!aiConfigured()) throw new LlmError('no AI model configured (set AI_BASE_URL, AI_MODEL and AI_API_KEY)');
  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      return state.api === 'responses' ? await viaResponses(fetchFn, req) : await viaChat(fetchFn, req);
    } catch (err) {
      if (!(err instanceof LlmError)) throw err;
      if (state.api === 'auto' && wantsResponses(err)) { state.api = 'responses'; continue; }   // before dropping anything
      if (state.effort && refusesEffort(err)) { state.effort = ''; continue; }
      if (state.api !== 'responses' && state.maxTokensKey === 'max_completion_tokens' && refusesMaxTokens(err)) { state.maxTokensKey = 'max_tokens'; continue; }
      throw err;
    }
  }
  throw new LlmError('AI request kept being refused');
}
