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


class QuoteRole(StrEnum):
    TARGET = "TARGET"
    ANCHOR = "ANCHOR"
    DIAGNOSTIC = "DIAGNOSTIC"


class SubscriptionStatus(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    PENDING_REVALIDATION = "PENDING_REVALIDATION"
    SUSPECT = "SUSPECT"
    DISCONNECTED = "DISCONNECTED"


class GateStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class FarmStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"


class MarginPreviewStatus(StrEnum):
    NOT_REQUESTED = "NOT_REQUESTED"
    PENDING = "PENDING"
    AVAILABLE = "AVAILABLE"
    FAILED = "FAILED"


class MarginQualificationStatus(StrEnum):
    NOT_REQUESTED = "NOT_REQUESTED"
    REFRESHING = "REFRESHING"
    CURRENT = "CURRENT"
    REFRESH_REQUIRED = "REFRESH_REQUIRED"
    FAILED = "FAILED"


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
    CONFIRM_RECONCILED = "CONFIRM_RECONCILED"
    RESET_STRATEGY_RISK = "RESET_STRATEGY_RISK"
