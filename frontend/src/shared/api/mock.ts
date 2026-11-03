import type {
  ApiClient,
  FileContentResponse,
  FileEntry,
  SaveFileResponse,
  SessionStatusResponse,
  SubmitResponse,
  VerifyTokenResponse,
} from '../types'

// 无后端时的内存实现，行为对齐 E.3 契约。切换真实后端见 client.ts。
const initialFiles: FileEntry[] = [
  { path: 'README.md', is_dir: false, version: 1, readonly: true },
  { path: 'src/main.py', is_dir: false, version: 1, readonly: false },
  { path: 'src/utils.py', is_dir: false, version: 1, readonly: false },
  { path: 'tests/test_public.py', is_dir: false, version: 1, readonly: true },
]

const initialContent: Record<string, string> = {
  'README.md': '# Python 工程缺陷修复考核\n\n阅读 src/main.py 并修复其中的缺陷。\n',
  'src/main.py': 'def fix_me(value):\n    if value:\n        return "fixed"\n    return "broken"\n',
  'src/utils.py': 'def helper():\n    return True\n',
  'tests/test_public.py': 'def test_fix_me():\n    assert fix_me(True) == "fixed"\n',
}

export class MockApiClient implements ApiClient {
  private files: FileEntry[] = initialFiles.map((f) => ({ ...f }))
  private contents: Record<string, string> = { ...initialContent }
  private versions: Record<string, number> = Object.fromEntries(
    initialFiles.map((f) => [f.path, f.version]),
  )

  async verifyToken(token: string): Promise<VerifyTokenResponse> {
    if (!token) {
      throw new Error('凭证无效')
    }
    return {
      session_id: 'ses_01j7c390a8e1b',
      status: 'provisioning',
      exam_info: {
        title: 'Python 工程缺陷修复考核',
        duration_minutes: 60,
      },
    }
  }

  async getStatus(sessionId: string): Promise<SessionStatusResponse> {
    return {
      session_id: sessionId,
      status: 'ready',
      time_remaining_seconds: 3600,
    }
  }

  async listFiles(): Promise<FileEntry[]> {
    return this.files.map((f) => ({ ...f }))
  }

  async readFile(_sessionId: string, path: string): Promise<FileContentResponse> {
    return {
      path,
      version: this.versions[path] ?? 1,
      content: this.contents[path] ?? '',
    }
  }

  async saveFile(
    _sessionId: string,
    path: string,
    expectedVersion: number,
    content: string,
  ): Promise<SaveFileResponse> {
    const current = this.versions[path] ?? 1
    if (current !== expectedVersion) {
      throw new Error('版本冲突')
    }
    this.contents[path] = content
    this.versions[path] = current + 1
    const entry = this.files.find((f) => f.path === path)
    if (entry) {
      entry.version = current + 1
    }
    return {
      path,
      new_version: current + 1,
      saved_at: Date.now(),
    }
  }

  async submit(sessionId: string): Promise<SubmitResponse> {
    return {
      submission_id: 'sub_99a8b7c6d5e4',
      session_id: sessionId,
      status: 'frozen',
      submitted_at: Date.now(),
      snapshot_id: 'snp_33214abc',
    }
  }
}
