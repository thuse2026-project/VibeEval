import { useState, type FormEvent } from 'react';
import { api, type Examiner } from '../../shared/api';

export default function Login({ onLogin, notice }: { onLogin: (user: Examiner) => void; notice: string }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [showPassword, setShowPassword] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (!username.trim() || !password) { setError('请输入账号和密码。'); return; }
    setBusy(true);
    setError('');
    try { onLogin(await api<Examiner>('login', { username: username.trim(), password })); }
    catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  }

  return <section className="login-panel" aria-labelledby="login-title">
    <div className="section-label">考官工作台</div>
    <h1 id="login-title">登录 VibeEval</h1>
    <p className="description">使用预先分配的账号，进入考核管理空间。</p>
    {notice && <p className="notice" role="status">{notice}</p>}
    <form onSubmit={submit}>
      <label htmlFor="username">账号</label>
      <input id="username" name="username" autoComplete="username" required maxLength={64}
        value={username} onChange={event => setUsername(event.target.value)} placeholder="请输入考官账号" disabled={busy} />
      <label htmlFor="password">密码</label>
      <div className="password-field">
        <input id="password" name="password" type={showPassword ? 'text' : 'password'} autoComplete="current-password"
          required maxLength={128} value={password} onChange={event => setPassword(event.target.value)}
          placeholder="请输入密码" disabled={busy} aria-describedby={error ? 'login-error' : undefined} />
        <button type="button" className="password-toggle" onClick={() => setShowPassword(!showPassword)}
          aria-label={showPassword ? '隐藏密码' : '显示密码'} aria-pressed={showPassword}>{showPassword ? '隐藏' : '显示'}</button>
      </div>
      {error && <p id="login-error" className="error" role="alert">{error}</p>}
      <button className="primary" disabled={busy}>{busy ? '正在登录…' : '登录'}</button>
    </form>
    <p className="login-note">登录有效期为 3 小时。没有账号？请联系项目维护者。</p>
  </section>;
}
