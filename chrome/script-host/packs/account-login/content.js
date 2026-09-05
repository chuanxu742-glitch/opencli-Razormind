const PACK_ID = "account-login";
const PACK_VERSION = "1.0.0";
const ALLOWED_ACTIONS = new Set([
  "login.observe",
  "login.open",
  "login.refresh",
  "login.switch-mode",
]);
const SAFE_IDENTITY = /^[A-Za-z0-9_.:@-]{1,80}$/;

function fail(code) {
  return { ok: false, error_code: code };
}

function targetFor(message) {
  const target = message?.target;
  if (!target || !Number.isInteger(Number(target.frameId))) return null;
  if (!Number.isInteger(Number(target.viewGeneration)) || Number(target.viewGeneration) < 0) {
    return null;
  }
  if (typeof target.origin !== "string" || target.origin !== window.location.origin) return null;
  return target;
}

function targetGenerationIsCurrent(target) {
  const root = document.documentElement;
  const documentMarker = root.dataset.documentId ?? root.dataset.documentGeneration;
  return documentMarker == null || String(documentMarker) === String(target.documentId);
}

function rectFor(node) {
  if (!node) return null;
  const rect = node.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  return {
    x: Math.max(0, Math.round(rect.x)),
    y: Math.max(0, Math.round(rect.y)),
    width: Math.max(1, Math.round(rect.width)),
    height: Math.max(1, Math.round(rect.height)),
  };
}

function observe(message, args, target) {
  if (!targetGenerationIsCurrent(target)) return fail("target_generation_changed");
  const root = document.documentElement;
  const state = root.dataset.flowState || "unknown";
  const ruleId = typeof args.rule_id === "string" ? args.rule_id : "controlled-login-fixture";
  const ruleVersion = typeof args.rule_version === "string" ? args.rule_version : PACK_VERSION;
  const qrNodes = [...document.querySelectorAll('[data-qr-generation] img')];
  const approvedQrNodes = qrNodes.filter((node) => {
    const region = node.closest("[data-qr-origin]");
    if (!region || region.dataset.qrOrigin !== window.location.origin) return false;
    const imageUrl = new URL(node.currentSrc || node.src || "", window.location.href);
    return imageUrl.origin === window.location.origin;
  });
  const expectedQrGeneration = args.expected_qr_generation;
  const currentQrNodes = approvedQrNodes.filter(
    (node) => String(node.dataset.qrGeneration) === String(expectedQrGeneration),
  );
  const ambiguousQr = qrNodes.length !== 1 || approvedQrNodes.length !== 1 || currentQrNodes.length !== 1;
  const qrRegion = currentQrNodes.length === 1
    ? rectFor(currentQrNodes[0].closest("[data-qr-origin]"))
    : null;

  const identityEvidence = document.querySelector("#identity-evidence");
  const identityStatus = identityEvidence?.dataset.identityStatus;
  const authenticated = document.querySelector("[data-authenticated='true']");
  const identity = authenticated?.dataset.authenticatedIdentity;
  const safeIdentity = typeof identity === "string" && SAFE_IDENTITY.test(identity) ? identity : null;
  const challenge = document.querySelector("[data-challenge-state='required']");
  const form = document.querySelector("form[data-sensitive-form], #login-form");
  const sensitiveFields = [...document.querySelectorAll("[data-sensitive-field]")];
  const identityFields = [...document.querySelectorAll("[data-identity-field='true']")];
  const regions = [];
  if (qrRegion) regions.push({ ...qrRegion, kind: "qr" });
  const formRegion = rectFor(form);
  if (formRegion) regions.push({ ...formRegion, kind: "form" });
  const sensitiveRegions = sensitiveFields.map(rectFor).filter(Boolean);

  let observedState = "unknown";
  let evidenceKind = "unknown";
  let errorCode = null;
  if (challenge) {
    observedState = "challenge";
  } else if (state === "expired" || root.dataset.qrStatus === "expired") {
    observedState = "error";
    errorCode = "qr_expired";
  } else if (authenticated && identityStatus === "mismatch") {
    observedState = "error";
    evidenceKind = "invalid";
    errorCode = "account_identity_mismatch";
  } else if (authenticated && identityStatus === "valid" && safeIdentity) {
    observedState = "verifying";
    evidenceKind = "valid";
  } else if (qrNodes.length > 0 && ambiguousQr) {
    observedState = "unknown";
    errorCode = "ambiguous_qr";
  } else if (currentQrNodes.length === 1) {
    observedState = "presenting";
  } else if (form) {
    observedState = "presenting";
  } else if (state === "refreshing" || state === "verifying") {
    observedState = state;
  }

  const result = {
    session_id: args.session_id,
    epoch: args.epoch,
    rule_id: ruleId,
    rule_version: ruleVersion,
    target,
    view_generation: target.viewGeneration,
    state: observedState,
    evidence_kind: evidenceKind,
    observed_at: new Date().toISOString(),
    regions,
    focus: identityFields.map((node) => rectFor(node)).filter(Boolean),
    sensitive_regions: sensitiveRegions,
  };
  if (safeIdentity && evidenceKind === "valid") {
    result.external_identity = {
      provider: "controlled-login-fixture",
      subject: safeIdentity,
      label: safeIdentity,
    };
  }
  if (errorCode) result.error_code = errorCode;
  return { ok: true, result };
}

function invoke(message) {
  if (
    message?.type !== "opencli-script-host.invoke" ||
    message.pack !== PACK_ID ||
    message.version !== PACK_VERSION ||
    !ALLOWED_ACTIONS.has(message.action)
  ) {
    return fail("invalid_action");
  }
  const args = message.args && typeof message.args === "object" ? message.args : {};
  const target = targetFor(message);
  if (!target) return fail("incomplete_target");
  if (message.action !== "login.open" && !targetGenerationIsCurrent(target)) {
    return fail("target_generation_changed");
  }
  if (message.action === "login.observe") return observe(message, args, target);
  if (message.action === "login.refresh") {
    if (args.expected_view_generation !== target.viewGeneration) return fail("stale_generation");
    window.location.reload();
    return { ok: true, result: { state: "refreshing", view_generation: target.viewGeneration } };
  }
  if (message.action === "login.switch-mode") {
    return { ok: true, result: { state: "presenting", mode: "native", view_generation: target.viewGeneration } };
  }
  if (typeof args.login_url !== "string") return fail("login_url_required");
  let loginUrl;
  try {
    loginUrl = new URL(args.login_url);
  } catch {
    return fail("invalid_login_url");
  }
  if (loginUrl.origin !== target.origin || !["/", "/login"].includes(loginUrl.pathname) || loginUrl.search || loginUrl.hash) {
    return fail("login_url_not_allowed");
  }
  window.location.assign(loginUrl.href);
  return { ok: true, result: { state: "opening", view_generation: target.viewGeneration } };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  sendResponse(invoke(message));
  return false;
});
