"""可控时钟：运行时使用系统时钟，测试使用可推进的手工时钟。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class SystemClock:
    """生产环境时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """测试时钟，可显式推进或重置。"""

    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> datetime:
        self._now = self._now + timedelta(**kwargs)
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value
