import { useEffect, useState } from 'react';
import { api, ApiError } from '../../shared/api';
import { returnToOverview } from './navigation';
import { formatTime, type DataSource, type SubmissionData } from './types';

type Props = {
  submissionId: string; source: DataSource; search: string; onNavigate: (url: string) => void;
  onExpired: () => void; onLogout: () => void; loggingOut: boolean; logoutError: string;
};

export default function Detail({ submissionId, source, search, onNavigate, onExpired, onLogout, loggingOut, logoutError }: Props) {
  const [refresh, setRefresh] = useState(0);
  const [result, setResult] = useState<{ key: string; data?: SubmissionData; error?: string }>({ key: '' });
  const [selectedPath, setSelectedPath] = useState('');
  const key = JSON.stringify([submissionId, source, refresh]);
  const loading = result.key !== key;
  const data = loading ? undefined : result.data;
  const error = loading ? '' : result.error;
  const back = returnToOverview(search, source);

  useEffect(() => {
    let cancelled = false;
    const path = `submissions/${encodeURIComponent(submissionId)}?source=${source}`;
    api<SubmissionData>(path).then(data => {
      if (!cancelled) setResult({ key, data });
    }).catch(failure => {
      if (cancelled) return;
      if (failure instanceof ApiError && failure.status === 401) onExpired();
      else setResult({ key, error: (failure as Error).message });
    });
    return () => { cancelled = true; };
  }, [key, submissionId, source, onExpired]);

  const selected = data?.files.find(file => file.path === selectedPath) ?? data?.files[0];
  return <section className="submission-detail" aria-labelledby="detail-title">
    <div className="detail-toolbar"><a className="text-link" href={back} onClick={event => {
      if (!event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey && event.button === 0) {
        event.preventDefault(); onNavigate(back);
      }
    }}>返回考务总览</a><button className="secondary" disabled={loggingOut} onClick={onLogout}>{loggingOut ? '正在退出…' : '退出登录'}</button></div>
    <div className="section-label">只读调阅</div><h1 id="detail-title">答卷详情</h1>
    {source === 'demo' && <p className="demo-banner" role="status"><strong>演示数据</strong> · 此答卷和测试输出为虚构记录，仅供预览。</p>}
    {logoutError && <p className="error" role="alert">{logoutError}</p>}
    {loading ? <p className="list-message" role="status">正在读取提交快照…</p>
      : error ? <div className="detail-error"><p className="error" role="alert">{error}</p><button className="secondary" onClick={() => setRefresh(value => value + 1)}>重新读取</button></div>
      : data && <>
        <div className="submission-meta">
          <p><span>考生</span><strong>{data.candidate_name}</strong><small>{data.candidate_id}</small></p>
          <p><span>场次</span><strong>{data.exam_title}</strong><small>{data.exam_id}</small></p>
          <p><span>提交时间</span><strong>{formatTime(data.submitted_at)}</strong></p>
          <p><span>提交编号</span><code>{data.submission_id}</code></p>
          <p><span>快照编号</span><code>{data.snapshot_id}</code></p>
        </div>
        <p className="live-note">这里显示提交时固定的文件内容，只读展示，不读取或修改考生当前工作区。</p>
        <section className="snapshot-panel" aria-labelledby="snapshot-title">
          <div className="records-title"><h2 id="snapshot-title">提交代码快照</h2><span>{data.files.length} 个文本文件</span></div>
          {!data.files.length ? <p className="list-message">此快照没有可展示的文本文件。</p>
            : <div className="snapshot-layout"><nav className="snapshot-files" aria-label="快照文件">
              {data.files.map(file => <button key={file.path} type="button" aria-pressed={selected?.path === file.path}
                onClick={() => setSelectedPath(file.path)}>{file.path}</button>)}
            </nav><div className="snapshot-content"><h3>{selected?.path}</h3>
              {selected?.content === '' ? <p className="list-message">此文件为空。</p>
                : <pre tabIndex={0} aria-label="只读文件内容"><code>{selected?.content}</code></pre>}
            </div></div>}
        </section>
        <section className="test-output-panel" aria-labelledby="output-title"><div className="records-title"><h2 id="output-title">公开测试输出</h2></div>
          {data.public_test_output === null ? <p className="list-message">尚未提供公开测试结果。</p>
            : data.public_test_output === '' ? <p className="list-message">已记录测试结果，输出为空。</p>
              : <pre tabIndex={0} aria-label="公开测试原始输出"><code>{data.public_test_output}</code></pre>}
          <p className="output-note">保留原始输出，不根据文字自动推断分数或通过率。</p>
        </section>
      </>}
  </section>;
}
