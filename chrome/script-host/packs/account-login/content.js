const PACK_ID = "account-login";
const PACK_VERSION = "1.0.0";
const ALLOWED_ACTIONS = new Set([
  "login.observe",
  "login.open",
  "login.refresh",
  "login.switch-mode",
]);
const SAFE_IDENTITY = /^[A-Za-z0-9_.:@-]{1,80}$/;
const LOGIN_ERROR_CODES = new Set([
  "auth_required",
  "account_identity_mismatch",
  "stale_generation",
  "login_rule_unknown",
  "ambiguous_login_region",
  "capability_missing",
  "session_expired",
]);

function fail(code) {
  return {
    ok: false,
    error_code: LOGIN_ERROR_CODES.has(code) ? code : "login_rule_unknown",
  };
}

function validDocumentId(value) {
  return (
    (Number.isInteger(value) && value >= 0) ||
    (typeof value === "string" && value.length > 0 && value.length <= 255)
  );
}

function targetFor(message) {
  const target = message?.target;
  if (!target || typeof target !== "object") return null;
  const keys = Object.keys(target).sort().join(",");
  if (keys !== "documentId,frameId,origin,tabId,viewGeneration") return null;
  if (!Number.isInteger(target.tabId) || target.tabId < 0) return null;
  if (!Number.isInteger(target.frameId) || target.frameId < 0) return null;
  if (!validDocumentId(target.documentId)) return null;
  if (!Number.isInteger(target.viewGeneration) || target.viewGeneration < 0) return null;
  if (typeof target.origin !== "string" || target.origin !== window.location.origin) return null;
  return target;
}

function targetWire(target) {
  return {
    tab_id: target.tabId,
    frame_id: target.frameId,
    document_id: target.documentId,
    origin: target.origin,
  };
}

function generationValue(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

function sameGeneration(left, right) {
  return (
    left !== null &&
    right !== null &&
    String(left.documentId) === String(right.documentId) &&
    String(left.viewGeneration) === String(right.viewGeneration)
  );
}

function fixedRule(message) {
  const rule = message?.rule;
  if (!rule || typeof rule !== "object") return null;
  if (rule.id !== "controlled-login-fixture" || rule.version !== "1.0.0") return null;
  const fixedOrigins = ["http://127.0.0.1:49906", "http://localhost:49906"];
  if (
    !Array.isArray(rule.allowed_origins) ||
    rule.allowed_origins.length !== fixedOrigins.length ||
    new Set(rule.allowed_origins).size !== fixedOrigins.length ||
    !rule.allowed_origins.every((origin) => fixedOrigins.includes(origin)) ||
    !rule.allowed_origins.includes(window.location.origin)
  ) {
    return null;
  }
  if (
    !Array.isArray(rule.allowed_redirect_origins) ||
    rule.allowed_redirect_origins.length !== fixedOrigins.length ||
    new Set(rule.allowed_redirect_origins).size !== fixedOrigins.length ||
    !rule.allowed_redirect_origins.every((origin) => fixedOrigins.includes(origin)) ||
    rule.platform !== "controlled-login-fixture"
  ) return null;
  if (typeof rule.login_url !== "string") return null;
  let loginUrl;
  try {
    loginUrl = new URL(rule.login_url, window.location.origin);
  } catch {
    return null;
  }
  if (
    loginUrl.origin !== window.location.origin ||
    loginUrl.pathname !== "/login" ||
    loginUrl.search ||
    loginUrl.hash
  ) return null;
  if (
    !rule.auth ||
    rule.auth.identity_path !== "/identity" ||
    rule.auth.status_path !== "/auth-status" ||
    !rule.refresh ||
    rule.refresh.trigger !== "qr_expired" ||
    rule.refresh.interval !== 30 ||
    rule.refresh.max_attempts !== 3
  ) return null;
  if (
    !rule.mode_selectors ||
    rule.mode_selectors.qr !== "#qr-region img#login-qr" ||
    rule.mode_selectors.form !== "#login-form" ||
    rule.mode_selectors.native !== "#login-form" ||
    !rule.mode_evidence ||
    rule.mode_evidence.qr !== "#qr-region img#login-qr" ||
    rule.mode_evidence.form !== "#login-form" ||
    rule.mode_evidence.native !== "#login-form"
  ) return null;
  return rule;
}

function selectorFor(rule, kind) {
  const selectors = Array.isArray(rule.selectors) ? rule.selectors : [];
  const entry = selectors.find((candidate) => candidate?.kind === kind);
  return typeof entry?.selector === "string" ? entry.selector : null;
}

function nodesFor(selector) {
  if (!selector) return [];
  try {
    return [...document.querySelectorAll(selector)];
  } catch {
    return [];
  }
}

function formPresent(rule) {
  return nodesFor(selectorFor(rule, "form")).length === 1;
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

function uniqueQrCandidates(rule) {
  const configured = nodesFor(selectorFor(rule, "qr"));
  const allQrImages = [...document.querySelectorAll("img[data-qr-generation], img[data-qr-origin]")];
  const qrNodes = [...new Set([...configured, ...allQrImages])];
  const approved = qrNodes.filter((node) => {
    const region = node.closest("[data-qr-origin]");
    if (!region || region.dataset.qrOrigin !== window.location.origin) return false;
    let imageUrl;
    try {
      imageUrl = new URL(node.currentSrc || node.src || "", window.location.href);
    } catch {
      return false;
    }
    return imageUrl.origin === window.location.origin;
  });
  return { qrNodes, approved };
}

async function fetchJson(path) {
  if (typeof path !== "string" || !path.startsWith("/") || path.includes("//")) {
    throw new Error("invalid auth endpoint");
  }
  const url = new URL(path, window.location.origin);
  if (url.origin !== window.location.origin || url.search || url.hash) {
    throw new Error("auth endpoint origin changed");
  }
  const response = await fetch(url.href, {
    credentials: "same-origin",
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error("auth endpoint unavailable");
  const value = await response.json();
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("auth endpoint returned invalid data");
  }
  return value;
}

async function readTrustedEvidence(rule) {
  const auth = rule.auth;
  if (!auth || typeof auth !== "object") return null;
  try {
    const identity = await fetchJson(auth.identity_path);
    const status = await fetchJson(auth.status_path);
    const authEvidence = status.evidence;
    if (!authEvidence || typeof authEvidence !== "object" || Array.isArray(authEvidence)) {
      return null;
    }
    const identityValue = identity.identity;
    const statusValue = status.identity;
    const identityValid = typeof identityValue === "string" && SAFE_IDENTITY.test(identityValue);
    const statusValid = typeof statusValue === "string" && SAFE_IDENTITY.test(statusValue);
    const identityGeneration = {
      documentId: generationValue(identity.document_generation),
      viewGeneration: generationValue(identity.view_generation),
    };
    const statusGeneration = {
      documentId: generationValue(authEvidence.document_generation),
      viewGeneration: generationValue(authEvidence.view_generation),
    };
    if (
      identity.authenticated !== status.authenticated ||
      identity.identity_status !== status.identity_status ||
      !sameGeneration(identityGeneration, statusGeneration) ||
      authEvidence.identity_endpoint !== auth.identity_path ||
      authEvidence.state_endpoint !== auth.status_path ||
      typeof authEvidence.frame_label !== "string" ||
      authEvidence.frame_label.length === 0 ||
      authEvidence.frame_label.length > 255
    ) {
      return null;
    }
    return {
      authenticated: identity.authenticated === true && status.authenticated === true,
      trusted: status.trusted === true,
      identityStatus:
        identity.identity_status === "valid" && status.identity_status === "valid",
      identity: identityValid && statusValid && identityValue === statusValue ? identityValue : null,
      generation: identityGeneration,
      mismatch:
        identity.identity_status === "mismatch" || status.identity_status === "mismatch" ||
        (identityValid && statusValid && identityValue !== statusValue),
    };
  } catch {
    return null;
  }
}

async function trustedGeneration(rule) {
  const evidence = await readTrustedEvidence(rule);
  return evidence?.generation ?? null;
}

async function targetGenerationIsCurrent(target, rule) {
  const current = await trustedGeneration(rule);
  return sameGeneration(current, {
    documentId: target.documentId,
    viewGeneration: target.viewGeneration,
  });
}

async function trustedAuthEvidence(rule, target) {
  const evidence = await readTrustedEvidence(rule);
  if (!evidence) return null;
  return {
    ...evidence,
    generation: sameGeneration(evidence.generation, {
      documentId: target.documentId,
      viewGeneration: target.viewGeneration,
    }),
  };
}

async function observe(message, args, target, rule) {
  if (!(await targetGenerationIsCurrent(target, rule))) return fail("stale_generation");
  if (
    typeof args.session_id !== "string" || args.session_id.length === 0 ||
    !Number.isInteger(args.epoch) || args.epoch < 0
  ) {
    return fail("login_rule_unknown");
  }
  const root = document.documentElement;
  const pageState = root.dataset.flowState || "unknown";
  const challenge = nodesFor("[data-challenge-state='required']").length > 0;
  const authMarker = nodesFor("[data-authenticated='true']").length > 0;
  const pageClaimsAuthenticated = pageState === "authenticated" || authMarker;
  const { qrNodes, approved } = uniqueQrCandidates(rule);
  const expectedQrGeneration = args.expected_qr_generation;
  const currentQr = approved.filter(
    (node) => String(node.dataset.qrGeneration) === String(expectedQrGeneration),
  );
  const ambiguousQr =
    qrNodes.length !== 1 || approved.length !== 1 || currentQr.length !== 1;
  const auth = await trustedAuthEvidence(rule, target);

  let state = "unknown";
  let evidenceKind = "unknown";
  let errorCode = null;
  let externalIdentity = null;
  if (challenge) {
    state = "challenge";
  } else if (pageState === "expired" || root.dataset.qrStatus === "expired") {
    state = "error";
    errorCode = "auth_required";
  } else if (auth?.mismatch) {
    state = "error";
    evidenceKind = "invalid";
    errorCode = "account_identity_mismatch";
  } else if (pageClaimsAuthenticated && !auth?.trusted) {
    state = "unknown";
    errorCode = "auth_required";
  } else if (
    auth?.authenticated && auth.trusted && auth.identityStatus && auth.identity && auth.generation &&
    pageState === "authenticated" && authMarker
  ) {
    state = "verifying";
    evidenceKind = "valid";
    externalIdentity = {
      provider: rule.platform,
      subject: auth.identity,
      label: auth.identity,
    };
  } else if (ambiguousQr && qrNodes.length > 0) {
    state = "unknown";
    errorCode = "ambiguous_login_region";
  } else if (currentQr.length === 1 && approved.length === 1) {
    state = "presenting";
  } else if (nodesFor(selectorFor(rule, "form")).length === 1) {
    state = "presenting";
  } else if (pageState === "refreshing" || pageState === "verifying") {
    state = pageState;
  } else if (pageClaimsAuthenticated) {
    state = "unknown";
    errorCode = "auth_required";
  }

  const result = {
    session_id: args.session_id,
    epoch: args.epoch,
    rule_id: rule.id,
    rule_version: rule.version,
    target: targetWire(target),
    view_generation: target.viewGeneration,
    state,
    evidence_kind: evidenceKind,
    observed_at: new Date().toISOString(),
  };
  if (externalIdentity) result.external_identity = externalIdentity;
  if (errorCode) result.error_code = errorCode;
  return { ok: true, result };
}

async function switchMode(message, args, target, rule) {
  if (!(await targetGenerationIsCurrent(target, rule))) return fail("stale_generation");
  const requestedMode = args.mode;
  if (!Array.isArray(rule.modes) || !rule.modes.includes(requestedMode)) {
    return fail("login_rule_unknown");
  }
  const selectors = rule.mode_selectors;
  const selector = selectors && typeof selectors === "object" ? selectors[requestedMode] : null;
  const controls = nodesFor(selector);
  if (controls.length !== 1) {
    return fail(controls.length > 1 ? "ambiguous_login_region" : "capability_missing");
  }
  const evidenceSelector = rule.mode_evidence?.[requestedMode];
  if (typeof evidenceSelector !== "string") return fail("capability_missing");
  const beforeEvidence = nodesFor(evidenceSelector).length;
  if (beforeEvidence !== 0) {
    return fail(beforeEvidence > 1 ? "ambiguous_login_region" : "capability_missing");
  }
  controls[0].click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  if (!(await targetGenerationIsCurrent(target, rule))) return fail("stale_generation");
  if (typeof evidenceSelector !== "string" || nodesFor(evidenceSelector).length !== 1) {
    return fail("capability_missing");
  }
  return {
    ok: true,
    result: {
      state: "presenting",
      mode: requestedMode,
      view_generation: target.viewGeneration,
    },
  };
}

async function targetProbe(rule) {
  const current = await trustedGeneration(rule);
  if (current === null) return fail("stale_generation");
  const qr = document.querySelector("#qr-region img#login-qr");
  const qrGeneration = qr && Number.isInteger(Number(qr.dataset.qrGeneration))
    ? Number(qr.dataset.qrGeneration)
    : null;
  return {
    ok: true,
    target: {
      documentId: current.documentId,
      viewGeneration: current.viewGeneration,
      origin: window.location.origin,
      qrGeneration,
    },
  };
}

async function invoke(message) {
  if (
    message?.type !== "opencli-script-host.invoke" ||
    message.pack !== PACK_ID ||
    message.version !== PACK_VERSION ||
    !ALLOWED_ACTIONS.has(message.action)
  ) {
    return fail("login_rule_unknown");
  }
  const rule = fixedRule(message);
  if (!rule) return fail("login_rule_unknown");
  const args = message.args && typeof message.args === "object" ? message.args : {};
  const target = targetFor(message);
  if (!target) return fail("stale_generation");
  if (!(await targetGenerationIsCurrent(target, rule))) return fail("stale_generation");
  if (message.action === "login.observe") return observe(message, args, target, rule);
  if (message.action === "login.refresh") {
    // Refresh is controlled by the background worker so it can verify that a
    // real document/view generation changed before claiming success.
    return fail("capability_missing");
  }
  if (message.action === "login.switch-mode") {
    return switchMode(message, args, target, rule);
  }
  window.location.assign(new URL(rule.login_url, window.location.origin).href);
  return { ok: true, result: { state: "opening", view_generation: target.viewGeneration } };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (
    message?.type === "opencli-script-host.login-target" &&
    message.pack === PACK_ID &&
    message.version === PACK_VERSION
  ) {
    const rule = fixedRule(message);
    if (!rule) {
      sendResponse(fail("login_rule_unknown"));
      return false;
    }
    Promise.resolve(targetProbe(rule)).then(
      (result) => sendResponse(result),
      () => sendResponse(fail("stale_generation")),
    );
    return true;
  }
  // Messages for another Script Host pack must be ignored so this listener
  // cannot race its response with page-basics or future packs.
  if (
    message?.type !== "opencli-script-host.invoke" ||
    message.pack !== PACK_ID ||
    message.version !== PACK_VERSION
  ) {
    return false;
  }
  Promise.resolve(invoke(message)).then(
    (result) => sendResponse(result),
    () => sendResponse(fail("login_rule_unknown")),
  );
  return true;
});
