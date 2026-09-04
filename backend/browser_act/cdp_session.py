"""CDP-backed browser-act session for an already-running Chrome profile.

This adapter is intentionally narrow: trusted BrowserAct pack steps can reuse a
headed Chrome over CDP without launching a second browser or copying cookies.
Closing the Playwright connection detaches from Chrome; it never closes the
underlying browser or its persistent profile.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlparse


def _url_key(url: str) -> tuple[str, str, str, str]:
    parsed = urlparse(url)
    return (
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        unquote(parsed.path),
        unquote(parsed.query),
    )


class CdpSessionError(RuntimeError):
    """Raised when a configured persistent CDP session cannot be used."""


class CdpBrowserActSession:
    """Minimal session interface consumed by ``BrowserActChannel._run_page``."""

    def __init__(self, endpoint: str, target_url: str | None = None) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise CdpSessionError("cdp_endpoint must be an absolute http(s) URL")
        self.endpoint = endpoint.rstrip("/")
        self.target_url = target_url
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None

    @property
    def page(self) -> Any:
        if self._page is None:
            raise CdpSessionError("persistent CDP session is not open")
        return self._page

    async def __aenter__(self) -> CdpBrowserActSession:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(self.endpoint)
            context = self._browser.contexts[0] if self._browser.contexts else None
            if context is None:
                raise CdpSessionError("connected Chrome has no browser context")
            pages = context.pages
            target_key = _url_key(self.target_url) if self.target_url else None
            self._page = next(
                (page for page in pages if target_key and _url_key(page.url) == target_key),
                pages[0] if pages else await context.new_page(),
            )
        except CdpSessionError:
            await self._close_connection()
            raise
        except Exception as exc:
            await self._close_connection()
            raise CdpSessionError(f"could not connect to persistent Chrome: {exc}") from exc
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._close_connection()

    async def _close_connection(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            finally:
                self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            finally:
                self._playwright = None
        self._page = None
    async def navigate(self, url: str) -> str:
        try:
            if self._page.url and _url_key(self._page.url) == _url_key(url):
                return self._page.url
            # Commerce pages often keep subresources open indefinitely. The
            # following stable wait handles hydration; navigation only needs
            # the document shell and must not wait for every asset.
            await self._page.goto(url, wait_until="domcontentloaded")
            return self._page.url
        except Exception as exc:
            raise CdpSessionError(f"navigation failed: {exc}") from exc

    async def wait(self, mode: str = "stable") -> None:
        try:
            if mode in {"load", "domcontentloaded", "networkidle"}:
                await self._page.wait_for_load_state(mode)
            else:
                # Listing pages hydrate asynchronously after the document
                # shell. Give client-side product cards a bounded settle
                # window without waiting for network-idle, which may never
                # occur on commerce sites.
                await self._page.wait_for_timeout(5000)
        except Exception as exc:
            raise CdpSessionError(f"wait failed: {exc}") from exc

    async def wait_for_selector(self, selector: str, *, timeout: float = 8000) -> None:
        """Wait for a hydrated element without paying the fixed settle delay."""
        try:
            await self._page.wait_for_load_state("domcontentloaded")
            await self._page.wait_for_selector(
                selector, state="attached", timeout=timeout
            )
        except Exception as exc:
            raise CdpSessionError(f"selector wait failed: {exc}") from exc

    async def eval(self, js: str) -> str:
        try:
            result = await self._page.evaluate(js)
            return str(result)
        except Exception as exc:
            raise CdpSessionError(f"script evaluation failed: {exc}") from exc

    async def run(self, args: list[str], *, timeout: float | None = None) -> str:
        if args[:2] == ["scroll", "down"]:
            try:
                amount = 1000
                if "--amount" in args:
                    amount = int(args[args.index("--amount") + 1])
                await self._page.evaluate("amount => window.scrollBy(0, amount)", amount)
                return ""
            except Exception as exc:
                raise CdpSessionError(f"scroll failed: {exc}") from exc
        raise CdpSessionError(f"unsupported CDP session operation: {' '.join(args)}")

    async def click(self, index: int, *, timeout: float | None = None) -> str:
        raise CdpSessionError("click steps are not supported by persistent cdp_endpoint sessions")

    async def input(self, index: int, value: str, *, timeout: float | None = None) -> str:
        raise CdpSessionError("input steps are not supported by persistent cdp_endpoint sessions")
