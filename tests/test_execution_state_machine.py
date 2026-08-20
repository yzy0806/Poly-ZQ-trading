from __future__ import annotations

from decimal import Decimal

import pytest

from zq_arb.domain.enums import BatchState
from zq_arb.execution.state_machine import BatchMachine


def test_fill_creates_idempotent_exact_obligations() -> None:
    machine = BatchMachine()
    machine.transition(BatchState.QUALIFIED, "all gates")
    machine.transition(BatchState.ZQ_SUBMITTED, "paper order")
    obligations = machine.record_zq_execution("exec-1", Decimal("2"))
    assert machine.state is BatchState.ZQ_PARTIAL
    assert machine.filled_quantity == 2
    assert [item.due_shares for item in obligations] == [Decimal("972.30"), Decimal("1944.60")]
    assert machine.record_zq_execution("exec-1", Decimal("2")) == ()
    assert machine.filled_quantity == 2


def test_execution_cannot_exceed_ten_contract_child() -> None:
    machine = BatchMachine(state=BatchState.ZQ_SUBMITTED)
    with pytest.raises(ValueError, match="exceed"):
        machine.record_zq_execution("exec-over", Decimal("11"))


def test_invalid_transition_is_rejected() -> None:
    machine = BatchMachine()
    with pytest.raises(ValueError, match="invalid"):
        machine.transition(BatchState.HEDGED, "skip states")
