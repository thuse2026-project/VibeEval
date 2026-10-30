export type Examiner = {
  username: string;
  name: string;
  expires_at: number;
  server_time: number;
};

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function api<T>(path: string, body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/v1/admin/${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-VibeEval-Request': '1' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(10000),
    });
  } catch {
    throw new ApiError(0, '暂时无法连接服务，请检查网络或确认后端已启动，然后重试。');
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new ApiError(response.status, typeof payload.detail === 'string'
      ? payload.detail : '请求未能完成，请稍后重试。');
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
