"""Virtual duration timers: a min-heap of deadlines with lazy deletion.

The queue knows nothing about rules or events. The engine decides when to
call pop_due and what a fired timer means.

Lazy deletion: cancelling or rescheduling a key does not search the heap.
The old heap entry stays behind and is skipped when it reaches the top,
because it no longer matches the key's live (deadline, sequence).
"""

import heapq
from datetime import datetime

TimerKey = tuple[str, str]  # (rule_id, subject_id)


class TimerQueue:
    def __init__(self) -> None:
        self._heap: list[tuple[datetime, int, TimerKey]] = []
        self._live: dict[TimerKey, tuple[datetime, int]] = {}  # key -> (deadline, sequence)
        self._sequence = 0  # creation counter: breaks ties between equal deadlines

    def schedule(self, key: TimerKey, deadline: datetime) -> None:
        """Set the key's deadline, replacing any earlier one."""
        self._sequence += 1
        heapq.heappush(self._heap, (deadline, self._sequence, key))
        self._live[key] = (deadline, self._sequence)

    def cancel(self, key: TimerKey) -> None:
        """Forget the key's timer. Unknown keys are ignored."""
        self._live.pop(key, None)

    def pop_due(self, clock: datetime, *, inclusive: bool = False) -> list[tuple[TimerKey, datetime]]:
        """Remove and return live timers due before clock (or at it, if inclusive).

        Ordered by (deadline, creation sequence).
        """
        due: list[tuple[TimerKey, datetime]] = []
        while self._heap:
            deadline, sequence, key = self._heap[0]
            if deadline > clock or (deadline == clock and not inclusive):
                break
            heapq.heappop(self._heap)
            if self._live.get(key) == (deadline, sequence):  # skip obsolete entries
                del self._live[key]
                due.append((key, deadline))
        return due

    def live(self) -> dict[TimerKey, datetime]:
        """Every live timer's deadline."""
        return {key: deadline for key, (deadline, _) in self._live.items()}
