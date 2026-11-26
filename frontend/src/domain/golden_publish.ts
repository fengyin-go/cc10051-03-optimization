/**
 * 金记录发布续派领域类型：与后端 /api/golden_publish/{id}/continuation 对齐。
 * 三类安排由服务端逐条核对真实送达结果后给出，前端只负责展示。
 */
export type ContinuationLine = {
  dist_id: number
  分发批次: string
  订阅方: string
  分发状态: string
  回执状态: string
  尝试次数: number
  失败原因: string
  送达时间: string
  receipt: {
    has_receipt: boolean
    receipt_id: string
    receipt_status: string
    verified: boolean
    confirmer: string
  }
}

export type ContinuationDetail = {
  ok: boolean
  publish_id: number
  发布批次: string
  版本标识: string
  实体编码: string
  eligible_subscribers: string[]
  missing_subscribers: string[]
  delivered: ContinuationLine[]
  pending_verify: ContinuationLine[]
  pending_dispatch: ContinuationLine[]
  counts: {
    delivered: number
    pending_verify: number
    pending_dispatch: number
    missing: number
  }
}

export type ContinueResult = {
  ok: boolean
  message: string
  created: number[]
  retained: number[]
  detail: ContinuationDetail
}
