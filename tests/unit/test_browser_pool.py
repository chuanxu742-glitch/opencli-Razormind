"""Unit tests for browser pool routing and ownership."""

import asyncio
import time

import pytest

from backend.browser_pool import LocalBrowserPool


@pytest.fixture
def pool():
    return LocalBrowserPool(["http://chrome:9222", "http://chrome-2:9222"])


@pytest.fixture
def empty_pool():
    return LocalBrowserPool([])


# ── properties ────────────────────────────────────────────────────────────────


def test_total_equals_endpoint_count(pool):
    assert pool.total == 2


def test_endpoints_returns_all(pool):
    endpoints = pool.endpoints
    assert "http://chrome:9222" in endpoints
    assert "http://chrome-2:9222" in endpoints
    assert len(endpoints) == 2


def test_available_initially_equals_total(pool):
    assert pool.available == 2


def test_available_for_all_endpoints_initially(pool):
    assert pool.available_for("http://chrome:9222") is True
    assert pool.available_for("http://chrome-2:9222") is True


def test_available_for_unknown_endpoint(pool):
    assert pool.available_for("http://unknown:9222") is False


# ── mode management ────────────────────────────────────────────────────────────


def test_default_mode_is_bridge(pool):
    assert pool.get_mode("http://chrome:9222") == "bridge"


def test_set_mode_updates_mode(pool):
    pool.set_mode("http://chrome:9222", "cdp")
    assert pool.get_mode("http://chrome:9222") == "cdp"


def test_get_mode_unknown_endpoint_returns_bridge(pool):
    assert pool.get_mode("http://unknown:9222") == "bridge"


# ── agent_url management ───────────────────────────────────────────────────────


def test_default_agent_url_is_none(pool):
    assert pool.get_agent_url("http://chrome:9222") is None


def test_set_agent_url(pool):
    pool.set_agent_url("http://chrome:9222", "http://agent:8080")
    assert pool.get_agent_url("http://chrome:9222") == "http://agent:8080"


def test_default_agent_protocol_is_none(pool):
    assert pool.get_agent_protocol("http://chrome:9222") is None


def test_set_agent_protocol(pool):
    pool.set_agent_protocol("http://chrome:9222", "http")
    assert pool.get_agent_protocol("http://chrome:9222") == "http"


# ── add/remove endpoints ───────────────────────────────────────────────────────


def test_add_endpoint_increases_total(pool):
    pool.add_endpoint("http://chrome-3:9222")
    assert pool.total == 3
    assert "http://chrome-3:9222" in pool.endpoints


def test_add_endpoint_already_exists_no_duplicate(pool):
    pool.add_endpoint("http://chrome:9222")
    assert pool.total == 2  # no change


def test_remove_endpoint_decreases_total(pool):
    pool.remove_endpoint("http://chrome:9222")
    assert pool.total == 1
    assert "http://chrome:9222" not in pool.endpoints


def test_remove_endpoint_nonexistent_no_error(pool):
    pool.remove_endpoint("http://nonexistent:9222")
    assert pool.total == 2  # no change


# ── acquire / release via context manager ─────────────────────────────────────


@pytest.mark.asyncio
async def test_acquire_specific_endpoint(pool):
    """Acquiring a specific endpoint yields that endpoint URL."""
    async with pool.acquire("http://chrome:9222") as ep:
        assert ep == "http://chrome:9222"
        # While acquired, that slot is not available
        assert pool.available_for("http://chrome:9222") is False


@pytest.mark.asyncio
async def test_acquire_any_endpoint(pool):
    """Acquiring any endpoint returns one of the available endpoints."""
    async with pool.acquire() as ep:
        assert ep in ["http://chrome:9222", "http://chrome-2:9222"]


@pytest.mark.asyncio
async def test_acquire_releases_on_exit(pool):
    """Slot is returned to the pool after the context manager exits."""
    async with pool.acquire("http://chrome:9222") as ep:
        assert ep == "http://chrome:9222"

    # After exit, slot should be available again
    assert pool.available_for("http://chrome:9222") is True


# ── empty pool ────────────────────────────────────────────────────────────────


def test_empty_pool_total_is_zero(empty_pool):
    assert empty_pool.total == 0


def test_empty_pool_available_is_zero(empty_pool):
    assert empty_pool.available == 0


def test_empty_pool_endpoints_is_empty(empty_pool):
    assert empty_pool.endpoints == []


# ── init_pool / get_pool ───────────────────────────────────────────────────────


def test_init_pool_local(monkeypatch):
    """init_pool with use_redis=False creates a LocalBrowserPool."""
    from backend import browser_pool

    browser_pool.init_pool(["http://chrome:9222"], use_redis=False)
    pool = browser_pool.get_pool()
    assert isinstance(pool, LocalBrowserPool)
    assert pool.total == 1


def test_get_pool_raises_when_not_initialized():
    """get_pool raises RuntimeError when init_pool hasn't been called."""
    from backend import browser_pool

    browser_pool._pool = None
    with pytest.raises(RuntimeError, match="not initialized"):
        browser_pool.get_pool()


@pytest.mark.asyncio
async def test_ensure_ready_local_pool_noop():
    """ensure_ready does nothing for LocalBrowserPool."""
    from backend import browser_pool

    browser_pool.init_pool(["http://chrome:9222"], use_redis=False)
    # Should not raise
    await browser_pool.ensure_ready()


# ── LocalBrowserPool: _acquire_any concurrent acquisition ─────────────────────


@pytest.mark.asyncio
async def test_acquire_any_concurrent_gets_endpoints():
    """Two concurrent unrouted acquires each get a Chrome endpoint."""
    pool = LocalBrowserPool(["http://chrome:9222", "http://chrome-2:9222"])
    acquired = []

    async def grab():
        async with pool.acquire() as ep:
            acquired.append(ep)
            await asyncio.sleep(0.01)  # hold briefly

    await asyncio.gather(grab(), grab())
    assert len(acquired) == 2
    assert set(acquired) == {"http://chrome:9222", "http://chrome-2:9222"}
    assert pool.available == 2


@pytest.mark.asyncio
async def test_acquire_unknown_endpoint_fails_closed():
    """Requesting an unknown endpoint must not acquire another instance."""
    from backend.browser_pool import NoReadyBrowserSlotError

    p = LocalBrowserPool(["http://chrome:9222"])
    with pytest.raises(NoReadyBrowserSlotError):
        async with p.acquire("http://not-in-pool:9222"):
            pass
    assert p.available == 1


@pytest.mark.asyncio
async def test_required_anonymous_route_never_falls_back_or_uses_authenticated():
    from backend.browser_pool import NoCleanProfileError

    p = LocalBrowserPool(["http://authenticated:9222"])

    with pytest.raises(NoCleanProfileError):
        async with p.acquire("http://missing:9222", required_profile_kind="anonymous"):
            pass
    with pytest.raises(NoCleanProfileError):
        async with p.acquire("http://authenticated:9222", required_profile_kind="anonymous"):
            pass


# ── init_pool with Redis ──────────────────────────────────────────────────────


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.expires = {}
        self.renewal_lost = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def _purge(self, key):
        if self.expires.get(key, float("inf")) <= time.monotonic():
            self.values.pop(key, None)
            self.expires.pop(key, None)

    async def incr(self, key):
        self._purge(key)
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    async def set(self, key, value, *, nx=False, px=None):
        self._purge(key)
        if nx and key in self.values:
            return False
        self.values[key] = value
        if px is not None:
            self.expires[key] = time.monotonic() + px / 1000
        return True

    async def eval(self, script, _numkeys, key, owner, ttl=None):
        self._purge(key)
        if "pexpire" in script:
            if self.renewal_lost:
                return 0
            if self.values.get(key) != owner:
                return 0
            self.expires[key] = time.monotonic() + ttl / 1000
            return 1
        if self.values.get(key) == owner:
            self.values.pop(key, None)
            self.expires.pop(key, None)
            return 1
        return 0

    def expire_leases(self):
        for key in list(self.values):
            if key.endswith(":lease"):
                self.expires[key] = time.monotonic() - 1
                self._purge(key)

    def has_lease(self, endpoint):
        key = f"browser_pool:ep:{endpoint}:lease"
        self._purge(key)
        return key in self.values


@pytest.mark.asyncio
async def test_local_cancel_unrouted_waiter_restores_all_tokens():
    from backend.browser_pool import LocalBrowserPool

    pool = LocalBrowserPool(["A", "B", "C"])
    holders = [pool.acquire(ep) for ep in pool.endpoints]
    for holder in holders:
        await holder.__aenter__()

    waiter = asyncio.create_task(pool.acquire().__aenter__())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    for holder in holders:
        await holder.__aexit__(None, None, None)

    assert pool.available == 3
    for endpoint in pool.endpoints:
        async with pool.acquire(endpoint) as acquired:
            assert acquired == endpoint


@pytest.mark.asyncio
async def test_local_cancel_waiting_profile_lock_restores_endpoint():
    from backend.browser_pool import LocalBrowserPool

    pool = LocalBrowserPool(["A", "B"])
    pool.set_profile_name("A", "shared")
    pool.set_profile_name("B", "shared")
    holder = pool.acquire("A")
    await holder.__aenter__()

    waiter = asyncio.create_task(pool.acquire("B").__aenter__())
    for _ in range(10):
        await asyncio.sleep(0)
        if not pool.available_for("B"):
            break
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert pool.available_for("B") is True

    await holder.__aexit__(None, None, None)
    assert pool.available == 2


@pytest.mark.asyncio
async def test_local_cancel_after_profile_lock_wins_restores_slot_once():
    from backend.browser_pool import LocalBrowserPool

    pool = LocalBrowserPool(["A", "B"])
    pool.set_profile_name("A", "shared")
    pool.set_profile_name("B", "shared")
    holder = pool.acquire("A")
    await holder.__aenter__()

    waiter = asyncio.create_task(pool.acquire("B").__aenter__())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert pool.available_for("B") is False

    await holder.__aexit__(None, None, None)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)

    assert pool.available == 2
    for endpoint in pool.endpoints:
        async with pool.acquire(endpoint) as acquired:
            assert acquired == endpoint


@pytest.mark.asyncio
async def test_redis_pools_compete_by_endpoint_lease_and_reacquire(monkeypatch):
    from backend.browser_pool import RedisBrowserPool

    redis = _FakeRedis()
    first = RedisBrowserPool(["A", "B"], "redis://test")
    second = RedisBrowserPool(["A", "B"], "redis://test")
    monkeypatch.setattr(first, "_client", lambda: redis)
    monkeypatch.setattr(second, "_client", lambda: redis)
    first._ACQUIRE_TIMEOUT_SECONDS = 0.01
    second._ACQUIRE_TIMEOUT_SECONDS = 0.01
    second._RETRY_SECONDS = 0.001

    async with first.acquire("A") as acquired:
        assert acquired == "A"
        async with second.acquire() as other:
            assert other == "B"
        with pytest.raises(TimeoutError):
            async with second.acquire("A"):
                pass
    async with second.acquire("A") as reacquired:
        assert reacquired == "A"


@pytest.mark.asyncio
async def test_redis_cancel_after_set_cleans_owner_lease(monkeypatch):
    from backend.browser_pool import RedisBrowserPool

    class PausedRedis(_FakeRedis):
        def __init__(self):
            super().__init__()
            self.set_done = asyncio.Event()

        async def set(self, key, value, *, nx=False, px=None):
            result = await super().set(key, value, nx=nx, px=px)
            if key.endswith(":lease") and not self.set_done.is_set():
                self.set_done.set()
                await asyncio.Future()
            return result

    redis = PausedRedis()
    first = RedisBrowserPool(["A"], "redis://test")
    second = RedisBrowserPool(["A"], "redis://test")
    monkeypatch.setattr(first, "_client", lambda: redis)
    monkeypatch.setattr(second, "_client", lambda: redis)
    first._ACQUIRE_TIMEOUT_SECONDS = 0.01
    second._ACQUIRE_TIMEOUT_SECONDS = 0.01

    waiter = asyncio.create_task(first.acquire("A").__aenter__())
    await asyncio.wait_for(redis.set_done.wait(), 1)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)

    assert redis.has_lease("A") is False
    async with second.acquire("A") as reacquired:
        assert reacquired == "A"


@pytest.mark.asyncio
async def test_redis_cancel_during_fence_acquisition_leaves_no_lease(monkeypatch):
    from backend.browser_pool import RedisBrowserPool

    class PausedRedis(_FakeRedis):
        def __init__(self):
            super().__init__()
            self.incr_done = asyncio.Event()

        async def incr(self, key):
            result = await super().incr(key)
            if not self.incr_done.is_set():
                self.incr_done.set()
                await asyncio.Future()
            return result

    redis = PausedRedis()
    first = RedisBrowserPool(["A"], "redis://test")
    second = RedisBrowserPool(["A"], "redis://test")
    monkeypatch.setattr(first, "_client", lambda: redis)
    monkeypatch.setattr(second, "_client", lambda: redis)

    waiter = asyncio.create_task(first.acquire("A").__aenter__())
    await asyncio.wait_for(redis.incr_done.wait(), 1)
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)

    assert redis.has_lease("A") is False
    async with second.acquire("A") as reacquired:
        assert reacquired == "A"


@pytest.mark.asyncio
async def test_redis_set_error_after_acceptance_cleans_owner_lease(monkeypatch):
    from backend.browser_pool import RedisBrowserPool

    class FailingRedis(_FakeRedis):
        def __init__(self):
            super().__init__()
            self.fail_next = True

        async def set(self, key, value, *, nx=False, px=None):
            result = await super().set(key, value, nx=nx, px=px)
            if key.endswith(":lease") and self.fail_next:
                self.fail_next = False
                raise ConnectionError("response lost after SET")
            return result

    redis = FailingRedis()
    first = RedisBrowserPool(["A"], "redis://test")
    second = RedisBrowserPool(["A"], "redis://test")
    monkeypatch.setattr(first, "_client", lambda: redis)
    monkeypatch.setattr(second, "_client", lambda: redis)

    with pytest.raises(ConnectionError, match="response lost"):
        await first.acquire("A").__aenter__()

    assert redis.has_lease("A") is False
    async with second.acquire("A") as reacquired:
        assert reacquired == "A"


@pytest.mark.asyncio
async def test_redis_owner_cleanup_does_not_delete_replacement_lease(monkeypatch):
    from backend.browser_pool import RedisBrowserPool

    redis = _FakeRedis()
    first = RedisBrowserPool(["A"], "redis://test")
    second = RedisBrowserPool(["A"], "redis://test")
    monkeypatch.setattr(first, "_client", lambda: redis)
    monkeypatch.setattr(second, "_client", lambda: redis)

    old_holder = first.acquire("A")
    assert await old_holder.__aenter__() == "A"
    redis.expire_leases()
    replacement = second.acquire("A")
    assert await replacement.__aenter__() == "A"

    await old_holder.__aexit__(None, None, None)
    assert redis.has_lease("A") is True
    await replacement.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_redis_expired_owner_can_reacquire_without_queue_token(monkeypatch):
    from backend.browser_pool import RedisBrowserPool

    redis = _FakeRedis()
    first = RedisBrowserPool(["A"], "redis://test")
    second = RedisBrowserPool(["A"], "redis://test")
    monkeypatch.setattr(first, "_client", lambda: redis)
    monkeypatch.setattr(second, "_client", lambda: redis)
    holder = first.acquire("A")
    assert await holder.__aenter__() == "A"

    redis.expire_leases()
    async with second.acquire("A") as reacquired:
        assert reacquired == "A"
    await holder.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_redis_renewal_loss_cleans_lease_and_allows_reacquire(monkeypatch):
    from backend.browser_pool import RedisBrowserPool

    redis = _FakeRedis()
    redis.renewal_lost = True
    first = RedisBrowserPool(["A"], "redis://test")
    second = RedisBrowserPool(["A"], "redis://test")
    monkeypatch.setattr(first, "_client", lambda: redis)
    monkeypatch.setattr(second, "_client", lambda: redis)
    first._LEASE_RENEW_SECONDS = 0.001

    with pytest.raises(asyncio.CancelledError):
        async with first.acquire("A"):
            await asyncio.sleep(0.02)
    assert redis.has_lease("A") is False
    async with second.acquire("A") as reacquired:
        assert reacquired == "A"


@pytest.mark.asyncio
async def test_redis_renewal_error_cancels_holder_and_allows_reacquire(monkeypatch):
    from backend.browser_pool import RedisBrowserPool

    class FailingRenewalRedis(_FakeRedis):
        def __init__(self):
            super().__init__()
            self.renewal_failed = asyncio.Event()

        async def eval(self, script, _numkeys, key, owner, ttl=None):
            if "pexpire" in script and not self.renewal_failed.is_set():
                self.renewal_failed.set()
                raise ConnectionError("renewal connection lost")
            return await super().eval(script, _numkeys, key, owner, ttl)

    redis = FailingRenewalRedis()
    first = RedisBrowserPool(["A"], "redis://test")
    second = RedisBrowserPool(["A"], "redis://test")
    monkeypatch.setattr(first, "_client", lambda: redis)
    monkeypatch.setattr(second, "_client", lambda: redis)
    first._LEASE_RENEW_SECONDS = 0.001

    with pytest.raises(asyncio.CancelledError):
        async with first.acquire("A"):
            await redis.renewal_failed.wait()
            await asyncio.Future()

    assert redis.has_lease("A") is False
    async with second.acquire("A") as reacquired:
        assert reacquired == "A"


@pytest.mark.asyncio
async def test_redis_renewal_client_error_cancels_holder_and_allows_reacquire(monkeypatch):
    from backend.browser_pool import RedisBrowserPool

    class FailingClient:
        def __init__(self, redis):
            self.redis = redis
            self.calls = 0
            self.failed = asyncio.Event()

        def __call__(self):
            self.calls += 1
            if self.calls == 2:
                self.failed.set()
                raise ConnectionError("renewal client unavailable")
            return self.redis

    redis = _FakeRedis()
    first = RedisBrowserPool(["A"], "redis://test")
    second = RedisBrowserPool(["A"], "redis://test")
    failing_client = FailingClient(redis)
    monkeypatch.setattr(first, "_client", failing_client)
    monkeypatch.setattr(second, "_client", lambda: redis)
    first._LEASE_RENEW_SECONDS = 0.001

    with pytest.raises(asyncio.CancelledError):
        async with first.acquire("A"):
            await failing_client.failed.wait()
            await asyncio.Future()

    assert redis.has_lease("A") is False
    async with second.acquire("A") as reacquired:
        assert reacquired == "A"


# Managed official-site acquisition must never fall back to the operator's
# default (potentially authenticated) browser profile.
@pytest.mark.asyncio
async def test_acquire_anonymous_profile_fails_closed_when_none_is_registered():
    from backend.browser_pool import LocalBrowserPool, NoCleanProfileError

    pool = LocalBrowserPool(["http://default-profile:9222"])

    with pytest.raises(NoCleanProfileError, match="no_clean_profile"):
        async with pool.acquire_anonymous():
            pass


@pytest.mark.asyncio
async def test_acquire_anonymous_profile_routes_only_to_explicit_anonymous_profile():
    from backend.browser_pool import LocalBrowserPool

    pool = LocalBrowserPool(["http://signed-in-profile:9222", "http://anonymous-profile:9222"])
    pool.set_profile_kind("http://anonymous-profile:9222", "anonymous")

    async with pool.acquire_anonymous() as endpoint:
        assert endpoint == "http://anonymous-profile:9222"


@pytest.mark.asyncio
async def test_runtime_drift_slot_never_receives_a_new_session():
    from backend.browser_pool import LocalBrowserPool, NoReadyBrowserSlotError

    drifted = "http://drifted-slot:9222"
    ready = "http://ready-slot:9222"
    pool = LocalBrowserPool([drifted, ready])
    pool.set_runtime_status(drifted, "CONFIG_DRIFT")
    pool.set_runtime_status(ready, "READY")

    with pytest.raises(NoReadyBrowserSlotError):
        async with pool.acquire(drifted):
            pass

    async with pool.acquire() as endpoint:
        assert endpoint == ready
