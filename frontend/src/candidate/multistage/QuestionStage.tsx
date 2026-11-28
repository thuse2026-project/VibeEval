import { useCallback, useEffect, useRef, useState } from 'react'
import { useCandidateTheme } from '../../shared/candidateTheme'
import { AnswerWorkspace } from './AnswerWorkspace'
import { ReadonlyFiles } from './ReadonlyFiles'
import { command, current, examRequest, RequestError, type Attempt, type Receipt, v2 } from './api'

type Question = { id: string; title: string; body: string; answer_schema?: { maxLength?: number } }
type SavedDraft = { revision: number; value: unknown }
type Draft = { value: string; revision: number; status: 'saved' | 'dirty' | 'saving' | 'conflict' | 'error'; message?: string; serverValue?: string }
type FrozenCode = { workspace_id: string; sha256: string; coding_phase_id: string; closed_at: number }
type EvidenceItem = { id: string; kind: 'message' | 'approval' | 'edit' | 'test_run' | 'terminal' | 'command'; actor: 'candidate' | 'agent' | 'system'; at: number; data: Record<string, unknown> }
type EvidencePage = { coding_phase_id: string; sha256: string; completeness: 'complete' | 'partial'; reason: string | null; items: EvidenceItem[]; next_cursor: string | null }

const actorName = { candidate: '本人', agent: 'Agent', system: '系统' }
const kindName = { message: '消息', approval: '审批', edit: '文件改动', test_run: '测试运行', terminal: '终端回显', command: 'Agent 命令' }

function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '无'
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value, null, 2)
}

function EvidenceView({ phase, frozen }: { phase: string; frozen: FrozenCode }) {
  const themed = useCandidateTheme() !== null
  const [page, setPage] = useState<EvidencePage | null>(null)
  const [items, setItems] = useState<EvidenceItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async (cursor?: string) => {
    setLoading(true); setError('')
    try {
      const query = new URLSearchParams({ limit: '50' })
      if (cursor) query.set('cursor', cursor)
      const next = await examRequest<EvidencePage>(`${v2}/phases/${phase}/evidence?${query}`)
      if (next.sha256 !== frozen.sha256 || next.coding_phase_id !== frozen.coding_phase_id) throw new Error('证据与冻结代码不属于同一快照，请停止作答并联系考官。')
      setPage(next)
      setItems(values => cursor ? [...values, ...next.items] : next.items)
    } catch (failure) { setError((failure as Error).message) }
    finally { setLoading(false) }
  }, [frozen.coding_phase_id, frozen.sha256, phase])

  useEffect(() => { void load() }, [load])
  const completedRuns = new Set(items.filter(item => item.kind === 'test_run' && item.data.status !== 'started').map(item => item.data.run_id))
  const reason = page?.reason === 'COLLECTION_NOT_CONNECTED' ? '操作记录采集尚未接通，请考官复核。'
    : page?.reason === 'CAPTURE_GAP' ? '平台检测到部分操作记录缺失，请考官复核。' : page?.reason
  return <section className="r2-evidence" aria-label="本题操作记录">
    <div className="r2-section-heading">{themed ? <span>编程阶段 · {items.length} 条记录</span> : <><h2>操作记录</h2><span>{items.length} 条</span></>}</div>
    {page?.completeness === 'partial' && <p className="r2-evidence-warning" role="status">记录不完整：{reason ?? '原因未知，请考官复核。'}</p>}
    {error && <p role="alert">{error} <button onClick={() => void load()}>重试</button></p>}
    {!loading && !error && items.length === 0 && <p className="r2-muted">本题没有已收录的操作记录。</p>}
    <ol className="r2-evidence-list">{items.map(item => {
      const unfinished = page && !page.next_cursor && item.kind === 'test_run' && item.data.status === 'started' && !completedRuns.has(item.data.run_id)
      const showSummary = themed && (item.kind === 'edit' || item.kind === 'test_run')
      return <li key={item.id}><header><strong>{actorName[item.actor]} · {kindName[item.kind]}</strong><time>{new Date(item.at * 1000).toLocaleString()}</time></header>
        {unfinished && <p className="r2-unfinished">截止时未完成</p>}
        {showSummary && <div className="evidence-summary">
          {['path', 'command', 'output'].map(key => item.data[key] != null && <pre key={key}>{display(item.data[key])}</pre>)}
        </div>}
        {showSummary
          ? <details><summary>记录详情</summary><EvidenceFields data={item.data} /></details>
          : <EvidenceFields data={item.data} />}
      </li>
    })}</ol>
    {page?.next_cursor && <button disabled={loading} onClick={() => void load(page.next_cursor ?? undefined)}>{loading ? '正在读取…' : '加载更多记录'}</button>}
  </section>
}

function EvidenceFields({ data }: { data: Record<string, unknown> }) {
  return <dl>{Object.entries(data).map(([key, value]) => <div key={key}><dt>{key}</dt><dd><pre>{display(value)}</pre></dd></div>)}</dl>
}

export function QuestionStage({ state, remaining, refresh }: { state: Attempt; remaining: number; refresh: () => Promise<void> }) {
  const phase = state.current_phase.id
  const prefix = `${v2}/phases/${phase}`
  const [questions, setQuestions] = useState<Question[]>([])
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})
  const draftsRef = useRef<Record<string, Draft>>({})
  const timers = useRef(new Map<string, number>())
  const pending = useRef(new Map<string, Promise<void>>())
  const [active, setActive] = useState('')
  const [notes, setNotes] = useState<string | null>(null)
  const [frozen, setFrozen] = useState<FrozenCode | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const submissionKey = useRef<string | null>(null)
  const canReadNotes = state.capabilities.includes('read_notes')
  const canReadFrozen = state.capabilities.includes('read_frozen_code')

  function update(id: string, change: Partial<Draft>) {
    const next = { ...draftsRef.current[id], ...change }
    draftsRef.current = { ...draftsRef.current, [id]: next }
    setDrafts(draftsRef.current)
  }

  async function write(id: string, draft: Draft) {
    const sent = draft.value
    update(id, { status: 'saving', message: undefined })
    try {
      const result = await examRequest<SavedDraft>(`${prefix}/answers/${encodeURIComponent(id)}`, 'PUT', { expected_revision: draft.revision, value: sent })
      const latest = draftsRef.current[id]
      update(id, { revision: result.revision, status: latest.value === sent ? 'saved' : 'dirty' })
    } catch (failure) {
      if (failure instanceof RequestError && failure.code === 'REVISION_CONFLICT') {
        update(id, { status: 'conflict', serverValue: undefined, message: '版本冲突。请重新获取服务器答案后核对。' })
        const latest = (await examRequest<Record<string, SavedDraft>>(`${prefix}/answers`))[id]
        if (latest?.value === sent) {
          update(id, { revision: latest.revision, status: draftsRef.current[id].value === sent ? 'saved' : 'dirty', message: undefined })
          return
        }
        update(id, { revision: latest?.revision ?? draft.revision, serverValue: String(latest?.value ?? ''), status: 'conflict', message: '另一窗口已保存新版本。请核对后选择要保留的答案。' })
      } else update(id, { status: 'error', message: (failure as Error).message })
      throw failure
    }
  }

  async function save(id: string) {
    while (true) {
      const running = pending.current.get(id)
      if (running) { await running; continue }
      const draft = draftsRef.current[id]
      if (!draft || draft.status === 'saved') return
      if (draft.status === 'conflict') throw new Error('请先处理答案的版本冲突。')
      const next = write(id, draft).finally(() => pending.current.delete(id))
      pending.current.set(id, next)
      await next
    }
  }

  function edit(id: string, value: string) {
    update(id, { value, status: draftsRef.current[id]?.status === 'conflict' ? 'conflict' : 'dirty' })
    window.clearTimeout(timers.current.get(id))
    if (draftsRef.current[id].status === 'conflict') return
    timers.current.set(id, window.setTimeout(() => { timers.current.delete(id); void save(id).catch(() => undefined) }, 1200))
  }

  useEffect(() => {
    const abort = new AbortController()
    const timerMap = timers.current
    void (async () => {
      const [questionPage, saved] = await Promise.all([
        examRequest<{ items: Question[] }>(`${prefix}/questions`, 'GET', undefined, abort.signal),
        examRequest<Record<string, SavedDraft>>(`${prefix}/answers`, 'GET', undefined, abort.signal),
      ])
      if (abort.signal.aborted) return
      const initial = Object.fromEntries(questionPage.items.map(question => [question.id, {
        value: typeof saved[question.id]?.value === 'string' ? saved[question.id].value as string : '',
        revision: saved[question.id]?.revision ?? 0, status: 'saved' as const,
      }]))
      draftsRef.current = initial; setDrafts(initial); setQuestions(questionPage.items); setActive(questionPage.items[0]?.id ?? '')
      if (canReadNotes) {
        const value = await examRequest<{ value: string | null }>(`${prefix}/notes`, 'GET', undefined, abort.signal)
        if (!abort.signal.aborted) setNotes(value.value ?? '')
      }
      if (canReadFrozen) {
        const value = await examRequest<FrozenCode>(`${prefix}/frozen-code`, 'GET', undefined, abort.signal)
        if (!abort.signal.aborted) setFrozen(value)
      }
    })().catch(failure => { if (!abort.signal.aborted) setError((failure as Error).message) })
    return () => { abort.abort(); for (const timer of timerMap.values()) window.clearTimeout(timer) }
  }, [canReadFrozen, canReadNotes, prefix])

  async function submit() {
    setBusy(true); setError('')
    try {
      for (const timer of timers.current.values()) window.clearTimeout(timer)
      timers.current.clear()
      for (const question of questions) await save(question.id)
      if (Object.values(draftsRef.current).some(draft => draft.status !== 'saved')) throw new Error('仍有回答未保存，请核对冲突或网络错误后重试。')
      const latest = await current()
      if (latest.current_phase.id !== phase || latest.current_phase.status !== 'active') {
        await refresh()
        return
      }
      if (!submissionKey.current) submissionKey.current = crypto.randomUUID()
      await command<Receipt>(`/stages/${latest.current_stage.id}/submit`, latest, {}, submissionKey.current)
      await refresh()
    } catch (failure) { setError((failure as Error).message) }
    finally { setBusy(false) }
  }

  const selected = questions.find(question => question.id === active)
  const draft = active ? drafts[active] : undefined
  return <AnswerWorkspace state={state} remaining={remaining} status={Object.values(drafts).some(d => d.status !== 'saved') ? '有未保存的回答' : '已保存'}
    busy={busy || !questions.length} submit={submit} questions={questions} active={active} select={setActive}
    answered={id => Boolean(drafts[id]?.value.trim())} alerts={error && <p className="r2-page-alert" role="alert">{error}</p>}
    notes={notes} files={<ReadonlyFiles phase={phase} notes={state.current_stage.key === 'column-statistics' ? undefined : notes} sha={frozen?.sha256} />}
    evidence={frozen && <EvidenceView phase={phase} frozen={frozen} />}>
      <section className="r2-answer-panel">
        {selected ? <><h1>{selected.title}</h1><p className="r2-question-body">{selected.body}</p>
          <label htmlFor="r2-answer">你的回答</label><textarea id="r2-answer" rows={14} maxLength={selected.answer_schema?.maxLength ?? 1500} disabled={busy || remaining <= 0} value={draft?.value ?? ''} onChange={event => edit(selected.id, event.target.value)} />
          <div className="r2-answer-status"><span>{draft?.value.length ?? 0} / {selected.answer_schema?.maxLength ?? 1500}</span><span>{draft?.status === 'saving' ? '正在保存…' : draft?.status === 'saved' ? '已保存' : draft?.status === 'conflict' ? '版本冲突，草稿已保留' : '尚未保存'}</span></div>
          {draft?.message && <div role="alert">{draft.message}
            {draft.status === 'conflict' && draft.serverValue !== undefined && <>
              <details><summary>查看服务器答案</summary><pre>{draft.serverValue}</pre></details>
              <button disabled={busy} onClick={() => update(selected.id, { value: draft.serverValue ?? '', status: 'saved', message: undefined })}>使用服务器答案</button>
            </>}
            <button disabled={busy} onClick={() => {
              update(selected.id, { status: 'dirty' })
              void save(selected.id).catch(failure => setError((failure as Error).message))
            }}>{draft.status === 'conflict' && draft.serverValue !== undefined ? '保留我的答案' : '重试保存'}</button>
          </div>}
        </> : <p>正在读取题目…</p>}
      </section>
  </AnswerWorkspace>
}
