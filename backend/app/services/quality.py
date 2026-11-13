"""质量探针业务规则：状态流转、字段校验、阈值版本与 ETL 阻断联动都收在这里。"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "quality"
ETL_MODULE = "etl"

REQUIRED_FIELDS = ["规则编号", "表名称", "校验字段"]
PERSISTED_FIELDS = [
    "规则编号", "表名称", "校验字段", "校验类型",
    "阈值上界", "阈值下界", "阻断级别", "最近校验",
]
THRESHOLD_FIELDS = ["阈值上界", "阈值下界", "阻断级别"]
EDITABLE_FIELDS = ["校验字段", "校验类型", "阈值上界", "阈值下界", "阻断级别"]
STATUS_ORDER = ["草稿", "运行中", "已暂停", "已归档"]
BLOCK_LEVEL = "阻断"

# 动作 -> (允许的前置状态, 目标状态)。状态只能沿 STATUS_ORDER 单向推进，不允许回退。
ACTION_RULES: dict[str, tuple[tuple[str, ...], str]] = {
    "启动校验": (("草稿", "运行中"), "运行中"),
    "暂停校验": (("运行中",), "已暂停"),
    "归档规则": (("已暂停",), "已归档"),
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def block_reason_text(rule: dict[str, Any], record: dict[str, Any]) -> str:
    """阻断原因的唯一拼装入口：质量页与调度页看到的待处理原因必须一致。"""
    detail = record.get("说明") or "校验未通过"
    return (
        f"质量规则{rule.get('规则编号')}校验{record.get('结论')}"
        f"（表{rule.get('表名称')}，阻断级别{rule.get('阻断级别')}，"
        f"阈值版本v{record.get('阈值版本')}）：{detail}"
    )


def table_block_items(tables: list[str]) -> list[dict[str, Any]]:
    """调度准入口径：逐表核对运行中的质量规则，返回阻断项列表（空列表表示放行）。

    阈值、阻断级别、阈值版本任一缺失，或校验结论缺失/版本过旧，都视为不可放行，
    不允许以运行中状态静默通过。
    """
    items: list[dict[str, Any]] = []
    for table in tables:
        for rule in store.rows(MODULE):
            if rule.get("表名称") != table or rule.get("status") != "运行中":
                continue
            code = rule.get("规则编号")
            version = rule.get("阈值版本")
            missing = [f for f in THRESHOLD_FIELDS if not str(rule.get(f) or "").strip()]
            if not isinstance(version, int):
                missing.append("阈值版本")
            if missing:
                items.append({
                    "规则编号": code, "阈值版本": version or 0,
                    "原因": f"质量规则{code}字段缺失（{'、'.join(missing)}），调度不予放行",
                })
                continue
            if rule.get("校验待办"):
                items.append({
                    "规则编号": code, "阈值版本": version,
                    "原因": f"质量规则{code}存在未完成的校验待办，调度不予放行",
                })
                continue
            history = rule.get("校验历史") or []
            if not history:
                items.append({
                    "规则编号": code, "阈值版本": version,
                    "原因": f"质量规则{code}尚无阈值版本v{version}的校验结论，调度不予放行",
                })
                continue
            latest = history[-1]
            if latest.get("阈值版本") != version:
                items.append({
                    "规则编号": code, "阈值版本": version,
                    "原因": (
                        f"质量规则{code}最新校验基于阈值版本v{latest.get('阈值版本')}，"
                        f"与当前版本v{version}不一致，需重新校验"
                    ),
                })
                continue
            if latest.get("结论") == "失败" and rule.get("阻断级别") == BLOCK_LEVEL:
                items.append({
                    "规则编号": code, "阈值版本": version,
                    "原因": block_reason_text(rule, latest),
                })
    return items


def apply_block_to_task(task: dict[str, Any], items: list[dict[str, Any]]) -> None:
    """把阻断写进调度清单：任务停在待处理并保留失败原因，阻断历史只增不删。"""
    if not items:
        return
    history = task.setdefault("阻断历史", [])
    for item in items:
        duplicated = any(
            block.get("规则编号") == item["规则编号"]
            and block.get("阈值版本") == item["阈值版本"]
            and block.get("状态") == "生效中"
            for block in history
        )
        if not duplicated:
            history.append({**item, "时间": _now(), "状态": "生效中"})
    active = [block for block in history if block.get("状态") == "生效中"]
    task["阻断状态"] = "已阻断"
    task["待处理原因"] = "；".join(block["原因"] for block in active)
    task["abnormal"] = True
    if task.get("status") != "运行中":
        if task.get("status") != "待处理":
            task["阻断前状态"] = task.get("status") or "草稿"
        task["status"] = "待处理"
        task["pending"] = True


class QualityService:
    """质量规则从登记、启停到校验结论落库的处理链路。"""

    _lock = threading.Lock()  # 启动校验幂等控制：并发提交只允许登记一次校验待办

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
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
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
        # 阈值上下界、阻断级别等字段必须随登记完整落库，不允许静默丢弃
        for field in PERSISTED_FIELDS:
            entry[field] = values.get(field)
        entry["最近校验"] = entry["最近校验"] or ""
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["阈值版本"] = 1
        entry["校验待办"] = False
        entry["校验历史"] = []
        rows.append(entry)
        return entry, []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """修改阈值类字段：阈值变化时版本号递增，旧版本的校验结论随即过期。"""
        with self._lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"质量规则 {entry_id} 不存在或已归档"
            changed = False
            for field in EDITABLE_FIELDS:
                if field not in values or values[field] is None:
                    continue
                if field in THRESHOLD_FIELDS and str(values[field]) != str(entry.get(field) or ""):
                    changed = True
                entry[field] = values[field]
            if changed:
                entry["阈值版本"] = int(entry.get("阈值版本") or 0) + 1
            suffix = "，阈值版本已递增，旧校验结论需重验" if changed else ""
            return entry, f"质量规则已更新{suffix}"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str, bool]:
        with self._lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"质量规则 {entry_id} 不存在或已归档", False
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于质量探针可执行范围", False
            sources, target = ACTION_RULES[action]
            current = str(entry.get("status") or STATUS_ORDER[0])
            if current not in sources:
                return None, (
                    f"当前状态「{current}」不允许执行「{action}」，"
                    f"规则只能按{'→'.join(STATUS_ORDER)}单向推进"
                ), False
            if STATUS_ORDER.index(target) < STATUS_ORDER.index(current):
                return None, f"状态不允许从「{current}」回退到「{target}」", False
            if action == "启动校验":
                if entry.get("校验待办"):
                    # 幂等合并：同一规则的并发启动请求只保留一次校验待办
                    return entry, "校验待办已存在，本次启动请求已合并，不重复登记", True
                entry["校验待办"] = True
            if action in ("暂停校验", "归档规则"):
                entry["校验待办"] = False
            entry["status"] = target
            entry["pending"] = target in ("草稿", "运行中")
            if action in ("暂停校验", "归档规则"):
                revoked = self._revoke_blocks(entry, "已撤销")
                suffix = f"，已撤销{revoked}条未开始的阻断" if revoked else ""
                return entry, f"质量规则已{action}{suffix}", True
            return entry, f"质量规则已{action}", True

    def record_result(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, bool]:
        """登记一次校验结论：落库最近校验与校验历史，并按阻断级别联动调度清单。"""
        with self._lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"质量规则 {entry_id} 不存在或已归档", False
            if entry.get("status") != "运行中":
                return None, f"当前状态「{entry.get('status')}」不能登记校验结论，请先启动校验", False
            if not entry.get("校验待办"):
                return None, "当前没有待完成的校验待办，请先启动校验", False
            conclusion, detail = self._judge(entry, values)
            if conclusion is None:
                return None, detail, False
            record = {
                "时间": _now(),
                "结论": conclusion,
                "校验值": values.get("校验值"),
                "阈值上界": entry.get("阈值上界"),
                "阈值下界": entry.get("阈值下界"),
                "阻断级别": entry.get("阻断级别"),
                "阈值版本": entry.get("阈值版本"),
                "说明": detail,
            }
            # 校验历史只增不删：失败证据不因后续启停、归档而丢失
            entry.setdefault("校验历史", []).append(record)
            entry["最近校验"] = f"{record['时间']} {conclusion}"
            entry["校验待办"] = False
            entry["abnormal"] = conclusion == "失败"
            if conclusion == "通过":
                lifted = self._revoke_blocks(entry, "已解除")
                suffix = f"，已解除{lifted}条阻断" if lifted else ""
                return entry, f"校验通过，结论已按阈值版本v{record['阈值版本']}落库{suffix}", True
            if entry.get("阻断级别") == BLOCK_LEVEL:
                blocked = self._apply_blocks(entry, record)
                return entry, f"校验失败且达到阻断级别，已阻断{blocked}个依赖调度任务", True
            return entry, "校验失败，未达阻断级别，结论已留档", True

    def _judge(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[str | None, str]:
        """判定校验结论。提交结论与落库阈值冲突时，以已落库且版本完整的阈值一侧为准。"""
        upper = _to_float(entry.get("阈值上界"))
        lower = _to_float(entry.get("阈值下界"))
        value = _to_float(values.get("校验值"))
        submitted = str(values.get("校验结论") or "").strip()
        computed: tuple[str, str] | None = None
        if value is not None and (upper is not None or lower is not None):
            if upper is not None and value > upper:
                computed = ("失败", f"校验值{value:g}超出阈值上界{upper:g}")
            elif lower is not None and value < lower:
                computed = ("失败", f"校验值{value:g}低于阈值下界{lower:g}")
            else:
                computed = ("通过", f"校验值{value:g}处于阈值区间内")
        if computed is not None:
            conclusion, detail = computed
            if submitted and submitted != conclusion:
                detail += "；提交结论与落库阈值判定冲突，已按落库阈值为准"
            return conclusion, detail
        if submitted in ("通过", "失败"):
            return submitted, "人工登记结论（无可用阈值自动判定）"
        return None, "缺少校验结论且阈值字段不完整，无法判定，请补齐阈值或结论后重试"

    def _apply_blocks(self, entry: dict[str, Any], record: dict[str, Any]) -> int:
        item = {
            "规则编号": entry.get("规则编号"),
            "阈值版本": record.get("阈值版本"),
            "原因": block_reason_text(entry, record),
        }
        count = 0
        for task in store.rows(ETL_MODULE):
            if entry.get("表名称") in (task.get("依赖表") or []):
                apply_block_to_task(task, [item])
                count += 1
        return count

    def _revoke_blocks(self, entry: dict[str, Any], mode: str) -> int:
        """撤销未开始的阻断；已产生的失败证据（校验历史、阻断历史）一律保留。"""
        count = 0
        for task in store.rows(ETL_MODULE):
            history = task.get("阻断历史") or []
            active = [
                block for block in history
                if block.get("规则编号") == entry.get("规则编号") and block.get("状态") == "生效中"
            ]
            if not active:
                continue
            if task.get("status") == "运行中":
                continue  # 任务已开始执行，阻断不撤销，证据保留
            for block in active:
                block["状态"] = mode
                block["撤销时间"] = _now()
                count += 1
            remaining = [block for block in history if block.get("状态") == "生效中"]
            if task.get("status") == "待处理" and not remaining:
                task["status"] = task.pop("阻断前状态", "") or "草稿"
                task["阻断状态"] = ""
                task["待处理原因"] = ""
                task["abnormal"] = False
                task["pending"] = task["status"] in ("草稿", "运行中")
            elif remaining:
                task["待处理原因"] = "；".join(block["原因"] for block in remaining)
        return count
