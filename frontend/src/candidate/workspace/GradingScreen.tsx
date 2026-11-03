import { Topbar } from '../../shared/components/Topbar'

type Props = {
  submissionId: string
  snapshotId: string
}

const stages = [
  { name: '冻结文件', state: 'done' },
  { name: '创建不可变快照', state: 'done' },
] as const

export function GradingScreen({ submissionId, snapshotId }: Props) {
  return (
    <div className="screen-page">
      <Topbar left={<span className="validity">交卷完成</span>} />
      <main className="grading-body">
        <h1>答卷已提交</h1>
        <p className="submission-id">
          提交编号 <b>{submissionId}</b> · 快照 {snapshotId}
        </p>
        <ul className="pipeline">
          {stages.map((s) => (
            <li key={s.name} className={s.state}>
              <span className="idx">{s.state === 'done' ? '✓' : s.state === 'doing' ? '…' : '·'}</span>
              <span className="pname">{s.name}</span>
              <span className={`tag ${s.state === 'done' ? 'green' : s.state === 'doing' ? 'blue' : 'gray'}`}>
                {s.state === 'done' ? '已完成' : s.state === 'doing' ? '处理中' : '等待'}
              </span>
            </li>
          ))}
        </ul>
        <p className="note">代码快照已保存。第一阶段不提供隐藏测试、AI 评分或能力报告。刷新可恢复同一份提交回执。</p>
      </main>
    </div>
  )
}
