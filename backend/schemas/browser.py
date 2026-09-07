from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator



class BrowserBindingRead(BaseModel):
    id: str
    browser_endpoint: str
    site: str
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RuntimeComponent(BaseModel):
    """One immutable, bundle-local component that the launcher may load."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["extension", "script", "opencli_plugin"]
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    version: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    path: str = Field(min_length=1, max_length=255)
    required: bool = True
    capabilities: list[str] = Field(default_factory=list)


class RuntimeCapability(BaseModel):
    """An allowlisted structured action exposed by one bundle component."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    component_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    action: str = Field(min_length=1, max_length=255)
    runtime: str = Field(
        default="opentabs",
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    args_schema: dict = Field(default_factory=lambda: {"type": "object"})
    allowed_hosts: list[str] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"] = "low"
    required_gate: str | None = Field(default=None, max_length=100)
    config: dict = Field(default_factory=dict)


class RuntimeBundleManifest(BaseModel):
    """Desired runtime source of truth; no user profile data may occur here."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    version: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    components: list[RuntimeComponent] = Field(min_length=1)
    capabilities: list[RuntimeCapability] = Field(default_factory=list)
    act_pack_ids: list[str] = Field(default_factory=list)


class BrowserRuntimeBundleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest: RuntimeBundleManifest
    trust_level: Literal["trusted", "reviewed"] = "trusted"
    source: str = Field(default="local", min_length=1, max_length=255)


class BrowserRuntimeBundleUpdate(BrowserRuntimeBundleCreate):
    pass


class BrowserRuntimeBundleRead(BaseModel):
    id: str
    name: str
    version: str
    manifest: RuntimeBundleManifest
    trust_level: str
    source: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}



class BrowserInstanceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(min_length=1, max_length=255)
    mode: Literal["bridge", "cdp"] = "bridge"
    label: str = Field(default="", max_length=100)
    agent_url: str | None = Field(default=None, max_length=255)
    agent_protocol: Literal["http", "ws"] | None = None
    profile_kind: Literal["anonymous", "authenticated"] = "authenticated"
    profile_name: str = Field(min_length=1, max_length=100)
    runtime_bundle_id: str | None = None
    resource_class: Literal["standard", "medium", "large"] = "standard"
    startup_pages: list[str] = Field(default_factory=list, max_length=10)
    network_policy: dict = Field(default_factory=lambda: {"mode": "direct"})

    @field_validator("startup_pages")
    @classmethod
    def validate_startup_pages(cls, pages: list[str]) -> list[str]:
        if any(not page.startswith(("http://", "https://")) for page in pages):
            raise ValueError("startup_pages must contain only HTTP(S) URLs")
        return pages

    @field_validator("network_policy")
    @classmethod
    def validate_network_policy(cls, policy: dict) -> dict:
        mode = policy.get("mode", "direct")
        if mode not in {"direct", "restricted"}:
            raise ValueError("network_policy.mode must be direct or restricted")
        return {**policy, "mode": mode}


class BrowserInstanceConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["bridge", "cdp"] | None = None
    agent_url: str | None = Field(default=None, max_length=255)
    agent_protocol: Literal["http", "ws"] | None = None
    profile_kind: Literal["anonymous", "authenticated"] | None = None
    profile_name: str | None = Field(default=None, min_length=1, max_length=100)
    runtime_bundle_id: str | None = None
    resource_class: Literal["standard", "medium", "large"] | None = None
    startup_pages: list[str] | None = Field(default=None, max_length=10)
    network_policy: dict | None = None

    @field_validator("startup_pages")
    @classmethod
    def validate_startup_pages(cls, pages: list[str] | None) -> list[str] | None:
        if pages is not None and any(
            not page.startswith(("http://", "https://")) for page in pages
        ):
            raise ValueError("startup_pages must contain only HTTP(S) URLs")
        return pages

    @field_validator("network_policy")
    @classmethod
    def validate_network_policy(cls, policy: dict | None) -> dict | None:
        if policy is None:
            return None
        mode = policy.get("mode", "direct")
        if mode not in {"direct", "restricted"}:
            raise ValueError("network_policy.mode must be direct or restricted")
        return {**policy, "mode": mode}


class BrowserInstanceRead(BaseModel):
    id: str
    endpoint: str
    mode: str
    agent_url: str | None
    agent_protocol: str | None
    label: str
    profile_kind: str
    profile_name: str
    runtime_bundle_id: str | None
    resource_class: str
    startup_pages: list[str]
    network_policy: dict
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LoadedRuntimeComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["extension", "script", "opencli_plugin"]
    id: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=100)
    healthy: bool
    diagnostic: str | None = Field(default=None, max_length=1000)


class SlotRuntimeReport(BaseModel):
    loaded_bundle_name: str | None = Field(default=None, max_length=100)
    loaded_bundle_version: str | None = Field(default=None, max_length=100)
    loaded_components: list[LoadedRuntimeComponent] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    self_check: dict = Field(default_factory=dict)
    restart_required: bool = False


class BrowserRuntimeDeploymentRead(BaseModel):
    browser_instance_id: str
    loaded_bundle_name: str | None
    loaded_bundle_version: str | None
    loaded_components: list[LoadedRuntimeComponent]
    self_check: dict
    state: str
    diagnostics: list[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

class CapabilityInvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    args: dict = Field(default_factory=dict)
    gate: str | None = Field(default=None, max_length=100)


from backend.schemas.browser_account import (
    AccountRef,
    LoginObservationV1,
    LoginRuleV1,
    NodeCapacityFactV1,
    NodeClaimV1,
    NodeResultV1,
    PortalControlMessageV1,
    PortalPixelFrameV1,
    PortalTransientV1,
    ProfileManifestV1,
    SessionEnvelopeV1,
)


class BrowserCapabilityInvocationRead(BaseModel):
    id: str
    browser_instance_id: str
    capability: str
    desired_bundle_name: str | None
    desired_bundle_version: str | None
    loaded_bundle_version: str | None
    component_versions: list[dict]
    input_payload: dict
    output_payload: dict | None
    page_before: dict | None
    page_after: dict | None
    duration_ms: int | None
    risk: str
    gate: str | None
    error: dict | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
