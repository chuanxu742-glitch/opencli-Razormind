"""The **record** leg — capture a human demonstration into ``journey_trace_v1``
(2026-07-01 addendum, ADR-0003). The one leg of the closed loop
(record → distill → store → execute → correct) that was never built: every
prior skill had to be hand-crafted as a raw trace dict and POSTed at the API.

Design (mirrors the execute leg's substrate — no new browser plumbing):

  * **Same CDP attach** as the execute leg — :func:`backend.skills.page.
    open_skill_page` connects to an **already-running, human-visible (headed)**
    Chrome via ``connect_over_cdp``. The human drives that real window directly;
    this module never renders a live view or streams video (ADR-0003 record
    addendum: this whole subsystem is single-user/local — the human is already
    looking at the same machine's screen).
  * **Capture, not drive.** Unlike :mod:`backend.skills.loop` (a cheap model
    *proposes* one action per step and an executor runs it), here a human acts
    freely and the page **reports** what happened via a small injected listener
    + ``page.expose_binding`` — the one genuinely new piece of browser
    plumbing in this codebase (no prior ``expose_binding``/event-listener
    precedent existed; ``loop.py`` is purely action-driven).
  * **Semantic capture, not ref replay.** The listener reports each element's
    *accessible name* (same algorithm as :data:`backend.skills.perception.
    SNAPSHOT_JS`: aria-label → text → placeholder → title → name → value), not
    a numeric ``data-skill-ref``. A recorded trace never needs to be replayed
    ref-for-ref — the distiller reads it holistically and the execute leg's
    *own* cheap model re-perceives the live page and picks its own refs at run
    time ("举一反三,非复刻坐标" — generalize, don't hard-code coordinates). This
    also sidesteps keeping a capture-time ref scheme in sync with
    ``perception.py``'s independent per-step tagging.
  * **Same trace shape.** Steps are built as the *same* :class:`~backend.skills
    .loop.StepRecord` dataclass the execute leg produces, and
    :func:`~backend.skills.trace.assemble_trace` (unchanged, zero new code)
    assembles the final ``journey_trace_v1`` — the distiller
    (:func:`backend.skills.distill.distill_trace`) needs no adaptation to
    consume a human-recorded trace.
  * **Human asserts the outcome.** ``stop(status=...)`` is the human explicitly
    marking "this demo succeeded / failed" — the most honest ground truth
    available (the execute leg has to *infer* success via ``self_eval``
    heuristics because no human is present there).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from backend.skills.loop import StepRecord
from backend.skills.page import SkillPage
from backend.skills.trace import assemble_trace, outcome_from_loop

logger = logging.getLogger(__name__)

# Injected once via `page.add_init_script` (Playwright re-runs init scripts on
# every navigation automatically — no manual re-injection on framenavigated
# needed, unlike perception.py's per-step SNAPSHOT_JS which is called fresh
# each loop iteration instead). Capture-phase listeners so a click/change deep
# inside a component still bubbles to `document` before any site handler can
# stop propagation. Reports {verb, name, role, value?, ts} — never `ref` (see
# module docstring "semantic capture, not ref replay").
#
# The guard flag is keyed by `sessionId` (passed as the function arg), NOT a
# fixed name — `open_skill_page` may hand back an *already-open* page (it
# reuses `context.pages[0]`), so a second, unrelated RecordSession attaching to
# the same live page must NOT see a prior session's leftover flag and skip
# installing its own listener (verified live: a fixed flag name silently
# no-ops the second session's capture entirely — the bug this comment is
# guarding against actually happened during development).
CAPTURE_JS = r"""
(sessionId) => {
  const boundKey = '__skillRecordBound_' + sessionId;
  const stateKey = boundKey + '_state';
  const sensitiveKey = boundKey + '_sensitive';
  const currentDocument = document;
  const state = window[stateKey] || {
    document: currentDocument,
    blocked: Boolean(window[sensitiveKey]),
    listenersInstalled: false,
    listenerRevoked: false,
    handlers: null,
  };
  const targetIsCurrent = (event) => {
    if (state.blocked || window[sensitiveKey]) return false;
    if (state.document !== currentDocument || currentDocument.defaultView !== window) return false;
    const target = event && event.target;
    return Boolean(target && target.ownerDocument === currentDocument);
  };
  const nameOf = (el) => {
    if (!el || !el.getAttribute) return '';
    let name = (
      el.getAttribute('aria-label') ||
      (el.textContent || '').trim() ||
      el.getAttribute('placeholder') ||
      el.getAttribute('title') ||
      el.getAttribute('name') ||
      el.getAttribute('value') ||
      ''
    );
    return String(name).replace(/\s+/g, ' ').trim().slice(0, 200);
  };
  const roleOf = (el) =>
    (el && el.getAttribute && el.getAttribute('role')) || ((el && el.tagName) || '').toLowerCase();
  const install = () => {
    if (state.listenersInstalled || state.document !== currentDocument) return;
    const click = (e) => {
      if (!targetIsCurrent(e)) return;
      window.__record_event({ verb: 'click', name: nameOf(e.target), role: roleOf(e.target) });
    };
    const change = (e) => {
      if (!targetIsCurrent(e)) return;
      const tag = ((e.target && e.target.tagName) || '').toLowerCase();
      const isSelect = tag === 'select';
      const inputType = ((e.target && e.target.type) || '').toLowerCase();
      const isSensitive = inputType === 'password';
      window.__record_event({
        verb: isSelect ? 'select' : 'type',
        name: nameOf(e.target),
        role: roleOf(e.target),
        value: isSensitive ? '' : String((e.target && e.target.value) ?? ''),
        redacted: isSensitive,
      });
    };
    const submit = (e) => {
      if (!targetIsCurrent(e)) return;
      window.__record_event({ verb: 'submit', name: nameOf(e.target), role: 'form' });
    };
    state.handlers = { click, change, submit };
    document.addEventListener('click', click, true);
    document.addEventListener('change', change, true);
    document.addEventListener('submit', submit, true);
    state.listenersInstalled = true;
    state.listenerRevoked = false;
  };
  state.install = install;
  state.revoke = () => {
    if (!state.listenersInstalled || !state.handlers) {
      state.listenerRevoked = true;
      return;
    }
    document.removeEventListener('click', state.handlers.click, true);
    document.removeEventListener('change', state.handlers.change, true);
    document.removeEventListener('submit', state.handlers.submit, true);
    state.handlers = null;
    state.listenersInstalled = false;
    state.listenerRevoked = true;
  };
  window[stateKey] = state;
  window[boundKey + '_install'] = install;
  window[boundKey + '_revoke'] = state.revoke;
  install();
}
"""
SENSITIVE_JS = r"""
({sessionId, enabled}) => {
  const boundKey = '__skillRecordBound_' + sessionId;
  const sensitiveKey = boundKey + '_sensitive';
  const state = window[boundKey + '_state'];
  window[sensitiveKey] = Boolean(enabled);
  if (!state) return;
  state.blocked = Boolean(enabled);
  if (enabled) state.revoke();
  else state.install();
}
"""


@dataclass
class RecordSession:
    """One in-progress recording — accumulates :class:`StepRecord`\\ s until
    :meth:`stop` assembles them into a ``journey_trace_v1`` trace.

    Construct via :func:`start_recording`; the caller (the API router, issue
    skill-record) owns the session's lifetime in an in-process dict keyed by
    ``session_id`` — this whole subsystem is single-user/local (no Redis/DB-
    backed session store needed, matching the rest of ADR-0003).
    """

    session_id: str
    domain: str
    capability: str
    page: SkillPage
    steps: list[StepRecord] = field(default_factory=list)
    _last_ts: float = field(default_factory=time.monotonic, repr=False)
    _event_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _drain_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _pending_events: int = field(default=0, init=False, repr=False)
    _listener_installed: bool = field(default=False, init=False, repr=False)
    _listener_revoked: bool = field(default=False, init=False, repr=False)
    _pending_events_drained: bool = field(default=True, init=False, repr=False)
    _document_generation: int = field(default=0, init=False, repr=False)
    _main_frame: Any = field(default=None, init=False, repr=False)
    stopped: bool = False
    sensitive: bool = False
    _trace: dict[str, Any] | None = field(default=None, init=False, repr=False)
    def _raw_page(self) -> Any:
        return getattr(self.page, "page", self.page)

    def _frames(self) -> list[Any]:
        frames = getattr(self._raw_page(), "frames", None)
        if callable(frames):
            frames = frames()
        if frames:
            return list(frames)
        return [self._raw_page()]

    async def _evaluate_frame(self, frame: Any, script: str, value: Any) -> None:
        evaluator = getattr(frame, "evaluate", None)
        if not callable(evaluator):
            raise RuntimeError("record page cannot evaluate capture lifecycle")
        result = evaluator(script, value)
        if hasattr(result, "__await__"):
            await result

    async def _set_page_capture_state(self, enabled: bool) -> None:
        # Keep the flag in every future document; add_init_script is the only
        # reliable way to carry sensitive mode across cross-origin navigation.
        raw_page = self._raw_page()
        add_init_script = getattr(raw_page, "add_init_script", None)
        if not callable(add_init_script):
            raise RuntimeError("record page cannot install capture lifecycle")
        state = (
            f'({SENSITIVE_JS})({{"sessionId": {self.session_id!r}, '
            f'"enabled": {"true" if enabled else "false"}}})'
        )
        result = add_init_script(state)
        if hasattr(result, "__await__"):
            await result
        for frame in self._frames():
            await self._evaluate_frame(
                frame,
                SENSITIVE_JS,
                {"sessionId": self.session_id, "enabled": enabled},
            )

    async def _drain_events(self) -> None:
        if self._pending_events:
            self._drain_event.clear()
            await self._drain_event.wait()
        self._pending_events_drained = True

    async def start(self) -> None:
        """Wire the capture binding and listener onto every live frame."""
        raw_page = self._raw_page()
        await raw_page.expose_binding("__record_event", self._on_event)
        add_init_script = getattr(raw_page, "add_init_script", None)
        if not callable(add_init_script):
            raise RuntimeError("record page cannot install capture lifecycle")
        await add_init_script(f"({CAPTURE_JS})({self.session_id!r})")
        frames = self._frames()
        for frame in frames:
            await self._evaluate_frame(frame, CAPTURE_JS, self.session_id)
        self._main_frame = getattr(raw_page, "main_frame", None)
        self._listener_installed = True
        self._listener_revoked = False
        self._pending_events_drained = True
        raw_page.on("framenavigated", self._on_navigate)

    def binding_identity(self) -> tuple[Any, Any, int, Any]:
        """Return page/frame/document identity without trusting payload fields."""
        raw_page = self._raw_page()
        frame = self._main_frame or getattr(raw_page, "main_frame", None)
        reported_document = getattr(self, "document_id", None)
        return raw_page, frame, self._document_generation, reported_document

    def _append(self, *, verb: str, args: dict[str, Any], target: Any) -> None:
        now = time.monotonic()
        elapsed_ms = int((now - self._last_ts) * 1000)
        self._last_ts = now
        self.steps.append(
            StepRecord(
                index=len(self.steps),
                verb=verb,
                args=args,
                target=target,
                result={"captured": True},
                elapsed_ms=elapsed_ms,
            )
        )

    def _on_navigate(self, frame: Any) -> None:
        # Playwright's `on("framenavigated")` handler is sync; only the main
        # frame counts as a step (iframe navigations are noise for this v1).
        raw_page = self._raw_page()
        main_frame = getattr(raw_page, "main_frame", None)
        if frame is not main_frame:
            return
        self._document_generation += 1
        if self.sensitive or self.stopped:
            return
        self._append(verb="navigate", args={"url": frame.url}, target=frame.url)

    async def _on_event(self, source: dict[str, Any], payload: dict[str, Any]) -> None:
        """`page.expose_binding` callback — one call per captured DOM event."""
        self._pending_events += 1
        self._pending_events_drained = False
        self._drain_event.clear()
        try:
            async with self._event_lock:
                if self.sensitive or self.stopped or self._listener_revoked:
                    return
                verb = payload.get("verb")
                name = payload.get("name") or ""
                role = payload.get("role") or ""
                if verb == "click":
                    args: dict[str, Any] = {"name": name, "role": role}
                elif verb == "type":
                    args = {"name": name, "role": role, "text": payload.get("value", "")}
                elif verb == "select":
                    args = {"name": name, "role": role, "value": payload.get("value", "")}
                elif verb == "submit":
                    args = {"name": name}
                else:  # pragma: no cover - CAPTURE_JS never emits anything else
                    return
                self._append(verb=verb, args=args, target=name)
        finally:
            self._pending_events -= 1
            if self._pending_events == 0:
                self._pending_events_drained = True
                self._drain_event.set()

    async def stop_common_listener(self) -> None:
        await self.set_sensitive(True)

    async def restore_common_listener(self) -> None:
        await self.set_sensitive(False)

    async def drain_pending_events(self) -> bool:
        await self._drain_events()
        return self._pending_events == 0

    def is_common_listener_installed(self) -> bool:
        return self._listener_installed

    def is_common_listener_revoked(self) -> bool:
        return self._listener_revoked

    def has_pending_events(self) -> bool:
        return self._pending_events != 0

    async def set_sensitive(self, enabled: bool = True) -> bool:
        """Revoke or restore the real capture listeners and drain callbacks."""
        if self.stopped:
            raise RuntimeError("stopped recording cannot change sensitive mode")
        async with self._event_lock:
            await self._set_page_capture_state(enabled)
            self.sensitive = enabled
            self._listener_installed = not enabled
            self._listener_revoked = enabled
        await self._drain_events()
        return True

    async def stop(self, *, status: str = "success", note: str | None = None) -> dict[str, Any]:
        """Revoke capture, drain callbacks, and assemble the recorded trace."""
        if self._trace is not None:
            return self._trace
        try:
            if not self._listener_revoked:
                try:
                    await self.set_sensitive(True)
                except RuntimeError:
                    # Stopping a session must remain safe after page teardown.
                    self.sensitive = True
                    self._listener_installed = False
                    self._listener_revoked = True
            else:
                await self._drain_events()
        finally:
            self.stopped = True
            remove_listener = getattr(self._raw_page(), "remove_listener", None)
            if callable(remove_listener):
                remove_listener("framenavigated", self._on_navigate)
        self._append(
            verb="done",
            args={"status": status, "note": note},
            target=status,
        )

        trace_id = f"record-{self.session_id}"
        outcome = outcome_from_loop("done_success" if status == "success" else "done_failed")
        outcome["trace_id"] = trace_id
        self._trace = assemble_trace(
            [s.to_dict() for s in self.steps],
            outcome,
            domain=self.domain,
            label=self.capability,
            trace_id=trace_id,
            extra={"recorded": True, "step_count": len(self.steps)},
        )
        return self._trace


async def start_recording(cdp_endpoint: str, *, domain: str, capability: str) -> RecordSession:
    """Attach to ``cdp_endpoint`` (same acquisition path as the execute leg —
    the caller resolves it via ``browser_pool.get_pool().acquire()``) and start
    a new :class:`RecordSession`."""
    from backend.skills.page import open_skill_page

    page = await open_skill_page(cdp_endpoint)
    session = RecordSession(
        session_id=uuid.uuid4().hex, domain=domain, capability=capability, page=page,
    )
    try:
        await session.start()
    except Exception:
        # Never leak the already-opened page/CDP connection if wiring the
        # capture listener fails — same "never leak a held Chrome on
        # failure" rule the API layer's own record_start applies.
        await page.aclose()
        raise
    logger.info(
        "record session started | id=%s domain=%s capability=%s",
        session.session_id, domain, capability,
    )
    return session
