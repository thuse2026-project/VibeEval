import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../../shared/api';
import { formatTime } from './types';

type Row = { definition?:string; submission_id: string; submitted_at: number; close_reason: string; title: string; candidate_id: string; candidate_name: string; exam_title: string };
type Criterion = { key: string; name: string; max: number; category?: string; full?: string; partial?: string; zero?: string };
type Objective = { status: string; score: number | null; cap: number | null; groups: (Criterion & { passed: boolean; score: number })[]; output: string; message?: string };
type Review = { revision: number; scores: Record<string, number>; note: string; reviewer: string; reviewed_at: number; evidence?: Record<string, string>; novel_tests_confirmed?: boolean };
type TestOutcome = { status: string; message: string; truncated?: boolean };
type TestEvidence = { status: string; notice: string; test_sha256: string; observed_at: number; versions: Record<string, { status: string; tests?: unknown[]; output?: string; message?: string; output_truncated?: boolean }>; rows: { id: string; original: TestOutcome | null; submitted: TestOutcome | null; possible_public_copy: boolean }[] };
type Detail = Row & { task_version: string; rule_version: string; summary: { value: unknown } | null; files: { path: string; content: string }[]; changes: { path: string; diff: string; truncated: boolean }[]; objective: Objective | null; last_attempt: Objective | null; review: Review | null; total_score: number | null; criteria: Criterion[]; manual_criteria: Criterion[]; review_evidence_required?: boolean; policies?: string[]; candidate_tests?: TestEvidence | null; candidate_tests_last_attempt?: TestEvidence | null; previous_scores?: { rule_sha256: string; objective: Objective | null; review: Review | null }[] };
type List = { items: Row[]; total: number; page: number; page_size: number };
const base = '/api/v1/admin/r2';
const outcomeNames: Record<string, string> = { passed: '通过', failed: '断言失败', error: '测试运行错误', skipped: '已跳过', expected_failure: '预期失败（非通过）', unexpected_success: '意外通过（需核查）', completed: '运行完成', infrastructure_failed: '平台未完成', timed_out: '运行超时', unconfirmed: '运行结果待核查', truncated: '测试条目已截断（证据不完整，不据此扣分）' };

export default function StageReview({ submissionId, search, onNavigate, onExpired }: { submissionId: string | null; search: string; onNavigate: (path: string) => void; onExpired: () => void }) {
  const page = Math.max(1, Number(new URLSearchParams(search).get('page')) || 1);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [list, setList] = useState<List | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [scores, setScores] = useState<Record<string, string>>({});
  const [note, setNote] = useState('');
  const [evidence, setEvidence] = useState<Record<string, string>>({});
  const [novelConfirmed, setNovelConfirmed] = useState(false);
  const [file, setFile] = useState('');
  const generation = useRef(0);
  const draftOwner = useRef<string | null>(null);
  const draftRevision = useRef(0);
  const endpoint = submissionId ? `submissions/${encodeURIComponent(submissionId)}` : `submissions?kind=all&page=${page}`;

  useEffect(() => {
    const current = ++generation.current;
    setDetail(null); setList(null); setError(''); setBusy(false);
    api<Detail | List>(endpoint, undefined, base).then(result => {
      if (generation.current !== current) return;
      if (submissionId) {
        const value = result as Detail;
        setDetail(value);
        if (draftOwner.current !== submissionId) {
          setNote(value.review?.note ?? '');
          setEvidence(value.review?.evidence ?? {});
          setNovelConfirmed(value.review?.novel_tests_confirmed ?? false);
          setScores(Object.fromEntries(value.manual_criteria.map(c => [c.key, value.review?.scores[c.key]?.toString() ?? ''])));
          draftRevision.current = value.review?.revision ?? 0;
          draftOwner.current = submissionId;
        }
        setFile(current => value.files.some(f => f.path === current) ? current : value.changes[0]?.path ?? value.files[0]?.path ?? '');
      } else setList(result as List);
    }).catch(value => {
      if (generation.current !== current) return;
      if (value instanceof ApiError && value.status === 401) onExpired();
      else setError((value as Error).message);
    });
    return () => { generation.current = current + 1; };
  }, [endpoint, onExpired, refresh, submissionId]);

  async function action(kind: 'evaluate' | 'review' | 'candidate-tests') {
    if (!detail || busy) return;
    const current = generation.current;
    setBusy(true); setError('');
    try {
      const body = kind !== 'review' ? {} : { expected_revision: draftRevision.current, scores: Object.fromEntries(detail.manual_criteria.map(c => [c.key, Number(scores[c.key])])), note, evidence, novel_tests_confirmed: novelConfirmed };
      await api(`${endpoint}/${kind}`, body, base, kind === 'candidate-tests' ? 180000 : 120000);
      if (generation.current === current) {
        if (kind === 'review') draftOwner.current = null;
        setRefresh(value => value + 1);
      }
    } catch (value) {
      if (generation.current !== current) return;
      if (value instanceof ApiError && value.status === 401) onExpired();
      else setError((value as Error).message);
    } finally { if (generation.current === current) setBusy(false); }
  }

  const objective = detail?.objective?.status === 'completed' ? detail.objective : null;
  const observation = detail?.candidate_tests ?? detail?.candidate_tests_last_attempt;
  const needsNovel = detail?.manual_criteria.some(c => c.category === 'tests' && Number(scores[c.key]) > 0);
  return <section className="r2-review">
    {!submissionId && <a href="/admin/r2/rooms">考试房间与入场凭证</a>}
    <div className="records-title"><div><p className="eyebrow">考官评阅 · 多阶段答卷</p><h1>{detail ? detail.title : '多阶段已交卷答卷'}</h1></div>
      <button className="secondary" onClick={() => onNavigate(submissionId ? '/admin/r2' : '/admin')}>{submissionId ? '返回多阶段列表' : '返回一期管理'}</button>
    </div>
    {error && <p role="alert" className="error">{error} <button className="secondary" onClick={() => setRefresh(value => value + 1)}>重新读取</button></p>}
    {!list && !detail && !error && <p role="status">正在读取固定答卷…</p>}
    {list && <>
      <p>共 {list.total} 份阶段答卷；每道题独立评阅。</p>
      <div className="r2-review-table"><table><thead><tr><th>考生</th><th>题目 / 场次</th><th>交卷时间</th><th>评阅</th></tr></thead><tbody>
        {list.items.map(row => <tr key={row.submission_id}><td>{row.candidate_name}<small>{row.candidate_id}</small></td><td>{row.title}<small>{row.exam_title}</small></td><td>{formatTime(row.submitted_at)}<small>{row.close_reason === 'manual' ? '手动交卷' : '到时交卷'}</small></td><td><button className="secondary" onClick={() => onNavigate(`/admin/r2/${row.definition === "r2-four-stage" ? "four" : "submissions"}/${encodeURIComponent(row.submission_id)}`)}>查看与评分</button></td></tr>)}
      </tbody></table></div>
      {list.total === 0 && <p>暂无已交卷的多阶段答卷。</p>}
      <div className="r2-review-actions"><button className="secondary" disabled={page <= 1} onClick={() => onNavigate(`/admin/r2/submissions?page=${page - 1}`)}>上一页</button><span>第 {page} 页</span><button className="secondary" disabled={page * list.page_size >= list.total} onClick={() => onNavigate(`/admin/r2/submissions?page=${page + 1}`)}>下一页</button></div>
    </>}
    {detail && <>
      <p>{detail.candidate_name} · {detail.candidate_id} · {detail.exam_title}</p>
      <details className="r2-score-policy"><summary>评分规则与给分边界</summary><ul>{detail.policies?.map(text => <li key={text}>{text}</li>)}</ul></details>
      <div className="score-grid"><p><span>客观功能与回归</span><strong>{objective ? `${objective.score} / 70` : '未评 / 70'}</strong></p><p><span>考官评价</span><strong>{detail.review ? `${Object.values(detail.review.scores).reduce((a, b) => a + b, 0)} / 30` : '未评 / 30'}</strong></p><p><span>本题总分</span><strong>{detail.total_score === null ? '尚未评完' : `${detail.total_score} / 100`}</strong></p></div>
      <p className="output-note">客观分由平台对固定交卷代码执行可信行为测试。考生终端的输出和自写测试通过数量不直接换算成客观分；未评不等于零分。</p>
      {objective?.cap !== null && objective?.cap !== undefined && <p className="error">原始缺陷仍可复现，本题综合总分上限为 {objective.cap} 分。</p>}
      {objective?.cap != null && detail.review && <p>封顶前合计：{(objective.score ?? 0) + Object.values(detail.review.scores).reduce((a, b) => a + b, 0)} 分；按规则最终为 {detail.total_score} 分。</p>}
      <section className="evaluation-panel"><div className="records-title"><h2>客观评测 · 70 分</h2><button className="secondary" disabled={busy} onClick={() => void action('evaluate')}>{busy ? '正在处理…' : objective ? '重新评测固定快照' : '开始客观评测'}</button></div>
        <div className="evaluation-body"><ul className="test-results">{detail.criteria.map(c => { const result = objective?.groups.find(g => g.key === c.key); return <li key={c.key}>{result ? result.passed ? '通过' : '未通过' : '未评'} · {c.name}<span>{result ? result.score : '—'} / {c.max} 分</span></li>; })}</ul>
          {detail.last_attempt?.status === 'infrastructure_failed' && <p className="error">最近一次评测因平台问题未完成；不会计零分，有既有成功结果时继续保留。</p>}
          {objective?.output && <details><summary>查看平台评测记录</summary><pre tabIndex={0}>{objective.output}</pre></details>}
        </div></section>
      <section className="evaluation-panel"><div className="records-title"><h2>作答与代码依据</h2></div><div className="evaluation-body">
        <h3>考生说明</h3><p className="r2-review-summary">{typeof detail.summary?.value === 'string' && detail.summary.value.trim() ? detail.summary.value : '未提供实现说明。'}</p>
        <h3>改动文件</h3>{detail.changes.length ? detail.changes.map(change => <details key={change.path}><summary>{change.path}</summary>{change.diff && <pre tabIndex={0}>{change.diff}</pre>}{change.truncated && <p>差异较长，请在下方核对完整交卷文件。</p>}</details>) : <p>与原题一致。</p>}
        <h3>固定代码快照</h3><label>查看答卷文件<select value={file} onChange={event => setFile(event.target.value)}>{detail.files.map(value => <option key={value.path} value={value.path}>{value.path}</option>)}</select></label>
        <pre tabIndex={0} aria-label="固定答卷文件内容">{detail.files.find(value => value.path === file)?.content ?? ''}</pre>
      </div></section>
      <section className="evaluation-panel"><div className="records-title"><h2>新增测试：双版本运行证据</h2><button className="secondary" disabled={busy} onClick={() => void action('candidate-tests')}>{busy ? '正在处理…' : '运行新增测试对照'}</button></div><div className="evaluation-body">
        <p>把同一份交卷测试分别放到原始代码和交卷代码上运行；只运行 tests/test_candidate.py，不包含公开测试。结果仅辅助评分，不按通过数量给分。</p>
        <p>原始失败 → 交卷通过：可能抓住了已修复问题；两边失败：可能仍有代码缺陷，也可能测试错误，须核对断言；两边通过：可能是正常行为或边界检查，不能直接判无效。</p>
        {detail.candidate_tests_last_attempt?.status === 'incomplete' && <p className="error">最近一次对照未完整完成，不扣分；已有完整证据继续保留。</p>}
        {detail.candidate_tests_last_attempt?.status === 'incomplete' && <p>最新原始代码：{outcomeNames[detail.candidate_tests_last_attempt.versions.original.status] ?? detail.candidate_tests_last_attempt.versions.original.status}；最新交卷代码：{outcomeNames[detail.candidate_tests_last_attempt.versions.submitted.status] ?? detail.candidate_tests_last_attempt.versions.submitted.status}</p>}
        {!observation ? <p>尚未运行对照；新增测试分仍待评。</p> : <>
          <p className="output-note">{observation.notice} · {formatTime(observation.observed_at)}</p>
          <p>原始代码：{outcomeNames[observation.versions.original.status] ?? observation.versions.original.status}；交卷代码：{outcomeNames[observation.versions.submitted.status] ?? observation.versions.submitted.status}</p>
          {observation.rows.length === 0 && <p>{observation.status === 'completed' ? '两次均未发现可执行的新增测试。' : '运行未完成，暂无法逐条对照。'}</p>}
          <div className="r2-review-table"><table><thead><tr><th>新增测试</th><th>原始代码</th><th>交卷代码</th></tr></thead><tbody>{observation.rows.map(row => <tr key={row.id}><td>{row.id}{row.possible_public_copy && <small>疑似重复公开测试，须核对夹具及新增验证；复制部分不得新增测试分。</small>}</td>{(['original', 'submitted'] as const).map(version => <td key={version}>{row[version] ? outcomeNames[row[version].status] ?? row[version].status : '未观察到同名用例'}{row[version]?.message && <details><summary>查看原因</summary><pre>{row[version].message}</pre>{row[version].truncated && <small>原因过长，已截断；请结合测试代码核查。</small>}</details>}</td>)}</tr>)}</tbody></table></div>
          {(['original', 'submitted'] as const).map(version => <details key={version}><summary>{version === 'original' ? '原始代码' : '交卷代码'}运行输出</summary><pre>{observation.versions[version].message || observation.versions[version].output || '无额外输出。'}</pre>{observation.versions[version].output_truncated && <p>输出已截断，不代表全部运行记录。</p>}</details>)}
        </>}
      </div></section>
      <section className="evaluation-panel"><div className="records-title"><h2>考官评价 · 30 分</h2></div><div className="evaluation-body">
        <p>按小项独立给分。解释可来自提交说明、代码或测试注释；只扣对应缺失项，不重复扣分。正确发现尚未修好问题的测试仍可得分。</p>
        <div className="r2-review-fields">{detail.manual_criteria.map(c => <div key={c.key} className="r2-rubric-item"><label>{c.name}（满分 {c.max}）<input disabled={busy} type="number" min="0" max={c.max} step="0.5" value={scores[c.key] ?? ''} onChange={event => setScores(value => ({ ...value, [c.key]: event.target.value }))} /></label>
          {c.full && <ul className="r2-rubric-anchors"><li>满分：{c.full}</li><li>部分分：{c.partial}</li><li>零分：{c.zero}</li></ul>}
          {detail.review_evidence_required && <label>本项依据：{c.name}<textarea aria-label={`本项依据：${c.name}`} disabled={busy} maxLength={2000} rows={2} value={evidence[c.key] ?? ''} onChange={event => setEvidence(values => ({ ...values, [c.key]: event.target.value }))} placeholder="引用文件、测试名、注释或说明，并解释得分；零分也说明缺失内容。" /></label>}
        </div>)}</div>
        {detail.review_evidence_required && <label className="r2-novel-confirm"><input type="checkbox" disabled={busy} checked={novelConfirmed} onChange={event => setNovelConfirmed(event.target.checked)} />已确认测试项得分对应真正新增的验证，未给复制公开测试或只改名部分计分。</label>}
        <label>评分依据<textarea aria-label="评分依据" disabled={busy} rows={4} value={note} onChange={event => setNote(event.target.value)} placeholder="说明测试覆盖了什么、是否有效，以及实现或解释的优缺点。" /></label>
        <button className="primary" disabled={busy || !note.trim() || detail.manual_criteria.some(c => !scores[c.key]?.trim()) || (detail.review_evidence_required && (detail.manual_criteria.some(c => !evidence[c.key]?.trim()) || (!!needsNovel && !novelConfirmed)))} onClick={() => void action('review')}>{busy ? '正在保存…' : '保存考官评分'}</button>
        <button className="secondary" disabled={busy} onClick={() => { draftOwner.current = null; setRefresh(value => value + 1); }}>载入已保存评分（替换草稿）</button>
        {detail.review && <p className="output-note">评分人：{detail.review.reviewer} · {formatTime(detail.review.reviewed_at)} · 修订 {detail.review.revision}</p>}
      </div></section>
      {!!detail.previous_scores?.length && <details className="r2-score-policy"><summary>旧规则评分（只读，不套用新细则）</summary>{detail.previous_scores.map(prior => <div key={prior.rule_sha256}><p>规则摘要：{prior.rule_sha256}</p><p>旧客观分：{prior.objective?.score ?? '未评'} / 70；旧考官分：{prior.review ? Object.values(prior.review.scores).reduce((a, b) => a + b, 0) : '未评'} / 30</p>{prior.review && <p>{prior.review.note} · {prior.review.reviewer} · {formatTime(prior.review.reviewed_at)}</p>}</div>)}</details>}
      <p className="output-note">题目：{detail.task_version} · 规则：{detail.rule_version} · 回执：{detail.submission_id}</p>
    </>}
  </section>;
}
