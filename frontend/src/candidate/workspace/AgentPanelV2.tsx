import { useCallback, useEffect, useRef, useState } from 'react'
import {
  agentApi,
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
  sessionId: string | null
  onClose: () => void
  onWorkspaceChange: (change: AgentFileChange) => void
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

export function AgentPanel({ sessionId, onClose, onWorkspaceChange }: Props) {
  const [conversations, setConversations] = useState<AgentConversation[]>([])
  const [conversationId, setConversationId] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [interactions, setInteractions] = useState<AgentInteraction[]>([])
  const [operations, setOperations] = useState<AgentOperation[]>([])
  const [reads, setReads] = useState<AgentRead[]>([])
  const [run, setRun] = useState<AgentRun | null>(null)
  const [draft, setDraft] = useState('')
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
    const [history, runs, operationResult, readResult] = await Promise.all([
      agentApi.history(sid, cid),
      agentApi.runs(sid, cid),
      agentApi.operations(sid, cid),
      agentApi.reads(sid, cid),
    ])
    setMessages(historyMessages(history.turns))
    setOperations(operationResult.items)
    setReads(readResult.items)
    const current = runs.items[0] ?? null
    setRun(current)
    if (current && ['waiting_approval', 'waiting_user_input'].includes(current.status)) {
      setInteractions((await agentApi.interactions(sid, cid, current.run_id)).items.filter(item => item.status === 'pending'))
    } else {
      setInteractions([])
    }
    for (const item of operationResult.items) {
      const change = item.file_result?.change
      if (item.kind !== 'file_edit' || item.status !== 'succeeded' || change?.applied !== true || !change.path || notifiedChanges.current.has(item.operation_id)) continue
      notifiedChanges.current.add(item.operation_id)
      onWorkspaceChange({
        operationId: item.operation_id,
        path: change.path,
        version: change.current_version ?? null,
      })
    }
  }, [onWorkspaceChange])

  useEffect(() => {
    if (!sessionId) return
    let cancelled = false
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
        setConversationId(selected.conversation_id)
        await refresh(sessionId, selected.conversation_id)
      }
    }).catch(value => {
      if (!cancelled) setError((value as Error).message)
    })
    return () => { cancelled = true }
  }, [sessionId, refresh])

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
          })
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
  }, [sessionId, conversationId, refresh])

  const selectConversation = async (id: string) => {
    if (!sessionId) return
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

  const createConversation = async () => {
    if (!sessionId) return null
    const conversation = await agentApi.createConversation(sessionId)
    setConversations(items => [conversation, ...items])
    setConversationId(conversation.conversation_id)
    setMessages([])
    setRun(null)
    setInteractions([])
    setOperations([])
    setReads([])
    window.localStorage.setItem(`vibeeval.agent.${sessionId}`, conversation.conversation_id)
    return conversation
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
  }, [sessionId, conversationId, refresh])

  const runId = run?.run_id
  const runStatus = run?.status
  useEffect(() => {
    if (!runId || !runStatus || advancing || !['queued', 'waiting_operation'].includes(runStatus)) return
    const timer = window.setTimeout(() => void advance(runId), 1200)
    return () => window.clearTimeout(timer)
  }, [runId, runStatus, advancing, advance])

  const send = async () => {
    if (!sessionId || !draft.trim() || busy || run && activeStatuses.has(run.status)) return
    const text = draft.trim()
    setBusy(true)
    setError('')
    try {
      const conversation = conversationId ? null : await createConversation()
      const cid = conversationId || conversation?.conversation_id
      if (!cid) throw new Error('无法创建 Agent 对话。')
      setDraft('')
      const queued = await agentApi.turn(sessionId, cid, text)
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
        await agentApi.approval(sessionId, conversationId, run.run_id, item.interaction_id, decision)
      }
      setInteractions(items => items.filter(value => value.interaction_id !== item.interaction_id))
      setRun({ ...run, status: 'queued' })
    } catch (value) {
      setError((value as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return <aside className="agent-panel" aria-label="Agent 助手">
    <header className="agent-panel-head">
      <div><h2>Agent 助手</h2><span>{streamState} · 操作需服务端授权</span></div>
      <button className="agent-close" title="关闭面板" onClick={onClose}>×</button>
    </header>
    {!sessionId ? <p className="agent-empty">当前题未开放 Agent：请确认考试状态或联系监考。</p> : <>
      <div className="agent-conversation-bar">
        <select aria-label="对话" value={conversationId} onChange={event => void selectConversation(event.target.value)}>
          <option value="">发送时自动新建对话</option>
          {conversations.map(item => <option key={item.conversation_id} value={item.conversation_id}>{item.title || '新对话'}</option>)}
        </select>
        <button title="新建对话" onClick={() => void createConversation()} disabled={busy}>新建</button>
      </div>
      <div className="agent-messages" aria-live="polite">
        {messages.length === 0 && <p className="agent-empty">可以让 Agent 阅读当前题目和文件；修改文件或运行命令时会请求批准。</p>}
        {messages.map((message, index) => <article className={`agent-message ${message.role}`} key={`${index}-${message.role}`}>
          <b>{message.role === 'user' ? '你' : 'Agent'}</b><p>{message.text}</p>
        </article>)}
        {run && <div className="agent-run-state" role="status">
          <span>运行状态：{statusLabel[run.status] ?? run.status}{run.error_code ? ` · ${run.error_code}` : ''}</span>
          {activeStatuses.has(run.status) && <button disabled={busy} onClick={() => void stop()}>停止</button>}
        </div>}
        {(reads.length > 0 || operations.length > 0) && <section className="agent-activity" aria-label="Agent 操作记录">
          <h3>工具活动</h3>
          {reads.map(item => <div className="agent-tool-row" key={item.observation_id}>
            <span className="agent-tool-kind">读取</span><code>{readTarget(item)}</code><em>{item.status}</em>
          </div>)}
          {operations.map(item => <div className="agent-tool-row" key={item.operation_id}>
            <span className="agent-tool-kind">{item.kind === 'file_edit' ? '修改' : '命令'}</span>
            <code title={operationTarget(item)}>{operationTarget(item)}</code>
            <em>{item.status}{item.exit_code !== null ? ` (${item.exit_code})` : ''}{item.reason_code ? ` · ${item.reason_code}` : ''}</em>
          </div>)}
        </section>}
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
        <textarea aria-label="发送给 Agent" placeholder="让 Agent 阅读文件、解释代码或提出修改……" value={draft} onChange={event => setDraft(event.target.value)} disabled={busy || !!run && activeStatuses.has(run.status)} maxLength={12000} />
        <button className="primary" disabled={busy || !draft.trim() || !!run && activeStatuses.has(run.status)}>{busy ? '处理中…' : '发送'}</button>
      </form>
    </>}
  </aside>
}
