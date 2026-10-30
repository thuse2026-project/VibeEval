import { StrictMode, useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import Login from '../admin/login/Login';
import { api, ApiError, type Examiner } from '../shared/api';
import './style.css';

function App() {
  const [user, setUser] = useState<Examiner | null>(null);
  const [loading, setLoading] = useState(true);
  const [connectionError, setConnectionError] = useState('');
  const [notice, setNotice] = useState('');
  const [logoutError, setLogoutError] = useState('');
  const [loggingOut, setLoggingOut] = useState(false);

  const checkSession = useCallback(async () => {
    setLoading(true);
    setConnectionError('');
    try { setUser(await api<Examiner>('me')); }
    catch (error) {
      setUser(null);
      if (!(error instanceof ApiError && error.status === 401)) setConnectionError((error as Error).message);
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { void checkSession(); }, [checkSession]);
  useEffect(() => {
    document.title = user ? '管理首页 · VibeEval' : '考官登录 · VibeEval';
    const target = user ? '/admin' : '/admin/login';
    if (!loading && !connectionError) window.history.replaceState(null, '', target);
  }, [user, loading, connectionError]);

  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    const expiresIn = Math.max(0, user.expires_at - user.server_time) * 1000;
    const started = performance.now();
    function expire() { setUser(null); setNotice('登录已过期，请重新登录。'); }
    const timer = window.setTimeout(expire, expiresIn);
    async function onFocus() {
      if (performance.now() - started >= expiresIn) { expire(); return; }
      try {
        const refreshed = await api<Examiner>('me');
        if (!cancelled) setUser(refreshed);
      }
      catch (error) {
        if (!cancelled && error instanceof ApiError && error.status === 401) expire();
      }
    }
    window.addEventListener('focus', onFocus);
    return () => { cancelled = true; window.clearTimeout(timer); window.removeEventListener('focus', onFocus); };
  }, [user]);

  async function logout() {
    setLoggingOut(true);
    setLogoutError('');
    try { await api('logout', {}); setUser(null); setNotice('已安全退出登录。'); }
    catch (error) { setLogoutError((error as Error).message); }
    finally { setLoggingOut(false); }
  }

  return <div className="shell">
    <header className="topbar"><a className="brand" href="/admin">VibeEval<span className="brand-separator">/</span><span className="brand-context">考核管理</span></a></header>
    <main>
      {loading ? <div className="status-panel" role="status">正在确认登录状态…</div>
        : connectionError ? <div className="status-panel"><h1>暂时无法连接服务</h1><p role="alert">{connectionError}</p><button className="primary" onClick={() => void checkSession()}>重新连接</button></div>
        : user ? <section className="home-panel" aria-labelledby="home-title">
          <div className="section-label">管理首页</div>
          <h1 id="home-title">你好，{user.name}</h1>
          <p className="description">你已登录考官工作台。</p>
          <dl><div><dt>当前账号</dt><dd>{user.username}</dd></div><div><dt>本次登录到期</dt><dd>{new Date(user.expires_at * 1000).toLocaleString('zh-CN', { hour12: false })}</dd></div></dl>
          <p className="login-note">刷新页面不会延长登录有效期。</p>
          {logoutError && <p className="error" role="alert">{logoutError}</p>}
          <button className="secondary" disabled={loggingOut} onClick={() => void logout()}>{loggingOut ? '正在退出…' : '退出登录'}</button>
        </section> : <Login notice={notice} onLogin={value => { setUser(value); setNotice(''); }} />}
    </main>
    <footer>VibeEval · 考官管理端</footer>
  </div>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
