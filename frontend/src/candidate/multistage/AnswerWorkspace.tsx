import { AppearanceSettings } from '../../shared/components/CandidateAppearance'
import { PaneResize } from '../../shared/components/PaneResize'
import type { CSSProperties } from 'react'
import { useState, type ReactNode } from 'react'
import { renderMarkdown } from '../../shared/markdown'
import { stageTitle, type Attempt } from './api'

type Props = {
  state: Attempt; remaining: number; busy: boolean; submit: () => Promise<void>
  questions: { id: string; title: string }[]; active: string; select: (id: string) => void
  answered: (id: string) => boolean; navigation?: ReactNode; alerts?: ReactNode; children: ReactNode; files: ReactNode; notes?: string | null; initialNotes?: boolean; evidence?: ReactNode
}

export function AnswerWorkspace({ state, remaining, busy, submit, questions, active, select,
  answered, navigation, alerts, children, files, notes, initialNotes, evidence }: Props) {
  const [source, setSource] = useState<'files' | 'notes' | 'evidence'>(initialNotes ? 'notes' : 'files')
  const [referenceWidth, setReferenceWidth] = useState(32)
  const [sending, setSending] = useState(false)
  const index = questions.findIndex(q => q.id === active)
  return <div className="answer-studio">
    <header className="answer-bar"><strong><i aria-hidden="true">&lt;/&gt;</i> VibeEval</strong>
      <span className="answer-stage">{stageTitle(state)} · {state.current_phase.title}</span>
      <time aria-label="当前环节剩余时间">{sending ? '正在提交…' : `剩余 ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}`}</time>
      <AppearanceSettings />
      <button disabled={sending || busy || remaining <= 0} onClick={async () => { setSending(true); try { await submit() } finally { setSending(false) } }}>{state.next_action === 'submit_exam' ? '提交整场考试' : '提交本题'}</button>
    </header>
    {alerts}
    <div className="answer-layout" style={{ '--reference-width': referenceWidth + '%' } as CSSProperties}>
      <nav className="answer-index" aria-label="问题导航">{navigation ?? <><h2>本题作答</h2>
        <p>{questions.filter(q => answered(q.id)).length} / {questions.length} 已填写</p>
        {questions.map((q,i) => <button key={q.id} aria-current={q.id === active ? 'page' : undefined}
          onClick={() => select(q.id)}><b>{String(i+1).padStart(2,'0')}</b><span>{q.title}</span>
          <small aria-label={answered(q.id) ? '已填写' : '未填写'}>{answered(q.id) ? '●' : '○'}</small></button>)}
      </>}</nav>
      <section className="answer-sheet"><div className="answer-paper">
        {children}
        {questions.length > 1 && <footer><button disabled={index <= 0} onClick={() => select(questions[index-1].id)}>上一问</button>
          <button disabled={index < 0 || index+1 >= questions.length} onClick={() => select(questions[index+1].id)}>下一问</button></footer>}
      </div></section>
      <PaneResize label="调整参考材料宽度" min={25} max={52} value={referenceWidth} change={setReferenceWidth} />
      <aside className="answer-reference" aria-label="参考材料">
        {(notes != null || evidence) && <div className="reference-tabs">
          {notes != null && <button aria-pressed={source==='notes'} onClick={() => setSource('notes')}>笔记</button>}
          <button aria-pressed={source==='files'} onClick={() => setSource('files')}>{state.capabilities.includes('read_frozen_code') ? '冻结代码' : '只读源码'}</button>
          {evidence && <button aria-pressed={source==='evidence'} onClick={() => setSource('evidence')}>操作记录</button>}
        </div>}
        {source === 'notes' && <article className="reference-markdown" tabIndex={0} dangerouslySetInnerHTML={renderMarkdown(notes || '笔记为空', true)} />}
        <div hidden={source !== 'files'}>{files}</div>
        {evidence && <div hidden={source !== 'evidence'}>{evidence}</div>}
      </aside>
    </div>
  </div>
}
