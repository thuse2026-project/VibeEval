import { StrictMode, useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import Login from '../admin/login/Login';
import Entry from '../candidate/entry/Entry';
import Overview from '../admin/submissions/Overview';
import Detail from '../admin/submissions/Detail';
import { api, ApiError, type Examiner } from '../shared/api';
import './style.css';

function App() {
  const [user, setUser] = useState<Examiner | null>(null);
  const [loading, setLoading] = useState(true);
  const [connectionError, setConnectionError] = useState('');
  const [notice, setNotice] = useState('');
  const [logoutError, setLogoutError] = useState('');
  const [loggingOut, setLoggingOut] = useState(false);
  const [location, setLocation] = useState(() => window.location.pathname + window.location.search);
  const authRequest = useRef(0);
  const navigate = useCallback((url: string, replace = false) => {
    if (replace) window.history.replaceState(null, '', url);
    else window.history.pushState(null, '', url);
    setLocation(window.location.pathname + window.location.search);
  }, []);
  useEffect(() => {
    const onPop = () => setLocation(window.location.pathname + window.location.search);
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);
  const pathname = location.split('?')[0];
  const search = location.includes('?') ? location.slice(location.indexOf('?')) : '';
  const detailMatch = pathname.match(/^\/admin\/submissions\/([^/]+)$/);
  const detailId = detailMatch?.[1] ?? null;
  const source = new URLSearchParams(search).get('source') === 'demo' ? 'demo' : 'live';
  const expireSession = useCallback(() => {
    authRequest.current++;
    setUser(null);
    setNotice('登录已过期，请重新登录。');
  }, []);

  const checkSession = useCallback(async () => {
    const request = ++authRequest.current;
    setLoading(true);
    setConnectionError('');
    try {
      const examiner = await api<Examiner>('me');
      if (request === authRequest.current) setUser(examiner);
    }
    catch (error) {
      if (request !== authRequest.current) return;
      setUser(null);
      if (!(error instanceof ApiError && error.status === 401)) setConnectionError((error as Error).message);
    } finally { if (request === authRequest.current) setLoading(false); }
  }, []);

  useEffect(() => { void checkSession(); }, [checkSession]);
  useEffect(() => {
    document.title = user ? (detailId ? '答卷详情 · VibeEval' : '管理首页 · VibeEval') : '考官登录 · VibeEval';
    if (loading || connectionError || detailId) return;
    if (user && pathname === '/admin/login') navigate(`/admin${search}`, true);
    else if (!user && pathname !== '/admin/login') navigate(`/admin/login${search}`, true);
  }, [user, loading, connectionError, detailId, pathname, search, navigate]);

  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    const expiresIn = Math.max(0, user.expires_at - user.server_time) * 1000;
    const started = performance.now();
    const expire = expireSession;
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
  }, [user, expireSession]);

  async function logout() {
    setLoggingOut(true);
    setLogoutError('');
    try {
      await api('logout', {}); authRequest.current++; setUser(null);
      setNotice('已安全退出登录。'); navigate('/admin/login', true);
    }
    catch (error) { setLogoutError((error as Error).message); }
    finally { setLoggingOut(false); }
  }

  return <div className="shell">
    <header className="topbar"><a className="brand" href="/admin">VibeEval<span className="brand-separator">/</span><span className="brand-context">考核管理</span></a></header>
    <main className={user ? 'dashboard-main' : undefined}>
      {loading ? <div className="status-panel" role="status">正在确认登录状态…</div>
        : connectionError ? <div className="status-panel"><h1>暂时无法连接服务</h1><p role="alert">{connectionError}</p><button className="primary" onClick={() => void checkSession()}>重新连接</button></div>
        : user ? detailId
          ? <Detail key={`${source}:${detailId}`} submissionId={detailId} source={source} search={search}
            onNavigate={navigate} onExpired={expireSession} onLogout={() => void logout()}
            loggingOut={loggingOut} logoutError={logoutError} />
          : <Overview user={user} onExpired={expireSession} onLogout={() => void logout()}
            loggingOut={loggingOut} logoutError={logoutError} search={search} onNavigate={navigate} />
          : <Login notice={notice} onLogin={value => {
            authRequest.current++; setUser(value); setNotice(''); setLogoutError('');
          }} />}
    </main>
    <footer>VibeEval · 考官管理端</footer>
  </div>;
}

const candidateEntry = window.location.pathname === '/entry' || window.location.pathname === '/entry/';
createRoot(document.getElementById('root')!).render(candidateEntry ? <Entry /> : <StrictMode><App /></StrictMode>);
