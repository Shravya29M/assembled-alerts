"""Checkpoint 3.1 — TimerQueue on its own: strict boundary, ordering, cancellation."""

from alerts.timers import TimerQueue
from tests.builders import at

A = ("long_call.lead_support", "a_11")
B = ("adherence.lead_support", "a_88")
C = ("long_call.lead_billing", "a_07")


def test_not_due_at_exactly_the_deadline() -> None:
    queue = TimerQueue()
    queue.schedule(A, at("09:55"))
    assert queue.pop_due(at("09:55")) == []


def test_due_strictly_after_the_deadline() -> None:
    queue = TimerQueue()
    queue.schedule(A, at("09:55"))
    assert queue.pop_due(at("09:55:01")) == [(A, at("09:55"))]
    assert queue.live() == {}


def test_inclusive_pop_for_end_of_input() -> None:
    queue = TimerQueue()
    queue.schedule(A, at("09:55"))
    assert queue.pop_due(at("09:55"), inclusive=True) == [(A, at("09:55"))]


def test_order_is_deadline_then_creation_sequence() -> None:
    queue = TimerQueue()
    queue.schedule(A, at("10:10"))
    queue.schedule(B, at("10:00"))
    queue.schedule(C, at("10:10"))
    assert queue.pop_due(at("10:11")) == [(B, at("10:00")), (A, at("10:10")), (C, at("10:10"))]


def test_cancelled_timer_never_fires() -> None:
    queue = TimerQueue()
    queue.schedule(A, at("09:55"))
    queue.cancel(A)
    queue.cancel(B)  # cancelling an unknown key is harmless
    assert queue.pop_due(at("11:00")) == []


def test_rescheduled_timer_skips_obsolete_entry() -> None:
    queue = TimerQueue()
    queue.schedule(A, at("09:55"))
    queue.schedule(A, at("10:10"))
    assert queue.pop_due(at("10:00")) == []
    assert queue.pop_due(at("10:11")) == [(A, at("10:10"))]


def test_future_timers_are_untouched() -> None:
    queue = TimerQueue()
    queue.schedule(A, at("09:55"))
    queue.schedule(B, at("10:30"))
    assert queue.pop_due(at("10:00")) == [(A, at("09:55"))]
    assert queue.live() == {B: at("10:30")}
