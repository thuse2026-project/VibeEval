import { useEffect, useState } from 'react';
import { api, ApiError, type Examiner } from '../../shared/api';

const statuses = {
  invited: '尚未入场', admitted: '已入场，未开始', in_progress: '作答中', submitted: '已交卷',
  invitation_expired: '凭证已过期，未入场', recovery_expired: '恢复期限已过，未交卷',
};
type Status = keyof typeof statuses;
type RecordRow = {
  candidate_id: string; candidate_name: string; exam_id: string; exam_title: string;
  status: Status; session_id: string | null; admitted_at: number | null;
  started_at: number | null; submission_id: string | null; submitted_at: number | null;
};
type OverviewData = {
  source: 'live' | 'demo'; items: RecordRow[]; total: number; page: number; page_size: number;
  summary: { total: number; not_submitted: number; submitted: number };
  exams: { id: string; title: string }[]; updated_at: number; submission_service_connected: boolean;
};
type Props = {
  user: Examiner; onExpired: () => void; onLogout: () => void; loggingOut: boolean; logoutError: string;
};
const formatTime = (value: number) => new Date(value * 1000).toLocaleString('zh-CN', { hour12: false });

export default function Overview({ user, onExpired, onLogout, loggingOut, logoutError }: Props) {
  const [query, setQuery] = useState({ source: 'live', exam: '', status: 'all', page: 1 });
  const [refresh, setRefresh] = useState(0);
  const [result, setResult] = useState<{ key: string; data?: OverviewData; error?: string }>({ key: '' });
  const [examOptions, setExamOptions] = useState<{ source: string; exams: OverviewData['exams'] }>({ source: '', exams: [] });
  const queryKey = JSON.stringify([query, refresh]);
  const loading = result.key !== queryKey;
  const data = loading ? undefined : result.data;
  const error = loading ? '' : result.error;

  useEffect(() => {
    let cancelled = false;
    const parameters = new URLSearchParams({ source: query.source, status: query.status, page: String(query.page) });
    if (query.exam) parameters.set('exam_id', query.exam);
    api<OverviewData>(`overview?${parameters}`).then(result => {
      if (!cancelled) {
        setResult({ key: queryKey, data: result });
        setExamOptions({ source: result.source, exams: result.exams });
      }
    }).catch(failure => {
      if (cancelled) return;
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setResult({ key: queryKey, error: (failure as Error).message });
    });
    return () => { cancelled = true; };
  }, [query, queryKey, onExpired]);

  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;
  const exams = examOptions.source === query.source ? examOptions.exams : [];
  return <section className="overview" aria-labelledby="overview-title">
    <div className="overview-heading">
      <div><div className="section-label">考务总览</div><h1 id="overview-title">你好，{user.name}</h1>
        <p className="description">查看各场次考生的入场和交卷进度。</p>
        <p className="account-meta">账号 {user.username} · 登录有效至 {formatTime(user.expires_at)}</p></div>
      <button className="secondary" disabled={loggingOut} onClick={onLogout}>{loggingOut ? '正在退出…' : '退出登录'}</button>
    </div>
    {logoutError && <p className="error" role="alert">{logoutError}</p>}
    <div className="overview-filters">
      <div><label htmlFor="overview-source">数据来源</label><select id="overview-source" value={query.source}
        onChange={event => setQuery({ source: event.target.value, exam: '', status: 'all', page: 1 })}>
        <option value="live">真实记录</option><option value="demo">联调示例</option></select></div>
      <div><label htmlFor="overview-exam">考核场次</label><select id="overview-exam" value={query.exam}
        onChange={event => setQuery({ ...query, exam: event.target.value, page: 1 })}>
        <option value="">全部场次</option>{exams.map(exam => <option key={exam.id} value={exam.id}>{exam.title}（{exam.id}）</option>)}
        {query.exam && !exams.some(exam => exam.id === query.exam) && <option value={query.exam}>{query.exam}</option>}
      </select></div>
      <div><label htmlFor="overview-status">考生状态</label><select id="overview-status" value={query.status}
        onChange={event => setQuery({ ...query, status: event.target.value, page: 1 })}>
        <option value="all">全部状态</option>{Object.entries(statuses).map(([key, value]) => <option key={key} value={key}>{value}</option>)}
      </select></div>
      <button className="secondary refresh-button" disabled={loading} onClick={() => setRefresh(value => value + 1)}>{loading ? '刷新中…' : '刷新记录'}</button>
    </div>
    {query.source === 'demo'
      ? <p className="demo-banner" role="status"><strong>联调示例</strong> · 下方均为虚构数据，用于检查页面交互，不代表真实考试或提交。</p>
      : <p className="live-note">{data?.submission_service_connected
        ? '当前展示真实考务记录。'
        : '当前已接入真实凭证与入场记录。交卷记录待提交服务接入，已入场不会自动视为作答或交卷。'}</p>}
    <p className="summary-scope">所选场次整体统计，包含全部状态及全部页；每位考生在每个场次计一人次。</p>
    <div className="overview-summary">
      <div><span>考生人次</span><strong>{data?.summary.total ?? '—'}</strong></div>
      <div><span>尚未交卷</span><strong>{data?.summary.not_submitted ?? '—'}</strong></div>
      <div><span>已交卷</span><strong>{data?.summary.submitted ?? '—'}</strong></div>
    </div>
    <div className="records-panel" aria-busy={loading}>
      <div className="records-title"><h2>考生与交卷记录</h2>{data && <span>更新于 {formatTime(data.updated_at)}</span>}</div>
      {loading ? <p className="list-message" role="status">正在加载考生记录…</p>
        : error ? <p className="list-message error" role="alert">{error} 点击“刷新记录”可重试。</p>
        : !data?.items.length ? <div className="list-message"><p>{query.exam || query.status !== 'all' ? '暂无符合筛选条件的考生记录。' : '暂无考生记录，配置入场凭证后将在这里显示。'}</p>
          {(query.exam || query.status !== 'all') && <button className="secondary" onClick={() => setQuery({ ...query, exam: '', status: 'all', page: 1 })}>清除筛选</button>}</div>
        : <div className="table-scroll" role="region" aria-label="考生记录表，可横向滚动" tabIndex={0}>
          <table><caption className="sr-only">{query.source === 'demo' ? '联调示例' : '真实'}考生记录</caption>
            <thead><tr><th scope="col">考生</th><th scope="col">场次</th><th scope="col">状态</th><th scope="col">入场时间</th><th scope="col">提交时间</th></tr></thead>
            <tbody>{data.items.map(row => <tr key={`${row.exam_id}:${row.candidate_id}`}>
              <td><strong>{row.candidate_name}</strong><span className="cell-meta">{row.candidate_id}</span></td>
              <td>{row.exam_title}<span className="cell-meta">{row.exam_id}</span></td>
              <td><span className={`record-status ${row.status}`}>{statuses[row.status]}</span></td>
              <td>{row.admitted_at ? formatTime(row.admitted_at) : '尚未入场'}</td>
              <td>{row.submitted_at ? formatTime(row.submitted_at) : '尚未交卷'}</td>
            </tr>)}</tbody>
          </table></div>}
      {data && <nav className="pagination" aria-label="记录分页"><span>共 {data.total} 条 · 第 {data.page} / {pages} 页</span><div>
        <button className="secondary" disabled={loading || data.page <= 1} onClick={() => setQuery({ ...query, page: data.page - 1 })}>上一页</button>
        <button className="secondary" disabled={loading || data.page >= pages} onClick={() => setQuery({ ...query, page: data.page + 1 })}>下一页</button>
      </div></nav>}
    </div>
  </section>;
}
