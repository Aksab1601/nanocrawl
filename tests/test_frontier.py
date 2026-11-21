"""The frontier: order, fairness, politeness and -- above all -- termination.

The termination tests are the ones that matter. A crawler whose workers exit
while another worker is still discovering links stops silently, half done, and
reports success.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from nanocrawl.dedup import UrlSet
from nanocrawl.frontier import Frontier, Request


def make(**kwargs: object) -> Frontier:
    options: dict = {"seen": UrlSet(), "max_depth": 3, "default_delay": 0.0}
    options.update(kwargs)
    return Frontier(**options)  # type: ignore[arg-type]


class TestOrdering:
    async def test_breadth_first_within_a_host(self) -> None:
        frontier = make()
        for i in range(4):
            await frontier.add(f"https://a.com/{i}")
        got = []
        for _ in range(4):
            request = await frontier.get()
            assert request is not None
            got.append(request.url)
            await frontier.done()
        assert got == [f"https://a.com/{i}" for i in range(4)]

    async def test_hosts_take_turns(self) -> None:
        """One big site must not starve a small one."""
        frontier = make()
        for i in range(3):
            await frontier.add(f"https://big.com/{i}")
        await frontier.add("https://small.com/only")

        hosts = []
        for _ in range(4):
            request = await frontier.get()
            assert request is not None
            hosts.append(request.host)
            await frontier.done()
        # Round-robin: the small site is served second, not last.
        assert hosts[:2] == ["big.com", "small.com"]

    async def test_depth_is_carried_through(self) -> None:
        frontier = make()
        await frontier.add("https://a.com/x", depth=2, referrer="https://a.com/")
        request = await frontier.get()
        assert request is not None
        assert request.depth == 2
        assert request.referrer == "https://a.com/"


class TestFiltering:
    async def test_duplicate_urls_are_rejected(self) -> None:
        frontier = make()
        assert await frontier.add("https://a.com/x") is True
        assert await frontier.add("https://a.com/x") is False
        assert frontier.skipped["duplicate-url"] == 1

    async def test_too_deep_is_rejected(self) -> None:
        frontier = make(max_depth=2)
        assert await frontier.add("https://a.com/x", depth=3) is False
        assert frontier.skipped["too-deep"] == 1

    async def test_depth_rejection_does_not_poison_the_seen_set(self) -> None:
        """A URL rejected for depth must still be queueable at a lower depth.

        Otherwise a page found deep first can never be reached by its shorter
        route, and whole sections silently vanish from the crawl.
        """
        frontier = make(max_depth=2)
        assert await frontier.add("https://a.com/x", depth=5) is False
        assert await frontier.add("https://a.com/x", depth=1) is True

    async def test_budget_caps_the_queue(self) -> None:
        frontier = make(max_pages=3)
        for i in range(10):
            await frontier.add(f"https://a.com/{i}")
        assert frontier.accepted == 3
        assert frontier.skipped["over-budget"] == 7

    async def test_add_many_counts_only_what_it_queued(self) -> None:
        frontier = make()
        await frontier.add("https://a.com/1")
        added = await frontier.add_many(
            ["https://a.com/1", "https://a.com/2", "https://a.com/3"], depth=1
        )
        assert added == 2


class TestTermination:
    async def test_get_returns_none_when_empty_and_idle(self) -> None:
        assert await make().get() is None

    async def test_waits_while_work_is_in_flight(self) -> None:
        """An empty queue is not a finished crawl if a worker may still add to it."""
        frontier = make()
        await frontier.add("https://a.com/1")
        first = await frontier.get()
        assert first is not None  # now in flight, queue empty

        waiter = asyncio.create_task(frontier.get())
        await asyncio.sleep(0.05)
        assert not waiter.done(), "get() returned None while work was in flight"

        await frontier.add("https://a.com/2")
        second = await asyncio.wait_for(waiter, timeout=1)
        assert second is not None
        assert second.url == "https://a.com/2"

    async def test_all_workers_exit_when_work_runs_out(self) -> None:
        """The deadlock test: every worker must see the end."""
        frontier = make()
        await frontier.add("https://a.com/1")

        async def worker() -> int:
            handled = 0
            while True:
                request = await frontier.get()
                if request is None:
                    return handled
                handled += 1
                await frontier.done()

        results = await asyncio.wait_for(asyncio.gather(*(worker() for _ in range(5))), timeout=5)
        assert sum(results) == 1

    async def test_discovery_during_a_crawl_keeps_workers_alive(self) -> None:
        frontier = make(max_depth=50)
        await frontier.add("https://a.com/0")
        handled: list[str] = []

        async def worker() -> None:
            while True:
                request = await frontier.get()
                if request is None:
                    return
                handled.append(request.url)
                index = int(request.url.rsplit("/", 1)[1])
                if index < 20:
                    await frontier.add(f"https://a.com/{index + 1}", depth=request.depth + 1)
                await frontier.done()

        await asyncio.wait_for(asyncio.gather(*(worker() for _ in range(4))), timeout=10)
        assert len(handled) == 21

    async def test_close_wakes_every_waiter(self) -> None:
        frontier = make()
        await frontier.add("https://a.com/1")
        request = await frontier.get()
        assert request is not None

        waiters = [asyncio.create_task(frontier.get()) for _ in range(3)]
        await asyncio.sleep(0.05)
        await frontier.close()
        results = await asyncio.wait_for(asyncio.gather(*waiters), timeout=2)
        assert all(result is None for result in results)


class TestPoliteness:
    async def test_slots_are_spaced_by_the_delay(self) -> None:
        frontier = make(default_delay=0.1)
        waits = [await frontier.reserve("a.com") for _ in range(4)]
        # First is immediate; each later one is one delay further out.
        assert waits[0] == pytest.approx(0.0, abs=0.02)
        for earlier, later in zip(waits, waits[1:], strict=False):
            assert later - earlier == pytest.approx(0.1, abs=0.02)

    async def test_hosts_are_scheduled_independently(self) -> None:
        frontier = make(default_delay=0.2)
        await frontier.reserve("a.com")
        # A reservation on one host must not delay another.
        assert await frontier.reserve("b.com") == pytest.approx(0.0, abs=0.02)

    async def test_wait_turn_actually_sleeps(self) -> None:
        frontier = make(default_delay=0.15)
        await frontier.wait_turn("a.com")
        started = time.monotonic()
        slept = await frontier.wait_turn("a.com")
        elapsed = time.monotonic() - started
        assert slept == pytest.approx(0.15, abs=0.05)
        assert elapsed >= 0.1

    async def test_concurrent_workers_do_not_share_a_slot(self) -> None:
        """Ten workers on one host still produce ten distinct times."""
        frontier = make(default_delay=0.05)
        waits = await asyncio.gather(*(frontier.reserve("a.com") for _ in range(10)))
        assert len({round(w, 4) for w in waits}) == 10


class TestCheckpointing:
    async def test_pending_round_trips(self) -> None:
        frontier = make()
        for i in range(3):
            await frontier.add(f"https://a.com/{i}", depth=1)
        pending = frontier.pending()
        assert len(pending) == 3

        restored = make()
        restored.restore(pending)
        assert restored.queued == 3
        request = await restored.get()
        assert request is not None
        assert request.url == "https://a.com/0"

    async def test_restore_bypasses_the_seen_set(self) -> None:
        # Restored URLs are already in the restored seen-set, so the normal
        # path would reject every one of them.
        seen = UrlSet()
        seen.add("https://a.com/1")
        frontier = Frontier(seen=seen, default_delay=0.0)
        frontier.restore([Request(url="https://a.com/1", depth=0)])
        assert frontier.queued == 1
