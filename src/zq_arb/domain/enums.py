from __future__ import annotations

from enum import StrEnum


class RunMode(StrEnum):
    READ_ONLY = "READ_ONLY"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIMITED_LIVE = "LIMITED_LIVE"
    LIVE_ARMED = "LIVE_ARMED"

    @property
    def permits_zero_reserves(self) -> bool:
        return self in {self.READ_ONLY, self.PAPER, self.SHADOW}

    @property
    def is_live(self) -> bool:
        return self in {self.LIMITED_LIVE, self.LIVE_ARMED}


class BatchState(StrEnum):
    IDLE = "IDLE"
    QUALIFIED = "QUALIFIED"
    ZQ_SUBMITTED = "ZQ_SUBMITTED"
    ZQ_PARTIAL = "ZQ_PARTIAL"
    POLY_HEDGE_PENDING = "POLY_HEDGE_PENDING"
    PARTIALLY_HEDGED = "PARTIALLY_HEDGED"
    HEDGED = "HEDGED"
    COMPLETE = "COMPLETE"
    RECOVERY = "RECOVERY"
    HALTED = "HALTED"
    HALTED_MANUAL = "HALTED_MANUAL"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class DataQuality(StrEnum):
    LIVE = "LIVE"
    DELAYED = "DELAYED"
    FROZEN = "FROZEN"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class ConnectionStatus(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class AlertSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class ControlAction(StrEnum):
    ARM = "ARM"
    DISARM = "DISARM"
    PAUSE_NEW_TRADES = "PAUSE_NEW_TRADES"
    CANCEL_UNFILLED = "CANCEL_UNFILLED"
    EMERGENCY_HALT = "EMERGENCY_HALT"
    ACKNOWLEDGE_ALERT = "ACKNOWLEDGE_ALERT"
