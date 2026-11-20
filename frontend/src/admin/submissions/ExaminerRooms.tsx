import { useEffect, useState } from 'react';
import { api, ApiError } from '../../shared/api';

const request = <T,>(path: string, body?: unknown) => api<T>(path, body, '/api/v1/admin/r2');
const rooms = 'rooms';
const states: Record<string, string> = { invited: '待入场', expired: '已过期', admitted: '已入场', in_progress: '考试中', completed: '已完成' };
const time = (seconds: number) => new Date(seconds * 1000).toLocaleString('zh-CN');
type Room = { id: string; candidate_name: string; exam_title: string; exam_status: string; admission_expires_at: number; token_available: boolean; stage_count: number };
type Stage = { id: string; title: string; receipt: { id: string } | null; decision: Decision | null };
type Quote = { objective: number; original_subjective: number; subjective: number; raw: number; cap: number | null; final: number };
type Decision = { score: Quote; delta: number; reason: string; examiner: string; at: number; action: string };
type Listing = { items: Room[]; next_cursor: string | null; meta: { current_template: { title: string; version: string; can_create: boolean } | null } };
type Detail = { title: string; receipt: { id: string; kind: string }; submission: { summary?: { value: string }; phases: { questions?: { id: string; title: string; body: string }[]; answers: Record<string, { value: unknown }> }[] }; grading: { state: string; message?: string; revision: string; maximum?: number; subjective?: number; objective?: { score: number; groups: { key: string; name?: string; score: number; max: number }[] }; manual_criteria?: { key: string; name?: string; max: number }[]; review?: { scores: Record<string, number>; evidence?: Record<string, string> }; changes?: { path: string; diff: string; truncated: boolean }[] }; decision: Decision | null };
type Event = { id: string; actor: string; kind: string; text?: string; occurred_at: number; conversation_id: string; status?: string; approval?: string; path?: string; diff_id?: string; exit_code?: number; tool?: string };
const eventLabel = (event: Event) => {
  if (event.kind === 'approval') return event.status === 'approved' ? '批准执行' : event.status === 'rejected' ? '拒绝执行' : '审批结束';
  if (event.kind === 'tool') return event.tool ?? '工具操作';
  if (event.kind === 'file_edit') return event.status === 'succeeded' ? '已写入' : '文件操作';
  return '命令';
};
type Timeline = { items: Event[]; next_cursor: number | null; conversations: { id: string; title: string }[] };
type Diff = { diff: string; truncated: boolean; next_cursor: number | null };

export default function ExaminerRooms({ roomId, onNavigate, onExpired }: { roomId: string | null; onNavigate: (p: string) => void; onExpired: () => void }) {
  const [list, setList] = useState<Listing | null>(null);
  const [room, setRoom] = useState<(Room & { stages: Stage[] }) | null>(null);
  const [stage, setStage] = useState('');
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState('');
  const [filter, setFilter] = useState('');
  const [query, setQuery] = useState('');
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [expiry, setExpiry] = useState('');
  const [createKey, setCreateKey] = useState('');
  const [credential, setCredential] = useState<{ id: string; token: string; entry_url: string } | null>(null);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [conversation, setConversation] = useState('');
  const [diffs, setDiffs] = useState<Record<string, Diff>>({});
  const [delta, setDelta] = useState('0');
  const [reason, setReason] = useState('');
  const [preview, setPreview] = useState<{ body: { score_revision: string; delta: number; reason: string; action: string; request_key: string }; score: Quote } | null>(null);
  const [reload, refresh] = useState(0);
  const endpoint = `${rooms}/${roomId}/stages/${stage}`;

  function failed(value: unknown) {
    if (value instanceof ApiError && value.status === 401) onExpired();
    else setError(value instanceof Error ? value.message : '请求失败');
  }
  async function run(action: () => Promise<void>) {
    setBusy(true); setError('');
    try { await action(); } catch (value) { failed(value); }
    finally { setBusy(false); }
  }

  useEffect(() => {
    let active = true;
    const path = roomId ? `${rooms}/${roomId}` : `${rooms}?${new URLSearchParams({ status: filter, q: query })}`;
    void request<Listing | (Room & { stages: Stage[] })>(path).then(value => {
      if (!active) return;
      if ('stages' in value) {
        setRoom(value);
        setStage(previous => value.exam_status !== 'completed' ? '' : value.stages.some(item => item.id === previous) ? previous : value.stages[0]?.id ?? '');
      } else setList(value);
    }).catch(value => {
      if (!active) return;
      if (value instanceof ApiError && value.status === 401) onExpired();
      else setError((value as Error).message);
    });
    return () => { active = false; };
  }, [roomId, filter, query, reload, onExpired]);

  useEffect(() => {
    if (!roomId || !stage) return;
    let active = true;
    void request<Detail>(endpoint).then(value => {
      if (active) {
        setDetail(value);
        setPreview(previous => value.decision || previous?.body.score_revision !== value.grading.revision ? null : previous);
      }
    }).catch(value => {
      if (!active) return;
      if (value instanceof ApiError && value.status === 401) onExpired();
      else setError((value as Error).message);
    });
    return () => { active = false; };
  }, [roomId, stage, endpoint, reload, onExpired]);

  async function loadTimeline(cursor = 0, selected = conversation) {
    await run(async () => {
      const data = await request<Timeline>(`${endpoint}/timeline?${new URLSearchParams({ cursor: String(cursor), conversation_id: selected })}`);
      setTimeline(previous => ({ ...data, items: cursor ? [...(previous?.items ?? []), ...data.items] : data.items }));
    });
  }

  async function loadDiff(event: Event, cursor = 0) {
    await run(async () => {
      const diff = await request<Diff>(`${endpoint}/diffs/${event.diff_id}?cursor=${cursor}`);
      setDiffs(old => ({ ...old, [event.id]: { ...diff, diff: cursor ? (old[event.id]?.diff ?? '') + diff.diff : diff.diff } }));
    });
  }

  return <section className="status-panel examiner-rooms">
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    {roomId ? <>
      <button className="secondary" onClick={() => onNavigate('/admin/r2')}>返回考试房间</button>
      <button className="secondary" onClick={() => { setError(''); refresh(v => v + 1); }}>刷新结果</button>
      <h1>{room?.candidate_name ?? '考试结果'}</h1>
      {room && <p>{room.exam_title} · {states[room.exam_status]}</p>}
      <nav className="r2-review-actions" aria-label="题目">{room?.stages.map((item, i) =>
        <button key={item.id} disabled={busy || !item.receipt || room.exam_status !== 'completed'} aria-pressed={item.id === stage}
          onClick={() => { if (stage !== item.id) { setDetail(null); setTimeline(null); setConversation(''); setPreview(null); setDelta('0'); setReason(''); setStage(item.id); } }}>
          {i + 1}. {item.title}{item.decision ? ` · ${item.decision.score.final} 分 · 已锁定` : ''}
        </button>)}</nav>
      {detail && <>
        <h2>{detail.title}</h2>
        {detail.submission.phases.flatMap(phase => (phase.questions ?? []).map(question => <details key={question.id}>
          <summary>{question.title}</summary><pre>{question.body}</pre>
          {phase.answers[question.id] && <pre>{typeof phase.answers[question.id].value === 'string' ? String(phase.answers[question.id].value) : JSON.stringify(phase.answers[question.id].value, null, 2)}</pre>}
        </details>))}
        {detail.submission.summary?.value && <p>提交说明：{detail.submission.summary.value}</p>}
        <h3>评分</h3>
        {detail.grading.objective && <ul>{detail.grading.objective.groups.map(part => <li key={part.key}>{part.name ?? part.key} · {part.score} / {part.max}</li>)}</ul>}
        {detail.grading.review && <ul>{detail.grading.manual_criteria?.map(part => <li key={part.key}>{part.name ?? part.key} · {detail.grading.review?.scores[part.key]} / {part.max}{detail.grading.review?.evidence?.[part.key] && <p>{detail.grading.review.evidence[part.key]}</p>}</li>)}</ul>}
        {detail.grading.state !== 'scored' && <p>{detail.grading.message ?? '评分尚未齐全'}</p>}
        {detail.receipt.kind === 'code' && <a href={`/admin/r2/submissions/${detail.receipt.id}`}>查看评测与评分来源</a>}
        {detail.grading.changes?.map(change => <details key={change.path}><summary>最终差异 · {change.path}</summary><pre>{change.diff}</pre>{change.truncated && <p>差异已截断</p>}</details>)}
        <h3>作答过程</h3>
        {!timeline ? <button disabled={busy} onClick={() => void loadTimeline()}>加载作答过程</button> :
          <><label>对话 <select disabled={busy} value={conversation} onChange={event => { setConversation(event.target.value); void loadTimeline(0, event.target.value); }}>
            <option value="">全部对话</option>{timeline.conversations.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}
          </select></label>
          {!timeline.items.length && <p>没有可回放的 Agent 记录。</p>}
          <ol>{timeline.items.map(event => <li key={event.id}><strong>{event.actor === 'candidate' ? '考生' : 'Agent'} · {time(event.occurred_at)}</strong>
            {event.text ? <p className="examiner-preserve">{event.text}</p> : <p>{eventLabel(event)} · {event.status}{event.approval && ` · ${event.approval}`}{event.exit_code != null && ` · 退出码 ${event.exit_code}`}</p>}
            {event.diff_id && <details onToggle={e => { if (e.currentTarget.open && !diffs[event.id]) void loadDiff(event); }}>
              <summary>查看差异 · {event.path}</summary><pre>{diffs[event.id]?.diff ?? '正在读取差异…'}</pre>
              {diffs[event.id]?.truncated && <p>仅存历史预览</p>}
              {diffs[event.id]?.next_cursor != null && <button disabled={busy} onClick={() => void loadDiff(event, diffs[event.id].next_cursor!)}>加载下一段差异</button>}
            </details>}
          </li>)}</ol>
          {timeline.next_cursor !== null && <button disabled={busy} onClick={() => void loadTimeline(timeline.next_cursor!)}>加载更多</button>}</>}
        <h3>考官复核</h3>
        {detail.decision ? <p>{detail.decision.action === 'confirm' ? '已确认原分' : '已调分'}并锁定 · {detail.decision.score.final} 分 · {detail.decision.examiner} · {time(detail.decision.at)}{detail.decision.reason && ` · ${detail.decision.reason}`}</p> :
          detail.grading.state === 'scored' && <form onSubmit={event => { event.preventDefault(); void run(async () => {
            const number = Number(delta);
            if (number !== 0 && !reason.trim()) throw new Error('调分需要填写理由');
            const payload = { score_revision: detail.grading.revision, delta: number };
            const score = await request<Quote>(`${endpoint}/review-preview`, payload);
            setPreview({ body: { ...payload, action: number ? 'adjust' : 'confirm', reason: reason.trim(), request_key: crypto.randomUUID() }, score });
          }); }}>
            <p>基础主观分 {detail.grading.subjective} / {detail.grading.maximum}。调整 0 分表示确认原分。</p>
            <label>主观调整分 <input required type="number" min={-10} max={10} value={delta} disabled={busy || !!preview} onChange={e => setDelta(e.target.value)} /></label>
            <label>调分理由 <input value={reason} disabled={busy || !!preview} maxLength={2000} onChange={e => setReason(e.target.value)} /></label>
            <button disabled={busy || !!preview}>预览确认</button>
          </form>}
        {preview && !detail.decision && <section><p>封顶前 {preview.score.raw} 分 · 最终 {preview.score.final} 分{preview.score.cap != null && ` · 上限 ${preview.score.cap} 分`}。确认后锁定。</p>
          <button disabled={busy} onClick={() => setPreview(null)}>返回修改</button>
          <button disabled={busy} onClick={() => void run(async () => {
            try {
              const decision = await request<Decision>(`${endpoint}/review`, preview.body);
              setDetail(old => old && { ...old, decision }); setPreview(null);
            } finally { refresh(v => v + 1); }
          })}>确认锁定</button>
        </section>}
      </>}
    </> : <>
      <h1>考试房间</h1>
      <div className="r2-review-actions">
        <input aria-label="搜索考生" placeholder="搜索考生姓名" value={query} onChange={e => { setQuery(e.target.value); setCredential(null); }} />
        <select aria-label="考试状态" value={filter} onChange={e => { setFilter(e.target.value); setCredential(null); }}><option value="">全部状态</option>{Object.entries(states).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
        <button className="primary" disabled={busy || !list?.meta.current_template?.can_create} onClick={() => { setCreating(true); setCreateKey(crypto.randomUUID()); }}>创建房间</button>
        <a href="/admin/r2/submissions">历史答卷与评分</a>
      </div>
      {list && !list.meta.current_template?.can_create && <p>房间创建需要启用试卷与凭证配置。</p>}
      {creating && <form onSubmit={e => { e.preventDefault(); void run(async () => {
        const created = await request<{ room: Room; token: string | null; entry_url: string }>(rooms, {
          candidate_name: name, admission_expires_at: Math.floor(new Date(expiry).getTime() / 1000),
          template_version: list?.meta.current_template?.version, request_key: createKey,
        });
        setCredential(created.token ? { id: created.room.id, token: created.token, entry_url: created.entry_url } : null);
        setCreating(false); setFilter(''); setQuery(''); setName(''); setExpiry(''); refresh(v => v + 1);
      }); }}>
        <p>{list?.meta.current_template?.title}</p>
        <label>考生姓名 <input required maxLength={80} value={name} onChange={e => { setName(e.target.value); setCreateKey(crypto.randomUUID()); }} /></label>
        <label>入场截止（本地时间） <input required type="datetime-local" value={expiry} onChange={e => { setExpiry(e.target.value); setCreateKey(crypto.randomUUID()); }} /></label>
        <p>截止时间用于首次入场，作答时间另计。</p>
        <button disabled={busy}>生成凭证</button><button type="button" disabled={busy} onClick={() => setCreating(false)}>取消</button>
      </form>}
      <div className="examiner-scroll"><table><thead><tr><th>考生</th><th>试卷</th><th>状态</th><th>入场截止</th><th>操作</th></tr></thead>
        <tbody>{list?.items.flatMap(item => [
          <tr key={item.id}><td>{item.candidate_name}</td><td>{item.exam_title}</td><td>{states[item.exam_status]}</td><td>{time(item.admission_expires_at)}</td><td>
            {item.exam_status === 'completed' && <button onClick={() => { setCredential(null); onNavigate(`/admin/r2/rooms/${item.id}`); }}>查看结果</button>}
            {item.token_available && <button disabled={busy} aria-expanded={credential?.id === item.id} onClick={() => {
              if (credential?.id === item.id) setCredential(null);
              else void run(async () => setCredential({ id: item.id, ...await request<{ token: string; entry_url: string }>(`${rooms}/${item.id}/token`, {}) }));
            }}>入场凭证</button>}
          </td></tr>,
          ...(credential?.id === item.id ? [<tr key={item.id + ':token'}><td colSpan={5}><code>{credential.token}</code>
            <button onClick={() => void run(async () => { await navigator.clipboard.writeText(credential.token); setMessage('已复制凭证'); })}>复制凭证</button>
            <button onClick={() => void run(async () => { await navigator.clipboard.writeText(`${new URL(credential.entry_url, location.origin)}\n入场凭证：${credential.token}\n截止：${time(item.admission_expires_at)}`); setMessage('已复制入场信息'); })}>复制入场信息</button>
          </td></tr>] : []),
        ])}</tbody></table></div>
      {list && !list.items.length && <p>没有符合条件的房间。</p>}
      {list?.next_cursor && <button disabled={busy} onClick={() => void run(async () => {
        const next = await request<Listing>(`${rooms}?${new URLSearchParams({ status: filter, q: query, cursor: list.next_cursor! })}`);
        setList({ ...next, items: [...list.items, ...next.items] });
      })}>加载更多</button>}
    </>}
  </section>;
}
