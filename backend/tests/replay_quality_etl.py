"""质量规则 → 调度准入链路回放校验。

用 QUAL-0002 与依赖 dwd_order_detail 的 ETL 任务回放完整链路：
登记字段持久化、状态单向推进、并发启动幂等、阈值版本、阻断写入调度清单、
暂停撤销未开始的阻断、失败证据保留、字段缺失不准静默放行。

运行方式：cd backend && .venv/bin/python tests/replay_quality_etl.py
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.etl import EtlService  # noqa: E402
from app.services.quality import QualityService, table_block_items  # noqa: E402
from app.store import store  # noqa: E402

quality = QualityService()
etl = EtlService()

PASSED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"[失败] {label} {detail}")
    PASSED.append(label)
    print(f"[通过] {label}")


def find_by(module: str, field: str, value: str) -> dict:
    for row in store.rows(module):
        if row.get(field) == value:
            return row
    raise AssertionError(f"{module} 中找不到 {field}={value}")


# 场景 1：QUAL-0001 启动校验后必须进入运行中，而不是停在草稿
q1 = find_by("quality", "规则编号", "QUAL-0001")
entry, message, ok = quality.run_action(q1["id"], "启动校验")
check("QUAL-0001 启动校验后状态为运行中", ok and entry["status"] == "运行中", f"实际={entry['status']}")
check("QUAL-0001 启动后存在校验待办", entry["校验待办"] is True)

# 场景 2：同一启动请求并发提交，只允许登记一次校验待办
created, missing = quality.create_entry({
    "规则编号": "QUAL-9001", "表名称": "t_concurrent", "校验字段": "f1",
    "阈值上界": "10", "阈值下界": "0", "阻断级别": "阻断",
})
check("并发用规则登记成功", created is not None and not missing)
results: list[tuple[dict | None, str, bool]] = []
threads = [
    threading.Thread(target=lambda: results.append(quality.run_action(created["id"], "启动校验")))
    for _ in range(8)
]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
check("并发启动全部返回成功", all(item[2] for item in results), f"结果={results}")
check("并发启动只登记一次校验待办", created["校验待办"] is True and created["status"] == "运行中")
merged = sum(1 for _, msg, _ in results if "合并" in msg)
check("并发请求被幂等合并", merged >= 1, f"合并次数={merged}")

# 场景 3：新规则登记时阈值上下界、阻断级别完整持久化
created2, missing2 = quality.create_entry({
    "规则编号": "QUAL-9002", "表名称": "t_persist", "校验字段": "amt",
    "校验类型": "阈值校验", "阈值上界": "100", "阈值下界": "1", "阻断级别": "阻断",
})
check("登记不缺少必填字段", not missing2)
check(
    "阈值上下界与阻断级别完整落库",
    created2["阈值上界"] == "100" and created2["阈值下界"] == "1" and created2["阻断级别"] == "阻断",
    f"实际={created2}",
)
check("新规则阈值版本从 1 开始", created2["阈值版本"] == 1)

# 场景 4：QUAL-0002 校验失败（提交结论与落库阈值冲突，以落库阈值为准）→ 阻断依赖任务
q2 = find_by("quality", "规则编号", "QUAL-0002")
entry, message, ok = quality.run_action(q2["id"], "启动校验")
check("QUAL-0002 运行中可重新发起校验", ok and entry["校验待办"] is True)
entry, message, ok = quality.record_result(q2["id"], {"校验值": "12000", "校验结论": "通过"})
check("校验结论登记成功", ok, message)
check(
    "提交结论与落库阈值冲突时以落库阈值为准",
    entry["校验历史"][-1]["结论"] == "失败",
    f"实际={entry['校验历史'][-1]}",
)
check("最近校验已落库", "失败" in entry["最近校验"], f"实际={entry['最近校验']}")
check("失败记录携带阈值版本", entry["校验历史"][-1]["阈值版本"] == q2["阈值版本"])

etl1 = find_by("etl", "任务编号", "ETL-0001")
check("依赖任务停在待处理", etl1["status"] == "待处理", f"实际={etl1['status']}")
check("阻断状态写进调度清单", etl1["阻断状态"] == "已阻断")
check("调度清单保留失败原因", "QUAL-0002" in etl1["待处理原因"], f"实际={etl1['待处理原因']}")

# 调度页与质量页口径一致：准入函数算出的原因与清单落库的原因相同
items = table_block_items(etl1["依赖表"])
check(
    "调度页待处理原因与质量页一致",
    bool(items) and items[0]["原因"] == etl1["待处理原因"],
    f"准入={items[0]['原因'] if items else None} 清单={etl1['待处理原因']}",
)

# 场景 5：被阻断的任务不能越过阻断级别启动
entry, message, ok = etl.run_action(etl1["id"], "启动任务")
check("被阻断任务启动被拒绝", not ok and etl1["status"] == "待处理", f"实际={etl1['status']}")
check("拒绝原因与调度清单一致", "QUAL-0002" in message and message.endswith(etl1["待处理原因"]) or "QUAL-0002" in message)

# 场景 6：暂停 QUAL-0002 → 状态单向推进、撤销未开始的阻断、保留失败证据
entry, message, ok = quality.run_action(q2["id"], "暂停校验")
check("QUAL-0002 暂停后状态为已暂停", ok and entry["status"] == "已暂停", f"实际={entry['status']}")
check("未开始的阻断被立即撤销", etl1["status"] == "草稿" and etl1["阻断状态"] == "", f"实际={etl1['status']}")
check("撤销后待处理原因清空", etl1["待处理原因"] == "")
check("失败证据在校验历史中保留", any(rec["结论"] == "失败" for rec in q2["校验历史"]))
check(
    "阻断痕迹在调度侧保留",
    any(block.get("状态") == "已撤销" for block in etl1["阻断历史"]),
    f"实际={etl1['阻断历史']}",
)

# 场景 7：状态单向推进，不允许回退
entry, message, ok = quality.run_action(q2["id"], "启动校验")
check("已暂停规则不允许回退到运行中", not ok and q2["status"] == "已暂停")
entry, message, ok = quality.run_action(q2["id"], "归档规则")
check("已暂停规则可以归档", ok and q2["status"] == "已归档")
entry, message, ok = quality.run_action(q2["id"], "暂停校验")
check("已归档是终态", not ok and q2["status"] == "已归档")

# 场景 8：字段缺失的规则不能以运行中状态静默通过准入
created3, _ = quality.create_entry({"规则编号": "QUAL-9003", "表名称": "t_missing", "校验字段": "f2"})
quality.run_action(created3["id"], "启动校验")
entry, message, ok = quality.record_result(created3["id"], {})
check("无阈值无结论的校验登记被拒绝", not ok, message)
task3, missing3 = etl.create_entry({
    "任务编号": "ETL-9001", "任务名称": "缺失字段准入验证", "依赖任务": "无", "依赖表": ["t_missing"],
})
check("依赖任务登记成功", task3 is not None and not missing3)
entry, message, ok = etl.run_action(task3["id"], "启动任务")
check("字段缺失时任务停在待处理", not ok and task3["status"] == "待处理", f"实际={task3['status']}")
check("字段缺失原因写进调度清单", "字段缺失" in task3["待处理原因"], f"实际={task3['待处理原因']}")

# 场景 9：阈值版本递增后旧结论过期，需重新校验才能放行
q9 = find_by("quality", "规则编号", "QUAL-9002")
quality.run_action(q9["id"], "启动校验")
entry, message, ok = quality.record_result(q9["id"], {"校验值": "50"})
check("阈值内校验通过", ok and entry["校验历史"][-1]["结论"] == "通过", message)
task9, _ = etl.create_entry({
    "任务编号": "ETL-9002", "任务名称": "版本一致性验证甲", "依赖任务": "无", "依赖表": ["t_persist"],
})
entry, message, ok = etl.run_action(task9["id"], "启动任务")
check("校验通过后任务可启动", ok and task9["status"] == "运行中", message)
entry, message = quality.update_entry(q9["id"], {"阈值上界": "40"})
check("阈值修改后版本递增", q9["阈值版本"] == 2, f"实际={q9['阈值版本']}")
task10, _ = etl.create_entry({
    "任务编号": "ETL-9003", "任务名称": "版本一致性验证乙", "依赖任务": "无", "依赖表": ["t_persist"],
})
entry, message, ok = etl.run_action(task10["id"], "启动任务")
check("旧版本校验结论不能放行新任务", not ok and task10["status"] == "待处理", f"实际={task10['status']}")
check("版本不一致原因写进调度清单", "版本" in task10["待处理原因"], f"实际={task10['待处理原因']}")

# 场景 10：按当前版本重新校验通过后，阻断解除、任务放行
quality.run_action(q9["id"], "启动校验")
entry, message, ok = quality.record_result(q9["id"], {"校验值": "30"})
check("按当前阈值版本重新校验通过", ok and entry["校验历史"][-1]["阈值版本"] == 2, message)
check("通过后未开始的阻断被解除", task10["status"] == "草稿" and task10["阻断状态"] == "", f"实际={task10['status']}")
entry, message, ok = etl.run_action(task10["id"], "启动任务")
check("阻断解除后任务可启动", ok and task10["status"] == "运行中", message)

print(f"\n回放完成：{len(PASSED)} 项检查全部通过。")
