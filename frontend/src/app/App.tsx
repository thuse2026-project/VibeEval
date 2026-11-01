import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../shared/api'
import { ConfirmScreen } from '../candidate/entry/ConfirmScreen'
import { ProvisioningScreen } from '../candidate/entry/ProvisioningScreen'
import { GradingScreen } from '../candidate/workspace/GradingScreen'
import { ReportScreen } from '../candidate/workspace/ReportScreen'
import { WorkspaceScreen } from '../candidate/workspace/WorkspaceScreen'
import type {
  ExamInfo,
  FileEntry,
  ProvisionStep,
  SaveState,
  ScreenId,
} from '../shared/types'
import './styles.css'

const initialSteps: ProvisionStep[] = [
  { id: 'verify', title: '校验会话', description: '凭证有效，会话归属正确', state: 'wait' },
  { id: 'create', title: '创建隔离沙箱', description: 'E2B 微虚拟机已分配', state: 'wait' },
  { id: 'mount', title: '挂载项目代码', description: '正在写入模板与依赖…', state: 'wait' },
  { id: 'health', title: '文件与终端健康检查', description: '等待上一步完成', state: 'wait' },
]

export default function App() {
  // ?screen=workspace|grading|report 是开发预览入口，默认从 confirm 开始，不影响正式流程。
  const preview = new URLSearchParams(window.location.search).get('screen')
  const [screen, setScreen] = useState<ScreenId>(
    (preview as ScreenId) || 'confirm',
  )
  const [token] = useState('demo-token')
  const devPreview = preview === 'workspace' || preview === 'grading' || preview === 'report'
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [examInfo, setExamInfo] = useState<ExamInfo | null>(null)
  const [confirmError, setConfirmError] = useState<string | null>(null)

  const [steps, setSteps] = useState<ProvisionStep[]>(initialSteps)
  const [provisionError, setProvisionError] = useState<string | null>(null)
  const [provisionReady, setProvisionReady] = useState(false)

  const [remaining, setRemaining] = useState(3600)
  const [files, setFiles] = useState<FileEntry[]>(
    devPreview
      ? [
          { path: 'README.md', is_dir: false, version: 1, readonly: true },
          { path: 'src/main.py', is_dir: false, version: 1, readonly: false },
          { path: 'src/utils.py', is_dir: false, version: 1, readonly: false },
          { path: 'tests/test_public.py', is_dir: false, version: 1, readonly: true },
        ]
      : [],
  )
  const [activePath, setActivePath] = useState<string | null>(devPreview ? 'src/main.py' : null)
  const [content, setContent] = useState(
    devPreview ? 'def fix_me(value):\n    if value:\n        return "fixed"\n    return "broken"\n' : '',
  )
  const [versions, setVersions] = useState<Record<string, number>>({})
  const [readonly, setReadonly] = useState<Record<string, boolean>>({})
  const [saveState, setSaveState] = useState<SaveState>('saved')
  const [terminalOutput, setTerminalOutput] = useState('$ pytest\n')

  const [submissionId, setSubmissionId] = useState<string | null>(devPreview ? 'sub_99a8b7c6d5e4' : null)
  const [snapshotId, setSnapshotId] = useState<string | null>(devPreview ? 'snp_33214abc' : null)

  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pendingSave = useRef<{ path: string; content: string } | null>(null)

  const startProvisioning = useCallback(async (sid: string) => {
    setSessionId(sid)
    setScreen('provisioning')
    setSteps((s) => s.map((x) => ({ ...x, state: x.id === 'verify' ? 'doing' : 'wait' })))

    const tick = (idx: number) => {
      setSteps((prev) => prev.map((x, i) => {
        if (i === idx) return { ...x, state: 'done' as const }
        if (i === idx + 1) return { ...x, state: 'doing' as const }
        return x
      }))
    }

    tick(0)
    setTimeout(() => tick(1), 400)
    setTimeout(() => tick(2), 900)
    setTimeout(() => {
      setSteps((prev) => prev.map((x, i) => (i === 3 ? { ...x, state: 'done' as const } : x)))
      setProvisionReady(true)
    }, 1400)
  }, [])

  const handleConfirm = useCallback(async () => {
    setConfirmError(null)
    try {
      const res = await api.verifyToken(token)
      setExamInfo(res.exam_info)
      await startProvisioning(res.session_id)
    } catch (e) {
      setConfirmError(e instanceof Error ? e.message : '凭证无效，无法进入考核')
    }
  }, [token, startProvisioning])

  const handleRetry = useCallback(async () => {
    setProvisionError(null)
    if (sessionId) {
      await startProvisioning(sessionId)
    }
  }, [sessionId, startProvisioning])

  const loadWorkspace = useCallback(async () => {
    if (!sessionId) return
    try {
      const status = await api.getStatus(sessionId)
      setRemaining(status.time_remaining_seconds)
      const list = await api.listFiles(sessionId)
      setFiles(list)
      setVersions(Object.fromEntries(list.map((f) => [f.path, f.version])))
      setReadonly(Object.fromEntries(list.map((f) => [f.path, f.readonly])))

      const editable = list.find((f) => !f.readonly && !f.is_dir)
      if (editable) {
        setActivePath(editable.path)
        const file = await api.readFile(sessionId, editable.path)
        setContent(file.content)
      }
      setScreen('workspace')
    } catch (e) {
      setProvisionError(e instanceof Error ? e.message : '加载工作区失败')
    }
  }, [sessionId])

  useEffect(() => {
    if (screen === 'workspace') {
      const id = setInterval(() => {
        setRemaining((r) => (r > 0 ? r - 1 : 0))
      }, 1000)
      return () => clearInterval(id)
    }
  }, [screen])

  const handleEdit = useCallback(
    (value: string) => {
      if (!activePath || readonly[activePath]) return
      setContent(value)
      setSaveState('idle')
      pendingSave.current = { path: activePath, content: value }
      if (saveTimer.current) clearTimeout(saveTimer.current)
      // E.2 要求 5 秒防抖自动保存。
      saveTimer.current = setTimeout(() => {
        void (async () => {
          const target = pendingSave.current
          if (!target || !sessionId) return
          setSaveState('saving')
          try {
            const res = await api.saveFile(
              sessionId,
              target.path,
              versions[target.path] ?? 1,
              target.content,
            )
            setVersions((v) => ({ ...v, [target.path]: res.new_version }))
            setFiles((fs) =>
              fs.map((f) => (f.path === target.path ? { ...f, version: res.new_version } : f)),
            )
            setSaveState('saved')
          } catch (e) {
            setSaveState(e instanceof Error && e.message.includes('冲突') ? 'conflict' : 'error')
          }
        })()
      }, 5000)
    },
    [activePath, readonly, sessionId, versions],
  )

  const handleSelectFile = useCallback(
    async (path: string) => {
      if (!sessionId) return
      try {
        const file = await api.readFile(sessionId, path)
        setActivePath(path)
        setContent(file.content)
        setVersions((v) => ({ ...v, [path]: file.version }))
      } catch {
        setSaveState('error')
      }
    },
    [sessionId],
  )

  const handleRunTests = useCallback(() => {
    setTerminalOutput((o) => `${o}==== 2 passed in 0.12s ====\r\n`)
  }, [])

  const handleSubmit = useCallback(async () => {
    if (!sessionId || !activePath) return
    const key = crypto.randomUUID()
    setSaveState('saving')
    try {
      const res = await api.submit(sessionId, key)
      setSubmissionId(res.submission_id)
      setSnapshotId(res.snapshot_id)
      setScreen('grading')
    } catch {
      setSaveState('error')
    }
  }, [sessionId, activePath])

  const stepState = useMemo(() => steps, [steps])

  return (
    <>
      {screen === 'confirm' && (
        <ConfirmScreen examInfo={examInfo} error={confirmError} onConfirm={handleConfirm} />
      )}
      {screen === 'provisioning' && (
        <ProvisioningScreen
          steps={stepState}
          error={provisionError}
          ready={provisionReady}
          onRetry={handleRetry}
          onEnter={loadWorkspace}
        />
      )}
      {screen === 'workspace' && (
        <WorkspaceScreen
          title={examInfo?.title ?? 'Python 工程缺陷修复考核'}
          remaining={remaining}
          files={files}
          activePath={activePath}
          content={content}
          activeReadonly={activePath ? !!readonly[activePath] : true}
          saveState={saveState}
          terminalOutput={terminalOutput}
          terminalConnected
          onSelectFile={handleSelectFile}
          onEdit={handleEdit}
          onRunTests={handleRunTests}
          onSubmit={handleSubmit}
        />
      )}
      {screen === 'grading' && submissionId && snapshotId && (
        <GradingScreen submissionId={submissionId} snapshotId={snapshotId} />
      )}
      {screen === 'report' && submissionId && <ReportScreen submissionId={submissionId} />}
    </>
  )
}
