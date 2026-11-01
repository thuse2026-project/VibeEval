import { Topbar } from '../../shared/components/Topbar'

type Props = {
  submissionId: string
}

const dims = [
  { name: '任务拆解', val: '8 / 10' },
  { name: '代码审查', val: '7 / 10' },
  { name: '调试纠偏', val: '6 / 10' },
  { name: '测试验证', val: '8 / 10' },
]

const events = [
  { time: '14:02', text: '运行测试 pytest', flag: '通过' },
  { time: '14:08', text: '修改 src/main.py · 第 3 行' },
  { time: '14:15', text: '大段文本粘贴', flag: '待人工核查' },
  { time: '14:20', text: '提交答卷 · 快照已冻结' },
]

export function ReportScreen({ submissionId }: Props) {
  return (
    <div className="screen-page">
      <Topbar
        left={<span className="validity">评审端</span>}
        right={<span className="tag green">报告已完成</span>}
      />
      <main className="report-body">
        <div className="report-head">
          <h1>候选人 C-014 · 提交 {submissionId}</h1>
        </div>
        <div className="report-grid">
          <div className="panel">
            <h3>客观测试结果</h3>
            <div className="score-row"><span>公开测试</span><span className="val">2 passed, 0 failed</span></div>
            <div className="score-row"><span>隐藏测试</span><span className="val">4 passed, 1 failed</span></div>
            <div className="score-row"><span>任务完成情况</span><span className="val">3 / 4</span></div>
            <div className="score-row"><span>待人工核查</span><span className="val">1 项</span></div>
          </div>
          <div className="panel">
            <h3>能力维度（模型辅助评价）</h3>
            {dims.map((d) => (
              <div key={d.name} className="score-row">
                <span>{d.name}</span>
                <span className="val">{d.val}</span>
              </div>
            ))}
          </div>
          <div className="panel timeline">
            <h3>操作时间线</h3>
            <div className="filters">
              <span className="filter active">全部</span>
              <span className="filter">Prompt</span>
              <span className="filter">Diff</span>
              <span className="filter">命令</span>
              <span className="filter">测试</span>
              <span className="filter">异常标记</span>
            </div>
            {events.map((e, i) => (
              <div key={i} className="event">
                <span className="time">{e.time}</span>
                <span>{e.text}</span>
                {e.flag && <span className="flag">{e.flag}</span>}
              </div>
            ))}
          </div>
        </div>
      </main>
    </div>
  )
}
