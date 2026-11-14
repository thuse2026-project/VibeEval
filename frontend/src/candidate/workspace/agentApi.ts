import { workspaceRequest, WorkspaceError } from './api'

export type AgentConversation = {
  conversation_id: string
  title: string
}

export type AgentRun = {
  run_id: string
  status: string
  error_code: string | null
}

export type AgentInteraction = {
  interaction_id: string
  kind: 'approval' | 'user_input'
  status: string
  request: Record<string, unknown>
}

export type AgentOperation = {
  operation_id: string
  kind: 'command' | 'file_edit'
  status: string
  reason_code: string | null
  arguments: Record<string, unknown>
  exit_code: number | null
  change: { path?: string } | null
  file_result: {
    change?: { path?: string; applied?: boolean; current_version?: number | null }
  } | null
}

export type AgentRead = {
  observation_id: string
  tool_name: string
  action_kind: string
  status: string
  result?: { content?: string }
}

export type AgentStreamFrame = {
  event: string
  data: Record<string, unknown>
}

function base(sessionId: string, conversationId: string) {
  return `${encodeURIComponent(sessionId)}/agent/conversations/${encodeURIComponent(conversationId)}`
}

function parseEventBlock(block: string): AgentStreamFrame | null {
  let event = 'message'
  const data: string[] = []
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
  }
  if (!data.length) return null
  return { event, data: JSON.parse(data.join('\n')) as Record<string, unknown> }
}

export async function streamAgentEvents(
  sessionId: string,
  conversationId: string,
  afterSequence: number,
  signal: AbortSignal,
  onFrame: (frame: AgentStreamFrame) => void,
) {
  const response = await fetch(
    `/api/v1/sessions/${base(sessionId, conversationId)}/events/stream?after_sequence=${afterSequence}`,
    {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'X-VibeEval-Request': '1' },
      signal,
    },
  )
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    const detail = payload.detail
    throw new WorkspaceError(
      response.status,
      typeof detail?.message === 'string' ? detail.message : `Agent 事件流连接失败（${response.status}）`,
      detail?.code ?? '',
    )
  }
  if (!response.body) throw new WorkspaceError(0, '浏览器无法读取 Agent 事件流。')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n')
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const frame = parseEventBlock(buffer.slice(0, boundary))
      buffer = buffer.slice(boundary + 2)
      if (frame) onFrame(frame)
      boundary = buffer.indexOf('\n\n')
    }
    if (done) return
  }
}

export const agentApi = {
  conversations: (sessionId: string) =>
    workspaceRequest<{ items: AgentConversation[] }>(`${encodeURIComponent(sessionId)}/agent/conversations`),
  createConversation: (sessionId: string) =>
    workspaceRequest<AgentConversation>(`${encodeURIComponent(sessionId)}/agent/conversations`, 'POST', { title: '新对话' }),
  history: (sessionId: string, conversationId: string) =>
    workspaceRequest<{ turns: Array<{ user_message: string; steps: Array<{ response: { message: { content: string | null } } | null }> }> }>(`${encodeURIComponent(sessionId)}/agent/conversations/${encodeURIComponent(conversationId)}/history?limit=100`),
  runs: (sessionId: string, conversationId: string) =>
    workspaceRequest<{ items: AgentRun[] }>(`${encodeURIComponent(sessionId)}/agent/conversations/${encodeURIComponent(conversationId)}/runs?limit=20`),
  reads: (sessionId: string, conversationId: string) =>
    workspaceRequest<{ items: AgentRead[] }>(`${base(sessionId, conversationId)}/reads?limit=100`),
  operations: (sessionId: string, conversationId: string) =>
    workspaceRequest<{ items: AgentOperation[] }>(`${base(sessionId, conversationId)}/operations?limit=100`),
  turn: (sessionId: string, conversationId: string, text: string) =>
    workspaceRequest<AgentRun>(`${encodeURIComponent(sessionId)}/agent/conversations/${encodeURIComponent(conversationId)}/turns`, 'POST', {
      request_key: crypto.randomUUID(), text, mode: 'confirm_actions',
    }),
  advance: (sessionId: string, conversationId: string, runId: string) =>
    workspaceRequest<AgentRun>(`${encodeURIComponent(sessionId)}/agent/conversations/${encodeURIComponent(conversationId)}/runs/${encodeURIComponent(runId)}/advance`, 'POST'),
  interrupt: (sessionId: string, conversationId: string, runId: string) =>
    workspaceRequest<AgentRun>(`${base(sessionId, conversationId)}/runs/${encodeURIComponent(runId)}/interrupt`, 'POST'),
  interactions: (sessionId: string, conversationId: string, runId: string) =>
    workspaceRequest<{ items: AgentInteraction[] }>(`${encodeURIComponent(sessionId)}/agent/conversations/${encodeURIComponent(conversationId)}/runs/${encodeURIComponent(runId)}/interactions`),
  approval: (sessionId: string, conversationId: string, runId: string, interactionId: string, decision: 'approve' | 'reject') =>
    workspaceRequest(`${encodeURIComponent(sessionId)}/agent/conversations/${encodeURIComponent(conversationId)}/runs/${encodeURIComponent(runId)}/interactions/${encodeURIComponent(interactionId)}/approval`, 'POST', { decision }),
  answer: (sessionId: string, conversationId: string, runId: string, interactionId: string, answer: string) =>
    workspaceRequest(`${encodeURIComponent(sessionId)}/agent/conversations/${encodeURIComponent(conversationId)}/runs/${encodeURIComponent(runId)}/interactions/${encodeURIComponent(interactionId)}/answer`, 'POST', { answer }),
}
