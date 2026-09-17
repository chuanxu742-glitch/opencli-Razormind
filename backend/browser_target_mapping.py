"""Map a Chrome tab to CDP using a one-use isolated-world probe.

No DOM/MAIN-world marker, URL-only fallback, debugger permission, or first-page
selection is used. The probe is not an authentication credential.
"""

import asyncio
import json
import secrets
from urllib.parse import urlparse

import websockets


async def _probe_page(websocket_url: str, key: str, nonce: str, origin: str) -> bool:
    async with websockets.connect(websocket_url, open_timeout=5) as ws:
        contexts = {}
        sequence = 0

        async def command(method, params=None):
            nonlocal sequence
            sequence += 1
            await ws.send(json.dumps({"id": sequence, "method": method, "params": params or {}}))
            while True:
                message = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if message.get("method") == "Runtime.executionContextCreated":
                    ctx = message["params"]["context"]
                    contexts[ctx["id"]] = ctx
                if message.get("method") in {
                    "Runtime.executionContextsCleared",
                    "Runtime.executionContextDestroyed",
                }:
                    raise RuntimeError("document changed during target mapping")
                if message.get("id") == sequence:
                    if "error" in message:
                        raise RuntimeError("target mapping command rejected")
                    return message.get("result", {})

        tree = await command("Page.getFrameTree")
        main_frame = tree.get("frameTree", {}).get("frame", {}).get("id")
        if not main_frame:
            return False
        await command("Runtime.enable")
        matched = 0
        for context in list(contexts.values()):
            auxiliary = context.get("auxData", {})
            if auxiliary.get("isDefault") is not False or auxiliary.get("frameId") != main_frame:
                continue
            result = await command(
                "Runtime.evaluate",
                {
                    "contextId": context["id"],
                    "returnByValue": True,
                    "expression": (
                        f"globalThis[{json.dumps(key)}] === {json.dumps(nonce)} && "
                        f"location.origin === {json.dumps(origin)}"
                    ),
                },
            )
            if result.get("result", {}).get("value") is True:
                matched += 1
        return matched == 1


async def map_chrome_tab(*, pages: list[dict], workers: list[dict], target, evaluate) -> str:
    """Resolve only the authorized main-frame document in this isolated browser."""
    if str(target.frame_id) != "0" or not str(target.tab_id).isdigit():
        raise RuntimeError("only a concrete main-frame Chrome tab can be mapped")
    tab_id = int(target.tab_id)
    key, nonce = "__opencli_mapping_" + secrets.token_hex(16), secrets.token_hex(32)
    expected = {
        "tabId": tab_id,
        "frameId": 0,
        "documentId": target.document_id,
        "viewGeneration": target.view_generation,
        "origin": target.origin,
    }
    # target.view_generation is supplied by the caller from its bound route.
    owner = None
    actual_document = None
    expected_origin = target.origin
    try:
        for worker in workers:
            url = worker.get("webSocketDebuggerUrl")
            if not url:
                continue
            try:
                if (
                    await evaluate(url, "chrome.runtime.getManifest().name")
                    != "OpenCLI Script Host"
                ):
                    continue
                owner = url
                expression = (
                    """(async (expected, key, nonce) => {
                  await globalThis.opencliScriptHost.verifyLoginTarget(expected);
                  const written = await chrome.scripting.executeScript({
                    target:{tabId:expected.tabId, frameIds:[0]}, world:'ISOLATED',
                    func:(key, nonce, origin) => {
                      if (location.origin !== origin) return false;
                      Object.defineProperty(globalThis, key, {value:nonce, configurable:true});
                      return true;
                    }, args:[key,nonce,expected.origin]
                  });
                  if (written.length !== 1 || written[0].result !== true || !written[0].documentId)
                    throw new Error('probe unavailable');
                  await globalThis.opencliScriptHost.verifyLoginTarget(expected);
                  return written[0].documentId;
                })"""
                    + f"({json.dumps(expected)},{json.dumps(key)},{json.dumps(nonce)})"
                )
                actual_document = await evaluate(url, expression)
                if not isinstance(actual_document, str) or not actual_document:
                    raise RuntimeError("probe document unavailable")
                owner = url
                break
            except Exception:
                # A trusted Script Host which cannot bind the requested target
                # is a rejection, never grounds to select another webpage.
                raise RuntimeError("authorized isolated-world target probe failed") from None
        if owner is None:
            raise RuntimeError("Script Host mapping owner unavailable")
        matches = []
        for page in pages:
            parsed = urlparse(page.get("url", ""))
            if f"{parsed.scheme}://{parsed.netloc}" != expected_origin or not page.get(
                "webSocketDebuggerUrl"
            ):
                continue
            if await _probe_page(page["webSocketDebuggerUrl"], key, nonce, expected_origin):
                matches.append(page["webSocketDebuggerUrl"])
        if len(matches) != 1:
            raise RuntimeError("CDP target is missing or ambiguous")
        verified = await evaluate(
            owner,
            """(async (expected, documentId) => {
          await globalThis.opencliScriptHost.verifyLoginTarget(expected);
          const checked = await chrome.scripting.executeScript({
            target:{tabId:expected.tabId,documentIds:[documentId]},
            world:'ISOLATED',func:() => true});
          return checked.length === 1 && checked[0].documentId === documentId;
        })"""
            + f"({json.dumps(expected)},{json.dumps(actual_document)})",
        )
        if verified is not True:
            raise RuntimeError("document changed during target mapping")
        return matches[0]
    finally:
        if owner:
            try:
                await evaluate(
                    owner,
                    """(async (tabId, documentId, key) => {
                  await chrome.scripting.executeScript({
                    target:documentId ? {tabId,documentIds:[documentId]} : {tabId,frameIds:[0]},
                    world:'ISOLATED',func:(key)=>{delete globalThis[key]},args:[key]});
                })"""
                    + f"({tab_id},{json.dumps(actual_document)},{json.dumps(key)})",
                )
            except Exception:
                pass  # A navigated/destroyed document has already discarded its probe.
