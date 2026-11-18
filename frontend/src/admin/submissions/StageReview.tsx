import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../../shared/api';
import { formatTime } from './types';

type Row = { submission_id: string; submitted_at: number; close_reason: string; title: string; candidate_id: string; candidate_name: string; exam_title: string };
type Criterion = { key: string; name: string; max: number };
type Objective = { status: string; score: number | null; cap: number | null; groups: (Criterion & { passed: boolean; score: number })[]; output: string; message?: string };
type Review = { revision: number; scores: Record<string, number>; note: string; reviewer: string; reviewed_at: number };
type Detail = Row & { task_version: string; rule_version: string; summary: { value: unknown } | null; files: { path: string; content: string }[]; changes: { path: string; diff: string; truncated: boolean }[]; objective: Objective | null; last_attempt: Objective | null; review: Review | null; total_score: number | null; criteria: Criterion[]; manual_criteria: Criterion[] };
type List = { items: Row[]; total: number; page: number; page_size: number };
const base = '/api/v1/admin/r2';

export default function StageReview({ submissionId, search, onNavigate, onExpired }: { submissionId: string | null; search: string; onNavigate: (path: string) => void; onExpired: () => void }) {
  const page = Math.max(1, Number(new URLSearchParams(search).get('page')) || 1);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [list, setList] = useState<List | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [scores, setScores] = useState<Record<string, string>>({});
  const [note, setNote] = useState('');
  const [file, setFile] = useState('');
  const generation = useRef(0);
  const draftOwner = useRef<string | null>(null);
  const draftRevision = useRef(0);
  const endpoint = submissionId ? `submissions/${encodeURIComponent(submissionId)}` : `submissions?page=${page}`;

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

  async function action(kind: 'evaluate' | 'review') {
    if (!detail || busy) return;
    const current = generation.current;
    setBusy(true); setError('');
    try {
      const body = kind === 'evaluate' ? {} : { expected_revision: draftRevision.current, scores: Object.fromEntries(detail.manual_criteria.map(c => [c.key, Number(scores[c.key])])), note };
      await api(`${endpoint}/${kind}`, body, base, 120000);
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
  return <section className="r2-review">
    <div className="records-title"><div><p className="eyebrow">考官评阅 · 多阶段答卷</p><h1>{detail ? detail.title : '多阶段已交卷答卷'}</h1></div>
      <button className="secondary" onClick={() => onNavigate(submissionId ? '/admin/r2' : '/admin')}>{submissionId ? '返回多阶段列表' : '返回一期管理'}</button>
    </div>
    {error && <p role="alert" className="error">{error} <button className="secondary" onClick={() => setRefresh(value => value + 1)}>重新读取</button></p>}
    {!list && !detail && !error && <p role="status">正在读取固定答卷…</p>}
    {list && <>
      <p>共 {list.total} 份阶段答卷；每道题独立评阅。</p>
      <div className="r2-review-table"><table><thead><tr><th>考生</th><th>题目 / 场次</th><th>交卷时间</th><th>评阅</th></tr></thead><tbody>
        {list.items.map(row => <tr key={row.submission_id}><td>{row.candidate_name}<small>{row.candidate_id}</small></td><td>{row.title}<small>{row.exam_title}</small></td><td>{formatTime(row.submitted_at)}<small>{row.close_reason === 'manual' ? '手动交卷' : '到时交卷'}</small></td><td><button className="secondary" onClick={() => onNavigate(`/admin/r2/submissions/${encodeURIComponent(row.submission_id)}`)}>查看与评分</button></td></tr>)}
      </tbody></table></div>
      {list.total === 0 && <p>暂无已交卷的多阶段代码答卷。</p>}
      <div className="r2-review-actions"><button className="secondary" disabled={page <= 1} onClick={() => onNavigate(`/admin/r2?page=${page - 1}`)}>上一页</button><span>第 {page} 页</span><button className="secondary" disabled={page * list.page_size >= list.total} onClick={() => onNavigate(`/admin/r2?page=${page + 1}`)}>下一页</button></div>
    </>}
    {detail && <>
      <p>{detail.candidate_name} · {detail.candidate_id} · {detail.exam_title}</p>
      <div className="score-grid"><p><span>客观功能与回归</span><strong>{objective ? `${objective.score} / 70` : '未评 / 70'}</strong></p><p><span>考官评价</span><strong>{detail.review ? `${Object.values(detail.review.scores).reduce((a, b) => a + b, 0)} / 30` : '未评 / 30'}</strong></p><p><span>本题总分</span><strong>{detail.total_score === null ? '尚未评完' : `${detail.total_score} / 100`}</strong></p></div>
      <p className="output-note">客观分由平台对固定交卷代码执行可信行为测试。考生终端的输出和自写测试通过数量不直接换算成客观分；未评不等于零分。</p>
      {objective?.cap !== null && objective?.cap !== undefined && <p className="error">原始缺陷仍可复现，本题综合总分上限为 {objective.cap} 分。</p>}
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
      <section className="evaluation-panel"><div className="records-title"><h2>考官评价 · 30 分</h2></div><div className="evaluation-body">
        <p>结合自写测试、实际修改和实现说明评分。尚未评分的项目保持空白。</p>
        <div className="r2-review-fields">{detail.manual_criteria.map(c => <label key={c.key}>{c.name}（满分 {c.max}）<input disabled={busy} type="number" min="0" max={c.max} step="0.5" value={scores[c.key] ?? ''} onChange={event => setScores(value => ({ ...value, [c.key]: event.target.value }))} /></label>)}</div>
        <label>评分依据<textarea aria-label="评分依据" disabled={busy} rows={4} value={note} onChange={event => setNote(event.target.value)} placeholder="说明测试覆盖了什么、是否有效，以及实现或解释的优缺点。" /></label>
        <button className="primary" disabled={busy || !note.trim() || detail.manual_criteria.some(c => !scores[c.key]?.trim())} onClick={() => void action('review')}>{busy ? '正在保存…' : '保存考官评分'}</button>
        <button className="secondary" disabled={busy} onClick={() => { draftOwner.current = null; setRefresh(value => value + 1); }}>载入已保存评分（替换草稿）</button>
        {detail.review && <p className="output-note">评分人：{detail.review.reviewer} · {formatTime(detail.review.reviewed_at)} · 修订 {detail.review.revision}</p>}
      </div></section>
      <p className="output-note">题目：{detail.task_version} · 规则：{detail.rule_version} · 回执：{detail.submission_id}</p>
    </>}
  </section>;
}
