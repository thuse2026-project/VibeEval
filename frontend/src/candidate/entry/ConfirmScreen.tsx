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
          <span>技术栈：Python 3.11</span>
          <span>考核时长：{examInfo?.duration_minutes ?? 60} 分钟</span>
          <span>题目版本：v1.2</span>
        </div>

        {error && <div className="banner banner-error">{error}</div>}

        <div className="rule-group">
          <h3>允许操作</h3>
          <div className="rule-card">
            编辑允许路径内的文件、运行公开测试、执行终端命令、提交答卷。
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
            平台记录进场、开始答题与最终提交时间，用于本场考务台账。
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
          <button className="btn danger">退出</button>
          <button className="btn primary" disabled={!checked} onClick={onConfirm}>
            确认并准备环境
          </button>
        </div>
      </footer>
    </div>
  )
}
