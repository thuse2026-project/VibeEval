import { useCallback, useEffect, useRef, useState } from 'react';
import { WorkspaceScreen } from './WorkspaceScreen';
import { GradingScreen } from './GradingScreen';
import { PreparationScreen } from './PreparationScreen';
import { workspaceApi, workspaceRequest, type CandidateSession } from './api';
import { SaveQueue } from './SaveQueue';
import type { FileContentResponse, FileEntry, SubmitResponse } from '../../shared/types';
import './workspace.css';

export default function CandidateWorkspace() {
  const [session, setSession] = useState<CandidateSession | null>(null);
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [rules, setRules] = useState(false);
  const [receipt, setReceipt] = useState<SubmitResponse | null>(null);
  const [clock, setClock] = useState(Date.now());
  const [recovery, setRecovery] = useState<FileContentResponse | null>(null);
  const [reload, setReload] = useState(0);
  const [, render] = useState(0);
  const timers = useRef(new Map<string, number>());
  const submitKey = useRef(crypto.randomUUID());
  const submitting = useRef(false);
  const statusRequest = useRef(0);
  const sid = session?.session_id;
  const sidRef = useRef('');
  const [queue] = useState(() => new SaveQueue(
    (path, version, content) => workspaceApi.save(sidRef.current, path, version, content),
    () => render(value => value + 1),
  ));
  const remaining = session?.deadline_at === null || !session
    ? 0 : Math.max(0, Math.ceil(session.deadline_at - clock / 1000));
  const writable = session?.status === 'in_progress' && remaining > 0 && !busy;

  const refresh = useCallback(async () => {
    const request = ++statusRequest.current;
    try {
      const current = await workspaceApi.status(sidRef.current);
      if (request === statusRequest.current) setSession(current);
      return current;
    } catch (failure) {
      if (request !== statusRequest.current) return null;
      setSession(null);
      setError((failure as Error).message);
      return null;
    }
  }, []);

  useEffect(() => {
    let alive = true;
    void workspaceRequest<CandidateSession>('current')
      .then(current => {
        if (!alive) return;
        sidRef.current = current.session_id;
        setSession(current);
      }).catch(failure => { if (alive) setError((failure as Error).message); });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    if (!sid) return;
    const timer = window.setInterval(() => { if (!submitting.current) void refresh(); }, 5000);
    return () => window.clearInterval(timer);
  }, [sid, refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    const pending = timers.current;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (Object.values(queue.drafts).some(draft => draft.dirty) || submitting.current) event.preventDefault();
    };
    window.addEventListener('beforeunload', beforeUnload);
    return () => {
      window.clearInterval(timer);
      pending.forEach(value => window.clearTimeout(value));
      window.removeEventListener('beforeunload', beforeUnload);
    };
  }, [queue]);

  const select = useCallback(async (path: string) => {
    setActive(path);
    setRecovery(null);
    if (queue.drafts[path]) return;
    try {
      const file = await workspaceApi.read(sidRef.current, path);
      queue.load(path, file.content, file.version, files.find(entry => entry.path === path)?.readonly ?? true);
    } catch (failure) { setError((failure as Error).message); }
  }, [queue, files]);

  useEffect(() => {
    if (!sid || session?.status !== 'in_progress' || files.length) return;
    let alive = true;
    void workspaceApi.files(sid).then(async result => {
      if (!alive) return;
      const first = result.files.find(file => !file.is_dir && !file.readonly);
      if (!first) { setFiles(result.files); return; }
      const file = await workspaceApi.read(sid, first.path);
      if (!alive) return;
      queue.load(file.path, file.content, file.version, first.readonly);
      setActive(file.path);
      setFiles(result.files);
    }).catch(failure => { if (alive) setError((failure as Error).message); });
    return () => { alive = false; };
  }, [sid, session?.status, files.length, queue, reload]);

  async function transition(action: 'prepare' | 'start') {
    if (busy) return;
    setBusy(true);
    statusRequest.current++;
    setError('');
    try { await workspaceApi.transition(sidRef.current, action); }
    catch (failure) { setError((failure as Error).message); }
    finally { await refresh(); setBusy(false); }
  }

  function edit(content: string) {
    if (!active || !writable) return;
    queue.edit(active, content);
    const old = timers.current.get(active);
    if (old) window.clearTimeout(old);
    const path = active;
    timers.current.set(path, window.setTimeout(() => {
      timers.current.delete(path);
      if (!submitting.current) void queue.save(path).catch(() => undefined);
    }, 5000));
  }

  async function submit() {
    if (submitting.current) return;
    submitting.current = true;
    statusRequest.current++;
    setBusy(true);
    timers.current.forEach(value => window.clearTimeout(value));
    timers.current.clear();
    try {
      if (session?.status === 'in_progress' && remaining > 0) await queue.flush();
      const result = await workspaceApi.submit(sidRef.current, submitKey.current);
      setReceipt(result);
    } catch (failure) {
      await refresh();
      throw failure;
    } finally { submitting.current = false; setBusy(false); }
  }

  async function compare() {
    if (!active) return;
    try { setRecovery(await workspaceApi.read(sidRef.current, active)); }
    catch (failure) { setError((failure as Error).message); }
  }

  if (receipt) return <div className="candidate-workspace"><GradingScreen submissionId={receipt.submission_id} snapshotId={receipt.snapshot_id} /></div>;
  if (!session || session.status !== 'in_progress') return <PreparationScreen session={session} busy={busy} rules={rules} error={error}
    onRules={setRules} onTransition={action => void transition(action)} onRefresh={() => void refresh()}
    onReceipt={() => void submit().catch(failure => setError(failure.message))} />;

  const current = active ? queue.drafts[active] : undefined;
  const drafts = Object.values(queue.drafts);
  return <div className="candidate-workspace">
    {error && <div role="alert" className="workspace-alert">{error}<button onClick={() => { setError(''); setReload(value => value + 1); }}>重试读取</button></div>}
    {current?.error && <div role="alert" className="workspace-alert">{current.error} 本地修改已保留。
      <button onClick={() => void compare()}>重新读取并比较</button>
    </div>}
    {recovery && current && recovery.path === active && <div className="workspace-recovery">
      <h2>服务器版本 {recovery.version}（本地内容仍保留在编辑器）</h2><pre>{recovery.content}</pre>
      <button onClick={() => { queue.load(recovery.path, recovery.content, recovery.version, current.readonly); setRecovery(null); }}>使用服务器内容（放弃本地修改）</button>
      <button onClick={() => {
        current.version = recovery.version; current.error = undefined; current.dirty = true;
        setRecovery(null); void queue.save(recovery.path).catch(() => undefined);
      }}>确认用本地内容覆盖此版本</button>
      <button onClick={() => setRecovery(null)}>取消，继续保留本地内容</button>
    </div>}
    <WorkspaceScreen title={session.exam_info.title} remaining={remaining} files={files}
      openFiles={Object.entries(queue.drafts).map(([path, draft]) => ({ path, readonly: draft.readonly, dirty: draft.dirty }))}
      activePath={active} content={current?.content ?? ''} activeReadonly={!writable || !current || current.readonly}
      saveState={queue.saving ? 'saving' : drafts.some(draft => draft.error) ? 'error' : drafts.some(draft => draft.dirty) ? 'idle' : 'saved'}
      sessionId={writable ? sid ?? null : null} onSelectFile={select} onEdit={edit} onSubmit={submit} busy={busy}
    />
  </div>;
}
