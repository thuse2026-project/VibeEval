import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

const key = 'vibeeval-examiner-preview';
const tasks = [
  { title: '仓库理解', problem: '解释 TinyDB 如何处理查询与存储。', earned: 84, parts: [['项目与模块', 18, 20], ['调用链', 16, 20], ['局部机制', 16, 20], ['边界行为', 16, 20], ['修改与验证', 18, 20]], answer: '从表的查询方法跟踪到存储读取。' },
  { title: '代码审阅', problem: '阅读源码并报告可复现的问题。', earned: 75, parts: [['问题报告得分', 75, 100]], answer: '报告包含触发顺序、预期结果及实际结果。' },
  { title: '业务拓展', problem: '为 Datasette 表格页补齐列统计。', objective: [['基础行为', 10, 10], ['列结构', 0, 15], ['筛选统计', 15, 15], ['权限', 10, 10], ['原有行为', 20, 20]], parts: [['需求判断', 8, 10], ['验证与风险', 8, 10], ['过程判断与验证', 7, 10]], answer: '我用分页后的表格核对统计范围，确认统计使用全部匹配行。特殊列还未验证。', explanation: '筛选与分页的验证有证据。未覆盖部分特殊列，因此列结构组未得分。' },
  { title: '历史 Bug 修复', problem: '修复 Datasette 筛选计数和分页链接。', objective: [['筛选计数', 20, 20], ['切换链接', 0, 25], ['排序组合', 10, 10], ['筛选变体', 0, 10], ['分页回归', 5, 5]], parts: [['根因判断', 7, 10], ['回归验证', 6, 10], ['过程判断与验证', 6, 10]], cap: 50, answer: '已修复统计范围。切换链接仍保留上一页的位置，需要补测。', explanation: '仍能复现原切换链接问题，按题目规则封顶 50 分。' },
];
const initial = [
  { id: 'sample', name: '林同学', status: '已完成' },
  { id: 'grading', name: '周同学', status: '已完成', pending: true },
  { id: 'working', name: '许同学', status: '考试中' },
  { id: 'expired', name: '陈同学', status: '入场已过期' },
];
const read = () => {
  try { return JSON.parse(localStorage.getItem(key)) ?? { rooms: initial, reviews: {} }; }
  catch { return { rooms: initial, reviews: {} }; }
};
const sum = parts => parts.reduce((total, [, score]) => total + score, 0);
const roomStatus = room => room.expires && room.expires <= Date.now() ? '入场已过期' : room.status;
const time = stamp => new Date(stamp).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' });
const deadline = value => new Date(value + ':00+08:00').getTime();
const defaultDate = () => new Date(Date.now() + (24 + 8) * 3600_000).toISOString().slice(0, 16);
const signed = number => number > 0 ? '+' + number : String(number);

function Modal({ titleId, close, children }) {
  const dialog = useRef(null);
  useEffect(() => { dialog.current.showModal(); }, []);
  return <dialog ref={dialog} aria-labelledby={titleId} onCancel={close}>{children}</dialog>;
}

function Parts({ title, rows }) {
  return <section className="panel"><h3>{title}</h3><dl className="parts">{rows.map(([label, points, max]) =>
    <div key={label}><dt>{label}</dt><dd>{points} / {max}</dd></div>)}</dl></section>;
}

function Timeline({ index }) {
  const [conversation, setConversation] = useState('all');
  if (index === 1) return <section className="panel">这道题独立审阅，无 Agent 对话。</section>;
  const development = index >= 2;
  return <section className="panel">
    <div className="between"><h3>作答过程</h3><select aria-label="筛选会话" value={conversation} onChange={event => setConversation(event.target.value)}>
      <option value="all">全部会话</option><option value="plan">理解任务</option>{development && <option value="verify">改动与验证</option>}
    </select></div>
    {conversation !== 'verify' && <ol className="timeline">
      <li><strong>考生</strong><small> 14:01</small><p>请先读题面，说明任务目标和你会检查的位置。</p></li>
      <li><strong>Agent</strong><small> 14:02</small><p>已读取题面和相关源码。我会先确认现有行为。</p></li>
    </ol>}
    {development && conversation !== 'plan' && <ol className="timeline">
      <li><strong>考生 · 拒绝审批</strong><small> 14:07</small><p>先缩小改动范围。</p><details><summary>拟议变更 · 未写入</summary><pre>{'- 原有分支\n+ 重写整个处理流程'}</pre></details></li>
      <li><strong>Agent · 已写入</strong><small> 14:12</small><details><summary>查看 diff · datasette/views/table.py</summary><pre>{'@@ -42,1 +42,1 @@\n- rows = current_page\n+ rows = filtered_rows'}</pre></details></li>
      <li><strong>测试回执</strong><small> 14:15</small><p>公开场景已运行。详细结果见评分分项。</p></li>
    </ol>}
  </section>;
}

function Exam({ room, index, setIndex, reviews, onReview, onBack }) {
  const [tab, setTab] = useState('score');
  const [reviewing, setReviewing] = useState('');
  const [delta, setDelta] = useState('1');
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const task = tasks[index], id = room.id + ':' + index;
  const review = reviews[id];
  const objective = sum(task.objective ?? []), subjective = sum(task.parts);
  const raw = task.earned ?? objective + subjective;
  const min = Math.max(-10, -subjective), max = Math.min(10, 30 - subjective);
  const score = change => Math.min(task.cap ?? 100, raw + change);
  const pending = room.pending && index >= 2;
  const proposal = reviewing === 'adjust' ? Number(delta) : 0;
  function confirm(event) {
    event.preventDefault();
    if (reviewing === 'adjust' && (!Number.isInteger(proposal) || proposal < min || proposal > max || !proposal || !reason.trim())) {
      setError('请选择合法的非零调分，并填写理由。');
      return;
    }
    if (!onReview(id, { delta: proposal, reason: reason.trim(), at: Date.now() })) {
      setError('该题已在另一个页面确认。');
    }
    setReviewing('');
  }
  return <>
    <button className="back" onClick={onBack}>← 返回考试房间</button>
    <h1>{room.name}<span className="badge">已完成</span></h1>
    <div className="exam">
      <aside aria-label="四题概览">{tasks.map((item, i) => {
        const saved = reviews[room.id + ':' + i];
        const points = Math.min(item.cap ?? 100, (item.earned ?? (sum(item.objective) + sum(item.parts))) + (saved?.delta ?? 0));
        return <button className={index === i ? 'selected' : ''} aria-pressed={index === i} key={item.title} onClick={() => { setIndex(i); setTab('score'); setReviewing(''); }}>
          <span>{i + 1}. {item.title}</span><small>{room.pending && i >= 2 ? '评分中' : `${points} / 100 · ${saved ? '已锁定' : i >= 2 ? '待确认' : '已评分'}`}</small>
        </button>;
      })}</aside>
      <article>
        <section className="panel"><div className="between"><h2>{task.title}</h2><strong className="score">{pending ? '评分中' : `${score(review?.delta ?? 0)} / 100`}</strong></div><p>{task.problem}</p></section>
        <div className="tabs" aria-label="题目详情">{[['score', '评分与答卷'], ['timeline', '作答过程']].map(([value, title]) =>
          <button aria-pressed={tab === value} key={value} onClick={() => setTab(value)}>{title}</button>)}</div>
        {tab === 'timeline' ? <Timeline key={index} index={index} /> : <>
          {task.objective && <Parts title={`客观测试 · ${objective} / 70`} rows={task.objective} />}
          {pending ? <section className="panel"><h3>主观评分进行中</h3><p>答卷已经提交。主观分就绪后即可复核。</p></section> :
            <Parts title={task.objective ? `AI 主观原分 · ${subjective} / 30` : '评分分项'} rows={task.parts} />}
          <section className="panel"><h3>考生答卷与依据</h3><p>{task.answer}</p>{task.explanation && <p className="muted">{task.explanation}</p>}</section>
        </>}
        {task.objective && !pending && <section className="panel review">
          <h3>考官复核</h3>
          {task.cap && <p>封顶前 {raw + (review?.delta ?? 0)} 分 · 本题上限 {task.cap} 分</p>}
          {review ? <><strong>{review.delta ? '已调分并锁定' : '已确认原分'}</strong><p>原分 {score(0)} · 调整 {signed(review.delta)} · 最终 {score(review.delta)}</p>
            <small>演示考官 · {time(review.at)}</small>{review.reason && <p>理由：{review.reason}</p>}</> :
            <div className="actions">
              <button onClick={() => { setReviewing('confirm'); setReason(''); setError(''); }}>确认原分</button>
              <button className="primary" onClick={() => { setReviewing('adjust'); setDelta(max > 0 ? String(Math.min(3, max)) : '-1'); setReason(''); setError(''); }}>调整分数</button>
            </div>}
        </section>}
      </article>
    </div>
    {reviewing && <Modal titleId="review-title" close={() => setReviewing('')}><form onSubmit={confirm}>
      <h2 id="review-title">{reviewing === 'adjust' ? '调整并锁定' : '确认并锁定原分'}</h2>
      <p>确认后，本题不能再次调整。</p>
      {reviewing === 'adjust' && <><label>主观调分（{min} 至 {max}）<input type="number" min={min} max={max} required value={delta} onChange={event => setDelta(event.target.value)} /></label>
        <label>调分理由<textarea required value={reason} onChange={event => setReason(event.target.value)} /></label></>}
      <p>原分 {score(0)} · 封顶前 {raw + proposal} · 最终 {score(proposal)} 分</p>
      {proposal !== 0 && task.cap && score(proposal) === score(0) &&
        <p className="muted">受本题封顶规则影响，这次调分不会改变最终分。</p>}
      {error && <p role="alert">{error}</p>}
      <div className="actions"><button type="button" onClick={() => setReviewing('')}>取消</button><button className="primary">确认锁定</button></div>
    </form></Modal>}
  </>;
}

function App() {
  const [state, setState] = useState(read);
  const [room, setRoom] = useState(null), [index, setIndex] = useState(0);
  const [filter, setFilter] = useState('全部状态'), [query, setQuery] = useState('');
  const [open, setOpen] = useState(''), [creating, setCreating] = useState(false);
  const [name, setName] = useState(''), [expires, setExpires] = useState(defaultDate);
  const [message, setMessage] = useState(''), [error, setError] = useState('');
  const save = next => { localStorage.setItem(key, JSON.stringify(next)); setState(next); };
  const active = state.rooms.find(item => item.id === room);
  const filtered = state.rooms.filter(item => (filter === '全部状态' || roomStatus(item) === filter) && item.name.includes(query.trim()));
  function create(event) {
    event.preventDefault();
    if (!name.trim() || !(deadline(expires) > Date.now())) { setError('请输入考生名称，并选择未来的入场截止时间。'); return; }
    const created = { id: crypto.randomUUID(), name: name.trim(), expires: deadline(expires), status: '待入场', token: 'demo_' + crypto.randomUUID() };
    const latest = read();
    save({ ...latest, rooms: [created, ...latest.rooms] });
    setOpen(created.id); setCreating(false); setQuery(''); setFilter('全部状态'); setMessage('演示房间已创建');
  }
  function review(id, value) {
    const latest = read();
    if (latest.reviews[id]) { setState(latest); setMessage('该题已确认，已载入保存结果。'); return false; }
    save({ ...latest, reviews: { ...latest.reviews, [id]: value } });
    setMessage('复核结果已锁定'); return true;
  }
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); setMessage('已复制'); }
    catch { setMessage('复制失败，可选中凭证手动复制。'); }
  }
  return <>
    <header><strong>VibeEval <span>/ 考官台</span></strong><span className="muted">本地预览 · 模拟数据</span></header>
    <main>
      <p role="status" className="notice">{message}</p>
      {active ? <Exam room={active} index={index} setIndex={setIndex} reviews={state.reviews} onReview={review} onBack={() => setRoom(null)} /> : <>
        <div className="between heading"><div><h1>考试房间</h1><p className="muted">查看入场状态、四题成绩与作答过程。</p></div>
          <button className="primary" onClick={() => { setCreating(true); setName(''); setExpires(defaultDate()); setOpen(''); setError(''); }}>＋ 创建房间</button></div>
        <section className="panel listing">
          <div className="filters"><input aria-label="搜索考生" placeholder="搜索考生姓名" value={query} onChange={event => { setQuery(event.target.value); setOpen(''); }} /><select aria-label="考试状态" value={filter} onChange={event => { setFilter(event.target.value); setOpen(''); }}>
            {['全部状态', '待入场', '已入场', '考试中', '已完成', '入场已过期'].map(item => <option key={item}>{item}</option>)}</select></div>
          <div className="table-scroll"><table><thead><tr><th>考生</th><th>考试状态</th><th>入场截止（北京时间）</th><th>操作</th></tr></thead>
            <tbody>{filtered.map(item => {
              const status = roomStatus(item);
              return <React.Fragment key={item.id}><tr><td><strong>{item.name}</strong><small>R2 四题考试</small></td><td><span className="badge">{status}</span>{status === '已完成' && <small>{item.pending ? '评分中' : [2, 3].every(i => state.reviews[item.id + ':' + i]) ? '复核完成' : '待确认'}</small>}</td><td>{item.expires ? time(item.expires) : '—'}</td>
                <td className="actions">{status === '已完成' && <button onClick={() => { setOpen(''); setIndex(0); setRoom(item.id); }}>查看结果</button>}<button aria-expanded={open === item.id} onClick={() => setOpen(open === item.id ? '' : item.id)}>入场凭证</button></td></tr>
                {open === item.id && <tr className="token"><td colSpan={4}>{status === '待入场' && item.token ? <><code>{item.token}</code> <button onClick={() => copy(item.token)}>复制凭证</button> <button onClick={() => copy(`VibeEval 演示凭证：${item.token}\n入场截止（北京时间）：${time(item.expires)}`)}>复制入场信息</button></> : <span>{status === '入场已过期' ? '入场凭证已过期' : '考生已入场，凭证已使用'}</span>}</td></tr>}
              </React.Fragment>;
            })}</tbody></table></div>
          {!filtered.length && <p className="empty">没有符合条件的考试房间。</p>}
        </section>
      </>}
      {creating && <Modal titleId="create-title" close={() => setCreating(false)}><form onSubmit={create}>
        <h2 id="create-title">创建考试房间</h2><p className="muted">R2 四题考试 · 演示版本</p>
        <label>考生名称<input required maxLength={80} value={name} onChange={event => setName(event.target.value)} /></label>
        <label>入场截止（北京时间）<input required type="datetime-local" value={expires} onChange={event => setExpires(event.target.value)} /></label>
        <p className="muted">截止前首次入场即可，作答时间按考试规则另计。</p>
        {error && <p role="alert">{error}</p>}
        <div className="actions"><button type="button" onClick={() => setCreating(false)}>取消</button><button className="primary">生成凭证</button></div>
      </form></Modal>}
    </main>
  </>;
}

createRoot(document.getElementById('root')).render(<App />);
