import asyncio

import pytest

from app.consume import CANCELLED, COMMITTED, FAILED, PENDING, ConsumeManager, NotPendingError
from app.grocy import GrocyError


class Recorder:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    async def __call__(self, product_id, stock_id):
        await asyncio.sleep(0)
        if self.fail:
            raise GrocyError(self.fail, 400)
        self.calls.append((product_id, stock_id))


def run(coro):
    return asyncio.run(coro)


async def start(manager, product_id, stock_id, item):
    async def prepare():
        return product_id, stock_id, item

    pending, committed, _duplicate = await manager.start(prepare)
    return pending, committed


def test_nothing_is_consumed_until_commit():
    async def scenario():
        rec = Recorder()
        manager = ConsumeManager(rec, countdown_seconds=5)
        pending, committed = await start(manager, 10, "abc", {"product_name": "Butter"})
        assert committed == []
        assert pending.status == PENDING
        assert rec.calls == []
        assert 4 < pending.to_dict()["remaining_seconds"] <= 5
        await manager.commit(pending.id)
        assert pending.status == COMMITTED
        assert rec.calls == [(10, "abc")]
        # Committing twice never consumes twice.
        await manager.commit(pending.id)
        assert rec.calls == [(10, "abc")]

    run(scenario())


def test_undo_cancels_and_never_consumes():
    async def scenario():
        rec = Recorder()
        manager = ConsumeManager(rec, countdown_seconds=0.05, grace_seconds=0)
        pending, _ = await start(manager, 10, None, {})
        await manager.cancel(pending.id)
        assert pending.status == CANCELLED
        await asyncio.sleep(0.1)  # safety timer fires but must not consume
        assert rec.calls == []
        with pytest.raises(NotPendingError):
            await manager.cancel(pending.id)

    run(scenario())


def test_new_scan_commits_previous_immediately():
    async def scenario():
        rec = Recorder()
        manager = ConsumeManager(rec, countdown_seconds=5)
        first, _ = await start(manager, 10, "a", {})
        second, committed = await start(manager, 11, "b", {})
        assert [p.id for p in committed] == [first.id]
        assert first.status == COMMITTED
        assert second.status == PENDING
        assert rec.calls == [(10, "a")]
        with pytest.raises(NotPendingError):
            await manager.cancel(first.id)

    run(scenario())


def test_safety_timer_commits_when_kiosk_goes_away():
    async def scenario():
        rec = Recorder()
        manager = ConsumeManager(rec, countdown_seconds=0.05, grace_seconds=0.05)
        pending, _ = await start(manager, 10, None, {})
        await asyncio.sleep(0.25)
        assert pending.status == COMMITTED
        assert rec.calls == [(10, None)]

    run(scenario())


def test_grocy_failure_is_reported():
    async def scenario():
        manager = ConsumeManager(Recorder(fail="Amount to be consumed cannot be > current stock amount"), 5)
        pending, _ = await start(manager, 10, None, {})
        await manager.commit(pending.id)
        assert pending.status == FAILED
        assert "cannot be >" in pending.error

    run(scenario())


def test_shutdown_flushes_pending():
    async def scenario():
        rec = Recorder()
        manager = ConsumeManager(rec, countdown_seconds=5)
        await start(manager, 10, "a", {})
        await manager.shutdown()
        assert rec.calls == [(10, "a")]

    run(scenario())


def test_rescanning_a_pending_label_is_a_duplicate_not_a_second_consume():
    async def scenario():
        rec = Recorder()
        manager = ConsumeManager(rec, countdown_seconds=5)

        async def prepare():
            return 10, "a", {}

        first, _, dup1 = await manager.start(prepare)
        again, committed, dup2 = await manager.start(prepare)
        assert (dup1, dup2) == (False, True)
        assert again is first and committed == []
        assert first.status == PENDING and rec.calls == []
        await manager.cancel(first.id)
        assert rec.calls == []

    run(scenario())


def test_scan_is_rechecked_after_committing_previous():
    async def scenario():
        rec = Recorder()
        manager = ConsumeManager(rec, countdown_seconds=5)
        await start(manager, 10, "a", {})

        async def prepare():
            # Stock is checked against Grocy again once the previous item was removed.
            if rec.calls:
                raise LookupError("no stock left")
            return 10, None, {}

        with pytest.raises(LookupError):
            await manager.start(prepare)
        assert rec.calls == [(10, "a")]
        assert manager.pending() == []

    run(scenario())
