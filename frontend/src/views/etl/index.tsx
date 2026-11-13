import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/etl'
const columns = [
  "任务编号",
  "任务名称",
  "依赖任务",
  "依赖表",
  "调度表达式",
  "重试次数",
  "超时分钟",
  "告警渠道",
  "任务状态",
  "阻断状态",
  "待处理原因",
  "状态"
]
// 状态机：草稿/待处理 → 运行中 → 已暂停 → 已完成；待处理任务需先解除质量阻断
const ACTIONS_BY_STATUS: Record<string, string[]> = {
  '草稿': ['启动任务'],
  '待处理': ['启动任务'],
  '运行中': ['暂停任务'],
  '已暂停': ['归档任务'],
  '已完成': [],
}
const stats = [
  {
    "label": "调度任务总数",
    "value": 0
  },
  {
    "label": "运行中任务",
    "value": 0
  },
  {
    "label": "已暂停任务",
    "value": 0
  }
]

type Row = Record<string, unknown>

function formatCell(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (Array.isArray(value)) return value.length ? value.join('、') : '—'
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}

export default function EtlView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('调度任务列表读取失败')
      }
      const payload = await response.json()
      const items = (payload.items ?? []).map((item: Row) => ({ ...item, 状态: item['状态'] ?? item.status }))
      setRows(items)
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'ETL任务调度列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? 'ETL任务调度动作未生效，请稍后重试')
      }
      setNoticeMessage(payload.message ?? `调度任务已${action}`)
      await reload()
    } catch (error) {
      // 质量阻断等原因导致的拒绝也要刷新列表，让待处理原因与质量页保持一致
      setErrorMessage(error instanceof Error ? error.message : 'ETL任务调度操作失败')
      await reload()
    }
  }

  return (
    <section className="page" data-module="etl">
      <header className="page-head">
        <div>
          <h2>ETL任务调度管理</h2>
          <p className="page-desc">维护ETL任务的调度配置、依赖关系与告警策略，为数据加工编排提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('调度任务登记入口尚未接入审批流')}>
            登记调度任务
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出ETL任务调度清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{formatCell(row[column])}</td>)}
              <td className="row-actions">
                {(ACTIONS_BY_STATUS[String(row['状态'] ?? '')] ?? []).map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无ETL任务调度数据，可先登记调度任务</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条ETL任务调度记录</span>
        {noticeMessage && <span>{noticeMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
