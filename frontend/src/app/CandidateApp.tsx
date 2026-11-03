import { useCallback, useEffect, useRef, useState } from 'react'
import { api, usingMock } from '../shared/api'
import { ConfirmScreen } from '../candidate/entry/ConfirmScreen'
import { ProvisioningScreen } from '../candidate/entry/ProvisioningScreen'
import { GradingScreen } from '../candidate/workspace/GradingScreen'
import { WorkspaceScreen } from '../candidate/workspace/WorkspaceScreen'
import type { ExamInfo, FileEntry, ProvisionStep, SaveState, ScreenId } from '../shared/types'
import './styles.css'

const initialSteps: ProvisionStep[] = [
  { id: 'verify', title: '校验会话', description: '等待凭证校验', state: 'wait' },
  { id: 'create', title: '创建隔离沙箱', description: '等待沙箱就绪', state: 'wait' },
  { id: 'mount', title: '挂载项目代码', description: '等待工作区挂载', state: 'wait' },
  { id: 'health', title: '文件与终端健康检查', description: '等待健康检查', state: 'wait' },
]

type OpenFile = { content: string; version: number; readonly: boolean }
type TerminalCommand = { id: number; data: string }

const previewFiles: FileEntry[] = [
  { path: 'README.md', is_dir: false, version: 1, readonly: true },
  { path: 'src/main.py', is_dir: false, version: 1, readonly: false },
  { path: 'src/utils.py', is_dir: false, version: 1, readonly: false },
  { path: 'tests/test_public.py', is_dir: false, version: 1, readonly: true },
]

export default function CandidateApp() {
  const params = new URLSearchParams(window.location.search)
  const preview = params.get('screen')
  const devPreview = usingMock && (preview === 'workspace' || preview === 'grading')
  const [screen, setScreen] = useState<ScreenId>(devPreview ? preview : 'confirm')
  const [token] = useState(() => params.get('token') ?? (usingMock ? 'demo-token' : ''))
  const [sessionId, setSessionId] = useState<string | null>(devPreview ? 'ses_preview' : null)
  const [examInfo, setExamInfo] = useState<ExamInfo | null>(null)
  const [confirmError, setConfirmError] = useState<string | null>(null)
  const [steps, setSteps] = useState<ProvisionStep[]>(initialSteps)
  const [provisionError, setProvisionError] = useState<string | null>(null)
  const [provisionReady, setProvisionReady] = useState(false)
  const pollRun = useRef(0)

  const [remaining, setRemaining] = useState(3600)
  const [files, setFiles] = useState<FileEntry[]>(devPreview ? previewFiles : [])
  const [openPaths, setOpenPaths] = useState<string[]>(devPreview ? ['src/main.py'] : [])
  const [activePath, setActivePath] = useState<string | null>(devPreview ? 'src/main.py' : null)
  const [documents, setDocuments] = useState<Record<string, OpenFile>>(
    devPreview
      ? { 'src/main.py': { content: 'def fix_me():\n    return False\n', version: 1, readonly: false } }
      : {},
  )
  const documentsRef = useRef(documents)
  const sessionRef = useRef(sessionId)
  const dirtyRef = useRef(new Set<string>())
  const saveTimers = useRef(new Map<string, number>())
  const inFlightSaves = useRef(new Map<string, Promise<unknown>>())
  const [dirtyPaths, setDirtyPaths] = useState<string[]>([])
  const [saveState, setSaveState] = useState<SaveState>('saved')
  const [terminalCommand, setTerminalCommand] = useState<TerminalCommand | null>(null)
  const [submissionId, setSubmissionId] = useState<string | null>(devPreview ? 'sub_99a8b7c6d5e4' : null)
  const [snapshotId, setSnapshotId] = useState<string | null>(devPreview ? 'snp_33214abc' : null)

  useEffect(() => {
    documentsRef.current = documents
  }, [documents])

  useEffect(() => {
    sessionRef.current = sessionId
  }, [sessionId])

  useEffect(() => () => {
    pollRun.current += 1
    saveTimers.current.forEach((timer) => window.clearTimeout(timer))
  }, [])

  const startProvisioning = useCallback((sid: string) => {
    const run = ++pollRun.current
    setSessionId(sid)
    setScreen('provisioning')
    setProvisionReady(false)
    setProvisionError(null)
    setSteps(initialSteps.map((step, index) => ({
      ...step,
      state: index === 0 ? 'done' : index === 1 ? 'doing' : 'wait',
    })))

    const poll = async () => {
      try {
        const status = await api.getStatus(sid)
        if (run !== pollRun.current) return
        if (status.status === 'ready') {
          setRemaining(status.time_remaining_seconds)
          setSteps(initialSteps.map((step) => ({ ...step, state: 'done' })))
          setProvisionReady(true)
          return
        }
        if (status.status === 'failed' || status.status === 'expired') {
          throw new Error(`沙箱状态：${status.status}`)
        }
        window.setTimeout(poll, 1000)
      } catch (error) {
        if (run !== pollRun.current) return
        setProvisionError(error instanceof Error ? error.message : '查询沙箱状态失败')
        setSteps((current) => current.map((step) => (
          step.state === 'doing' ? { ...step, state: 'fail' } : step
        )))
      }
    }
    void poll()
  }, [])

  const handleConfirm = useCallback(async () => {
    setConfirmError(null)
    if (!token) {
      setConfirmError('链接缺少入场 Token，请使用考务系统提供的完整链接')
      return
    }
    try {
      const response = await api.verifyToken(token)
      setExamInfo(response.exam_info)
      startProvisioning(response.session_id)
    } catch (error) {
      setConfirmError(error instanceof Error ? error.message : '凭证无效，无法进入考核')
    }
  }, [token, startProvisioning])

  const loadWorkspace = useCallback(async () => {
    if (!sessionId) return
    try {
      const list = await api.listFiles(sessionId)
      setFiles(list)
      const firstFile = list.find((file) => !file.is_dir && !file.readonly)
        ?? list.find((file) => !file.is_dir)
      if (firstFile) {
        const file = await api.readFile(sessionId, firstFile.path)
        const next = {
          [file.path]: { content: file.content, version: file.version, readonly: firstFile.readonly },
        }
        documentsRef.current = next
        setDocuments(next)
        setOpenPaths([file.path])
        setActivePath(file.path)
      }
      setScreen('workspace')
    } catch (error) {
      setProvisionError(error instanceof Error ? error.message : '加载工作区失败')
    }
  }, [sessionId])

  useEffect(() => {
    if (screen !== 'workspace') return
    const timer = window.setInterval(() => setRemaining((value) => Math.max(0, value - 1)), 1000)
    const sync = window.setInterval(() => {
      if (!sessionRef.current) return
      void api.getStatus(sessionRef.current).then((status) => {
        setRemaining(status.time_remaining_seconds)
      }).catch(() => undefined)
    }, 15000)
    return () => {
      window.clearInterval(timer)
      window.clearInterval(sync)
    }
  }, [screen])

  const savePath = useCallback(async (path: string): Promise<boolean> => {
    const sid = sessionRef.current
    const file = documentsRef.current[path]
    if (!sid || !file || !dirtyRef.current.has(path)) return true
    const savedContent = file.content
    setSaveState('saving')
    const request = api.saveFile(sid, path, file.version, savedContent)
    inFlightSaves.current.set(path, request)
    try {
      const response = await request
      const latest = documentsRef.current[path]
      const next = { ...documentsRef.current, [path]: { ...latest, version: response.new_version } }
      documentsRef.current = next
      if (latest.content === savedContent) dirtyRef.current.delete(path)
      setDocuments(next)
      setFiles((current) => current.map((entry) => (
        entry.path === path ? { ...entry, version: response.new_version } : entry
      )))
      setDirtyPaths([...dirtyRef.current])
      setSaveState(dirtyRef.current.size ? 'idle' : 'saved')
      return true
    } catch (error) {
      setSaveState(error instanceof Error && error.message.includes('冲突') ? 'conflict' : 'error')
      return false
    } finally {
      if (inFlightSaves.current.get(path) === request) inFlightSaves.current.delete(path)
    }
  }, [])

  const scheduleSave = useCallback((path: string) => {
    const previous = saveTimers.current.get(path)
    if (previous) window.clearTimeout(previous)
    saveTimers.current.set(path, window.setTimeout(() => {
      saveTimers.current.delete(path)
      void savePath(path)
    }, 5000))
  }, [savePath])

  const handleEdit = useCallback((value: string) => {
    if (!activePath || documentsRef.current[activePath]?.readonly) return
    setDocuments((current) => {
      const next = { ...current, [activePath]: { ...current[activePath], content: value } }
      documentsRef.current = next
      return next
    })
    dirtyRef.current.add(activePath)
    setDirtyPaths([...dirtyRef.current])
    setSaveState('idle')
    scheduleSave(activePath)
  }, [activePath, scheduleSave])

  const handleSelectFile = useCallback(async (path: string) => {
    if (documentsRef.current[path]) {
      setActivePath(path)
      return
    }
    if (!sessionRef.current) return
    try {
      const file = await api.readFile(sessionRef.current, path)
      const metadata = files.find((entry) => entry.path === path)
      setDocuments((current) => {
        const next = {
          ...current,
          [path]: { content: file.content, version: file.version, readonly: metadata?.readonly ?? false },
        }
        documentsRef.current = next
        return next
      })
      setOpenPaths((current) => current.includes(path) ? current : [...current, path])
      setActivePath(path)
    } catch {
      setSaveState('error')
    }
  }, [files])

  const handleSubmit = useCallback(async () => {
    const sid = sessionRef.current
    if (!sid) return
    saveTimers.current.forEach((timer) => window.clearTimeout(timer))
    saveTimers.current.clear()
    await Promise.allSettled(inFlightSaves.current.values())
    const results = await Promise.all([...dirtyRef.current].map(savePath))
    if (results.some((ok) => !ok)) throw new Error('仍有文件未保存，请处理后重试')
    const response = await api.submit(sid, crypto.randomUUID())
    setSubmissionId(response.submission_id)
    setSnapshotId(response.snapshot_id)
    setScreen('grading')
  }, [savePath])

  const activeDocument = activePath ? documents[activePath] : undefined

  return (
    <>
      {screen === 'confirm' && (
        <ConfirmScreen examInfo={examInfo} error={confirmError} onConfirm={handleConfirm} />
      )}
      {screen === 'provisioning' && (
        <ProvisioningScreen
          steps={steps}
          error={provisionError}
          ready={provisionReady}
          onRetry={() => sessionId && startProvisioning(sessionId)}
          onEnter={loadWorkspace}
        />
      )}
      {screen === 'workspace' && (
        <WorkspaceScreen
          title={examInfo?.title ?? 'Python 工程缺陷修复考核'}
          remaining={remaining}
          files={files}
          openFiles={openPaths.map((path) => ({
            path,
            readonly: documents[path]?.readonly ?? false,
            dirty: dirtyPaths.includes(path),
          }))}
          activePath={activePath}
          content={activeDocument?.content ?? ''}
          activeReadonly={activeDocument?.readonly ?? true}
          saveState={saveState}
          sessionId={sessionId}
          terminalCommand={terminalCommand}
          onSelectFile={handleSelectFile}
          onEdit={handleEdit}
          onRunTests={() => setTerminalCommand({ id: Date.now(), data: 'pytest\r' })}
          onSubmit={handleSubmit}
        />
      )}
      {screen === 'grading' && submissionId && snapshotId && (
        <GradingScreen submissionId={submissionId} snapshotId={snapshotId} />
      )}
    </>
  )
}
