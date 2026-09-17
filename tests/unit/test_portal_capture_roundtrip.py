"""Exercise actual capture, Pillow masking and binary pixel codecs together."""

import base64
import io
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from backend import agent_runtime_dispatch as dispatch
from backend.services.browser_portal_contract import (
    decode_portal_wire_frame,
    encode_portal_wire_frame,
    validate_portal_frame_binding,
)
from tests.unit.test_portal_interactive_controls import route_for


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["qr", "form", "approved"])
async def test_real_capture_masks_png_and_retains_focus_clip_through_wire(monkeypatch, kind):
    from backend import browser_account_runtime

    route = route_for(kind)
    image = Image.new("RGB", (100, 100), (255, 255, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    cdp = AsyncMock(return_value={"data": base64.b64encode(output.getvalue()).decode()})
    monkeypatch.setattr(dispatch, "_cdp_command", cdp)
    measure = AsyncMock(return_value=[{"x": 110, "y": 120, "width": 30, "height": 20}])
    monkeypatch.setattr(dispatch, "_evaluate_cdp_target", measure)
    running = SimpleNamespace(
        command_id="cmd", binding=SimpleNamespace(cdp_endpoint="http://local")
    )
    monkeypatch.setattr(
        browser_account_runtime,
        "account_runtime_allocator",
        lambda: SimpleNamespace(get=lambda _: running),
    )
    monkeypatch.setattr(
        dispatch,
        "runtime_lease_book",
        lambda: SimpleNamespace(
            current=lambda _: SimpleNamespace(
                session=SimpleNamespace(login_rule_id="rule", login_rule_version="1")
            )
        ),
    )
    monkeypatch.setattr(
        dispatch,
        "invoke_script_host",
        AsyncMock(
            return_value={
                "result": {"ok": True, "result": {"region_focus": route.region_focus.model_dump()}}
            }
        ),
    )

    frame = await dispatch.capture_portal_frame(websocket_url="ws://bound", route=route, sequence=1)
    assert measure.await_count == 2
    encoded = encode_portal_wire_frame(frame)
    decoded = decode_portal_wire_frame(encoded)
    validate_portal_frame_binding(route, decoded)
    pixel = decoded.transient.pixel
    assert pixel.region_kind == kind
    assert pixel.focused_field_ref == ("official-login:otp" if kind == "form" else None)
    assert pixel.clip == route.region_focus.approved_regions[0]
    assert pixel.byte_length == len(pixel.frame_bytes.get_secret_value())
    restored = Image.open(io.BytesIO(pixel.frame_bytes.get_secret_value()))
    assert restored.size == (100, 100)
    assert restored.getpixel((20, 30)) == (0, 0, 0)
    assert restored.getpixel((90, 90)) == (255, 255, 255)
    screenshot = cdp.await_args
    assert screenshot.args[1] == "Page.captureScreenshot"
    assert screenshot.args[2]["clip"] == {
        "x": 100,
        "y": 100,
        "width": 100,
        "height": 100,
        "scale": 1,
    }

    # Decode the actual Python-emitted wire with the production TypeScript reader,
    # not a second Python approximation of the browser contract.
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the cross-language pixel wire check")
    root = Path(__file__).resolve().parents[2]
    javascript = """
      import {createRequire} from 'node:module';
      import {readFileSync} from 'node:fs';
      const input = JSON.parse(readFileSync(0, 'utf8'));
      const require = createRequire(input.packagePath);
      const ts = require('typescript');
      const source = ts.transpileModule(readFileSync(input.protocolPath, 'utf8'), {
        compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022}
      }).outputText;
      const moduleUrl = 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
      const {decodePortalBinaryFrame} = await import(moduleUrl);
      const bytes = Uint8Array.from(Buffer.from(input.frame, 'base64'));
      const result = decodePortalBinaryFrame(bytes.buffer, input.binding);
      process.stdout.write(JSON.stringify(result ? {
        kind: result.kind, region: result.pixel.region_kind,
        focus: result.pixel.focused_field_ref, length: result.bytes.length
      } : null));
    """
    browser = subprocess.run(
        [node, "--input-type=module", "-e", javascript],
        input=json.dumps(
            {
                "packagePath": str(root / "frontend/package.json"),
                "protocolPath": str(root / "frontend/lib/browser-accounts/portal-protocol.ts"),
                "frame": base64.b64encode(encoded).decode(),
                "binding": dispatch._portal_binding(route).model_dump(mode="json"),
            }
        ),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(browser.stdout) == {
        "kind": "pixel",
        "region": kind,
        "focus": pixel.focused_field_ref,
        "length": pixel.byte_length,
    }
