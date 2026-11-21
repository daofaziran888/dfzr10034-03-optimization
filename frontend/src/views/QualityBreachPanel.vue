<template>
  <section class="quality-panel" data-testid="quality-breach-panel">
    <header class="panel-head">
      <div>
        <h3>越界区间台账</h3>
        <p class="panel-desc">
          同一规则同一版本的测点观测按顺序归并；区间验证前再次越界继续并入原区间，阈值调整后按版本分别统计。
        </p>
      </div>
      <label class="panel-toggle">
        <input v-model="pendingOnly" type="checkbox" data-testid="interval-pending-only" @change="reload" />
        只看待复测
      </label>
    </header>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <table class="data-table">
      <thead>
        <tr>
          <th>区间编号</th>
          <th>规则 / 版本</th>
          <th>测点</th>
          <th>首次越界值</th>
          <th>最新越界值</th>
          <th>越界次数</th>
          <th>最新复测</th>
          <th>工单</th>
          <th>状态</th>
          <th>处置</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="interval in intervals" :key="String(interval.id)">
          <tr :data-testid="`interval-${interval.id}`" :data-status="interval.状态">
            <td>{{ interval.区间编号 }}</td>
            <td>{{ interval.规则编号 }} / v{{ interval.规则版本 }}</td>
            <td>{{ interval.测点编号 }}（{{ interval.越界测点数 }} 个）</td>
            <td>
              {{ interval.首次越界值 ?? '—' }}
              <small class="cell-note">{{ interval.首次越界时间 }}</small>
            </td>
            <td>
              {{ interval.最新越界值 ?? '—' }}
              <small class="cell-note">{{ interval.最新越界时间 }}</small>
            </td>
            <td>{{ interval.越界次数 }}</td>
            <td>
              {{ interval.最新复测值 === '' ? '—' : interval.最新复测值 }}
              <small class="cell-note">{{ interval.最新复测时间 || '暂无复测' }}</small>
            </td>
            <td>{{ interval.工单编号 || '—' }}</td>
            <td>{{ interval.状态 }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`interval-detail-${interval.id}`" @click="toggle(interval)">
                {{ expandedId === interval.id ? '收起明细' : '查看明细' }}
              </button>
            </td>
          </tr>
          <tr v-if="expandedId === interval.id">
            <td colspan="10" class="interval-detail">
              <div class="detail-grid">
                <section>
                  <h4>越界观测（按观测顺序）</h4>
                  <ul class="detail-list">
                    <li v-for="item in interval.观测明细" :key="item.序号">
                      #{{ item.序号 }} {{ item.测点编号 }} = {{ item.观测值 }}
                      （{{ item.班次 || '未标注班次' }} {{ item.观测时间 }}，v{{ item.规则版本 }}）
                    </li>
                  </ul>
                </section>
                <section>
                  <h4>复测记录</h4>
                  <ul v-if="interval.复测记录.length" class="detail-list">
                    <li v-for="item in interval.复测记录" :key="item.序号">
                      {{ item.复测值 }}（{{ item.班次 || '未标注班次' }} {{ item.复测时间 }}）：{{ item.复测结论 }}
                    </li>
                  </ul>
                  <p v-else class="cell-note">尚未登记复测</p>
                </section>
              </div>
              <form class="conclusion-form" @submit.prevent="submitConclusion(interval)">
                <input
                  v-model="conclusions[interval.id]"
                  :data-testid="`conclusion-input-${interval.id}`"
                  placeholder="填写质检结论，将追加回写工单待办"
                />
                <label class="panel-toggle">
                  <input v-model="closeAfterWrite[interval.id]" type="checkbox" />
                  同时确认复测通过
                </label>
                <button class="btn primary" type="submit" :data-testid="`conclusion-submit-${interval.id}`">
                  回写工单
                </button>
              </form>
            </td>
          </tr>
        </template>
        <tr v-if="!intervals.length">
          <td colspan="10" class="empty-state">暂无越界区间，登记观测后会在这里按规则版本归并</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  listQualityIntervals,
  writeQualityConclusion,
  type QualityInterval,
} from '@/api/backend-service'

const intervals = ref<QualityInterval[]>([])
const pendingOnly = ref(false)
const expandedId = ref<number | null>(null)
const conclusions = ref<Record<number, string>>({})
const closeAfterWrite = ref<Record<number, boolean>>({})
const errorMessage = ref('')

async function reload() {
  errorMessage.value = ''
  try {
    const params: Record<string, string> = {}
    if (pendingOnly.value) params.pending = 'true'
    intervals.value = await listQualityIntervals(params)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '越界区间读取失败'
  }
}

function toggle(interval: QualityInterval) {
  expandedId.value = expandedId.value === interval.id ? null : interval.id
}

async function submitConclusion(interval: QualityInterval) {
  const conclusion = (conclusions.value[interval.id] ?? '').trim()
  if (!conclusion) {
    errorMessage.value = '请先填写质检结论'
    return
  }
  try {
    await writeQualityConclusion(interval.id, conclusion, Boolean(closeAfterWrite.value[interval.id]))
    conclusions.value[interval.id] = ''
    closeAfterWrite.value[interval.id] = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '质检结论回写失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.quality-panel { margin-top: 20px; }
.panel-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px; }
.panel-head h3 { margin: 0; font-size: 15px; }
.panel-desc { color: var(--muted); font-size: 12px; margin: 4px 0 0; }
.panel-toggle { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
.cell-note { display: block; color: var(--muted); font-size: 11px; }
.interval-detail { background: #fafbfc; }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.detail-grid h4 { margin: 0 0 6px; font-size: 13px; }
.detail-list { margin: 0; padding-left: 18px; font-size: 12px; line-height: 1.7; }
.conclusion-form { display: flex; gap: 8px; align-items: center; margin-top: 10px; }
.conclusion-form input[type='text'], .conclusion-form input:not([type]) { flex: 1; border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; }
</style>
