export type Receipt = { id: string; close_reason: string }
export type Stage = { id: string; title: string; status: string; receipt: Receipt | null }
export type Attempt = {
  attempt_id: string; status: string; server_now: number; state_version: number
  transition_deadline_at: number | null; error_code: string | null
  next_action: 'finish_phase' | 'submit_stage' | 'submit_exam' | null
  current_stage: { id: string; key: string; ordinal?: number; status: string; title: string }
  current_phase: { id: string; key: string; title: string; status: string; deadline_at: number | null }
  capabilities: string[]; stages: Stage[]
}

export class RequestError extends Error {
  constructor(public status: number, public code: string) { super(code) }
}

export async function examRequest<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    method, credentials: 'same-origin',
    headers: { 'X-VibeEval-Request': '1', 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: signal ?? AbortSignal.timeout(120000),
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    throw new RequestError(response.status, typeof payload.detail === 'string'
      ? payload.detail : payload.detail?.code ?? `HTTP ${response.status}`)
  }
  return response.json()
}

export const v2 = '/api/v2'
export const current = () => examRequest<Attempt>(v2 + '/attempts/current')
export const command = <T>(path: string, state: Attempt, extra = {}, key: string = crypto.randomUUID()) =>
  examRequest<T>(v2 + path, 'POST', { request_key: key, expected_state_version: state.state_version, ...extra })
