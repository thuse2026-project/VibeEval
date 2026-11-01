export type ScreenId =
  | 'confirm'
  | 'provisioning'
  | 'workspace'
  | 'grading'
  | 'report'

export type SaveState = 'idle' | 'saving' | 'saved' | 'conflict' | 'error'

export type ProvisionStepId = 'verify' | 'create' | 'mount' | 'health'
export type ProvisionStepState = 'wait' | 'doing' | 'done' | 'fail'

export type ProvisionStep = {
  id: ProvisionStepId
  title: string
  description: string
  state: ProvisionStepState
}

export interface ExamInfo {
  title: string
  duration_minutes: number
}

export interface VerifyTokenResponse {
  session_id: string
  status: string
  exam_info: ExamInfo
}

export interface SessionStatusResponse {
  session_id: string
  status: string
  time_remaining_seconds: number
}

export interface FileEntry {
  path: string
  is_dir: boolean
  version: number
  readonly: boolean
}

export interface FileContentResponse {
  path: string
  version: number
  content: string
}

export interface SaveFileResponse {
  path: string
  new_version: number
  saved_at: number
}

export interface SubmitResponse {
  submission_id: string
  session_id: string
  status: string
  submitted_at: number
  snapshot_id: string
}

export interface ApiClient {
  verifyToken(token: string): Promise<VerifyTokenResponse>
  getStatus(sessionId: string): Promise<SessionStatusResponse>
  listFiles(sessionId: string): Promise<FileEntry[]>
  readFile(sessionId: string, path: string): Promise<FileContentResponse>
  saveFile(
    sessionId: string,
    path: string,
    expectedVersion: number,
    content: string,
  ): Promise<SaveFileResponse>
  submit(sessionId: string, idempotencyKey: string): Promise<SubmitResponse>
}
