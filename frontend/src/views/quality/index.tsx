import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/quality'
const columns = [
  "规则编号",
  "表名称",
  "校验字段",
  "校验类型",
  "阈值上界",
  "阈值下界",
  "阻断级别",
  "最近校验",
  "阈值版本",
  "校验待办",
  "状态"
]
// 状态单向推进：草稿 → 运行中 → 已暂停 → 已归档，页面只露出当前状态允许的动作
const ACTIONS_BY_STATUS: Record<string, string[]> = {
  '草稿': ['启动校验'],
  '运行中': ['启动校验', '暂停校验'],
  '已暂停': ['归档规则'],
  '已归档': [],
}
const CREATE_FIELDS = ["规则编号", "表名称", "校验字段", "校验类型", "阈值上界", "阈值下界", "阻断级别"]
const stats = [
  {
    "label": "质量规则总数",
    "value": 0
  },
  {
    "label": "运行中规则",
    "value": 0
  },
  {
    "label": "已暂停规则",
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

export default function QualityView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [showCreate, setShowCreate] = useState(false)
  const [createValues, setCreateValues] = useState<Record<string, string>>({ 阻断级别: '阻断' })
  const [resultFor, setResultFor] = useState<Row | null>(null)
  const [resultValues, setResultValues] = useState<Record<string, string>>({ 校验值: '', 校验结论: '' })
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('质量规则列表读取失败')
      }
      const payload = await response.json()
      const items = (payload.items ?? []).map((item: Row) => ({ ...item, 状态: item['状态'] ?? item.status }))
      setRows(items)
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '质量探针列表读取失败')
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
        throw new Error(payload.message ?? '质量探针动作未生效，请稍后重试')
      }
      setNoticeMessage(payload.message ?? `质量规则已${action}`)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '质量探针操作失败')
      await reload()
    }
  }

  async function submitCreate(event: FormEvent) {
    event.preventDefault()
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(ENDPOINT, {
        method: 'POST',
        body: JSON.stringify({ values: createValues }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '质量规则登记失败')
      }
      setNoticeMessage(payload.message ?? '质量规则已登记')
      setShowCreate(false)
      setCreateValues({ 阻断级别: '阻断' })
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '质量规则登记失败')
    }
  }

  async function submitResult(event: FormEvent) {
    event.preventDefault()
    if (!resultFor) return
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${resultFor.id}/results`, {
        method: 'POST',
        body: JSON.stringify({ values: resultValues }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '校验结论登记失败')
      }
      setNoticeMessage(payload.message ?? '校验结论已落库')
      setResultFor(null)
      setResultValues({ 校验值: '', 校验结论: '' })
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '校验结论登记失败')
      await reload()
    }
  }

  return (
    <section className="page" data-module="quality">
      <header className="page-head">
        <div>
          <h2>质量探针管理</h2>
          <p className="page-desc">维护质量规则的校验类型、阈值设置与阻断策略，为数据质量保障提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setShowCreate((prev) => !prev)}>
            登记质量规则
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出质量探针清单
          </button>
        </div>
      </header>

      {showCreate && (
        <form className="filter-bar" onSubmit={(event) => void submitCreate(event)}>
          {CREATE_FIELDS.map((field) => (
            <label key={field} className="filter-item">
              <span>{field}</span>
              <input
                value={createValues[field] ?? ''}
                placeholder={`请输入${field}`}
                onChange={(event) => setCreateValues((prev) => ({ ...prev, [field]: event.target.value }))}
              />
            </label>
          ))}
          <button className="btn primary" type="submit">保存规则</button>
          <button className="btn ghost" type="button" onClick={() => setShowCreate(false)}>取消</button>
        </form>
      )}

      {resultFor && (
        <form className="filter-bar" onSubmit={(event) => void submitResult(event)}>
          <span>录入 {String(resultFor['规则编号'] ?? '')} 的校验结论（留空结论则按落库阈值自动判定）</span>
          <label className="filter-item">
            <span>校验值</span>
            <input
              value={resultValues['校验值'] ?? ''}
              placeholder="本次校验数值"
              onChange={(event) => setResultValues((prev) => ({ ...prev, 校验值: event.target.value }))}
            />
          </label>
          <label className="filter-item">
            <span>校验结论</span>
            <input
              value={resultValues['校验结论'] ?? ''}
              placeholder="通过 / 失败，可留空"
              onChange={(event) => setResultValues((prev) => ({ ...prev, 校验结论: event.target.value }))}
            />
          </label>
          <button className="btn primary" type="submit">登记结论</button>
          <button className="btn ghost" type="button" onClick={() => setResultFor(null)}>取消</button>
        </form>
      )}

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
                {String(row['状态'] ?? '') === '运行中' && (
                  <button className="link" type="button" onClick={() => setResultFor(row)}>
                    录入校验结果
                  </button>
                )}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无质量探针数据，可先登记质量规则</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条质量探针记录</span>
        {noticeMessage && <span>{noticeMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
