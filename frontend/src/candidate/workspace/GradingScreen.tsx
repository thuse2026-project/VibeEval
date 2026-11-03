type Props = {
  submissionId: string
  snapshotId: string
}

export function GradingScreen({ submissionId, snapshotId }: Props) {
  return (
    <div className="screen-page preparation-page">
      <header className="preparation-header"><span className="brand-mark" aria-hidden="true">&lt;/&gt;</span><strong>VibeEval</strong><span className="header-divider" />工程能力考核<span className="spacer" /><span className="status-chip">代码已冻结</span></header>
      <main className="grading-body">
        <div className="success-mark" aria-hidden="true">✓</div>
        <h1>答卷已提交</h1>
        <p>提交成功，代码已冻结并保存为不可变快照。</p>
        <section className="preparation-card receipt-card">
        <h2>提交回执</h2>
        <p className="submission-id">
          <span>提交编号</span> <b>{submissionId}</b>
        </p>
        <p className="submission-id"><span>快照编号</span> <b>{snapshotId}</b></p>
        <div className="receipt-check"><span aria-hidden="true">✓</span><div><b>冻结工作区</b><p>交卷后不能继续编辑或运行终端命令。</p></div></div>
        <div className="receipt-check"><span aria-hidden="true">✓</span><div><b>保存答卷快照</b><p>考官可在后台调阅代码快照与公开测试输出。</p></div></div>
        </section>
        <p className="note">请保存提交编号。现在可以安全关闭页面。</p>
        <a href="/entry">返回入场页</a>
      </main>
    </div>
  )
}
