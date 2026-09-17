"""Phase 1 HTTP infra tests: parse_rate, the token bucket, and the retry/backoff
client (429/5xx retry, Retry-After honored, give-up after max_retries)."""

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from backend.pipeline.http_client import RateLimitedClient, TokenBucket, parse_rate


class FakeClient:
    """Returns queued responses in order; counts requests."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.calls += 1
        return self._responses.pop(0)

    async def aclose(self) -> None:
        pass


# ── parse_rate ───────────────────────────────────────────────────────────────

def test_parse_rate():
    assert parse_rate("60/min") == 1.0
    assert parse_rate("10/sec") == 10.0
    assert parse_rate("120/hour") == pytest.approx(120 / 3600)
    assert parse_rate("garbage") == 1.0  # unparseable → safe default


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" 60 / MINUTE ", 1.0),
        ("10/Second", 10.0),
        ("0.5/sec", 0.5),
    ],
)
def test_parse_rate_preserves_valid_spacing_case_and_fraction(value, expected):
    assert parse_rate(value) == expected


@pytest.mark.parametrize("value", ["0/sec", "-1/sec", "nan/sec", "inf/sec", "60", "60/day", ""])
def test_parse_rate_rejects_invalid_values(value):
    assert parse_rate(value) == 1.0


@pytest.mark.parametrize("value", ["1e-400/sec", "5e-324/hour"])
def test_parse_rate_rejects_underflow_to_zero(value):
    assert parse_rate(value) == 1.0


@pytest.mark.parametrize(
    ("rate", "capacity"),
    [
        (0, None), (-1, None), (float("nan"), None), (float("inf"), None),
        (1, 0), (1, 0.5), (1, float("nan")), (1, float("inf")),
        (10 ** 400, None), (1, 10 ** 400),
    ],
)
def test_token_bucket_rejects_invalid_parameters(rate, capacity):
    with pytest.raises(ValueError):
        TokenBucket(rate, capacity)


# ── token bucket ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tiny_positive_rate_preserves_finite_wait(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("backend.pipeline.http_client.time.monotonic", lambda: 100.0)
    monkeypatch.setattr(
        "backend.pipeline.http_client.asyncio.sleep", lambda d: slept.append(d) or _noop()
    )
    bucket = TokenBucket(parse_rate("5e-324/sec"))

    await bucket.acquire()
    await bucket.acquire()

    assert slept == [1_000_000.0]  # the existing minimum rate still bounds the wait


@pytest.mark.asyncio
async def test_token_bucket_bursts_then_blocks(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr("backend.pipeline.http_client.asyncio.sleep", fake_sleep)

    bucket = TokenBucket(rate=10, capacity=5)
    for _ in range(5):
        await bucket.acquire()  # full bucket → no wait
    assert slept == []
    await bucket.acquire()      # empty → must wait
    assert len(slept) == 1 and slept[0] > 0


# ── retry / backoff ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(
        "backend.pipeline.http_client.asyncio.sleep", lambda d: slept.append(d) or _noop()
    )

    client = FakeClient([httpx.Response(429), httpx.Response(200)])
    rl = RateLimitedClient(client, TokenBucket(1000), log=None)
    resp = await rl.get("http://x")

    assert resp.status_code == 200
    assert client.calls == 2
    assert len(slept) == 1  # one backoff sleep (bucket full → no rate wait)


@pytest.mark.asyncio
async def test_honors_retry_after_header(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(
        "backend.pipeline.http_client.asyncio.sleep", lambda d: slept.append(d) or _noop()
    )

    client = FakeClient([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)])
    rl = RateLimitedClient(client, TokenBucket(1000))
    resp = await rl.get("http://x")

    assert resp.status_code == 200
    assert 7.0 in slept  # exact Retry-After used, not exponential backoff


@pytest.mark.asyncio
@pytest.mark.parametrize("header, expected", [("0", 0.0), ("7.5", 7.5), ("nan", 1.25)])
async def test_retry_after_values_control_request_cycle(monkeypatch, header, expected):
    slept: list[float] = []
    monkeypatch.setattr("backend.pipeline.http_client.random.uniform", lambda _start, _end: 0.25)
    monkeypatch.setattr(
        "backend.pipeline.http_client.asyncio.sleep", lambda d: slept.append(d) or _noop()
    )

    client = FakeClient([httpx.Response(429, headers={"Retry-After": header}), httpx.Response(200)])
    rl = RateLimitedClient(client, TokenBucket(1000), max_retries=1)

    response = await rl.get("http://x")

    assert response.status_code == 200
    assert client.calls == 2
    assert slept == [expected]


@pytest.mark.parametrize(
    "header",
    [
        "Wed, 21 Oct 2037 07:28:00 GMT",
        "Wednesday, 21-Oct-37 07:28:00 GMT",
        "Wed Oct 21 07:28:00 2037",
    ],
)
def test_retry_after_parses_all_http_date_forms(monkeypatch, header):
    now = datetime(2036, 10, 21, 7, 28, tzinfo=UTC).timestamp()
    monkeypatch.setattr("backend.pipeline.http_client.time.time", lambda: now)

    response = httpx.Response(429, headers={"Retry-After": header})
    expected_date = datetime(2037, 10, 21, 7, 28, tzinfo=UTC)

    assert RateLimitedClient._retry_after(response) == pytest.approx(
        expected_date.timestamp() - now
    )


def test_retry_after_past_date_returns_zero(monkeypatch):
    monkeypatch.setattr("backend.pipeline.http_client.time.time", lambda: 2_000_000_000.0)
    response = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})

    assert RateLimitedClient._retry_after(response) == 0.0


@pytest.mark.parametrize("header", ["", "not-a-date", "-1", "nan", "inf", "1e999"])
def test_retry_after_rejects_invalid_waits(monkeypatch, header):
    monkeypatch.setattr("backend.pipeline.http_client.time.time", lambda: 2_000_000_000.0)
    response = httpx.Response(429, headers={"Retry-After": header})

    assert RateLimitedClient._retry_after(response) is None


@pytest.mark.asyncio
async def test_future_http_date_controls_retry_cycle(monkeypatch):
    now = datetime(2036, 10, 21, 7, 28, tzinfo=UTC).timestamp()
    delay = datetime(2037, 10, 21, 7, 28, tzinfo=UTC).timestamp() - now
    slept: list[float] = []
    monkeypatch.setattr("backend.pipeline.http_client.time.time", lambda: now)
    monkeypatch.setattr(
        "backend.pipeline.http_client.asyncio.sleep", lambda d: slept.append(d) or _noop()
    )

    client = FakeClient(
        [
            httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2037 07:28:00 GMT"}),
            httpx.Response(200),
        ]
    )
    rl = RateLimitedClient(client, TokenBucket(1000), max_retries=1)

    response = await rl.get("http://x")

    assert response.status_code == 200
    assert client.calls == 2
    assert slept == [pytest.approx(delay)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_permanent_http_statuses_are_returned_without_retry(monkeypatch, status):
    slept: list[float] = []
    monkeypatch.setattr(
        "backend.pipeline.http_client.asyncio.sleep", lambda d: slept.append(d) or _noop()
    )
    client = FakeClient([httpx.Response(status)])
    rl = RateLimitedClient(client, TokenBucket(1000))

    response = await rl.get("http://x")

    assert response.status_code == status
    assert client.calls == 1
    assert slept == []


@pytest.mark.asyncio
async def test_gives_up_after_max_retries(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(
        "backend.pipeline.http_client.asyncio.sleep", lambda d: slept.append(d) or _noop()
    )

    client = FakeClient([httpx.Response(503) for _ in range(10)])
    rl = RateLimitedClient(client, TokenBucket(1000), max_retries=3)
    resp = await rl.get("http://x")

    assert resp.status_code == 503
    assert client.calls == 4  # initial + 3 retries
    assert len(slept) == 3  # budget exhaustion adds no trailing sleep


# ── AUDIT C13: gateway statuses (504 + Cloudflare 520/522/524) retry too ────

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [504, 520, 522, 524])
async def test_retries_on_gateway_statuses_then_succeeds(monkeypatch, status):
    """504 (gateway timeout) and Cloudflare's 520/522/524 are transient
    upstream/proxy conditions same as 502/503 — RETRY_STATUS previously
    stopped at 503, so these leaked straight to the caller with zero retry."""
    monkeypatch.setattr("backend.pipeline.http_client.asyncio.sleep", lambda d: _noop())

    client = FakeClient([httpx.Response(status), httpx.Response(200)])
    rl = RateLimitedClient(client, TokenBucket(1000), log=None)
    resp = await rl.get("http://x")

    assert resp.status_code == 200
    assert client.calls == 2


async def _noop() -> None:
    return None
