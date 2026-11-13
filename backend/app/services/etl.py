"""ETL任务调度业务规则：状态流转、质量准入与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.quality import apply_block_to_task, table_block_items
from app.store import store

MODULE = "etl"
REQUIRED_FIELDS = ["任务编号", "任务名称", "依赖任务"]
PERSISTED_FIELDS = ["任务编号", "任务名称", "依赖任务", "调度表达式", "重试次数", "超时分钟", "告警渠道", "任务状态"]
STATUS_ORDER = ["草稿", "待处理", "运行中", "已暂停", "已完成"]
# 动作 -> (允许的前置状态, 目标状态)。启动任务前必须通过质量准入。
ACTION_RULES: dict[str, tuple[tuple[str, ...], str]] = {
    "启动任务": (("草稿", "待处理"), "运行中"),
    "暂停任务": (("运行中",), "已暂停"),
    "归档任务": (("已暂停",), "已完成"),
}


def _parse_tables(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").replace("，", ",").replace("、", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


class EtlService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for field in PERSISTED_FIELDS:
            entry[field] = values.get(field)
        entry["依赖表"] = _parse_tables(values.get("依赖表"))
        entry["阻断状态"] = ""
        entry["待处理原因"] = ""
        entry["阻断历史"] = []
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str, bool]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"调度任务 {entry_id} 不存在或已归档", False
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于ETL任务调度可执行范围", False
        sources, target = ACTION_RULES[action]
        current = str(entry.get("status") or STATUS_ORDER[0])
        if current not in sources:
            return None, f"当前状态「{current}」不允许执行「{action}」", False
        if action == "启动任务":
            # 调度准入：依赖表上的运行中质量规则有任何阻断项都不予放行，
            # 阻断状态与待处理原因同步写回调度清单，与质量页口径一致。
            items = table_block_items(entry.get("依赖表") or [])
            if items:
                apply_block_to_task(entry, items)
                return entry, f"质量阻断未解除，任务停在待处理：{entry['待处理原因']}", False
            entry["阻断状态"] = ""
            entry["待处理原因"] = ""
            entry.pop("阻断前状态", None)
            entry["abnormal"] = False
        entry["status"] = target
        entry["pending"] = target in ("草稿", "待处理", "运行中")
        return entry, f"调度任务已{action}", True
