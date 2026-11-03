import type { ApiClient, FileContentResponse, FileEntry, SaveFileResponse, SessionStatusResponse, SubmitResponse, VerifyTokenResponse } from '../types';
export class CandidateError extends Error {
  constructor(public status:number, message:string, public code?:string) { super(message); }
}
export async function candidateRequest<T>(path:string, init:RequestInit = {}):Promise<T> {
  const response = await fetch(`/api/v1${path}`, {...init, credentials:'same-origin', headers:{'Content-Type':'application/json','X-VibeEval-Request':'1',...init.headers}});
  const value = await response.json().catch(()=>null);
  if (!response.ok) {
    const detail = value?.detail;
    throw new CandidateError(response.status, typeof detail === 'string' ? detail : detail?.message ?? '请求失败，请稍后重试', detail?.code);
  }
  return value as T;
}
export class HttpApiClient implements ApiClient {
  verifyToken(token:string) {return candidateRequest<VerifyTokenResponse>('/sessions/verify-token',{method:'POST',body:JSON.stringify({token})});}
  getStatus(id:string) {return candidateRequest<SessionStatusResponse>(`/sessions/${id}/status`);}
  async listFiles(id:string) {return (await candidateRequest<{files:FileEntry[]}>(`/sessions/${id}/files`)).files;}
  readFile(id:string,path:string) {return candidateRequest<FileContentResponse>(`/sessions/${id}/file?path=${encodeURIComponent(path)}`);}
  saveFile(id:string,path:string,version:number,content:string) {return candidateRequest<SaveFileResponse>(`/sessions/${id}/file`,{method:'PUT',body:JSON.stringify({path,expected_version:version,content})});}
  submit(id:string,key:string) {return candidateRequest<SubmitResponse>(`/sessions/${id}/submit`,{method:'POST',headers:{'Idempotency-Key':key}});}
}
