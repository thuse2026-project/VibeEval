import { useEffect, useRef, useState } from 'react';
import { ConfirmScreen } from '../candidate/entry/ConfirmScreen';
import { ProvisioningScreen } from '../candidate/entry/ProvisioningScreen';
import { WorkspaceScreen } from '../candidate/workspace/WorkspaceScreen';
import { GradingScreen } from '../candidate/workspace/GradingScreen';
import type { FileEntry, SaveState, SubmitResponse } from '../shared/types';
import { candidateRequest as request, CandidateError, serverNow } from '../shared/api/client';
import './styles.css';

type Session = {session_id:string; status:string; started_at:number|null; deadline_at:number|null;
  candidate:{id:string;name:string}; exam_info:{title:string;duration_minutes:number}};
const testCommand = 'python -m unittest discover -s tests -p "test_*.py" -v\r';

export default function CandidateApp() {
  const [session,setSession] = useState<Session|null>(null);
  const [token,setToken] = useState('');
  const [booting,setBooting] = useState(true);
  const [phase,setPhase] = useState('entry');
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState('');
  const [files,setFiles] = useState<FileEntry[]>([]);
  const metadata = useRef<FileEntry[]>([]);
  const drafts = useRef(new Map<string,string>());
  const dirty = useRef(new Set<string>());
  const [path,setPath] = useState('');
  const [content,setContent] = useState('');
  const [saveState,setSaveState] = useState<SaveState>('saved');
  const chain = useRef<Promise<void>>(Promise.resolve());
  const timer = useRef<ReturnType<typeof setTimeout>|null>(null);
  const locked = useRef(false);
  const [now,setNow] = useState(serverNow());
  const [receipt,setReceipt] = useState<SubmitResponse|null>(null);
  const [command,setCommand] = useState<{id:number;text:string}|null>(null);
  const [connected,setConnected] = useState(false);
  const key = useRef(crypto.randomUUID());
  const sid = session?.session_id;
  const remaining = session?.deadline_at ? Math.max(0,Math.ceil((session.deadline_at*1000-now)/1000)) : 0;
  const current = () => request<Session>('/sessions/current');
  async function getReceipt(value:Session) {
    const result = await request<SubmitResponse>(`/sessions/${value.session_id}/submit`,{method:'POST',headers:{'Idempotency-Key':key.current}});
    setReceipt(result); setPhase('submitted');
  }
  async function openWorkspace(value:Session) {
    const result = await request<{files:FileEntry[]}>(`/sessions/${value.session_id}/files`);
    metadata.current = result.files; setFiles(result.files);
    const first = result.files.find(f=>!f.is_dir&&!f.readonly) ?? result.files.find(f=>!f.is_dir);
    if (first) {
      const file = await request<{content:string;version:number}>(`/sessions/${value.session_id}/file?path=${encodeURIComponent(first.path)}`);
      if (!dirty.current.has(first.path)) drafts.current.set(first.path,file.content);
      first.version=file.version; setPath(first.path); setContent(drafts.current.get(first.path)!);
    }
    setPhase('workspace');
  }
  async function accept(value:Session) {
    setSession(value);
    if (value.status==='submitted') await getReceipt(value);
    else if (value.status==='in_progress') await openWorkspace(value);
    else if (['submitting','submission_failed'].includes(value.status)) setPhase('frozen');
    else setPhase(value.status==='admitted'?'confirm':'prepare');
  }
  async function restore() {
    setBooting(true); setError('');
    try {await accept(await current());}
    catch(e) {if(e instanceof CandidateError&&e.status===401) {setSession(null);setPhase('entry');} else setError((e as Error).message);}
    finally {setBooting(false);}
  }
  const initialRestore=useRef(restore);
  useEffect(()=>{document.title='考生工作台 · VibeEval';void initialRestore.current();},[]);
  useEffect(()=>{const id=setInterval(()=>setNow(serverNow()),1000);return()=>clearInterval(id);},[]);
  useEffect(()=>{
    if(!sid) return;
    let cancelled=false;
    const id=setInterval(()=>{void current().then(value=>{
      if(cancelled) return;
      setSession(value);
      if(value.status==='submitted'&&!receipt) void getReceipt(value).catch(e=>setError(e.message));
      else if(['submitting','submission_failed'].includes(value.status)) setPhase('frozen');
    }).catch(e=>{if(!cancelled){setError(e.message);if(e instanceof CandidateError&&[401,404,410].includes(e.status)){setPhase('frozen');setSession(value=>value?{...value,status:'unauthorized'}:value);}}});},5000);
    return()=>{cancelled=true;clearInterval(id);};
  },[sid,receipt]);
  useEffect(()=>{
    const warn=(event:BeforeUnloadEvent)=>{if(dirty.current.size)event.preventDefault();};
    window.addEventListener('beforeunload',warn);
    return()=>{window.removeEventListener('beforeunload',warn);if(timer.current)clearTimeout(timer.current);};
  },[]);
  async function act(operation:()=>Promise<void>) {
    if(locked.current)return;
    locked.current=true;setBusy(true);setError('');
    try {await operation();}catch(e){setError((e as Error).message);}finally{locked.current=false;setBusy(false);}
  }
  function flush():Promise<void> {
    if(timer.current)clearTimeout(timer.current);
    const save=async()=>{
      if(!sid||!dirty.current.size)return;
      setSaveState('saving');
      try {
        for(const name of [...dirty.current]) {
          const text=drafts.current.get(name)!;
          const file=metadata.current.find(f=>f.path===name)!;
          const result=await request<{new_version:number}>(`/sessions/${sid}/file`,{method:'PUT',body:JSON.stringify({path:name,content:text,expected_version:file.version})});
          file.version=result.new_version;
          if(drafts.current.get(name)===text)dirty.current.delete(name);
        }
        setSaveState(dirty.current.size?'idle':'saved');
      }catch(e){setSaveState(e instanceof CandidateError&&e.code==='FILE_VERSION_CONFLICT'?'conflict':'error');throw e;}
    };
    const next=chain.current.catch(()=>{}).then(save);chain.current=next;return next;
  }
  function edit(text:string) {
    if(locked.current||remaining<=0||session?.status!=='in_progress'||files.find(f=>f.path===path)?.readonly)return;
    drafts.current.set(path,text);dirty.current.add(path);setContent(text);setSaveState('idle');
    if(timer.current)clearTimeout(timer.current);
    timer.current=setTimeout(()=>{void flush().catch(e=>setError(e.message));},5000);
  }
  async function selectFile(name:string) {
    await act(async()=>{
      await flush();
      const result=await request<{content:string;version:number}>(`/sessions/${sid}/file?path=${encodeURIComponent(name)}`);
      drafts.current.set(name,result.content);
      const file=metadata.current.find(f=>f.path===name);if(file)file.version=result.version;
      setPath(name);setContent(result.content);
    });
  }
  async function prepare() {await act(async()=>{
    setPhase('prepare');await request(`/sessions/${sid}/prepare`,{method:'POST'});setSession(await current());
  });}
  async function start() {await act(async()=>{
    await request(`/sessions/${sid}/start`,{method:'POST'});const value=await current();setSession(value);await openWorkspace(value);
  });}
  async function submit() {
    if(!window.confirm('确认交卷？提交后不能继续修改。'))return;
    await act(async()=>{await flush();if(dirty.current.size)throw new Error('仍有未保存修改，请先保存。');if(session)await getReceipt(session);});
  }
  return <div className="candidate-app" aria-busy={busy}>
    {phase!=='workspace'&&dirty.current.size>0&&<section className="candidate-error" aria-label="未保存草稿">
      <h2>以下本地草稿尚未确认保存，不属于已提交答卷</h2><p>考试结束或会话失效后不能补写答卷，但可复制草稿留存。</p>
      {[...dirty.current].map(name=><label key={name}>{name}<textarea readOnly aria-label={`未保存草稿 ${name}`} value={drafts.current.get(name)??''}/></label>)}
    </section>}
    {error&&<div className="candidate-error" role="alert">{error}<button disabled={busy||dirty.current.size>0} onClick={()=>void restore()}>重新检查状态</button>
      {dirty.current.size>0&&<><button disabled={busy} onClick={()=>void act(flush)}>重试保存</button><button disabled={busy} onClick={()=>{
        if(window.confirm('放弃当前未保存修改，重新读取服务器文件？')){dirty.current.clear();drafts.current.clear();setSaveState('saved');void restore();}
      }}>放弃本地修改并重读</button></>}
    </div>}
    {booting?<p>正在恢复会话…</p>:!session?<main className="confirm-body"><h1>考生入场</h1>
      <form onSubmit={e=>{e.preventDefault();void act(async()=>{
        await current().catch(e=>{if(!(e instanceof CandidateError&&e.status===401))throw e;});
        const value=await request<Session>('/sessions/verify-token',{method:'POST',body:JSON.stringify({token:token.trim()})});setToken('');await accept(value);
      });}}><label>入场凭证<input aria-label="入场凭证" type="password" autoComplete="off" value={token} onChange={e=>setToken(e.target.value)} required maxLength={256}/></label>
        <button disabled={busy}>{busy?'正在验证…':'验证并入场'}</button></form></main>
      :phase==='confirm'?<ConfirmScreen examInfo={session.exam_info} error={null} onConfirm={()=>void prepare()}/>
      :phase==='prepare'?<ProvisioningScreen steps={[
        {id:'verify',title:'会话校验',description:'已确认当前考生身份',state:'done'},
        {id:'create',title:'准备独立环境',description:session.status==='ready'?'环境已就绪，尚未计时':'等待真实沙箱准备',state:session.status==='ready'?'done':busy?'doing':'wait'},
      ]} error={null} ready={!busy&&session.status==='ready'} onRetry={()=>void prepare()} onEnter={()=>void start()}/>
      :phase==='workspace'?<WorkspaceScreen title={session.exam_info.title} remaining={remaining} files={files} activePath={path} content={content}
        activeReadonly={busy||remaining<=0||session.status!=='in_progress'||!!files.find(f=>f.path===path)?.readonly} saveState={saveState} busy={busy}
        terminalConnected={connected} sessionId={sid!} command={command} onTerminalConnection={setConnected}
        terminalEnabled={remaining>0&&session.status==='in_progress'} onSelectFile={name=>void selectFile(name)} onEdit={edit}
        onRunTests={()=>void act(async()=>{await flush();if(!connected)throw new Error('请先连接终端。');setCommand({id:Date.now(),text:testCommand});})} onSubmit={()=>void submit()}/>
      :phase==='submitted'&&receipt?<GradingScreen submissionId={receipt.submission_id} snapshotId={receipt.snapshot_id}/>
      :<main className="confirm-body"><h1>答卷已禁止继续修改</h1><p>当前状态：{session.status}</p>
        <button disabled={busy} onClick={()=>void act(async()=>{if(session)await getReceipt(session);})}>重试交卷／获取回执</button></main>}
  </div>;
}
