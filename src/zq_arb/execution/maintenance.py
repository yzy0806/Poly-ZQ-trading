from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from zq_arb.config import Settings
from zq_arb.domain.models import utc_now


@dataclass(frozen=True)
class MaintenanceWindow:
    drain_at: datetime
    close_at: datetime
    restart_at: datetime
    reopen_at: datetime
    deadline: datetime


class MaintenanceHold:
    """One in-memory daily hold. It never owns or restores operator arming permission."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.now = utc_now
        self.zone = ZoneInfo(settings.ibkr_maintenance_timezone)
        self.window: MaintenanceWindow | None = None
        self.finished_window: datetime | None = None
        self.drained = False
        self.failed = False
        self.interruption_at: datetime | None = None
        self.recovered = False

    def scheduled_window(self, now: datetime) -> MaintenanceWindow:
        day = now.astimezone(self.zone).date()
        close = datetime.combine(day, self.settings.ibkr_maintenance_start, self.zone)
        restart = datetime.combine(day, self.settings.ibkr_gateway_restart_time, self.zone)
        reopen = datetime.combine(day, self.settings.ibkr_maintenance_end, self.zone)
        return MaintenanceWindow(
            drain_at=close.astimezone(UTC)
            - timedelta(seconds=self.settings.ibkr_maintenance_drain_seconds),
            close_at=close.astimezone(UTC),
            restart_at=restart.astimezone(UTC),
            reopen_at=reopen.astimezone(UTC),
            deadline=reopen.astimezone(UTC)
            + timedelta(seconds=self.settings.ibkr_maintenance_recovery_seconds),
        )

    def weekend_closed(self, now: datetime) -> bool:
        local = now.astimezone(self.zone)
        return (
            (local.weekday() == 4 and local.time() >= self.settings.ibkr_maintenance_start)
            or local.weekday() == 5
            or (local.weekday() == 6 and local.time() < self.settings.ibkr_maintenance_end)
        )

    def blocks_entry(self, now: datetime) -> bool:
        if not self.settings.ibkr_maintenance_enabled:
            return False
        window = self.scheduled_window(now)
        return (
            self.window is not None
            or window.drain_at <= now < window.reopen_at
            or self.weekend_closed(now)
        )

    def enter(self, now: datetime) -> bool:
        window = self.scheduled_window(now)
        if (
            not self.settings.ibkr_maintenance_enabled
            or self.window is not None
            or self.finished_window == window.close_at
            or not window.drain_at <= now < window.reopen_at
        ):
            return False
        self.window = window
        self.drained = False
        self.failed = False
        self.interruption_at = None
        self.recovered = False
        return True

    def observe_interruption(self, now: datetime) -> None:
        window = self.window
        if (
            window is not None
            and self.drained
            and not self.failed
            and not self.recovered
            and self.interruption_at is None
        ):
            # Retries cannot move an earlier unrelated outage into the restart window.
            self.interruption_at = now

    def recovering(self, now: datetime) -> bool:
        return bool(
            self.window is not None
            and self.drained
            and not self.failed
            and self.interruption_at is not None
            and self.window.restart_at - timedelta(minutes=1)
            <= self.interruption_at
            <= self.window.restart_at + timedelta(minutes=2)
            and not self.recovered
            and now < self.window.deadline
        )

    def finish(self) -> None:
        if self.window is not None:
            self.finished_window = self.window.close_at
        self.window = None

    def view(self, now: datetime) -> dict[str, object]:
        blocked = self.blocks_entry(now)
        reopen = self.window.reopen_at if self.window else self.scheduled_window(now).reopen_at
        if self.weekend_closed(now):
            local = now.astimezone(self.zone)
            sunday = local.date() + timedelta(days=(6 - local.weekday()) % 7)
            reopen = datetime.combine(
                sunday, self.settings.ibkr_maintenance_end, self.zone
            ).astimezone(UTC)
        return {
            "active": blocked,
            "reopen_at": reopen.isoformat() if blocked else None,
            "recovering": self.recovering(now),
            "drained": self.drained,
            "failed": self.failed,
            "reason": (
                "Maintenance recovery requires attention"
                if self.failed and self.window
                else "Waiting for Gateway recovery"
                if self.recovering(now)
                else "Regular weekend closure"
                if self.weekend_closed(now)
                else "Scheduled maintenance; new entries blocked"
                if blocked
                else "Maintenance hold inactive"
            ),
        }
