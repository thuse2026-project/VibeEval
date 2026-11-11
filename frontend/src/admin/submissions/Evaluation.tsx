import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../../shared/api';
import { formatTime } from './types';

type Report = {
  run_id: string; status: 'completed'|'infrastructure_failed'; score: number|null;
  passed: number|null; total: number; output: string; message: string; evaluated_at: number;
  tests: {name:string; passed:boolean; points:number}[];
};
type Results = {
  changes: {path:string; kind:string; added:number|null; removed:number|null; diff:string; truncated:boolean}[];
  report: Report|null; last_attempt?: Report|null; task_version: string; rule_version: string;
};
const titles: Record<string,string> = {
  test_valid_reservation:'正常预约', test_overlapping_reservation_rejected:'拒绝重叠预约',
  test_adjacent_reservations_allowed:'允许首尾相接', test_different_rooms_independent:'不同会议室互不影响',
  test_cancel_releases_room:'取消后释放时段', test_invalid_input_rejected:'拒绝非法输入',
  test_unknown_booking_cancel_rejected:'拒绝取消未知预约', test_repeated_cancel_is_idempotent:'重复取消保持一致',
};

export default function Evaluation({submissionId,onExpired}:{submissionId:string;onExpired:()=>void}) {
  const [result,setResult]=useState<Results|null>(null);
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const [refresh,setRefresh]=useState(0);
  const generation=useRef(0);
  const endpoint=`submissions/${encodeURIComponent(submissionId)}/evaluation`;
  useEffect(()=>{
    const current=++generation.current;
    setResult(null);setError('');setBusy(false);
    api<Results>(endpoint).then(value=>{if(current===generation.current)setResult(value);})
      .catch(e=>{if(current===generation.current){if(e instanceof ApiError&&e.status===401)onExpired();else setError(e.message);}});
    return()=>{generation.current=current+1;};
  },[endpoint,onExpired,refresh]);
  async function evaluate() {
    if(busy)return;
    const current=generation.current;setBusy(true);setError('');
    try {
      await api<Report>(endpoint,{},undefined,120000);
      if(current===generation.current)setRefresh(value=>value+1);
    }catch(e){if(current===generation.current){if(e instanceof ApiError&&e.status===401)onExpired();else setError((e as Error).message);}}
    finally{if(current===generation.current)setBusy(false);}
  }
  const report=result?.report;
  return <section className="evaluation-panel" aria-labelledby="evaluation-title">
    <div className="records-title"><h2 id="evaluation-title">作答结果</h2>
      <button className="secondary" disabled={busy||!result} onClick={()=>void evaluate()}>
        {busy?'正在评测…':report?'重新评测快照':'评测已提交答卷'}
      </button>
    </div>
    <div className="evaluation-body">
      <div className="score-grid">
        <p><span>结果分 · 占总分 60%</span><strong>{report?.status==='completed'?`${report.score} / 60`:'未评 / 60'}</strong></p>
        <p><span>过程分 · 占总分 40%</span><strong>未评 / 40</strong></p>
        <p><span>综合总分</span><strong>暂无总分</strong></p>
      </div>
      <p className="output-note">结果分 = 8 项公开行为测试通过数 ÷ 8 × 60，每项 7.5 分。过程评价尚未接入；未评不等于零分。</p>
      {error&&<p className="error" role="alert">{error}<button className="secondary" onClick={()=>setRefresh(v=>v+1)}>重新读取结果</button></p>}
      {!result&&!error&&<p role="status">正在读取改动和评测记录…</p>}
      {busy&&<p role="status">正在独立环境评测固定快照，不修改已交卷文件，请稍候。</p>}
      {result&&<>
        <h3>改了哪里</h3>
        {!result.changes.length?<p>与原题一致，未发现文件改动。</p>:<>
          <p>共 {result.changes.length} 个文件发生变化；{result.changes.some(f=>f.path==='tests/test_candidate.py')?'考生测试文件有修改，测试有效性待人工确认。':'未发现考生测试文件改动。'}</p>
          {result.changes.map(file=><details key={file.path} className="change-item">
            <summary>{file.path} · {file.kind==='added'?'新增':file.kind==='deleted'?'删除':'修改'}{file.added!==null?` · +${file.added} / −${file.removed} 行`:''}</summary>
            {file.diff?<pre tabIndex={0} aria-label={`${file.path} 代码差异`}>{file.diff}</pre>:<p>内容较大或仅换行变化，请对照代码快照人工查看。</p>}
            {file.truncated&&<p>差异未完整展开，请查看代码快照。</p>}
          </details>)}
        </>}
        <p className="output-note">摘要仅说明文件变化，不据此断言修复正确。考生新增测试不增加这 60 分的分母。</p>
        <h3>平台公开测试评测</h3>
        {!report?<p>尚未评测。点击上方按钮，基于这份已提交快照运行。</p>:<>
          <p>{report.message}</p>
          {report.status==='completed'&&<><p><strong>{report.passed} / {report.total} 项通过</strong> · 评测于 {formatTime(report.evaluated_at)}</p>
            <ul className="test-results">{report.tests.map(test=><li key={test.name}>{test.passed?'通过':'未通过'} · {titles[test.name]??test.name}<span>{test.points} / 7.5 分</span></li>)}</ul></>}
          {report.output&&<details><summary>查看平台评测输出</summary><pre tabIndex={0}>{report.output}</pre></details>}
        </>}
        {result.last_attempt?.status==='infrastructure_failed'&&result.last_attempt.run_id!==report?.run_id&&<p className="error">最近一次重试因平台故障未完成，上方保留此前成功评测的结果。</p>}
        <p className="output-note">题目版本：{result.task_version} · 评分规则：{result.rule_version}</p>
      </>}
    </div>
  </section>;
}
