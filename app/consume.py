"""Delayed ("undo-able") consumption of stock.

A scan in CONSUME mode creates a *pending* consumption. Nothing is removed from
Grocy until either:

* the kiosk reports that its countdown reached zero (``commit``),
* another item is scanned (all earlier pending items are committed immediately), or
* the server-side safety timer fires (countdown + grace period) in case the
  kiosk went away mid-countdown.

``cancel`` (the UNDO button) is only possible while the item is still pending.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .grocy import GrocyError

PENDING = "pending"
COMMITTED = "committed"
CANCELLED = "cancelled"
FAILED = "failed"

ConsumeFn = Callable[[int, Optional[str]], Awaitable[object]]
PrepareFn = Callable[[], Awaitable[tuple[int, Optional[str], dict]]]


class NotPendingError(Exception):
    def __init__(self, pending: "PendingConsume"):
        super().__init__(f"Consumption {pending.id} is already {pending.status}")
        self.pending = pending


@dataclass
class PendingConsume:
    id: str
    product_id: int
    stock_id: Optional[str]
    item: dict
    deadline: float
    status: str = PENDING
    error: Optional[str] = None
    finished_at: Optional[float] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    timer: Optional[asyncio.Task] = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "error": self.error,
            "product_id": self.product_id,
            "stock_id": self.stock_id,
            "item": self.item,
            "remaining_seconds": max(0.0, round(self.deadline - time.monotonic(), 2)),
        }


class ConsumeManager:
    def __init__(
        self,
        consume_fn: ConsumeFn,
        countdown_seconds: float,
        grace_seconds: float = 3.0,
        retention_seconds: float = 600.0,
    ):
        self._consume = consume_fn
        self.countdown_seconds = countdown_seconds
        self.grace_seconds = grace_seconds
        self.retention_seconds = retention_seconds
        self._items: dict[str, PendingConsume] = {}
        self._start_lock = asyncio.Lock()
        self._closing = False

    def get(self, pending_id: str) -> Optional[PendingConsume]:
        return self._items.get(pending_id)

    def pending(self) -> list[PendingConsume]:
        return [p for p in self._items.values() if p.status == PENDING]

    async def start(self, prepare: PrepareFn) -> tuple[PendingConsume, list[PendingConsume], bool]:
        """Register a new pending consumption, committing any earlier ones first.

        ``prepare`` resolves the scan to ``(product_id, stock_id, item)`` and must raise if
        nothing is in stock. It runs under the start lock: once before committing earlier
        items (so a repeated scan of a label that is still counting down is recognised as
        a duplicate instead of consuming it twice) and again after any commit, so stock is
        re-checked against Grocy's updated state.

        Returns ``(pending, committed_previous, duplicate)``.
        """
        async with self._start_lock:
            product_id, stock_id, item = await prepare()
            if stock_id is not None:
                for existing in self.pending():
                    if existing.stock_id == stock_id:
                        return existing, [], True
            committed = []
            for previous in self.pending():
                committed.append(await self.commit(previous.id))
            if committed:
                product_id, stock_id, item = await prepare()
            self._prune()
            pending = PendingConsume(
                id=uuid.uuid4().hex,
                product_id=int(product_id),
                stock_id=stock_id,
                item=item,
                deadline=time.monotonic() + self.countdown_seconds,
            )
            self._items[pending.id] = pending
            pending.timer = asyncio.create_task(self._safety_timer(pending))
            return pending, committed, False

    async def commit(self, pending_id: str) -> PendingConsume:
        pending = self._require(pending_id)
        async with pending.lock:
            if pending.status != PENDING:
                return pending
            try:
                await self._consume(pending.product_id, pending.stock_id)
            except GrocyError as exc:
                pending.status = FAILED
                pending.error = exc.message
            else:
                pending.status = COMMITTED
            pending.finished_at = time.monotonic()
            return pending

    async def cancel(self, pending_id: str) -> PendingConsume:
        pending = self._require(pending_id)
        async with pending.lock:
            if pending.status != PENDING:
                raise NotPendingError(pending)
            pending.status = CANCELLED
            pending.finished_at = time.monotonic()
            return pending

    async def shutdown(self) -> None:
        """Commit everything still pending (the user saw "Item consumed")."""
        self._closing = True
        for pending in self.pending():
            await self.commit(pending.id)

    # ----------------------------------------------------------------- private
    def _require(self, pending_id: str) -> PendingConsume:
        pending = self._items.get(pending_id)
        if pending is None:
            raise KeyError(pending_id)
        return pending

    async def _safety_timer(self, pending: PendingConsume) -> None:
        delay = pending.deadline + self.grace_seconds - time.monotonic()
        try:
            await asyncio.sleep(max(0.0, delay))
        except asyncio.CancelledError:
            return
        if not self._closing:
            await self.commit(pending.id)

    def _prune(self) -> None:
        cutoff = time.monotonic() - self.retention_seconds
        for key, pending in list(self._items.items()):
            if pending.finished_at is not None and pending.finished_at < cutoff:
                del self._items[key]
