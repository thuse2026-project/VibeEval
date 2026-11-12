import { workspaceRequest } from './api'

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

export const agentApi = {
  conversations: (sessionId: string) =>
    workspaceRequest<{ items: AgentConversation[] }>(`${encodeURIComponent(sessionId)}/agent/conversations`),
  createConversation: (sessionId: string) =>
    workspaceRequest<AgentConversation>(`${encodeURIComponent(sessionId)}/agent/conversations`, 'POST', { title: '新对话' }),
  history: (sessionId: string, conversationId: string) =>
    workspaceRequest<{ turns: Array<{ user_message: string; steps: Array<{ response: { message: { content: string | null } } | null }> }> }>(`${encodeURIComponent(sessionId)}/agent/conversations/${encodeURIComponent(conversationId)}/history?limit=100`),
  runs: (sessionId: string, conversationId: string) =>
    workspaceRequest<{ items: AgentRun[] }>(`${encodeURIComponent(sessionId)}/agent/conversations/${encodeURIComponent(conversationId)}/runs?limit=20`),
  turn: (sessionId: string, conversationId: string, text: string) =>
    workspaceRequest<AgentRun>(`${encodeURIComponent(sessionId)}/agent/conversations/${encodeURIComponent(conversationId)}/turns`, 'POST', {
      request_key: crypto.randomUUID(), text, mode: 'read_only',
    }),
  advance: (sessionId: string, conversationId: string, runId: string) =>
    workspaceRequest<AgentRun>(`${encodeURIComponent(sessionId)}/agent/conversations/${encodeURIComponent(conversationId)}/runs/${encodeURIComponent(runId)}/advance`, 'POST'),
  interactions: (sessionId: string, conversationId: string, runId: string) =>
    workspaceRequest<{ items: AgentInteraction[] }>(`${encodeURIComponent(sessionId)}/agent/conversations/${encodeURIComponent(conversationId)}/runs/${encodeURIComponent(runId)}/interactions`),
  approval: (sessionId: string, conversationId: string, runId: string, interactionId: string, decision: 'approve' | 'reject') =>
    workspaceRequest(`${encodeURIComponent(sessionId)}/agent/conversations/${encodeURIComponent(conversationId)}/runs/${encodeURIComponent(runId)}/interactions/${encodeURIComponent(interactionId)}/approval`, 'POST', { decision }),
  answer: (sessionId: string, conversationId: string, runId: string, interactionId: string, answer: string) =>
    workspaceRequest(`${encodeURIComponent(sessionId)}/agent/conversations/${encodeURIComponent(conversationId)}/runs/${encodeURIComponent(runId)}/interactions/${encodeURIComponent(interactionId)}/answer`, 'POST', { answer }),
}
