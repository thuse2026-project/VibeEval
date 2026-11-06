import type { CandidateSession } from './api';

type Props = {
  session: CandidateSession | null;
  busy: boolean;
  rules: boolean;
  error: string;
  onRules: (checked: boolean) => void;
  onTransition: (action: 'prepare' | 'start') => void;
  onRefresh: () => void;
  onReceipt: () => void;
};

export function PreparationScreen({ session, busy, rules, error, onRules, onTransition, onRefresh, onReceipt }: Props) {
  const status = session?.status;
  const ready = status === 'ready';
  const preparing = status === 'provisioning' || (busy && !ready);
  const submitted = !!status && ['submitted', 'submitting', 'submission_failed'].includes(status);
  const environment = preparing || ready || status === 'provision_failed';
  const heading = submitted ? '答卷已锁定' : environment ? '环境准备' : '考前规则确认';

  return <div className="candidate-workspace preparation-page">
    <header className="preparation-header">
      <span className="brand-mark" aria-hidden="true">&lt;/&gt;</span><strong>VibeEval</strong>
      <span className="header-divider" /><span>工程能力考核</span>
      <span className="spacer" /><span className="environment-label">独立考试环境</span>
      {session && <span className="candidate-name">{session.candidate.name}</span>}
    </header>
    <main className="preparation-main">
      <nav className="breadcrumbs" aria-label="考核流程">考核准备 <span>›</span> <b>{heading}</b> <span>›</span> 答题</nav>
      <div className="preparation-intro"><h1>{heading}</h1><p>{submitted ? '工作区已锁定，请恢复提交回执，确认快照已保存。' : environment ? '确认隔离环境可用后，再开始作答与计时。' : '请在开始前阅读本次考核范围、允许的操作与答卷保存说明。'}</p></div>
      <div className="preparation-columns">
        <section className="preparation-card">
          <div className="card-title"><span className="section-icon" aria-hidden="true">{submitted ? '✓' : environment ? '⌘' : '≡'}</span>
            <div><h2>{session?.exam_info.title ?? '恢复考核会话'}</h2><p>{session ? `考核编号 ${session.exam_info.id}` : '正在查询当前浏览器的入场会话'}</p></div>
            <span className="status-chip">{submitted ? '已锁定' : ready ? '已就绪' : preparing ? '准备中' : status === 'provision_failed' ? '准备失败' : '待开始'}</span>
          </div>
          {error && <div role="alert" className="preparation-error">{error}</div>}
          {environment ? <>
            <div className="environment-progress" role="status"><b>{ready ? '环境已就绪' : status === 'provision_failed' ? '环境准备失败，可重新尝试' : '正在创建隔离开发环境，请稍候'}</b><p>准备期间不计时。状态以服务器返回为准，请保持当前页面打开。</p></div>
            <ol className="preparation-steps environment-steps">
              <li className="complete"><span>✓</span><div><b>校验会话</b><p>已恢复当前候选人的考核会话。</p></div></li>
              <li className={ready ? 'complete' : 'current'}><span>{ready ? '✓' : '2'}</span><div><b>创建沙箱并载入题目</b><p>{ready ? '题目环境已由服务器准备完成。' : '等待服务器准备独立沙箱、项目文件和终端服务。'}</p></div></li>
              <li className={ready ? 'current' : ''}><span>3</span><div><b>确认开始作答</b><p>点击开始后才会计时；刷新页面不会重置截止时间。</p></div></li>
            </ol>
          </> : submitted ? <div className="environment-progress"><p>交卷期间不能继续编辑。网络中断后可查询状态，并恢复或重试同一份答卷的回执。</p></div> : <>
            <div className="exam-chips"><span>Python 3.12</span><span>{session?.exam_info.duration_minutes ?? '—'} 分钟</span><span>独立沙箱</span></div>
            <dl className="rules-list">
              <div><dt>考核任务</dt><dd>修复会议室预约工程中的缺陷，并补充回归测试。完整要求以工作区 README 为准。</dd></div>
              <div><dt>考核时长</dt><dd>{session?.exam_info.duration_minutes ?? '—'} 分钟，从确认开始作答起计时；中途离开或刷新不会暂停。</dd></div>
              <div><dt>允许操作</dt><dd><span className="rule-chip">编辑允许的文件</span><span className="rule-chip">终端命令</span><span className="rule-chip">公开测试</span></dd></div>
              <div><dt>操作边界</dt><dd>只修改当前会话允许编辑的文件；不得访问其他会话、平台密钥或绕过只读限制。本期独立作答，无 AI 辅助。</dd></div>
              <div><dt>保存与交卷</dt><dd>停止编辑 5 秒后自动保存。交卷前会保存全部修改；保存失败需先处理。提交后代码冻结，不能继续修改。</dd></div>
              <div><dt>数据说明</dt><dd>平台保存工作区文件、提交快照、公开测试输出以及入场、开始和提交时间，供考官调阅。</dd></div>
            </dl>
          </>}
          {!submitted && <label className="rules-consent"><input type="checkbox" checked={rules} onChange={event => onRules(event.target.checked)} />我已阅读并理解考核规则与答卷保存说明</label>}
          <div className="preparation-actions"><a href="/entry">返回入场页</a><span className="spacer" />
            {session && <button disabled={busy} onClick={onRefresh}>查询当前状态</button>}
            {session && ['admitted', 'provision_failed'].includes(session.status) && <button className="primary" disabled={busy || !rules} onClick={() => onTransition('prepare')}>{busy ? '正在准备…' : '准备环境'}</button>}
            {ready && <button className="primary" disabled={busy || !rules} onClick={() => onTransition('start')}>确认开始作答并计时</button>}
            {submitted && <button className="primary" disabled={busy} onClick={onReceipt}>恢复 / 重试交卷回执</button>}
          </div>
        </section>
        <aside className="preparation-aside">
          <section className="preparation-card"><h3>本次流程</h3><ol className="preparation-steps">
            <li className={environment || submitted ? 'complete' : 'current'}><span>1</span><div><b>规则确认</b><p>阅读并同意考核规则</p></div></li>
            <li className={environment ? 'current' : ''}><span>2</span><div><b>环境准备</b><p>创建独立沙箱，此时不计时</p></div></li>
            <li><span>3</span><div><b>答题与提交</b><p>在规定时间内完成并冻结答卷</p></div></li>
          </ol></section>
          <section className="preparation-card"><h3>开始前请检查</h3><ul className="preflight-list"><li>使用稳定网络与最新版桌面浏览器</li><li>无需在本地安装 Python 环境</li><li>先阅读 README 和公开测试，再修复缺陷</li></ul></section>
          <section className="preparation-tip"><h3>独立作答 · 真实验证</h3><p>修改后在终端运行公开测试，以实际输出核对结果。请在交卷前确认所有文件已保存。</p></section>
        </aside>
      </div>
      <footer className="preparation-footer"><span>© VibeEval · 工程能力评估平台</span><span>Release 1.0</span></footer>
    </main>
  </div>;
}
