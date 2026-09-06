"""CDP browser endpoint pool.

Distributes opencli collection tasks across multiple Chrome instances so tasks
can run concurrently without competing for a single browser.

Two implementations:
  LocalBrowserPool  — per-endpoint asyncio.Queue slots, for TASK_EXECUTOR=local.
  RedisBrowserPool  — Redis endpoint leases, for TASK_EXECUTOR=celery
                      (distributed workers across processes / machines).

Routing:
  acquire(endpoint=None)         — any available instance (round-robin / first-free)
  acquire(endpoint="http://...")  — wait specifically for that Chrome instance

Use routing to pin certain data sources to a Chrome instance that is logged into
a specific site (e.g. only chrome-2 is logged into Twitter).
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

logger = logging.getLogger(__name__)


class NoCleanProfileError(RuntimeError):
    """No explicitly anonymous browser profile is available for acquisition."""

    code = "no_clean_profile"

    def __init__(self) -> None:
        super().__init__(self.code)


class NoReadyBrowserSlotError(RuntimeError):
    """No slot has reported an exact desired/loaded runtime match and self-check."""

    code = "no_ready_browser_slot"

    def __init__(self) -> None:
        super().__init__(self.code)


class LocalBrowserPool:
    """In-process pool backed by per-endpoint asyncio.Queue slots.

    Each slot holds exactly one token (the endpoint URL string).  Acquiring a
    slot removes the token; releasing puts it back.  This ensures at most one
    concurrent task per Chrome instance.

    Unrouted acquire() races all slots and takes whichever becomes available
    first, mimicking the previous single-queue round-robin behaviour.
    """

    def __init__(self, endpoints: list[str]) -> None:
        # One single-element queue per endpoint acting as a semaphore
        self._slots: dict[str, asyncio.Queue[str]] = {}
        for ep in endpoints:
            q: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
            q.put_nowait(ep)
            self._slots[ep] = q
        self._total = len(endpoints)
        # mode per endpoint: "bridge" (opencli 1.0.0 daemon) or "cdp" (opencli 0.9.6 Playwright)
        self._modes: dict[str, str] = {ep: "bridge" for ep in endpoints}
        # agent_url per endpoint: HTTP base URL of the agent server (COLLECTION_MODE=agent only)
        self._agent_urls: dict[str, str | None] = {ep: None for ep in endpoints}
        # agent_protocol per endpoint: "http" (LAN/proxy) or "ws" (NAT reverse channel, Phase 2)
        self._agent_protocols: dict[str, str | None] = {ep: None for ep in endpoints}
        # node_type per endpoint: "docker" (started in container) | "shell" (native process)
        self._node_types: dict[str, str] = {ep: "docker" for ep in endpoints}
        # Fail closed: an endpoint is potentially personalized until an agent
        # or operator explicitly registers it as a dedicated anonymous profile.
        self._profile_kinds: dict[str, str] = {ep: "authenticated" for ep in endpoints}
        # Legacy slots have no runtime bundle and retain their historic
        # admission behaviour. A slot assigned a bundle is immediately set to
        # DEGRADED until a loaded-fact report proves it READY.
        self._runtime_states: dict[str, str] = {ep: "LEGACY" for ep in endpoints}
        # Profile write leases are independent of endpoint leases. They defend
        # against accidental duplicate profile assignment across Chromium slots.
        self._profile_names: dict[str, str] = {ep: ep for ep in endpoints}
        self._profile_locks: dict[str, asyncio.Lock] = {ep: asyncio.Lock() for ep in endpoints}
        logger.info(
            "BrowserPool (local): %d Chrome instance(s): %s",
            self._total,
            list(endpoints),
        )

    @asynccontextmanager
    async def acquire(
        self,
        endpoint: str | None = None,
        *,
        required_profile_kind: str | None = None,
    ) -> AsyncIterator[str]:
        if required_profile_kind and (
            endpoint is None
            or endpoint not in self._slots
            or self.get_profile_kind(endpoint) != required_profile_kind
        ):
            raise NoCleanProfileError()
        if endpoint is not None:
            if endpoint not in self._slots:
                logger.warning("Requested Chrome endpoint %r is not registered.", endpoint)
                raise NoReadyBrowserSlotError()
            if not self.is_ready(endpoint):
                raise NoReadyBrowserSlotError()
            get_task = asyncio.create_task(self._slots[endpoint].get())
            try:
                ep = await asyncio.shield(get_task)
            except asyncio.CancelledError:
                if not get_task.done():
                    get_task.cancel()
                try:
                    token = await get_task
                except (asyncio.CancelledError, Exception):
                    pass
                else:
                    self._restore_slot(endpoint, token)
                raise
            logger.debug("Chrome acquired (routed): %s", ep)
        else:
            acquire_task = asyncio.create_task(self._acquire_any())
            try:
                ep = await asyncio.shield(acquire_task)
            except asyncio.CancelledError:
                if not acquire_task.done():
                    acquire_task.cancel()
                try:
                    acquired_endpoint = await acquire_task
                except (asyncio.CancelledError, Exception):
                    pass
                else:
                    self._restore_slot(acquired_endpoint)
                raise

        if required_profile_kind and self.get_profile_kind(ep) != required_profile_kind:
            self._restore_slot(ep)
            raise NoCleanProfileError()

        profile_lock = self._profile_locks.setdefault(self.get_profile_name(ep), asyncio.Lock())
        lock_acquired = False
        try:
            lock_task = asyncio.create_task(profile_lock.acquire())
            try:
                lock_acquired = await asyncio.shield(lock_task)
            except asyncio.CancelledError:
                if not lock_task.done():
                    lock_task.cancel()
                try:
                    lock_acquired = await lock_task
                except (asyncio.CancelledError, Exception):
                    lock_acquired = False
                else:
                    if lock_acquired:
                        profile_lock.release()
                        lock_acquired = False
                raise
            lock_acquired = True
            yield ep
        finally:
            if lock_acquired:
                profile_lock.release()
            self._restore_slot(ep)
            logger.debug("Chrome released: %s", ep)

    @asynccontextmanager
    async def acquire_anonymous(self) -> AsyncIterator[str]:
        endpoint = self.select_anonymous_endpoint()
        async with self.acquire(endpoint, required_profile_kind="anonymous") as acquired_endpoint:
            yield acquired_endpoint

    def anonymous_endpoints(self) -> list[str]:
        candidates = [
            endpoint
            for endpoint in self.endpoints
            if self.get_profile_kind(endpoint) == "anonymous"
        ]
        if not candidates:
            raise NoCleanProfileError()
        ready_candidates = [endpoint for endpoint in candidates if self.is_ready(endpoint)]
        if not ready_candidates:
            raise NoReadyBrowserSlotError()
        return ready_candidates

    def select_anonymous_endpoint(self) -> str:
        candidates = self.anonymous_endpoints()
        return next(
            (endpoint for endpoint in candidates if self.available_for(endpoint)),
            candidates[0],
        )

    def _restore_slot(self, endpoint: str, token: str | None = None) -> None:
        slot = self._slots.get(endpoint)
        if slot is None or slot.full():
            return
        slot.put_nowait(endpoint if token is None else token)

    async def _acquire_any(self) -> str:
        """Wait for whichever READY endpoint slot becomes free first."""
        ready_slots = {ep: slot for ep, slot in self._slots.items() if self.is_ready(ep)}
        if not ready_slots:
            raise NoReadyBrowserSlotError()
        tasks: dict[asyncio.Task[str], str] = {
            asyncio.get_event_loop().create_task(slot.get()): ep for ep, slot in ready_slots.items()
        }
        winner_task: asyncio.Task[str] | None = None
        keep_winner = False
        try:
            done, _ = await asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED)
            winner_task = next(
                (
                    task
                    for task in done
                    if not task.cancelled() and task.exception() is None
                ),
                None,
            )
            if winner_task is None:
                raise RuntimeError("No browser slot acquisition completed")
            for task in tasks:
                if task is not winner_task and not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            winner_ep = tasks[winner_task]
            keep_winner = True
            logger.debug(
                "Chrome acquired (any): %s (available: %d/%d)",
                winner_ep,
                self.available,
                self._total,
            )
            return winner_ep
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for task, endpoint in tasks.items():
                if keep_winner and task is winner_task:
                    continue
                if task.cancelled():
                    continue
                try:
                    token = task.result()
                except BaseException:
                    continue
                self._restore_slot(endpoint, token)

    @property
    def total(self) -> int:
        return self._total

    @property
    def available(self) -> int:
        return sum(1 for q in self._slots.values() if not q.empty())

    @property
    def endpoints(self) -> list[str]:
        return list(self._slots.keys())

    def available_for(self, endpoint: str) -> bool:
        q = self._slots.get(endpoint)
        return q is not None and not q.empty()

    def get_mode(self, endpoint: str) -> str:
        """Return the connection mode for the given endpoint ("bridge" or "cdp")."""
        return self._modes.get(endpoint, "bridge")

    def set_mode(self, endpoint: str, mode: str) -> None:
        """Update the connection mode for an endpoint at runtime."""
        self._modes[endpoint] = mode
        logger.info("BrowserPool: endpoint %s mode set to %s", endpoint, mode)

    def get_agent_url(self, endpoint: str) -> str | None:
        """Return the agent HTTP base URL for the given endpoint."""
        return self._agent_urls.get(endpoint)

    def set_agent_url(self, endpoint: str, agent_url: str | None) -> None:
        """Update the agent URL for an endpoint at runtime."""
        self._agent_urls[endpoint] = agent_url
        logger.info("BrowserPool: endpoint %s agent_url set to %s", endpoint, agent_url)

    def get_agent_protocol(self, endpoint: str) -> str | None:
        """Return the agent protocol for the given endpoint ("http", "ws", or None)."""
        return self._agent_protocols.get(endpoint)

    def set_agent_protocol(self, endpoint: str, protocol: str | None) -> None:
        """Update the agent protocol for an endpoint at runtime."""
        self._agent_protocols[endpoint] = protocol
        logger.info("BrowserPool: endpoint %s agent_protocol set to %s", endpoint, protocol)

    def get_node_type(self, endpoint: str) -> str:
        """Return deployment type: 'docker' (started in container) or 'shell' (native process)."""
        return self._node_types.get(endpoint, "docker")

    def set_node_type(self, endpoint: str, node_type: str) -> None:
        """Update the node deployment type for an endpoint."""
        self._node_types[endpoint] = node_type
        logger.info("BrowserPool: endpoint %s node_type set to %s", endpoint, node_type)

    def get_profile_kind(self, endpoint: str) -> str:
        return self._profile_kinds.get(endpoint, "authenticated")

    def set_profile_kind(self, endpoint: str, profile_kind: str) -> None:
        if profile_kind not in {"anonymous", "authenticated"}:
            raise ValueError("profile_kind must be 'anonymous' or 'authenticated'")
        self._profile_kinds[endpoint] = profile_kind
        logger.info(
            "BrowserPool: endpoint %s profile_kind set to %s",
            endpoint,
            profile_kind,
        )

    def get_profile_name(self, endpoint: str) -> str:
        return self._profile_names.get(endpoint, endpoint)

    def set_profile_name(self, endpoint: str, profile_name: str) -> None:
        if not profile_name:
            raise ValueError("profile_name must not be empty")
        self._profile_names[endpoint] = profile_name
        self._profile_locks.setdefault(profile_name, asyncio.Lock())

    def runtime_status(self, endpoint: str) -> str:
        return self._runtime_states.get(endpoint, "LEGACY")

    def is_ready(self, endpoint: str) -> bool:
        return self.runtime_status(endpoint) in {"READY", "LEGACY"}

    def set_runtime_status(self, endpoint: str, state: str) -> None:
        self._runtime_states[endpoint] = state
        logger.info("BrowserPool: endpoint %s runtime status set to %s", endpoint, state)

    def add_endpoint(self, endpoint: str) -> None:
        """Hot-add a new Chrome instance to the pool without restarting."""
        if endpoint in self._slots:
            return
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        q.put_nowait(endpoint)
        self._slots[endpoint] = q
        self._modes.setdefault(endpoint, "bridge")
        self._agent_urls.setdefault(endpoint, None)
        self._agent_protocols.setdefault(endpoint, None)
        self._node_types.setdefault(endpoint, "docker")
        self._runtime_states.setdefault(endpoint, "LEGACY")
        self._profile_names.setdefault(endpoint, endpoint)
        self._profile_locks.setdefault(self._profile_names[endpoint], asyncio.Lock())
        self._profile_kinds.setdefault(endpoint, "authenticated")
        self._total += 1
        logger.info("BrowserPool: added endpoint %s (total: %d)", endpoint, self._total)

    def remove_endpoint(self, endpoint: str) -> None:
        """Remove a Chrome instance from the pool (best-effort; waits for slot to be free)."""
        if endpoint not in self._slots:
            return
        self._slots.pop(endpoint)
        self._modes.pop(endpoint, None)
        self._agent_urls.pop(endpoint, None)
        self._agent_protocols.pop(endpoint, None)
        self._node_types.pop(endpoint, None)
        self._runtime_states.pop(endpoint, None)
        self._profile_names.pop(endpoint, None)
        self._profile_kinds.pop(endpoint, None)
        self._total -= 1
        logger.info("BrowserPool: removed endpoint %s (total: %d)", endpoint, self._total)

class RedisBrowserPool:
    """Distributed pool backed by one owner-CAS lease per endpoint.

    Safe across multiple Celery worker processes and machines.

    Routing support:
      - Unrouted acquire() tries every ready endpoint and takes the first lease
        it wins.
      - Routed acquire(endpoint=...) tries only that endpoint's lease.  Routed
        and unrouted acquisition therefore compete for the same authority.

    Initialisation is idempotent (SET NX lock) so multiple API/worker
    replicas can safely register the same endpoint set.
    """

    _LOCK_KEY = "browser_pool:initialized"
    _REGISTRY_KEY = "browser_pool:registered"
    _LEASE_TTL_MS = 30_000
    _LEASE_RENEW_SECONDS = 10
    _ACQUIRE_TIMEOUT_SECONDS = 300
    _RETRY_SECONDS = 0.1

    @staticmethod
    def _ep_key(endpoint: str) -> str:
        """Per-endpoint Redis namespace key (safe characters only)."""
        safe = endpoint.replace("://", "_").replace(":", "_").replace("/", "_")
        return f"browser_pool:ep:{safe}"

    @classmethod
    def _lease_key(cls, endpoint: str) -> str:
        return f"{cls._ep_key(endpoint)}:lease"

    @classmethod
    def _fence_key(cls, endpoint: str) -> str:
        return f"{cls._ep_key(endpoint)}:fence"

    def __init__(self, endpoints: list[str], redis_url: str) -> None:
        self._endpoints = list(endpoints)
        self._redis_url = redis_url
        self._total = len(endpoints)
        self._modes: dict[str, str] = {ep: "bridge" for ep in endpoints}
        self._agent_urls: dict[str, str | None] = {ep: None for ep in endpoints}
        self._agent_protocols: dict[str, str | None] = {ep: None for ep in endpoints}
        self._node_types: dict[str, str] = {ep: "docker" for ep in endpoints}
        self._profile_kinds: dict[str, str] = {ep: "authenticated" for ep in endpoints}
        self._runtime_states: dict[str, str] = {ep: "LEGACY" for ep in endpoints}
        self._profile_names: dict[str, str] = {ep: ep for ep in endpoints}

    def _client(self):
        import redis.asyncio as aioredis  # type: ignore[import]

        return aioredis.from_url(self._redis_url, decode_responses=True)

    async def initialize(self) -> None:
        """Register known endpoints without maintaining a second token ledger."""
        async with self._client() as r:
            acquired = await r.set(self._LOCK_KEY, "1", nx=True)
            if not acquired:
                logger.info("BrowserPool (Redis): pool already initialized by another replica")
                return

            if self._endpoints:
                await r.sadd(self._REGISTRY_KEY, *self._endpoints)

            logger.info(
                "BrowserPool (Redis): %d Chrome instance(s) initialised",
                self._total,
            )

    async def register_endpoint(self, endpoint: str) -> None:
        """Add a DB-discovered endpoint to every worker and Redis exactly once."""
        if endpoint not in self._endpoints:
            self._endpoints.append(endpoint)
            self._total += 1
            self._modes.setdefault(endpoint, "bridge")
            self._agent_urls.setdefault(endpoint, None)
            self._agent_protocols.setdefault(endpoint, None)
            self._node_types.setdefault(endpoint, "docker")
            self._profile_kinds.setdefault(endpoint, "authenticated")

        async with self._client() as r:
            self._runtime_states.setdefault(endpoint, "LEGACY")
            self._profile_names.setdefault(endpoint, endpoint)
            added = await r.sadd(self._REGISTRY_KEY, endpoint)
            if not added:
                return

    @asynccontextmanager
    async def acquire(
        self,
        endpoint: str | None = None,
        *,
        required_profile_kind: str | None = None,
    ) -> AsyncIterator[str]:
        if required_profile_kind and (
            endpoint is None
            or endpoint not in self._endpoints
            or self.get_profile_kind(endpoint) != required_profile_kind
        ):
            raise NoCleanProfileError()
        if endpoint is not None and endpoint not in self._endpoints:
            logger.warning("Requested Chrome endpoint %r is not registered.", endpoint)
            raise NoReadyBrowserSlotError()
        if endpoint is not None and not self.is_ready(endpoint):
            raise NoReadyBrowserSlotError()

        candidates = (
            [endpoint]
            if endpoint is not None
            else [candidate for candidate in self._endpoints if self.is_ready(candidate)]
        )
        if not candidates:
            raise NoReadyBrowserSlotError()

        deadline = time.monotonic() + self._ACQUIRE_TIMEOUT_SECONDS
        ep: str | None = None
        owner: str | None = None
        cleanup_ep: str | None = None
        cleanup_owner: str | None = None
        renewal_task: asyncio.Task[None] | None = None
        stop_renewal = asyncio.Event()
        try:
            while time.monotonic() < deadline and ep is None:
                for candidate in candidates:
                    owner_nonce = str(uuid4())
                    candidate_owner: str
                    # Track ownership before every await.  If cancellation
                    # arrives after Redis accepts SET NX but before Python
                    # receives its result, owner-CAS cleanup still applies.
                    cleanup_ep = candidate
                    cleanup_owner = None
                    async with self._client() as r:
                        fence = await r.incr(self._fence_key(candidate))
                        candidate_owner = f"{fence}:{owner_nonce}"
                        cleanup_owner = candidate_owner
                        acquired = await r.set(
                            self._lease_key(candidate),
                            candidate_owner,
                            nx=True,
                            px=self._LEASE_TTL_MS,
                        )
                    if acquired:
                        ep = candidate
                        owner = candidate_owner
                        break
                    cleanup_ep = None
                    cleanup_owner = None
                if ep is None:
                    await asyncio.sleep(self._RETRY_SECONDS)
            if ep is None or owner is None:
                raise TimeoutError("No Chrome endpoint lease became available in time")

            owning_task = asyncio.current_task()
            lease_endpoint = ep
            lease_owner = owner

            async def renew() -> None:
                while True:
                    try:
                        await asyncio.wait_for(
                            stop_renewal.wait(), timeout=self._LEASE_RENEW_SECONDS
                        )
                        return
                    except TimeoutError:
                        pass
                    async with self._client() as r:
                        renewed = await r.eval(
                            "if redis.call('get',KEYS[1]) == ARGV[1] then "
                            "return redis.call('pexpire',KEYS[1],ARGV[2]) else return 0 end",
                            1,
                            self._lease_key(lease_endpoint),
                            lease_owner,
                            self._LEASE_TTL_MS,
                        )
                    if not renewed:
                        if owning_task is not None:
                            owning_task.cancel()
                        return

            renewal_task = asyncio.create_task(renew())
            yield ep
        finally:
            stop_renewal.set()
            if renewal_task is not None:
                renewal_task.cancel()
                await asyncio.gather(renewal_task, return_exceptions=True)
            if cleanup_ep is not None and cleanup_owner is not None:
                async with self._client() as r:
                    await r.eval(
                        "if redis.call('get',KEYS[1]) == ARGV[1] then "
                        "return redis.call('del',KEYS[1]) else return 0 end",
                        1,
                        self._lease_key(cleanup_ep),
                        cleanup_owner,
                    )
            if ep is not None:
                logger.debug("Chrome lease released (Redis): %s", ep)

    @property
    def total(self) -> int:
        return self._total

    @property
    def available(self) -> int:
        return -1  # unknown without a synchronous Redis call

    @property
    def endpoints(self) -> list[str]:
        return list(self._endpoints)

    def available_for(self, endpoint: str) -> bool:
        return endpoint in self._endpoints  # approximate; real check would need Redis

    def get_mode(self, endpoint: str) -> str:
        return self._modes.get(endpoint, "bridge")

    def set_mode(self, endpoint: str, mode: str) -> None:
        self._modes[endpoint] = mode

    def get_agent_url(self, endpoint: str) -> str | None:
        """Return the agent HTTP base URL for the given endpoint."""
        return self._agent_urls.get(endpoint)

    def set_agent_url(self, endpoint: str, agent_url: str | None) -> None:
        """Update the agent URL for an endpoint at runtime."""
        self._agent_urls[endpoint] = agent_url

    def get_agent_protocol(self, endpoint: str) -> str | None:
        """Return the agent protocol for the given endpoint ("http", "ws", or None)."""
        return self._agent_protocols.get(endpoint)

    def set_agent_protocol(self, endpoint: str, protocol: str | None) -> None:
        """Update the agent protocol for an endpoint at runtime."""
        self._agent_protocols[endpoint] = protocol

    def get_node_type(self, endpoint: str) -> str:
        """Return deployment type: 'docker' or 'shell'."""
        return self._node_types.get(endpoint, "docker")

    def set_node_type(self, endpoint: str, node_type: str) -> None:
        """Update the node deployment type for an endpoint."""
        self._node_types[endpoint] = node_type

    def get_profile_kind(self, endpoint: str) -> str:
        return self._profile_kinds.get(endpoint, "authenticated")

    def set_profile_kind(self, endpoint: str, profile_kind: str) -> None:
        if profile_kind not in {"anonymous", "authenticated"}:
            raise ValueError("profile_kind must be 'anonymous' or 'authenticated'")
        self._profile_kinds[endpoint] = profile_kind

    def get_profile_name(self, endpoint: str) -> str:
        return self._profile_names.get(endpoint, endpoint)

    def set_profile_name(self, endpoint: str, profile_name: str) -> None:
        if not profile_name:
            raise ValueError("profile_name must not be empty")
        self._profile_names[endpoint] = profile_name

    def runtime_status(self, endpoint: str) -> str:
        return self._runtime_states.get(endpoint, "LEGACY")

    def is_ready(self, endpoint: str) -> bool:
        return self.runtime_status(endpoint) in {"READY", "LEGACY"}

    def set_runtime_status(self, endpoint: str, state: str) -> None:
        self._runtime_states[endpoint] = state

    def anonymous_endpoints(self) -> list[str]:
        candidates = [
            endpoint
            for endpoint in self.endpoints
            if self.get_profile_kind(endpoint) == "anonymous" and self.is_ready(endpoint)
        ]
        if not candidates:
            raise NoCleanProfileError()
        return candidates

    def select_anonymous_endpoint(self) -> str:
        return self.anonymous_endpoints()[0]


# ── Module-level singleton ────────────────────────────────────────────────────

_pool: LocalBrowserPool | RedisBrowserPool | None = None


def init_pool(
    endpoints: list[str],
    use_redis: bool = False,
    redis_url: str = "",
) -> LocalBrowserPool | RedisBrowserPool:
    global _pool
    if use_redis and redis_url:
        _pool = RedisBrowserPool(endpoints, redis_url)
    else:
        _pool = LocalBrowserPool(endpoints)
    return _pool


async def ensure_ready() -> None:
    """Async post-init step (populates Redis lists if applicable)."""
    if isinstance(_pool, RedisBrowserPool):
        await _pool.initialize()


def get_pool() -> LocalBrowserPool | RedisBrowserPool:
    if _pool is None:
        raise RuntimeError("BrowserPool not initialized — call init_pool() first")
    return _pool
