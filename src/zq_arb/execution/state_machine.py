from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from zq_arb.analytics.payoff import hedge_shares_per_contract, round_shares_up
from zq_arb.domain.enums import BatchState

ALLOWED_TRANSITIONS: dict[BatchState, frozenset[BatchState]] = {
    BatchState.IDLE: frozenset({BatchState.QUALIFIED, BatchState.RECOVERY, BatchState.HALTED}),
    BatchState.QUALIFIED: frozenset({BatchState.ZQ_SUBMITTED, BatchState.IDLE, BatchState.HALTED}),
    BatchState.ZQ_SUBMITTED: frozenset(
        {
            BatchState.ZQ_PARTIAL,
            BatchState.CANCEL_PENDING,
            BatchState.COMPLETE,
            BatchState.RECOVERY,
            BatchState.HALTED,
        }
    ),
    BatchState.ZQ_PARTIAL: frozenset(
        {
            BatchState.CANCEL_PENDING,
            BatchState.POLY_HEDGE_PENDING,
            BatchState.PARTIALLY_HEDGED,
            BatchState.HEDGED,
            BatchState.RECOVERY,
            BatchState.HALTED_MANUAL,
        }
    ),
    BatchState.CANCEL_PENDING: frozenset(
        {
            BatchState.ZQ_PARTIAL,
            BatchState.POLY_HEDGE_PENDING,
            BatchState.HEDGED,
            BatchState.COMPLETE,
            BatchState.RECOVERY,
            BatchState.HALTED_MANUAL,
        }
    ),
    BatchState.POLY_HEDGE_PENDING: frozenset(
        {
            BatchState.PARTIALLY_HEDGED,
            BatchState.HEDGED,
            BatchState.RECOVERY,
            BatchState.HALTED_MANUAL,
        }
    ),
    BatchState.PARTIALLY_HEDGED: frozenset(
        {BatchState.POLY_HEDGE_PENDING, BatchState.HEDGED, BatchState.HALTED_MANUAL}
    ),
    BatchState.HEDGED: frozenset({BatchState.ZQ_PARTIAL, BatchState.COMPLETE, BatchState.RECOVERY}),
    BatchState.COMPLETE: frozenset({BatchState.IDLE, BatchState.RECOVERY}),
    BatchState.RECOVERY: frozenset(
        {BatchState.IDLE, BatchState.ZQ_SUBMITTED, BatchState.HEDGED, BatchState.HALTED_MANUAL}
    ),
    BatchState.HALTED: frozenset({BatchState.RECOVERY}),
    BatchState.HALTED_MANUAL: frozenset({BatchState.RECOVERY}),
}


@dataclass(frozen=True, slots=True)
class HedgeObligation:
    obligation_id: str
    batch_id: str
    exec_id: str
    token_code: str
    due_shares: Decimal
    confirmed_shares: Decimal = Decimal("0")
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def deficit(self) -> Decimal:
        return max(Decimal("0"), self.due_shares - self.confirmed_shares)


@dataclass(slots=True)
class BatchMachine:
    batch_id: str = field(default_factory=lambda: str(uuid4()))
    state: BatchState = BatchState.IDLE
    original_quantity: int = 10
    filled_quantity: Decimal = Decimal("0")
    seen_exec_ids: set[str] = field(default_factory=set)
    obligations: dict[str, HedgeObligation] = field(default_factory=dict)
    transition_history: list[tuple[BatchState, BatchState, str, datetime]] = field(
        default_factory=list
    )

    def transition(self, next_state: BatchState, reason: str) -> None:
        if next_state not in ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(f"invalid batch transition {self.state} -> {next_state}")
        previous = self.state
        self.state = next_state
        self.transition_history.append((previous, next_state, reason, datetime.now(UTC)))

    def record_zq_execution(self, exec_id: str, fill_delta: Decimal) -> tuple[HedgeObligation, ...]:
        if fill_delta <= 0:
            raise ValueError("fill delta must be positive")
        if exec_id in self.seen_exec_ids:
            return ()
        if self.filled_quantity + fill_delta > Decimal(self.original_quantity):
            raise ValueError("execution would exceed original ZQ quantity")
        self.seen_exec_ids.add(exec_id)
        self.filled_quantity += fill_delta
        if self.state is BatchState.ZQ_SUBMITTED:
            self.transition(BatchState.ZQ_PARTIAL, f"execution {exec_id}")

        obligations: list[HedgeObligation] = []
        for token_code, move in (("INC25", 25), ("INC50PLUS", 50)):
            due = round_shares_up(hedge_shares_per_contract(move) * fill_delta)
            obligation = HedgeObligation(
                obligation_id=f"{self.batch_id}:{exec_id}:{token_code}",
                batch_id=self.batch_id,
                exec_id=exec_id,
                token_code=token_code,
                due_shares=due,
            )
            self.obligations[obligation.obligation_id] = obligation
            obligations.append(obligation)
        return tuple(obligations)
