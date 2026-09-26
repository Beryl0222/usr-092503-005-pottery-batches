"""可控时钟：测试中可冻结、快进，生产中读取真实时间。

所有时间均为感知 UTC 时间，以 ISO-8601 字符串持久化。
"""

from datetime import datetime, timezone, timedelta


class Clock:
    def __init__(self, frozen: datetime | None = None):
        self._frozen = frozen

    def now(self) -> datetime:
        if self._frozen is not None:
            return self._frozen
        return datetime.now(timezone.utc)

    def freeze(self, moment: datetime | None = None) -> datetime:
        if moment is None:
            moment = datetime.now(timezone.utc)
        elif moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        self._frozen = moment.astimezone(timezone.utc)
        return self._frozen

    def advance(self, **delta) -> datetime:
        if self._frozen is None:
            self.freeze()
        self._frozen = self._frozen + timedelta(**delta)
        return self._frozen

    def resume(self) -> None:
        self._frozen = None

    def iso(self) -> str:
        return self.now().isoformat()


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
