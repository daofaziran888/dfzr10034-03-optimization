import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ── 质量越界区间 ────────────────────────────────────────────
// 通用资源路由只按模块 list/create/action，质检的测点、班次和报文编号
// 走 /api/quality_rule 下的专用接口。

export type QualityObservation = {
  id: number
  观测编号: string
  规则版本: string
  测点编号: string
  观测值: number
  观测时间: string
  班次: string
  报文编号: string
  是否越界: boolean
  区间编号: string
}

export type QualityInterval = {
  id: number
  区间编号: string
  规则编号: string
  规则版本: string
  测点编号: string
  状态: string
  阈值下限: string
  阈值上限: string
  首次越界值: number | null
  首次越界时间: string
  最新越界值: number | null
  最新越界时间: string
  越界次数: number
  越界测点数: number
  观测明细: {
    序号: number
    测点编号: string
    观测值: number
    观测时间: string
    班次: string
    规则版本: string
  }[]
  复测记录: {
    序号: number
    测点编号?: string
    复测值: number
    复测时间: string
    班次: string
    复测结论: string
  }[]
  最新复测值: number | string
  最新复测时间: string
  最新复测结论: string
  工单ID: number | null
  工单编号: string
  处理人: string
  复测负责人: string
}

export type QualityIssueDetail = {
  工单ID: number
  工单编号: string
  关联资产: string
  测点编号: string
  越界测点: string[]
  工单状态: string
  处理人: string
  首次越界值: number | null
  最新复测值: number | string
  最新复测时间: string
  最新复测结论: string
  修复方案: string
  修复时间: string
  仍待复测区间数: number
  区间: QualityInterval[]
}

export async function listQualityIntervals(params: Record<string, string> = {}): Promise<QualityInterval[]> {
  const query = new URLSearchParams(params).toString()
  const response = await request(`/api/quality_rule/intervals?${query}`)
  if (!response.ok) throw new Error('越界区间读取失败')
  const payload = (await response.json()) as { items: QualityInterval[] }
  return payload.items
}

export async function listQualityObservations(ruleId: number): Promise<QualityObservation[]> {
  const response = await request(`/api/quality_rule/observations?rule_id=${ruleId}`)
  if (!response.ok) throw new Error('观测明细读取失败')
  const payload = (await response.json()) as { items: QualityObservation[] }
  return payload.items
}

export async function loadRuleVersions(ruleId: number): Promise<{
  当前版本: string
  版本历史: { 版本: string; 阈值下限: string; 阈值上限: string; 生效时间: string; 变更原因: string }[]
}> {
  const response = await request(`/api/quality_rule/versions/${ruleId}`)
  if (!response.ok) throw new Error('规则版本读取失败')
  return await response.json()
}

export async function runQualityCheck(ruleId: number, payload: Record<string, unknown>): Promise<{
  ok: boolean
  breached: boolean
  duplicate: boolean
  interval_no?: string
  interval_first_value?: number
  interval_latest_value?: number
  latest_retest_value?: number | string
  message?: string
  breach_detail?: string
}> {
  const response = await request(`/api/quality_rule/${ruleId}/checks`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error('质量检查提交失败')
  return await response.json()
}

export async function loadIssueQuality(issueId: number): Promise<QualityIssueDetail> {
  const response = await request(`/api/quality_rule/issues/${issueId}`)
  if (!response.ok) throw new Error('工单质检明细读取失败')
  return await response.json()
}

export async function writeQualityConclusion(
  intervalId: number,
  conclusion: string,
  close = false,
): Promise<{ ok: boolean; message: string }> {
  const response = await request(`/api/quality_rule/intervals/${intervalId}/conclusion`, {
    method: 'POST',
    body: JSON.stringify({ conclusion, close }),
  })
  if (!response.ok) throw new Error('质检结论回写失败')
  return await response.json()
}
