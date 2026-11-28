import { useEffect, useState, type FormEvent } from 'react';
import { CandidateFrame } from '../../shared/components/CandidateFrame';
import { api, ApiError } from '../../shared/api';
import { statusLabels, formatTime } from '../../admin/submissions/types';

type EntrySession = {
  session_id: string;
  status: keyof typeof statusLabels;
  candidate: { id: string; name: string };
  exam_info: { id: string; title: string; duration_minutes: number };
  admitted_at: number;
  started_at: number | null;
  deadline_at: number | null;
};

function entryRequest(path: string, body?: unknown) {
  const run = () => api<EntrySession>(path, body, '/api/v1/sessions');
  // Serialize first-cookie initialization and admission across tabs of this origin.
  return navigator.locks ? navigator.locks.request('vibeeval-entry', run) : run();
}

export default function Entry() {
  const workspace = new URLSearchParams(window.location.search).get('mode') === 'r2' ? '/r2-workspace' : '/workspace';
  const [session, setSession] = useState<EntrySession | null>(null);
  const [token, setToken] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [connectionError, setConnectionError] = useState('');

  async function restore() {
    setLoading(true);
    setConnectionError('');
    try { setSession(await entryRequest('current')); }
    catch (failure) {
      if (failure instanceof ApiError && failure.status === 401) setSession(null);
      else setConnectionError((failure as Error).message);
    } finally { setLoading(false); }
  }

  useEffect(() => { document.title = '考生入场 · VibeEval'; void restore(); }, []);

  async function admit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (!token.trim()) { setError('请输入入场凭证。'); return; }
    setBusy(true);
    setError('');
    try { setSession(await entryRequest('verify-token', { token: token.trim() })); setToken(''); }
    catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  }

  return <CandidateFrame entryHref={workspace === '/r2-workspace' ? '/entry?mode=r2' : '/entry'}>
    {loading ? <p role="status">正在检查并恢复入场会话…</p>
      : connectionError ? <section className="entry-form"><h1>无法恢复会话</h1>
        <p className="flow-error" role="alert">{connectionError}</p><button className="flow-primary" onClick={() => void restore()}>重新检查</button>
      </section> : session ? <section className="entry-form" aria-labelledby="entry-success">
        <h1 id="entry-success">已入场，{session.candidate.name}</h1>
        <p className="entry-state" role="status">{statusLabels[session.status]}</p>
        <dl className="entry-facts">
          <div><dt>考核名称</dt><dd>{session.exam_info.title}</dd></div>
          <div><dt>考生编号</dt><dd>{session.candidate.id}</dd></div>
          <div><dt>作答时长</dt><dd>{session.exam_info.duration_minutes} 分钟</dd></div>
          <div><dt>会话编号</dt><dd>{session.session_id}</dd></div>
          <div><dt>考试计时</dt><dd>{session.started_at === null ? '尚未开始计时' : `开始于 ${formatTime(session.started_at)}`}</dd></div>
          {session.deadline_at !== null && <div><dt>截止时间</dt><dd>{formatTime(session.deadline_at)}</dd></div>}
        </dl>
        <div className="flow-actions"><a className="flow-primary" href={workspace}>进入工作台 / 恢复答卷</a>
          <button className="flow-link" onClick={() => void restore()}>检查并恢复会话</button></div>
        <p className="flow-note">用当前浏览器重新打开即可恢复原会话，计时会继续。</p>
      </section> : <section className="entry-form" aria-labelledby="entry-title">
        <h1 id="entry-title">考生入场</h1>
        <p className="flow-intro">使用考官分配的凭证进入考试工作台。</p>
        <form onSubmit={admit}>
          <label htmlFor="entry-token">入场凭证</label>
          <input id="entry-token" name="token" type="password" autoComplete="off" spellCheck={false} required maxLength={256}
            placeholder="粘贴以 tok_ 开头的凭证" value={token} disabled={busy}
            onChange={event => setToken(event.target.value)} aria-describedby="entry-help" aria-invalid={Boolean(error)} />
          {error && <p className="flow-error" role="alert">{error}</p>}
          <button className="flow-primary" disabled={busy}>{busy ? '正在校验…' : '校验并入场'}</button>
        </form>
        <aside id="entry-help" className="entry-help"><h2>关于会话恢复</h2>
          <p>凭证只需在首次入场时使用。请保留当前浏览器的本站数据。如需更换设备，或已清除数据，请联系考官。</p></aside>
      </section>}
  </CandidateFrame>;
}
