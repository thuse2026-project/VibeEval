import { useCallback, useEffect, useRef, useState } from 'react';
import { WorkspaceScreen } from './WorkspaceScreen';
import { GradingScreen } from './GradingScreen';
import { PreparationScreen } from './PreparationScreen';
import { workspaceApi, workspaceRequest, WorkspaceError, type CandidateSession } from './api';
import {serverNow} from '../../shared/clock';
import { SaveQueue } from './SaveQueue';
import type { AgentFileChange } from './AgentPanelV2';
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
  const [clock, setClock] = useState(serverNow());
  const [editorRevision,setEditorRevision]=useState(0);
  const activeRef=useRef<string|null>(null);
  const [recovery, setRecovery] = useState<FileContentResponse | null>(null);
  const [reload, setReload] = useState(0);
  const [agentConflicts, setAgentConflicts] = useState<string[]>([]);
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
      if(failure instanceof WorkspaceError&&[401,404,410].includes(failure.status))setSession(null);
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
    const timer = window.setInterval(() => setClock(serverNow()), 1000);
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
    activeRef.current=path;
    setActive(path);
    setRecovery(null);
    const cached=queue.drafts[path];
    if(cached?.dirty||cached?.error)return;
    try {
      const file = await workspaceApi.read(sidRef.current, path);
      const latest=queue.drafts[path];
      if(latest?.dirty||latest?.error||(latest&&latest.version>file.version))return;
      queue.load(path, file.content, file.version, files.find(entry => entry.path === path)?.readonly ?? true);
      if(activeRef.current===path)setEditorRevision(value=>value+1);
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
      activeRef.current=file.path;
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

  function edit(path:string,content: string) {
    if (!writable||submitting.current) return;
    queue.edit(path, content);
    const old = timers.current.get(path);
    if (old) window.clearTimeout(old);
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

  async function resolve(useLocal:boolean) {
    if(!recovery||!writable||busy)return;
    const draft=queue.drafts[recovery.path];
    if(!draft||draft.readonly)return;
    if(!window.confirm(useLocal?'按刚读取的服务器版本保存本地修改？如果服务器再变化会拒绝覆盖。':'放弃此文件本地修改，使用服务器内容？'))return;
    setBusy(true);
    try {
      await queue.settle();
      if(useLocal){draft.version=recovery.version;draft.error=undefined;draft.dirty=true;await queue.save(recovery.path);}
      else {
        const fresh=await workspaceApi.read(sidRef.current,recovery.path);
        if(fresh.version!==recovery.version||fresh.content!==recovery.content){setRecovery(fresh);throw new Error('服务器内容已再次变化，请核对后重新选择。');}
        queue.load(fresh.path,fresh.content,fresh.version,draft.readonly);setEditorRevision(value=>value+1);
      }
      setRecovery(null);setError('');
      setAgentConflicts(paths => paths.filter(path => path !== recovery.path));
    }catch(failure){setError((failure as Error).message);}
    finally{setBusy(false);}
  }

  const handleAgentWorkspaceChange = useCallback(async (change: AgentFileChange) => {
    const draft = queue.drafts[change.path];
    if (change.version !== null && draft && draft.version >= change.version) return;
    try {
      const file = await workspaceApi.read(sidRef.current, change.path);
      const latest = queue.drafts[change.path];
      if (latest?.dirty || latest?.error) {
        setAgentConflicts(paths => paths.includes(change.path) ? paths : [...paths, change.path]);
        if (activeRef.current === change.path) setRecovery(file);
        return;
      }
      const readonly = latest?.readonly ?? files.find(entry => entry.path === change.path)?.readonly ?? true;
      queue.load(change.path, file.content, file.version, readonly);
      setFiles(items => items.map(item => item.path === change.path ? { ...item, version: file.version } : item));
      setAgentConflicts(paths => paths.filter(path => path !== change.path));
      if (activeRef.current === change.path) setEditorRevision(value => value + 1);
    } catch (failure) {
      setError((failure as Error).message);
    }
  }, [files, queue]);

  useEffect(()=>{
    if(!sid||session?.status!=='submitted'||receipt)return;
    let cancelled=false;
    void workspaceApi.submit(sid,submitKey.current).then(value=>{if(!cancelled)setReceipt(value);})
      .catch(failure=>{if(!cancelled)setError(failure.message);});
    return()=>{cancelled=true;};
  },[sid,session?.status,receipt]);

  const unsaved=Object.entries(queue.drafts).filter(([,draft])=>draft.dirty);
  const unsavedView=unsaved.length>0&&<section className="workspace-alert" aria-label="未保存草稿"><h2>以下本地草稿尚未确认保存，不属于已提交答卷</h2>
    {unsaved.map(([path,draft])=><label key={path}>{path}<textarea readOnly aria-label={`未保存草稿 ${path}`} value={draft.content}/></label>)}</section>;

  if (receipt) return <div className="candidate-workspace">{unsavedView}<GradingScreen submissionId={receipt.submission_id} snapshotId={receipt.snapshot_id} /></div>;
  if (!session || session.status !== 'in_progress') return <div className="candidate-workspace">{unsavedView}<PreparationScreen session={session} busy={busy} rules={rules} error={error}
    onRules={setRules} onTransition={action => void transition(action)} onRefresh={() => void refresh()}
    onReceipt={() => void submit().catch(failure => setError(failure.message))} /></div>;

  const current = active ? queue.drafts[active] : undefined;
  const drafts = Object.values(queue.drafts);
  return <div className="candidate-workspace workspace-active">
    {error && <div role="alert" className="workspace-alert">{error}<button onClick={() => { setError(''); if(active)void select(active);else setReload(value => value + 1); }}>重试读取</button></div>}
    {current?.error && <div role="alert" className="workspace-alert">{current.error} 本地修改已保留。
      <button disabled={!writable} onClick={() => void compare()}>重新读取并比较</button>
    </div>}
    {agentConflicts.length > 0 && <div role="alert" className="workspace-alert">
      Agent 已修改 {agentConflicts.join('、')}；你的未保存草稿仍保留在编辑器中，请比较后选择版本。
      {active && agentConflicts.includes(active) && <button disabled={!writable} onClick={() => void compare()}>查看服务器版本</button>}
    </div>}
    {recovery && current && recovery.path === active && <div className="workspace-recovery">
      <h2>服务器版本 {recovery.version}（本地内容仍保留在编辑器）</h2><pre>{recovery.content}</pre>
      <button disabled={!writable} onClick={()=>void resolve(false)}>使用服务器内容（放弃本地修改）</button>
      <button disabled={!writable} onClick={()=>void resolve(true)}>确认用本地内容覆盖此版本</button>
      <button onClick={() => setRecovery(null)}>取消，继续保留本地内容</button>
    </div>}
    <WorkspaceScreen title={session.exam_info.title} remaining={remaining} files={files}
      editorRevision={editorRevision}
      openFiles={Object.entries(queue.drafts).map(([path, draft]) => ({ path, readonly: draft.readonly, dirty: draft.dirty }))}
      activePath={active} content={current?.content ?? ''} activeReadonly={!writable || !current || current.readonly}
      saveState={queue.saving ? 'saving' : drafts.some(draft => draft.error) ? 'error' : drafts.some(draft => draft.dirty) ? 'idle' : 'saved'}
      sessionId={session.status==='in_progress'&&remaining>0&&files.length>0 ? sid ?? null : null} onSelectFile={select} onEdit={edit}
      onAgentWorkspaceChange={handleAgentWorkspaceChange} onSubmit={submit} busy={busy}
    />
  </div>;
}
