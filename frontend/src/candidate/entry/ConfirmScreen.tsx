import { useState } from 'react'
import { Topbar } from '../../shared/components/Topbar'
import type { ExamInfo } from '../../shared/types'

type Props = {
  examInfo: ExamInfo | null
  error: string | null
  onConfirm: () => void
}

export function ConfirmScreen({ examInfo, error, onConfirm }: Props) {
  const [checked, setChecked] = useState(false)

  return (
    <div className="screen-page">
      <Topbar
        left={<span className="validity">本场次 {examInfo?.duration_minutes ?? 60} 分钟</span>}
      />
      <main className="confirm-body">
        <h1>{examInfo?.title ?? 'Python 工程缺陷修复考核'}</h1>
        <div className="task-meta">
          <span>技术栈：Python 3.12</span>
          <span>考核时长：{examInfo?.duration_minutes ?? 60} 分钟</span>
          <span>会议室预约缺陷修复 · 无 AI 辅助</span>
        </div>

        {error && <div className="banner banner-error">{error}</div>}

        <div className="rule-group">
          <h3>允许操作</h3>
          <div className="rule-card">
            编辑允许路径内的文件、补充回归测试、运行 unittest、执行终端命令、提交答卷。本阶段不提供 AI 助手。
          </div>
        </div>
        <div className="rule-group">
          <h3>禁止操作</h3>
          <div className="rule-card">
            访问其他会话、读取隐藏测试或标准答案、绕过沙箱限制、访问外部网络。
          </div>
        </div>
        <div className="rule-group">
          <h3>数据采集说明</h3>
          <div className="rule-card">
            平台保存考生身份、入场与开考时间、最终提交代码及已有测试输出。本阶段不声称记录 AI 对话、完整终端历史或能力画像。
          </div>
        </div>
      </main>
      <footer className="confirm-footer">
        <label className="checkbox">
          <input
            type="checkbox"
            checked={checked}
            onChange={(e) => setChecked(e.target.checked)}
          />
          我已阅读并理解规则与数据使用说明
        </label>
        <div className="actions">
          <a href="/entry">返回入场信息</a>
          <button className="btn primary" disabled={!checked} onClick={onConfirm}>
            确认并准备环境
          </button>
        </div>
      </footer>
    </div>
  )
}
