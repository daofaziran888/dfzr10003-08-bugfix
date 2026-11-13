"""质量探针接口：维护质量规则，覆盖登记、启停、校验结论登记与阈值版本管理。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.quality import QualityService

router = APIRouter(prefix="/api/quality", tags=["质量探针"])

service = QualityService()

LIST_FIELDS = ["规则编号", "表名称", "校验字段", "校验类型", "阈值上界", "阈值下界", "阻断级别", "最近校验", "阈值版本"]
STATUSES = ["草稿", "运行中", "已暂停", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已暂停、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤质量探针列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出质量探针清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "quality", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条质量规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"质量规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条质量规则，阈值上下界与阻断级别随登记完整落库；缺必填字段时说明原因。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="质量规则已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改阈值上下界、阻断级别等字段；阈值变化会让阈值版本递增，旧校验结论随即过期。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条质量规则执行启动校验、暂停校验、归档规则；状态只按草稿→运行中→已暂停→已归档单向推进。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message, ok = service.run_action(entry_id, action)
    return ActionResult(ok=ok, message=message, entry=entry)


@router.post("/{entry_id}/results", response_model=ActionResult)
def record_result(entry_id: int, payload: EntryPayload) -> ActionResult:
    """登记一次校验结论：落库最近校验与校验历史，达到阻断级别时同步阻断依赖该表的调度任务。"""
    entry, message, ok = service.record_result(entry_id, payload.values)
    return ActionResult(ok=ok, message=message, entry=entry)
