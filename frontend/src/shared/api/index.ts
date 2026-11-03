import type { ApiClient } from '../types'
import { MockApiClient } from './mock'
import { HttpApiClient } from './client'

// 默认使用 mock，组内后端就绪后设置 VITE_USE_MOCK=false 切换真实后端。
export const usingMock = import.meta.env.VITE_USE_MOCK !== 'false'

export const api: ApiClient = usingMock ? new MockApiClient() : new HttpApiClient()
