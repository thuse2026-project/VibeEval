import { Topbar } from '../../shared/components/Topbar'

type Props = {
  submissionId: string
  snapshotId: string
}

export function GradingScreen({ submissionId, snapshotId }: Props) {
  return (
    <div className="screen-page">
      <Topbar left={<span className="validity">提交成功</span>} />
      <main className="grading-body">
        <h1>答卷已提交</h1>
        <p className="submission-id">
          提交编号 <b>{submissionId}</b> · 快照 {snapshotId}
        </p>
        <p className="note">工作区已冻结为不可变快照。请保存提交编号，交卷后不能继续编辑。</p>
      </main>
    </div>
  )
}
