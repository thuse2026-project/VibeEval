import type { FileContentResponse, FileEntry, SaveFileResponse, SubmitResponse } from '../../shared/types';

export type CandidateSession = {
  session_id: string;
  status: string;
  candidate: { id: string; name: string };
  exam_info: { id: string; title: string; duration_minutes: number };
  started_at: number | null;
  deadline_at: number | null;
};

export class WorkspaceError extends Error {
  constructor(public status: number, message: string, public code = '', public retryAfter = 1) {
    super(message);
  }
}

export async function workspaceRequest<T>(path: string, method = 'GET', body?: unknown, key?: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/v1/sessions/${path}`, {
      method, credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-VibeEval-Request': '1', ...(key ? { 'Idempotency-Key': key } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(method === 'GET' ? 15000 : 120000),
    });
  } catch {
    throw new WorkspaceError(0, '连接中断或请求超时。操作结果未确认，请查询状态或重新读取核对。');
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = payload.detail;
    const message = typeof detail === 'string' ? detail
      : typeof detail?.message === 'string' ? detail.message
      : Array.isArray(detail) ? '请求字段不合法，请检查输入。' : `请求失败（${response.status}）`;
    throw new WorkspaceError(response.status, message, detail?.code ?? '', Number(response.headers.get('Retry-After')) || 1);
  }
  return response.json();
}

export const workspaceApi = {
  status: (id: string) => workspaceRequest<CandidateSession>(`${encodeURIComponent(id)}/status`),
  transition: (id: string, action: 'prepare' | 'start') => workspaceRequest(`${encodeURIComponent(id)}/${action}`, 'POST'),
  files: (id: string) => workspaceRequest<{ files: FileEntry[] }>(`${encodeURIComponent(id)}/files`),
  read: (id: string, path: string) => workspaceRequest<FileContentResponse>(`${encodeURIComponent(id)}/file?path=${encodeURIComponent(path)}`),
  save: async (id: string, path: string, expected_version: number, content: string): Promise<SaveFileResponse> => {
    for (let attempt = 0; ; attempt++) {
      try {
        return await workspaceRequest<SaveFileResponse>(`${encodeURIComponent(id)}/file`, 'PUT', { path, expected_version, content });
      } catch (error) {
        if (!(error instanceof WorkspaceError) || error.code !== 'SESSION_BUSY' || attempt >= 2) throw error;
        await new Promise(resolve => setTimeout(resolve, Math.min(5, Math.max(1, error.retryAfter)) * 1000));
      }
    }
  },
  submit: (id: string, key: string) => workspaceRequest<SubmitResponse>(`${encodeURIComponent(id)}/submit`, 'POST', undefined, key),
};
