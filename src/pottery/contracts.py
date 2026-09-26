"""作品流转记录使用的领域常量。"""

from enum import StrEnum
import re


class Role(StrEnum):
    """平台角色，决定 HTTP 接口的访问边界。"""

    COORDINATOR = "coordinator"  # 课程负责人：配置工艺版本与安全前置
    TEACHER = "teacher"          # 教师：记录作品、签认、复核、排批
    MASTER = "master"            # 非遗传承人：工艺版本维护与质检
    GUARDIAN = "guardian"        # 监护人：授予/撤回展示授权
    PUBLIC = "public"            # 匿名访客：只读公开视图


class CraftStep(StrEnum):
    """作品制作过程中的标准工序。"""

    WEDGING = "wedging"
    THROWING = "throwing"
    TRIMMING = "trimming"
    GLAZING = "glazing"
    FIRING = "firing"


# 工序顺序：后一道必须建立在前一道完成的基础上，不得越级。
STEP_ORDER = (
    CraftStep.WEDGING,
    CraftStep.THROWING,
    CraftStep.TRIMMING,
    CraftStep.GLAZING,
    CraftStep.FIRING,
)


class MaterialKind(StrEnum):
    """材料种类。釉料必须与作品适用釉类相容方可使用。"""

    CLAY = "clay"
    GLAZE = "glaze"


class WorkStatus(StrEnum):
    """作品生命周期状态。"""

    IN_PROGRESS = "in_progress"      # 制作中
    AWAITING_REVIEW = "awaiting_review"  # 待教师复核
    READY = "ready"                  # 复核通过，可排批
    SCHEDULED = "scheduled"          # 已排入窑次
    FIRED = "fired"                  # 烧制完成
    QC_FAILED = "qc_failed"          # 质检失败待处置
    REWORK = "rework"                # 返工中
    FINISHED = "finished"            # 终态合格
    DISCARDED = "discarded"          # 报废终态


class KilnStatus(StrEnum):
    """窑次状态。"""

    PLANNED = "planned"      # 接受排批
    FIRING = "firing"        # 已点火，不再接受调整
    DONE = "done"            # 烧制完成待质检
    QC_PASSED = "qc_passed"  # 质检通过
    CANCELED = "canceled"    # 窑炉取消（保留全部证据）
    QC_FAILED = "qc_failed"  # 质检失败（保留证据，逐件处置）


class WorkDisposition(StrEnum):
    """异常作品可采用的后续处置。"""

    REWORK = "rework"
    DISCARD = "discard"
    RESCHEDULE = "reschedule"


def validate_batch_code(value: str) -> str:
    """确保烧制批次编号便于人工抄录。"""

    if not re.fullmatch(r"KILN-\d{4}-\d{3}", value):
        raise ValueError("烧制批次编号格式不正确")
    return value
