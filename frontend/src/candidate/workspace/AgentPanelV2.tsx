import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { renderMarkdown } from '../../shared/markdown'
import {
  createAgentApi,
  streamAgentEvents,
  type AgentConversation,
  type AgentInteraction,
  type AgentOperation,
  type AgentRead,
  type AgentRun,
} from './agentApi'

export type AgentFileChange = {
  operationId: string
  path: string
  version: number | null
}

type Props = {
  sessionId: string
  prefix?: string
  modelReady: boolean
  canExecute?: boolean
  hidden: boolean
  beforeRun?: () => Promise<void>
  onBusy?: (busy: boolean) => void
  onClose: () => void
  onWorkspaceChange?: (change: AgentFileChange) => void
}
type Message = { role: 'user' | 'assistant'; text: string }

const activeStatuses = new Set(['queued', 'running', 'waiting_approval', 'waiting_user_input', 'waiting_operation'])
const statusLabel: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  waiting_approval: '等待批准',
  waiting_user_input: '等待补充信息',
  waiting_operation: '等待操作结果',
  completed: '已完成',
  failed: '失败',
  interrupted: '已停止',
  unknown: '结果待核查',
  capacity_exceeded: '上下文已达上限',
  budget_exhausted: '运行预算已用完',
  expired: '已过期',
}
const runErrorLabel: Record<string, string> = {
  RUN_TOKEN_BUDGET_EXHAUSTED: '本轮模型用量已达上限',
  RUN_BUDGET_EXHAUSTED: '本轮步骤已达上限',
  MODEL_RATE_LIMITED: '模型服务请求过多，请稍后再试',
  MODEL_QUOTA_EXHAUSTED: '模型服务额度不足',
  MODEL_ACCESS_DENIED: '模型服务鉴权失败',
  MODEL_OUTCOME_UNKNOWN: '连接中断或超时，模型结果待核查',
  MODEL_TOOL_ARGUMENTS_INVALID: '模型未能生成有效的工具参数',
  COMPACTION_NOT_ADOPTED: '当前对话无法继续压缩，可开启新对话',
}

function historyMessages(turns: Array<{ user_message: string; steps: Array<{ response: { message: { content: string | null } } | null }> }>): Message[] {
  return turns.flatMap(turn => [
    { role: 'user' as const, text: turn.user_message },
    ...turn.steps.flatMap(step => {
      const content = step.response?.message.content
      return content?.trim() ? [{ role: 'assistant' as const, text: content }] : []
    }),
  ])
}

function requestSummary(request: Record<string, unknown>) {
  if (typeof request.question === 'string') return request.question
  const details = { ...request }
  delete details.change
  return JSON.stringify(details, null, 2)
}

function readTarget(item: AgentRead) {
  try {
    const value = JSON.parse(item.result?.content ?? '{}') as Record<string, unknown>
    return String(value.path ?? value.source_id ?? value.material_id ?? item.tool_name)
  } catch {
    return item.tool_name
  }
}

function operationTarget(item: AgentOperation) {
  if (item.kind === 'file_edit') return String(item.change?.path ?? item.arguments.path ?? '文件')
  const argv = item.arguments.argv
  return Array.isArray(argv) ? argv.map(String).join(' ') : String(item.arguments.command ?? '命令')
}

export function AgentPanel({ sessionId, prefix, modelReady, canExecute = true, hidden, beforeRun, onBusy, onClose, onWorkspaceChange }: Props) {
  const previewEntry = import.meta.env.MODE === 'design-review' ? import.meta.env.VITE_AGENT_PREVIEW_ENTRY : undefined
  const agentApi = useMemo(() => createAgentApi(prefix), [prefix])
  const staged = prefix?.startsWith('/api/v2/') ?? false
  const [intent, setIntent] = useState<'plan' | 'default'>('plan')
  const target = useRef('')
  const requestOrder = useRef(0)
  const [conversations, setConversations] = useState<AgentConversation[]>([])
  const [loadingConversations, setLoadingConversations] = useState(modelReady)
  const [conversationId, setConversationId] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [interactions, setInteractions] = useState<AgentInteraction[]>([])
  const [operations, setOperations] = useState<AgentOperation[]>([])
  const [reads, setReads] = useState<AgentRead[]>([])
  const [run, setRun] = useState<AgentRun | null>(null)
  const [draft, setDraft] = useState('')
  const [titleEdit, setTitleEdit] = useState<{ id: string | null; value: string } | null>(null)
  const [answerDrafts, setAnswerDrafts] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [advancing, setAdvancing] = useState(false)
  const [streamState, setStreamState] = useState('等待事件')
  const [error, setError] = useState('')
  const eventSequence = useRef(0)
  const refreshTimer = useRef<number | null>(null)
  const notifiedChanges = useRef(new Set<string>())
  const advancePending = useRef(false)

  const refresh = useCallback(async (sid: string, cid: string) => {
    const order = ++requestOrder.current
    const [history, runs, operationResult, readResult] = await Promise.all([
      agentApi.history(sid, cid),
      agentApi.runs(sid, cid),
      agentApi.operations(sid, cid),
      agentApi.reads(sid, cid),
    ])
    const current = runs.items.find(item => activeStatuses.has(item.status)) ?? runs.items[0] ?? null
    const pending = current && ['waiting_approval', 'waiting_user_input'].includes(current.status)
      ? (await agentApi.interactions(sid, cid, current.run_id)).items.filter(item => item.status === 'pending')
      : []
    if (target.current !== cid || order !== requestOrder.current) return
    setMessages(historyMessages(history.turns))
    setOperations(operationResult.items)
    setReads(readResult.items)
    setRun(current)
    setInteractions(pending)
    for (const item of operationResult.items) {
      const change = item.file_result?.change
      if (item.kind !== 'file_edit' || item.status !== 'succeeded' || change?.applied !== true || !change.path || notifiedChanges.current.has(item.operation_id)) continue
      notifiedChanges.current.add(item.operation_id)
      onWorkspaceChange?.({
        operationId: item.operation_id,
        path: change.path,
        version: change.current_version ?? null,
      })
    }
  }, [agentApi, onWorkspaceChange])

  useEffect(() => {
    if (!modelReady || previewEntry) return
    let cancelled = false
    setLoadingConversations(true)
    setConversations([])
    setConversationId('')
    setMessages([])
    setOperations([])
    setReads([])
    setRun(null)
    setError('')
    eventSequence.current = 0
    notifiedChanges.current.clear()
    agentApi.conversations(sessionId).then(async result => {
      if (cancelled) return
      setConversations(result.items)
      const saved = window.localStorage.getItem(`vibeeval.agent.${sessionId}`)
      const selected = result.items.find(item => item.conversation_id === saved) ?? result.items[0]
      if (selected) {
        target.current = selected.conversation_id
        setConversationId(selected.conversation_id)
        await refresh(sessionId, selected.conversation_id)
      }
    }).catch(value => {
      if (!cancelled) setError((value as Error).message)
    }).finally(() => {
      if (!cancelled) setLoadingConversations(false)
    })
    return () => { cancelled = true; target.current = '' }
  }, [agentApi, sessionId, modelReady, refresh, previewEntry])

  useEffect(() => {
    if (!sessionId || !conversationId) return
    let disposed = false
    let controller: AbortController | null = null
    const scheduleRefresh = () => {
      if (refreshTimer.current !== null) return
      refreshTimer.current = window.setTimeout(() => {
        refreshTimer.current = null
        void refresh(sessionId, conversationId).catch(value => setError((value as Error).message))
      }, 120)
    }
    const listen = async () => {
      while (!disposed) {
        controller = new AbortController()
        try {
          setStreamState('实时事件已连接')
          await streamAgentEvents(sessionId, conversationId, eventSequence.current, controller.signal, frame => {
            if (frame.event === 'agent.event') {
              const sequence = frame.data.sequence
              if (typeof sequence === 'number') eventSequence.current = Math.max(eventSequence.current, sequence)
              scheduleRefresh()
            }
            if (frame.event === 'stream.error') {
              const code = frame.data.code
              setStreamState(typeof code === 'string' ? `事件流：${code}` : '事件流已断开')
            }
          }, prefix)
        } catch (value) {
          if (!controller.signal.aborted) setStreamState(`事件流重连中：${(value as Error).message}`)
        }
        if (!disposed) await new Promise(resolve => window.setTimeout(resolve, 1000))
      }
    }
    void listen()
    return () => {
      disposed = true
      controller?.abort()
      if (refreshTimer.current !== null) window.clearTimeout(refreshTimer.current)
      refreshTimer.current = null
    }
  }, [sessionId, conversationId, prefix, refresh])

  const selectConversation = async (id: string) => {
    if (!sessionId) return
    target.current = id
    setConversationId(id)
    setError('')
    setInteractions([])
    setRun(null)
    setOperations([])
    setReads([])
    eventSequence.current = 0
    window.localStorage.setItem(`vibeeval.agent.${sessionId}`, id)
    try {
      await refresh(sessionId, id)
    } catch (value) {
      setError((value as Error).message)
    }
  }

  const createConversation = async (title = '新对话') => {
    if (!sessionId) return null
    const conversation = await agentApi.createConversation(sessionId, title)
    setConversations(items => [conversation, ...items])
    target.current = conversation.conversation_id
    setConversationId(conversation.conversation_id)
    eventSequence.current = 0
    setMessages([])
    setRun(null)
    setInteractions([])
    setOperations([])
    setReads([])
    window.localStorage.setItem(`vibeeval.agent.${sessionId}`, conversation.conversation_id)
    return conversation
  }

  const saveTitle = async () => {
    const title = titleEdit?.value.trim()
    if (!titleEdit || !title || busy) return
    setBusy(true)
    setError('')
    try {
      if (titleEdit.id) {
        const renamed = await agentApi.renameConversation(sessionId, titleEdit.id, title)
        setConversations(items => items.map(item => item.conversation_id === renamed.conversation_id ? renamed : item))
      } else {
        await createConversation(title)
      }
      setTitleEdit(null)
    } catch (value) {
      setError((value as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const advance = useCallback(async (runId: string, sid = sessionId, cid = conversationId) => {
    if (!sid || !cid || advancePending.current) return
    advancePending.current = true
    setAdvancing(true)
    setError('')
    try {
      setRun(await agentApi.advance(sid, cid, runId))
      await refresh(sid, cid)
    } catch (value) {
      setError((value as Error).message)
      try {
        await refresh(sid, cid)
      } catch {
        // Preserve the action error.
      }
    } finally {
      advancePending.current = false
      setAdvancing(false)
    }
  }, [agentApi, sessionId, conversationId, refresh])

  useEffect(() => {
    if (!run || run.server_driven || advancing || !['queued', 'waiting_operation'].includes(run.status)) return
    const timer = window.setTimeout(() => void advance(run.run_id), 1200)
    return () => window.clearTimeout(timer)
  }, [run, advancing, advance])

  const send = async () => {
    if (!sessionId || !draft.trim() || busy || run && activeStatuses.has(run.status)) return
    const text = draft.trim()
    setBusy(true)
    setError('')
    try {
      const selectedIntent = staged && (!canExecute || intent === 'plan') ? 'plan' : 'default'
      if (selectedIntent === 'default') await beforeRun?.()
      const conversation = conversationId ? null : await createConversation()
      const cid = conversationId || conversation?.conversation_id
      if (!cid) throw new Error('无法创建 Agent 对话。')
      const queued = await agentApi.turn(sessionId, cid, text, selectedIntent)
      setDraft('')
      setRun(queued)
    } catch (value) {
      setError((value as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const stop = async () => {
    if (!sessionId || !conversationId || !run || !activeStatuses.has(run.status)) return
    setBusy(true)
    setError('')
    try {
      setRun(await agentApi.interrupt(sessionId, conversationId, run.run_id))
      await refresh(sessionId, conversationId)
    } catch (value) {
      setError((value as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const resolve = async (item: AgentInteraction, decision: 'approve' | 'reject' | 'answer') => {
    if (!sessionId || !conversationId || !run || busy) return
    setBusy(true)
    setError('')
    try {
      if (decision === 'answer') {
        const answer = answerDrafts[item.interaction_id]?.trim()
        if (!answer) return
        await agentApi.answer(sessionId, conversationId, run.run_id, item.interaction_id, answer)
      } else {
        if (decision === 'approve') await beforeRun?.()
        await agentApi.approval(sessionId, conversationId, run.run_id, item.interaction_id, decision)
      }
      setInteractions(items => items.filter(value => value.interaction_id !== item.interaction_id))
      await refresh(sessionId, conversationId)
    } catch (value) {
      setError((value as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const interactionBusy = loadingConversations || busy || Boolean(run && activeStatuses.has(run.status))
  const selectedConversation = conversations.find(item => item.conversation_id === conversationId)
  const serverExecution = run?.server_driven && run.status === 'running' && run.mode === 'confirm_actions'
  useEffect(() => { onBusy?.(busy || advancing || Boolean(serverExecution)) }, [onBusy, busy, advancing, serverExecution])
  useEffect(() => () => onBusy?.(false), [onBusy])

  return <aside hidden={hidden} className="agent-panel" aria-label="Agent 助手">
    <header className="agent-panel-head">
      <h2>Agent 助手</h2>
      <button className="agent-close" aria-label="关闭 Agent 面板" title="关闭面板" onClick={onClose}>×</button>
    </header>
    {previewEntry ? <p className="agent-empty">此页用于评审界面。要与只读 Agent 对话，请使用入场凭证进入
      <a href={previewEntry} target="_blank" rel="noreferrer">真实联测工作台</a>。</p>
      : !modelReady ? <p className="agent-empty">当前工作台未连接模型服务。</p> : <>
      {titleEdit ? <form className="agent-conversation-bar" onSubmit={event => { event.preventDefault(); void saveTitle() }}
        onKeyDown={event => { if (event.key === 'Escape' && !busy) setTitleEdit(null) }}>
        <input aria-label="对话名称" placeholder="输入对话名称" autoFocus required maxLength={200} disabled={busy}
          value={titleEdit.value} onChange={event => setTitleEdit({ ...titleEdit, value: event.target.value })} />
        <button className="primary" disabled={busy || !titleEdit.value.trim()}>{titleEdit.id ? '保存' : '创建'}</button>
        <button type="button" disabled={busy} onClick={() => setTitleEdit(null)}>取消</button>
      </form> : <div className="agent-conversation-bar">
        <select disabled={interactionBusy} aria-label="对话" value={conversationId} onChange={event => void selectConversation(event.target.value)}>
          <option value="">新对话</option>
          {conversations.map(item => <option key={item.conversation_id} value={item.conversation_id}>{item.title || '新对话'}</option>)}
        </select>
        <button disabled={interactionBusy || !selectedConversation} onClick={() => selectedConversation
          && setTitleEdit({ id: selectedConversation.conversation_id, value: selectedConversation.title })}>改名</button>
        <button aria-label="新建对话" title="新建对话" onClick={() => setTitleEdit({ id: null, value: '' })} disabled={interactionBusy}>＋ 新建</button>
      </div>}
      <div className="agent-messages" aria-live="polite">
        {messages.length === 0 && <p className="agent-empty">可以让 Agent 阅读已保存的文件并给出计划。{canExecute && '切到执行模式后，修改文件和运行命令会请求批准。'}</p>}
        {messages.map((message, index) => <article className={`agent-message ${message.role}`} key={`${index}-${message.role}`}>
          <b>{message.role === 'user' ? '你' : 'Agent'}</b>
          {message.role === 'assistant'
            ? <div className="agent-markdown" dangerouslySetInnerHTML={renderMarkdown(message.text)} />
            : <p>{message.text}</p>}
        </article>)}
        {(reads.length > 0 || operations.length > 0) && <details className="agent-activity" aria-label="Agent 操作记录">
          <summary>工具活动（{reads.length + operations.length}）</summary>
          {reads.map(item => <div className="agent-tool-row" key={item.observation_id}>
            <span className="agent-tool-kind">读取</span><code>{readTarget(item)}</code><em>{item.status}</em>
          </div>)}
          {operations.map(item => <div className="agent-tool-row" key={item.operation_id}>
            <span className="agent-tool-kind">{item.kind === 'file_edit' ? '修改' : '命令'}</span>
            <code title={operationTarget(item)}>{operationTarget(item)}</code>
            <em>{item.status}{item.exit_code !== null ? ` (${item.exit_code})` : ''}{item.reason_code ? ` · ${item.reason_code}` : ''}</em>
          </div>)}
        </details>}
        {interactions.map(item => <section className="agent-interaction" key={item.interaction_id}>
          <b>{item.kind === 'approval' ? '请求批准操作' : 'Agent 需要补充信息'}</b>
          <pre>{requestSummary(item.request)}</pre>
          {item.kind === 'approval' ? <div className="agent-actions">
            <button disabled={busy} onClick={() => void resolve(item, 'reject')}>拒绝</button>
            <button className="primary" disabled={busy} onClick={() => void resolve(item, 'approve')}>批准</button>
          </div> : <div className="agent-answer">
            <textarea aria-label="补充回答" value={answerDrafts[item.interaction_id] ?? ''} onChange={event => setAnswerDrafts(values => ({ ...values, [item.interaction_id]: event.target.value }))} />
            <button className="primary" disabled={busy || !answerDrafts[item.interaction_id]?.trim()} onClick={() => void resolve(item, 'answer')}>发送回答</button>
          </div>}
        </section>)}
      </div>
      {error && <div className="agent-error" role="alert">{error}</div>}
      <form className="agent-composer" onSubmit={event => { event.preventDefault(); void send() }}>
        {staged && canExecute && <select aria-label="Agent 模式" value={intent} disabled={interactionBusy}
          onChange={event => setIntent(event.target.value === 'default' ? 'default' : 'plan')}>
          <option value="plan">只读计划</option>
          <option value="default">执行需批准</option>
        </select>}
        <textarea onKeyDown={event => {
          if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
          event.preventDefault()
          if (!event.repeat && !interactionBusy && draft.trim()) event.currentTarget.form?.requestSubmit()
        }} aria-label="发送给 Agent" placeholder="阅读文件、解释代码或提出修改" value={draft} onChange={event => setDraft(event.target.value)} disabled={interactionBusy || !!titleEdit} maxLength={12000} />
        <div className="agent-run-state" data-state={run?.status ?? 'idle'}>
          <span role="status" title={run?.error_code ?? streamState}>{run
            ? `运行状态：${statusLabel[run.status] ?? run.status}${run.error_code ? ` · ${runErrorLabel[run.error_code] ?? run.error_code}` : ''}`
            : busy ? '正在处理…' : '就绪'}</span>
          {run && activeStatuses.has(run.status)
            ? <button type="button" disabled={busy} onClick={() => void stop()}>停止</button>
            : <button className="primary" disabled={interactionBusy || !!titleEdit || !draft.trim()}>{busy ? '处理中…' : '发送'}</button>}
        </div>
      </form>
    </>}
  </aside>
}
