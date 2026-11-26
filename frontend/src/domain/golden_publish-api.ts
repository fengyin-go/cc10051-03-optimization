import { request } from '@/api/client'
import type { ContinueResult, ContinuationDetail } from '@/domain/golden_publish'

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    let message = fallback
    try {
      const payload = (await response.json()) as { detail?: string }
      if (payload.detail) message = payload.detail
    } catch {
      // 非 JSON 错误体时沿用兜底文案
    }
    throw new Error(message)
  }
  return (await response.json()) as T
}

/** 整组派送：服务端按订阅实体过滤并对逐单去重，重试不重复建单。 */
export async function fanoutPublish(id: number): Promise<ContinueResult> {
  const response = await request(`/api/golden_publish/${id}/fanout`, { method: 'POST' })
  return readJson<ContinueResult>(response, '整组派送失败')
}

/** 拉取续派明细：已送达 / 送达待确认 / 仍需派送，全部来自服务端核对。 */
export async function fetchContinuation(id: number): Promise<ContinuationDetail> {
  const response = await request(`/api/golden_publish/${id}/continuation`)
  return readJson<ContinuationDetail>(response, '续派明细读取失败')
}

/** 批量续派：只补仍需派送的安排，已送达订阅方不会被重新提交。 */
export async function continuePublish(id: number): Promise<ContinueResult> {
  const response = await request(`/api/golden_publish/${id}/continue`, { method: 'POST' })
  return readJson<ContinueResult>(response, '续派失败')
}
