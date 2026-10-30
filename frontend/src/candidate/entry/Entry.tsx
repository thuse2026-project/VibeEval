import { useEffect, useState, type FormEvent } from 'react';
import { api, ApiError } from '../../shared/api';

type EntrySession = {
  session_id: string;
  status: 'admitted';
  candidate: { id: string; name: string };
  exam_info: { id: string; title: string; duration_minutes: number };
  admitted_at: number;
  started_at: null;
  deadline_at: null;
};

function entryRequest(path: string, body?: unknown) {
  const run = () => api<EntrySession>(path, body, '/api/v1/sessions');
  // Serialize first-cookie initialization and admission across tabs of this origin.
  return navigator.locks ? navigator.locks.request('vibeeval-entry', run) : run();
}

export default function Entry() {
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

  return <div className="shell">
    <header className="topbar"><a className="brand" href="/entry">VibeEval<span className="brand-separator">/</span><span className="brand-context">考生入场</span></a></header>
    <main>{loading ? <div className="status-panel" role="status">正在检查并恢复入场会话…</div>
      : connectionError ? <section className="status-panel"><h1>无法恢复会话</h1><p className="error" role="alert">{connectionError}</p><button className="primary" onClick={() => void restore()}>重新检查</button></section>
      : session ? <section className="home-panel" aria-labelledby="entry-success">
        <div className="section-label">入场确认</div>
        <h1 id="entry-success">已入场，{session.candidate.name}</h1>
        <p className="notice" role="status">尚未开始计时</p>
        <dl>
          <div><dt>考核名称</dt><dd>{session.exam_info.title}</dd></div>
          <div><dt>考生编号</dt><dd>{session.candidate.id}</dd></div>
          <div><dt>作答时长</dt><dd>{session.exam_info.duration_minutes} 分钟</dd></div>
          <div><dt>会话编号</dt><dd>{session.session_id}</dd></div>
        </dl>
        <p className="login-note">请保留当前浏览器的本站数据。刷新或重新打开本页可恢复原会话，无需再次输入凭证。</p>
        <p className="login-note">当前仅验证入场与恢复。环境准备和“开始作答”将在后续接入，尚未启动沙箱或考试计时。</p>
        <button className="secondary" onClick={() => void restore()}>检查并恢复会话</button>
      </section> : <section className="login-panel" aria-labelledby="entry-title">
        <div className="section-label">考核接入</div>
        <h1 id="entry-title">考生入场</h1>
        <p className="description">请输入考官分配给你的入场凭证。</p>
        <form onSubmit={admit}>
          <label htmlFor="entry-token">入场凭证</label>
          <input id="entry-token" name="token" type="password" autoComplete="off" spellCheck={false} required maxLength={256}
            placeholder="粘贴以 tok_ 开头的凭证" value={token} disabled={busy}
            onChange={event => setToken(event.target.value)} aria-describedby="entry-help" />
          {error && <p className="error" role="alert">{error}</p>}
          <button className="primary" disabled={busy}>{busy ? '正在校验…' : '校验并入场'}</button>
        </form>
        <p className="login-note" id="entry-help">凭证只用于首次入场。请使用准备作答的浏览器；换设备或清除本站数据后，请联系考官。</p>
      </section>}</main>
    <footer>VibeEval · 入场验证</footer>
  </div>;
}
