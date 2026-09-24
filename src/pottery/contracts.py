"""作品流转记录使用的领域常量。"""

from enum import StrEnum
import re


class CraftStep(StrEnum):
    """作品制作过程中的标准工序。"""

    WEDGING = "wedging"
    THROWING = "throwing"
    TRIMMING = "trimming"
    GLAZING = "glazing"
    FIRING = "firing"


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
