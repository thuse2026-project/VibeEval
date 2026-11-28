// Older design-review sessions stored a sample transcript without a sandbox run ID.
export function isReviewTestSample(data: { run_id?: unknown; output?: unknown }) {
  return !data.run_id && typeof data.output === 'string' &&
    (data.output.startsWith('[本地模拟测试输出]') || data.output.startsWith('[本地评审示例输出]'))
}

export const testUnavailable = '测试未执行。当前环境未连接测试沙箱。'
