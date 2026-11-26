<template>
  <div class="modal-backdrop" data-testid="continuation-modal" @click.self="$emit('close')">
    <section class="modal continuation-modal" :data-testid="`continuation-${detail.publish_id}`">
      <header class="continuation-head">
        <div>
          <h3>续派明细 · {{ detail.发布批次 }}</h3>
          <p class="page-desc">
            版本 {{ detail.版本标识 }} ｜ 实体 {{ detail.实体编码 }} ｜
            资格订阅方 {{ detail.eligible_subscribers.join('、') || '无' }}
          </p>
        </div>
        <button class="btn ghost" type="button" data-testid="continuation-close" @click="$emit('close')">
          关闭明细
        </button>
      </header>

      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">本次已送达</span>
          <strong class="stat-value delivered">{{ detail.counts.delivered }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">送达待确认</span>
          <strong class="stat-value pending-verify">{{ detail.counts.pending_verify }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">仍需派送</span>
          <strong class="stat-value pending-dispatch">{{ detail.counts.pending_dispatch }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">资格内缺单</span>
          <strong class="stat-value">{{ detail.counts.missing }}</strong>
        </article>
      </div>

      <div v-for="group in groups" :key="group.key" class="continuation-group" :data-testid="`group-${group.key}`">
        <h4 class="group-title" :class="group.key">
          {{ group.title }}（{{ group.lines.length }}）
        </h4>
        <table v-if="group.lines.length" class="data-table continuation-table">
          <thead>
            <tr>
              <th>原批次编号</th>
              <th>订阅方</th>
              <th>分发状态</th>
              <th>回执/确认</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="line in group.lines" :key="line.dist_id" :data-testid="`line-${line.dist_id}`">
              <td class="batch-no">{{ line.分发批次 }}</td>
              <td>{{ line.订阅方 }}</td>
              <td>{{ line.分发状态 }}<span v-if="line.尝试次数 > 1" class="attempt">（第{{ line.尝试次数 }}次）</span></td>
              <td>
                <span v-if="line.receipt.has_receipt">
                  {{ line.receipt.receipt_id }} · {{ line.receipt.confirmer || '待确认方' }}
                  <em v-if="line.receipt.verified" class="badge ok">已核对</em>
                  <em v-else class="badge warn">待核对</em>
                </span>
                <span v-else class="muted">暂无回执</span>
              </td>
              <td>{{ describe(line) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty-state">暂无该类安排</p>
      </div>

      <div v-if="detail.missing_subscribers.length" class="continuation-group">
        <h4 class="group-title pending-dispatch">资格内尚未建单（{{ detail.missing_subscribers.length }}）</h4>
        <p class="missing-list">{{ detail.missing_subscribers.join('、') }}</p>
      </div>

      <footer class="continuation-foot">
        <span v-if="feedback" class="feedback" data-testid="continuation-feedback">{{ feedback }}</span>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="continuation-refresh" :disabled="loading" @click="refresh">
            {{ loading ? '核对中…' : '重新核对送达结果' }}
          </button>
          <button
            class="btn primary"
            type="button"
            data-testid="continuation-submit"
            :disabled="loading || !hasPendingWork"
            @click="submitContinue"
          >
            批量续派（仅 {{ detail.counts.pending_dispatch + detail.counts.missing }} 单）
          </button>
        </div>
      </footer>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import { continuePublish, fetchContinuation } from '@/domain/golden_publish-api'
import type { ContinuationDetail, ContinuationLine } from '@/domain/golden_publish'

const props = defineProps<{ detail: ContinuationDetail }>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'changed'): void
}>()

const loading = ref(false)
const feedback = ref('')

const groups = computed(() => [
  { key: 'delivered', title: '本次已送达', lines: props.detail.delivered },
  { key: 'pending_verify', title: '送达待确认（回执待核对，不重发数据）', lines: props.detail.pending_verify },
  { key: 'pending_dispatch', title: '仍需派送（队列可继续领取原批次）', lines: props.detail.pending_dispatch },
])

const hasPendingWork = computed(
  () => props.detail.counts.pending_dispatch + props.detail.counts.missing > 0,
)

function describe(line: ContinuationLine): string {
  if (line.分发状态 === '已送达') {
    return line.receipt.verified
      ? `送达确认方：${line.receipt.confirmer || '分发执行端'}，回执已核对`
      : '分发端已确认送达，发布端回执核对仍未完成'
  }
  if (line.分发状态 === '分发失败') return `前次失败保留：${line.失败原因 || '未注明原因'}，续派沿用原批次`
  if (line.分发状态 === '分发中') return '队列已领取，等待送达确认'
  return '排队中，分发任务队列可领取'
}

async function refresh() {
  loading.value = true
  feedback.value = ''
  try {
    const latest = await fetchContinuation(props.detail.publish_id)
    Object.assign(props.detail, latest)
    feedback.value = '已按分发端真实送达与回执状态重新核对'
  } catch (error) {
    feedback.value = error instanceof Error ? error.message : '核对失败'
  } finally {
    loading.value = false
  }
}

async function submitContinue() {
  loading.value = true
  feedback.value = ''
  try {
    const result = await continuePublish(props.detail.publish_id)
    Object.assign(props.detail, result.detail)
    feedback.value = result.message
    emit('changed')
  } catch (error) {
    feedback.value = error instanceof Error ? error.message : '续派失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.continuation-modal {
  width: min(920px, 100%);
}
.continuation-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}
.continuation-head h3 {
  margin: 0 0 4px;
}
.continuation-group {
  margin-top: 14px;
}
.group-title {
  margin: 0 0 6px;
  font-size: 14px;
}
.group-title.delivered {
  color: #15803d;
}
.group-title.pending_verify {
  color: #b45309;
}
.group-title.pending-dispatch {
  color: #1d4ed8;
}
.continuation-table {
  font-size: 12px;
}
.batch-no {
  font-family: monospace;
}
.attempt {
  color: var(--muted);
  font-style: normal;
  font-size: 12px;
}
.badge {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 10px;
  font-style: normal;
  font-size: 11px;
}
.badge.ok {
  background: #dcfce7;
  color: #15803d;
}
.badge.warn {
  background: #fef3c7;
  color: #b45309;
}
.muted {
  color: var(--muted);
}
.missing-list {
  margin: 4px 0;
  color: #1d4ed8;
}
.continuation-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 16px;
  gap: 12px;
}
.feedback {
  font-size: 12px;
  color: var(--muted);
}
.stat-value.delivered {
  color: #15803d;
}
.stat-value.pending-verify {
  color: #b45309;
}
.stat-value.pending-dispatch {
  color: #1d4ed8;
}
</style>
