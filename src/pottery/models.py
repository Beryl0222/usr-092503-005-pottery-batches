"""领域模型：角色、状态机与工序顺序。"""

from __future__ import annotations

from enum import StrEnum

from .contracts import CraftStep, WorkDisposition


class Role(StrEnum):
    """平台角色，决定接口可见性。"""

    COURSE_ADMIN = "course_admin"  # 课程负责人：工艺版本与烧制批次
    TEACHER = "teacher"  # 教师：作品、工序签认、复核、保管
    REGISTRAR = "registrar"  # 教务：学籍与转班
    GUARDIAN = "guardian"  # 监护人：展示授权
    INHERITOR = "inheritor"  # 非遗传承人：只读查阅与导出


class WorkStatus(StrEnum):
    IN_PROGRESS = "in_progress"  # 工序进行中
    AWAITING_FIRING = "awaiting_firing"  # 工序与复核齐备，可排批
    SCHEDULED = "scheduled"  # 已进入烧制批次
    FIRED = "fired"  # 烧制完成且质检合格
    REWORK = "rework"  # 返工：需重新复核后才能再排批
    DISCARDED = "discarded"  # 报废：终态


class BatchStatus(StrEnum):
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"  # 烧制失败（窑炉故障等）


# 烧制前必须按序完成的工序；firing 本身由批次完成。
ORDERED_STEPS: tuple[CraftStep, ...] = (
    CraftStep.WEDGING,
    CraftStep.THROWING,
    CraftStep.TRIMMING,
    CraftStep.GLAZING,
)

# 工序签认的干燥时间基准：以施釉签认时刻起算。
DRYING_ANCHOR_STEP = CraftStep.GLAZING

# 终态作品不允许任何后续操作。
TERMINAL_WORK_STATUSES = frozenset({WorkStatus.DISCARDED})

__all__ = [
    "Role",
    "WorkStatus",
    "BatchStatus",
    "ORDERED_STEPS",
    "DRYING_ANCHOR_STEP",
    "TERMINAL_WORK_STATUSES",
    "CraftStep",
    "WorkDisposition",
]
