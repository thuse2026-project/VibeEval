import { useCallback, useEffect, useState } from 'react'
import { agentApi, type AgentConversation, type AgentInteraction, type AgentRun } from './agentApi'

type Props = { sessionId: string | null; onClose: () => void }
type Message = { role: 'user' | 'assistant'; text: string }

const activeStatuses = new Set(['queued', 'running', 'waiting_approval', 'waiting_user_input', 'waiting_operation'])
const statusLabel: Record<string, string> = {
  queued: '排队中', running: '运行中', waiting_approval: '等待批准',
  waiting_user_input: '等待补充信息', waiting_operation: '等待操作结果',
  completed: '已完成', failed: '失败', interrupted: '已中断', unknown: '结果待核查',
  capacity_exceeded: '上下文已达上限', budget_exhausted: '运行预算已用完', expired: '已过期',
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
  const question = request.question
  if (typeof question === 'string') return question
  const change = request.change
  const details = { ...request }
  delete details.change
  return [JSON.stringify(details, null, 2), change ? JSON.stringify(change, null, 2) : ''].filter(Boolean).join('\n')
}

export function AgentPanel({ sessionId, onClose }: Props) {
  const [conversations, setConversations] = useState<AgentConversation[]>([])
  const [conversationId, setConversationId] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [interactions, setInteractions] = useState<AgentInteraction[]>([])
  const [run, setRun] = useState<AgentRun | null>(null)
  const [draft, setDraft] = useState('')
  const [answerDrafts, setAnswerDrafts] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const refresh = useCallback(async (sid: string, cid: string) => {
    const [history, runs] = await Promise.all([agentApi.history(sid, cid), agentApi.runs(sid, cid)])
    setMessages(historyMessages(history.turns))
    const current = runs.items.find(item => activeStatuses.has(item.status)) ?? null
    setRun(current)
    if (current && ['waiting_approval', 'waiting_user_input'].includes(current.status)) {
      setInteractions((await agentApi.interactions(sid, cid, current.run_id)).items.filter(item => item.status === 'pending'))
    } else setInteractions([])
  }, [])

  useEffect(() => {
    if (!sessionId) return
    let cancelled = false
    agentApi.conversations(sessionId).then(async result => {
      if (cancelled) return
      setConversations(result.items)
      if (result.items[0]) {
        setConversationId(result.items[0].conversation_id)
        await refresh(sessionId, result.items[0].conversation_id)
      }
    }).catch(value => { if (!cancelled) setError((value as Error).message) })
    return () => { cancelled = true }
  }, [sessionId, refresh])

  async function selectConversation(id: string) {
    if (!sessionId) return
    setConversationId(id); setError(''); setInteractions([]); setRun(null)
    try { await refresh(sessionId, id) } catch (value) { setError((value as Error).message) }
  }

  async function createConversation() {
    if (!sessionId || busy) return
    setBusy(true); setError('')
    try {
      const conversation = await agentApi.createConversation(sessionId)
      setConversations(items => [conversation, ...items])
      setConversationId(conversation.conversation_id); setMessages([]); setRun(null); setInteractions([])
    } catch (value) { setError((value as Error).message) }
    finally { setBusy(false) }
  }

  async function advance(runId: string) {
    if (!sessionId || !conversationId) return
    setBusy(true); setError('')
    try {
      const result = await agentApi.advance(sessionId, conversationId, runId)
      setRun(result)
      await refresh(sessionId, conversationId)
    } catch (value) {
      setError((value as Error).message)
      try { await refresh(sessionId, conversationId) } catch { /* Keep the original request error. */ }
    } finally { setBusy(false) }
  }

  async function send() {
    if (!sessionId || !conversationId || !draft.trim() || busy || run && activeStatuses.has(run.status)) return
    const text = draft.trim(); setDraft(''); setBusy(true); setError('')
    try {
      const queued = await agentApi.turn(sessionId, conversationId, text)
      setRun(queued)
      await advance(queued.run_id)
    } catch (value) { setError((value as Error).message); setBusy(false) }
  }

  async function resolve(item: AgentInteraction, decision: 'approve' | 'reject' | 'answer') {
    if (!sessionId || !conversationId || !run || busy) return
    setBusy(true); setError('')
    try {
      if (decision === 'answer') {
        const answer = answerDrafts[item.interaction_id]?.trim()
        if (!answer) return
        await agentApi.answer(sessionId, conversationId, run.run_id, item.interaction_id, answer)
      } else {
        await agentApi.approval(sessionId, conversationId, run.run_id, item.interaction_id, decision)
      }
      setInteractions(items => items.filter(value => value.interaction_id !== item.interaction_id))
      await advance(run.run_id)
    } catch (value) { setError((value as Error).message) }
    finally { setBusy(false) }
  }

  return <>
    <button className="agent-scrim" aria-label="关闭 Agent 面板" onClick={onClose} />
    <aside className="agent-panel" aria-label="Agent 助手">
      <header className="agent-panel-head"><div><h2>Agent 助手</h2><span>只读分析 · 操作需服务端授权</span></div><button className="agent-close" title="关闭面板" onClick={onClose}>×</button></header>
      {!sessionId ? <p className="agent-empty">开始考试后可使用 Agent。</p> : <>
        <div className="agent-conversation-bar">
          <select aria-label="对话" value={conversationId} onChange={event => void selectConversation(event.target.value)}>
            <option value="">选择对话</option>
            {conversations.map(item => <option key={item.conversation_id} value={item.conversation_id}>{item.title || '新对话'}</option>)}
          </select>
          <button title="新建对话" onClick={() => void createConversation()} disabled={busy}>新建</button>
        </div>
        <div className="agent-messages" aria-live="polite">
          {messages.length === 0 && <p className="agent-empty">可以询问 Agent 关于当前题目或代码的问题。</p>}
          {messages.map((message, index) => <article className={`agent-message ${message.role}`} key={`${index}-${message.role}`}>
            <b>{message.role === 'user' ? '你' : 'Agent'}</b><p>{message.text}</p>
          </article>)}
          {run && <p className="agent-run-state" role="status">运行状态：{statusLabel[run.status] ?? run.status}{run.error_code ? ` · ${run.error_code}` : ''}</p>}
          {interactions.map(item => <section className="agent-interaction" key={item.interaction_id}>
            <b>{item.kind === 'approval' ? '请求批准操作' : 'Agent 需要补充信息'}</b>
            <pre>{requestSummary(item.request)}</pre>
            {item.kind === 'approval' ? <div className="agent-actions">
              <button disabled={busy} onClick={() => void resolve(item, 'reject')}>拒绝</button>
              <button className="primary" disabled={busy} onClick={() => void resolve(item, 'approve')}>批准</button>
            </div> : <div className="agent-answer"><textarea aria-label="补充回答" value={answerDrafts[item.interaction_id] ?? ''} onChange={event => setAnswerDrafts(values => ({ ...values, [item.interaction_id]: event.target.value }))} />
              <button className="primary" disabled={busy || !answerDrafts[item.interaction_id]?.trim()} onClick={() => void resolve(item, 'answer')}>发送回答</button></div>}
          </section>)}
        </div>
        {error && <div className="agent-error" role="alert">{error}</div>}
        {run && activeStatuses.has(run.status) && !['waiting_approval', 'waiting_user_input', 'running'].includes(run.status) && <button className="agent-resume" disabled={busy} onClick={() => void advance(run.run_id)}>继续运行</button>}
        <form className="agent-composer" onSubmit={event => { event.preventDefault(); void send() }}>
          <textarea aria-label="发送给 Agent" placeholder="输入问题…" value={draft} onChange={event => setDraft(event.target.value)} disabled={!conversationId || busy || !!run && activeStatuses.has(run.status)} maxLength={12000} />
          <button className="primary" disabled={!conversationId || busy || !draft.trim() || !!run && activeStatuses.has(run.status)}>{busy ? '处理中…' : '发送'}</button>
        </form>
      </>}
    </aside>
  </>
}
