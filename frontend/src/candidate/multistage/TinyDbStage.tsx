import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { renderMarkdown } from '../../shared/markdown'
import { ReadonlyFiles } from './ReadonlyFiles'
import { AnswerWorkspace } from './AnswerWorkspace'
import { ReadingWorkspace } from './ReadingWorkspace'
import {
  command,
  current,
  examRequest,
  RequestError,
  type Attempt,
  v2,
} from './api'

type Problem = { id: string; title: string; body: string }
const labels = {
  location: '位置',
  trigger: '触发条件或操作',
  actual: '实际行为',
  expected: '期望行为',
  cause: '根因解释',
}
type Finding = Record<keyof typeof labels, string>
type Report = { conclusion: 'undecided' | 'found' | 'none'; reports: Finding[] }
type Value = string | Report
type Draft = { value: Value | null; revision: number }
const blankReport: Report = { conclusion: 'undecided', reports: [] }
const blankFinding: Finding = {
  location: '',
  trigger: '',
  actual: '',
  expected: '',
  cause: '',
}
const fields = Object.keys(labels) as (keyof Finding)[]
const count = (text: string) => Array.from(text).length

function matchesSaved(value: Value, saved: Value | null) {
  if (typeof value === 'string' || typeof saved === 'string' || !saved)
    return value === saved
  return (
    value.conclusion === saved.conclusion &&
    value.reports.length === saved.reports.length &&
    value.reports.every((row, i) =>
      fields.every((key) => row[key] === saved.reports[i][key]),
    )
  )
}

export function ReportEditor({
  value,
  change,
}: {
  value: Report
  change: (value: Report) => void
}) {
  return (
    <section className="report-editor">
      <label className="report-conclusion">
        审阅结论{' '}
        <select id="report-conclusion"
          value={value.conclusion}
          onChange={(e) => {
            const conclusion = e.target.value as Report['conclusion']
            if (
              conclusion === 'none' &&
              value.reports.length &&
              !window.confirm('选择未发现问题会移除已填写的报告，继续吗？')
            )
              return
            change({
              conclusion,
              reports: conclusion === 'none' ? [] : value.reports,
            })
          }}
        >
          <option value="undecided">未完成判断</option>
          <option value="found">发现问题</option>
          <option value="none">未发现问题</option>
        </select>
      </label>
      {value.reports.map((item, i) => (
        <fieldset key={i} id={`finding-${i+1}`} tabIndex={-1}>
          <legend>报告 {i + 1}</legend>
          <div className="report-fields">{fields.map((k) => (
            <label key={k}>
              {labels[k]}
              <textarea
                aria-label={labels[k]}
                value={item[k]}
                onChange={(e) => {
                  const revised = { ...item, [k]: e.target.value }
                  change({
                    ...value,
                    reports: value.reports.map((v, j) =>
                      j === i ? revised : v,
                    ),
                  })
                }}
              />
            </label>
          ))}</div>
          <div className="report-row-actions"><small>
            {fields.reduce((a, k) => a + count(item[k]), 0)} / 1200 字符
          </small>
          <button
            type="button"
            onClick={() =>
              change({
                ...value,
                reports: value.reports.filter((_, j) => i !== j),
              })
            }
          >
            移除此条
          </button></div>
        </fieldset>
      ))}
      <button className="report-add"
        type="button"
        disabled={value.reports.length >= 5}
        onClick={() =>
          change({
            conclusion: 'found',
            reports: [...value.reports, { ...blankFinding }],
          })
        }
      >
        添加问题报告
      </button>
    </section>
  )
}

// The stage owns one save queue. Each question keeps its own server revision.
export function TinyDbStage({
  state,
  remaining,
  modelReady,
  refresh,
}: {
  state: Attempt
  remaining: number
  modelReady: boolean
  refresh: (updated?: Attempt) => Promise<void>
}) {
  const phase = state.current_phase.id
  const prefix = v2 + '/phases/' + phase
  const explore = !state.capabilities.includes('edit_answers')
  const canEditNotes = state.capabilities.includes('edit_notes')
  const [questionIndex, selectQuestion] = useState(0)
  const readNotes = state.capabilities.includes('read_notes')
  const [loaded, setLoaded] = useState(false)
  const [problems, setProblems] = useState<Problem[]>([])
  const [values, setValues] = useState<Record<string, Value>>({})
  const [error, setError] = useState('')
  const [status, setStatus] = useState('正在读取')
  const [busy, setBusy] = useState(false)
  const [conflict, setConflict] = useState<{
    key: string
    draft: Draft
  } | null>(null)
  const revisions = useRef<Record<string, number>>({})
  const dirty = useRef(new Map<string, Value>())
  const pending = useRef<Promise<void> | null>(null)
  const transitionKey = useRef(crypto.randomUUID())
  const [retry, setRetry] = useState(0)
  const timer = useRef<number | undefined>(undefined)
  useEffect(() => {
    const abort = new AbortController()
    const get = <T,>(suffix: string) =>
      examRequest<T>(prefix + suffix, 'GET', undefined, abort.signal)
    Promise.all([
      explore ? { items: [] } : get<{ items: Problem[] }>('/questions'),
      readNotes ? get<Draft>('/notes') : null,
      explore ? {} : get<Record<string, Draft>>('/answers'),
    ])
      .then(([questions, notes, answers]) => {
        if (abort.signal.aborted) return
        const drafts: Record<string, Draft> = { ...answers }
        if (notes) drafts.notes = notes
        revisions.current = Object.fromEntries(
          Object.entries(drafts).map(([k, d]) => [k, d.revision]),
        )
        setValues(
          Object.fromEntries(
            Object.entries(drafts)
              .filter(([, d]) => d.value !== null)
              .map(([k, d]) => [k, d.value!]),
          ),
        )
        setProblems(questions.items)
        setLoaded(true)
        setStatus('已保存')
        setError('')
      })
      .catch((e) => {
        if (!abort.signal.aborted) setError(String(e))
      })
    return () => {
      abort.abort()
      window.clearTimeout(timer.current)
    }
  }, [prefix, explore, readNotes, retry])
  const flush = useCallback(async () => {
    if (pending.current) return pending.current
    const save = async () => {
      for (const [k, value] of dirty.current) {
        if (
          typeof value === 'string' &&
          count(value) > (k === 'notes' ? 20000 : 1500)
        )
          throw new Error('草稿超过字符上限，请缩短后保存。')
        if (
          typeof value !== 'string' &&
          value.reports.some(
            (r) => fields.reduce((n, f) => n + count(r[f]), 0) > 1200,
          )
        )
          throw new Error('每条问题报告最多1200个字符。')
        const notes = k === 'notes'
        try {
          const result = await examRequest<Draft>(
            prefix + (notes ? '/notes' : '/answers/' + k),
            'PUT',
            {
              expected_revision: revisions.current[k] ?? 0,
              ...(notes ? { text: value } : { value }),
            },
          )
          revisions.current[k] = result.revision
          if (dirty.current.get(k) === value) dirty.current.delete(k)
        } catch (e) {
          if (
            !(e instanceof RequestError) ||
            e.status >= 500 ||
            e.code === 'REVISION_CONFLICT'
          ) {
            const draft = notes
              ? await examRequest<Draft>(prefix + '/notes')
              : (await examRequest<Record<string, Draft>>(prefix + '/answers'))[
                  k
                ]
            if (draft && matchesSaved(value, draft.value)) {
              revisions.current[k] = draft.revision
              if (dirty.current.get(k) === value) dirty.current.delete(k)
              continue
            }
            if (draft && draft.revision > (revisions.current[k] ?? 0))
              setConflict({ key: k, draft })
          }
          throw e
        }
      }
    }
    pending.current = (async () => {
      while (dirty.current.size) await save()
      setStatus('已保存')
      setError('')
    })()
    try {
      await pending.current
    } finally {
      pending.current = null
    }
  }, [prefix])
  function schedule() {
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(
      () =>
        void flush().catch((e) => {
          setError(String(e))
          setStatus('尚未保存')
        }),
      450,
    )
  }
  function update(k: string, v: Value) {
    setValues((old) => ({ ...old, [k]: v }))
    dirty.current.set(k, v)
    setStatus('尚未保存')
    if (!conflict) schedule()
  }
  async function finish() {
    if (
      !window.confirm(
        explore
          ? '结束当前阅读环节，进入独立答题？'
          : '确认提交当前作答？提交后无法返回。',
      )
    )
      return
    setBusy(true)
    try {
      window.clearTimeout(timer.current)
      await flush()
      const latest = await current()
      if (
        latest.current_phase.id === phase &&
        latest.current_phase.status === 'active'
      ) {
        const cmd =
          state.next_action === 'finish_phase'
            ? '/phases/' + phase + '/finish'
            : '/stages/' + state.current_stage.id + '/submit'
        if (state.next_action === 'finish_phase') {
          await refresh(await command<Attempt>(cmd, latest, {}, transitionKey.current))
          return
        }
        await command(cmd, latest, {}, transitionKey.current)
      }
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  const alerts = <>
      {error && (
        <p className="workspace-alert" role="alert">
          {error}{' '}
          {!loaded && (
            <button onClick={() => setRetry(retry + 1)}>重新读取</button>
          )}
          {loaded && status === '尚未保存' && !conflict && (
            <button onClick={schedule}>重试保存</button>
          )}
        </p>
      )}
      {conflict && (
        <section className="workspace-alert" role="alert">
          另一窗口已经保存了新版本。服务器内容：
          <pre>
            {typeof conflict.draft.value === 'string'
              ? conflict.draft.value
              : JSON.stringify(conflict.draft.value, null, 2)}
          </pre>
          <button
            onClick={() => {
              revisions.current[conflict.key] = conflict.draft.revision
              setConflict(null)
              schedule()
            }}
          >
            保留我的草稿并保存
          </button>
          <button
            onClick={() => {
              dirty.current.delete(conflict.key)
              revisions.current[conflict.key] = conflict.draft.revision
              setValues((v) => ({
                ...v,
                [conflict.key]: conflict.draft.value ?? '',
              }))
              setConflict(null)
              schedule()
            }}
          >
            采用服务器版本
          </button>
        </section>
      )}
    </>
  const locked = !loaded || busy || !!conflict
  const q = problems[questionIndex]
  const document = useMemo(() => q ? renderMarkdown(q.body, true) : null, [q])
  if (explore) return <ReadingWorkspace state={state} remaining={remaining} modelReady={modelReady}
    busy={locked} saveState={conflict ? 'conflict' : error ? 'error' : status === '已保存' ? 'saved' : 'idle'}
    processing={busy} saveNote={() => void flush().catch(e => setError(String(e)))}
    discardNote={async () => {
      window.clearTimeout(timer.current)
      try {
        await pending.current?.catch(() => undefined)
        const note = await examRequest<Draft>(prefix + '/notes')
        dirty.current.delete('notes')
        revisions.current.notes = note.revision
        setValues(old => ({ ...old, notes: note.value ?? '' }))
        setConflict(null); setError(''); setStatus('已保存')
        return true
      } catch (e) { setError(String(e)); return false }
    }}
    notes={readNotes && loaded ? String(values.notes ?? '') : undefined} noteRevision={revisions.current.notes}
    editNotes={canEditNotes ? value => update('notes', value) : undefined}
    submitLabel={canEditNotes ? '结束探索，进入独立答题' : '结束阅读，填写报告'} submit={finish} alerts={alerts} />
  const findings = q?.id === 'findings' ? (values.findings as Report | undefined) ?? blankReport : null
  const navigation = findings && problems.length === 1 ? <>
    <h2>审阅报告</h2>
    <a className="report-index-link" href="#report-conclusion">审阅结论</a>
    {findings.reports.map((row, i) => <a key={i} className="report-index-link" href={`#finding-${i+1}`}>
      报告 {i+1}{row.location && <small title={row.location}>{row.location}</small>}
    </a>)}
  </> : undefined
  return <AnswerWorkspace navigation={navigation} state={state} remaining={remaining} busy={locked} submit={finish}
    questions={problems} active={q?.id ?? ''} select={id => selectQuestion(problems.findIndex(p => p.id === id))}
    answered={id => { const v = values[id]; return typeof v === 'string' ? !!v.trim() : !!v && v.conclusion !== 'undecided' }}
    alerts={alerts} initialNotes={readNotes} notes={readNotes ? String(values.notes ?? '') : undefined} files={<ReadonlyFiles phase={phase} initialPath={state.current_stage.key === 'tinydb-review' ? 'repository/tinydb/table.py' : undefined} />}>
    <fieldset disabled={!loaded || busy || remaining <= 0}>{q && (
            <section key={q.id} className="reading-answer">
              <h1>{q.title}</h1>
              {q.id === 'findings' ? <>
                <div className="task-document report-brief" dangerouslySetInnerHTML={renderMarkdown(q.body.trim().split(/\n\s*\n/)[1] || q.body, true)} />
                <details className="report-rules"><summary>查看完整审阅任务和适用规则</summary>{document && <article className="task-document" dangerouslySetInnerHTML={document} />}</details>
              </> : document && <div className="task-document" dangerouslySetInnerHTML={document} />}
              {q.id === 'findings' ? (
                <ReportEditor
                  value={findings ?? blankReport}
                  change={(v) => update(q.id, v)}
                />
              ) : (
                <label>
                  回答
                  <textarea
                    value={String(values[q.id] ?? '')}
                    onChange={(e) => update(q.id, e.target.value)}
                  />
                </label>
              )}
              <div className="answer-save-status"><span>{q.id === 'findings' ? `已添加 ${findings?.reports.length ?? 0} / 5 条报告` : `${count(String(values[q.id] ?? ''))} / 1500`}</span><span role="status">{conflict ? '版本冲突' : status}</span></div>
            </section>
          )}</fieldset>
  </AnswerWorkspace>
}
