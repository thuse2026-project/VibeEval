import type {
  ApiClient,
  FileContentResponse,
  FileEntry,
  SaveFileResponse,
  SessionStatusResponse,
  SubmitResponse,
  VerifyTokenResponse,
} from '../types'

// 真实后端客户端，按 E.3 契约访问 http://localhost:8000/api/v1。
const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000/api/v1'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`请求失败 ${res.status}: ${text}`)
  }
  return res.json() as Promise<T>
}

export class HttpApiClient implements ApiClient {
  async verifyToken(token: string): Promise<VerifyTokenResponse> {
    return request<VerifyTokenResponse>('/sessions/verify-token', {
      method: 'POST',
      body: JSON.stringify({ token }),
    })
  }

  async getStatus(sessionId: string): Promise<SessionStatusResponse> {
    return request<SessionStatusResponse>(`/sessions/${sessionId}/status`)
  }

  async listFiles(sessionId: string): Promise<FileEntry[]> {
    const data = await request<{ files: FileEntry[] }>(`/sessions/${sessionId}/files`)
    return data.files
  }

  async readFile(sessionId: string, path: string): Promise<FileContentResponse> {
    return request<FileContentResponse>(
      `/sessions/${sessionId}/file?path=${encodeURIComponent(path)}`,
    )
  }

  async saveFile(
    sessionId: string,
    path: string,
    expectedVersion: number,
    content: string,
  ): Promise<SaveFileResponse> {
    return request<SaveFileResponse>(`/sessions/${sessionId}/file`, {
      method: 'PUT',
      body: JSON.stringify({ path, expected_version: expectedVersion, content }),
    })
  }

  async submit(sessionId: string, idempotencyKey: string): Promise<SubmitResponse> {
    return request<SubmitResponse>(`/sessions/${sessionId}/submit`, {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
    })
  }
}
