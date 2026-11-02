import { Topbar } from '../../shared/components/Topbar'
import type { ProvisionStep } from '../../shared/types'

type Props = {
  steps: ProvisionStep[]
  error: string | null
  ready: boolean
  onRetry: () => void
  onEnter: () => void
}

const stateLabel: Record<ProvisionStep['state'], string> = {
  wait: '等待',
  doing: '进行中',
  done: '完成',
  fail: '失败',
}

function Dot({ state }: { state: ProvisionStep['state'] }) {
  const glyph = state === 'done' ? '✓' : state === 'fail' ? '!' : state === 'doing' ? '…' : '·'
  return <span className={`step-dot ${state}`}>{glyph}</span>
}

export function ProvisioningScreen({ steps, error, ready, onRetry, onEnter }: Props) {
  return (
    <div className="screen-page">
      <Topbar left={<span className="validity">环境准备中</span>} />
      <main className="provision-body">
        <h1>正在准备作答环境</h1>
        <ul className="steps">
          {steps.map((s) => (
            <li key={s.id} className="step">
              <Dot state={s.state} />
              <div className="step-info">
                <div className="step-title">{s.title}</div>
                <div className="step-desc">{s.description}</div>
              </div>
              <span className={`tag ${s.state === 'done' ? 'green' : s.state === 'fail' ? 'red' : s.state === 'doing' ? 'blue' : 'gray'}`}>
                {stateLabel[s.state]}
              </span>
            </li>
          ))}
        </ul>
        {error && (
          <div className="banner banner-error">
            {error}
            <button className="btn" onClick={onRetry}>
              重试
            </button>
          </div>
        )}
        <p className="provision-tip">复用当前会话，重试不会重复创建环境</p>
        <div className="provision-actions">
          <button className="btn primary" disabled={!ready} onClick={onEnter}>
            {ready ? '进入工作台' : '准备中…'}
          </button>
        </div>
      </main>
    </div>
  )
}
