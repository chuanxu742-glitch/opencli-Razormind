"""OpenTabs 与 BBX 共用权限策略时仍保持默认拒绝和审批顺序。"""

from types import SimpleNamespace

import pytest

from backend.workflow import opencli_hda_tracer as tracer


@pytest.mark.parametrize(
    "check,mode,capability,label",
    [
        (
            tracer._opentabs_tool_block_reason,
            tracer.OPENTABS_EXECUTOR_MODE,
            tracer.OPENTABS_TOOL_CAPABILITY_ID,
            "OpenTabs",
        ),
        (
            tracer._bbx_tool_block_reason,
            tracer.BBX_EXECUTOR_MODE,
            tracer.BBX_TOOL_CAPABILITY_ID,
            "BBX",
        ),
    ],
)
@pytest.mark.parametrize(
    "read_only,proposal,fetch,mutate,expected_code",
    [
        (True, "proposed", True, False, None),
        (True, "accepted", False, True, "fetch_permission_required"),
        (False, "proposed", True, True, "opencli_write_approval_required"),
        (False, "accepted", True, False, "opencli_write_permission_required"),
        (False, "accepted", False, True, None),
        (None, "accepted", True, False, "opencli_write_permission_required"),
        ("true", "accepted", True, False, "opencli_write_permission_required"),
        (1, "accepted", True, False, "opencli_write_permission_required"),
        (None, None, True, True, "opencli_write_approval_required"),
    ],
)
def test_external_tool_permission_policy(
    check, mode, capability, label, read_only, proposal, fetch, mutate, expected_code
):
    params = {} if read_only is None else {"readOnly": read_only}
    node = SimpleNamespace(
        id="tool-node",
        runtime={
            "proposal_state": proposal,
            "binding": {
                "binding_id": tracer.EXTERNAL_TOOL_BINDING_ID,
                "input": {
                    "executorMode": mode,
                    "toolCapabilityId": capability,
                    "executorParams": params,
                },
            },
        },
    )
    permissions = SimpleNamespace(canFetchNetwork=fetch, canMutateExternalSites=mutate)

    reason = check(node, permissions)

    if expected_code is None:
        assert reason is None
    else:
        assert reason.code == expected_code
        assert reason.message.startswith(label)
        assert reason.source == "workflow_permissions"
        assert reason.details["nodeId"] == node.id
        assert reason.details["bindingId"] == tracer.EXTERNAL_TOOL_BINDING_ID


@pytest.mark.parametrize(
    "check", [tracer._opentabs_tool_block_reason, tracer._bbx_tool_block_reason]
)
def test_unrelated_nodes_are_not_subject_to_external_tool_policy(check):
    assert check(SimpleNamespace(id="unrelated", runtime={}), object()) is None
