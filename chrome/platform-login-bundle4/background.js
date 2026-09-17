import {createXhsIdentityProbe} from './xhs-identity.js?build=1.4.3';
const SCRIPT_ID_PREFIX = "opencli-pack-";
const packs = new Map();
const loginRefreshes = new Map();
const loginTargetRules = new Map();
let startupError = null;
let startupReady = false;

function canonicalRuleJson(value) {
  const ordered = item => Array.isArray(item) ? item.map(ordered) :
    item && typeof item === 'object' ? Object.fromEntries(Object.keys(item).sort().map(key => [key, ordered(item[key])])) : item;
  return JSON.stringify(ordered(value)).replace(/[\u0080-\uffff]/g, character =>
    '\\u' + character.charCodeAt(0).toString(16).padStart(4, '0'));
}

async function packagedLoginRules() {
  const response = await fetch(chrome.runtime.getURL('platform-catalog.json'));
  if (!response.ok) throw new Error('packaged platform catalog unavailable');
  const catalog = await response.json();
  if (catalog.schema_version !== 1 || catalog.source?.package !== '@jackwener/opencli' ||
      catalog.source?.version !== '1.8.7' || !Array.isArray(catalog.items))
    throw new Error('unsupported packaged platform catalog');
  const rules = new Map();
  for (const item of catalog.items) {
    if (!item.rule_id) continue;
    if (!/^[a-z0-9][a-z0-9-]*$/.test(item.rule_id) || !/^[a-f0-9]{64}$/.test(item.rule_sha256) ||
        rules.has(item.rule_id)) throw new Error('invalid packaged platform rule identity');
    const path = `packs/account-login/${item.rule_id}.json`;
    const response = await fetch(chrome.runtime.getURL(path));
    if (!response.ok) throw new Error('packaged platform rule unavailable');
    const rule = await response.json();
    const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',
      new TextEncoder().encode(canonicalRuleJson(rule))))).map(byte => byte.toString(16).padStart(2, '0')).join('');
    if (hash !== item.rule_sha256 || rule.id !== item.rule_id || rule.version !== item.rule_version ||
        rule.platform !== item.id || !Array.isArray(rule.allowed_origins) || !rule.allowed_origins.length ||
        !rule.allowed_origins.every(origin => {try {const url = new URL(origin); return url.protocol === 'https:' &&
          url.origin === origin && !url.username && !url.password;} catch {return false;}}) ||
        JSON.stringify(rule.allowed_redirect_origins) !== JSON.stringify(rule.allowed_origins) ||
        !rule.allowed_origins.some(origin => new URL(rule.login_url, origin).href === item.login_url) ||
        rule.authentication_verified !== false)
      throw new Error('platform rule differs from packaged official catalog');
    rules.set(rule.id, {rule: Object.freeze(rule), path});
  }
  return rules;
}

function isSafeAssetPath(value) {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    !value.startsWith("/") &&
    !value.split("/").includes("..")
  );
}

async function loadPackIndex() {
  const response = await fetch(chrome.runtime.getURL("packs/index.json"));
  if (!response.ok)
    throw new Error(`pack index returned HTTP ${response.status}`);
  const index = await response.json();
  if (!Array.isArray(index)) throw new Error("pack index must be an array");
  const catalogRules = await packagedLoginRules();
  const allowedMatches = new Set(['http://127.0.0.1:49906/*', 'http://localhost:49906/*',
    ...[...catalogRules.values()].flatMap(({rule}) => rule.allowed_origins.map(origin => origin + '/*'))]);

  for (const pack of index) {
    if (
      !pack ||
      typeof pack.id !== "string" ||
      typeof pack.version !== "string" ||
      !Array.isArray(pack.matches) ||
      !Array.isArray(pack.js) ||
      !Array.isArray(pack.actions)
    ) {
      throw new Error(
        "pack entries require id, version, matches, js, and actions",
      );
    }
    if (packs.has(pack.id)) throw new Error(`duplicate pack ${pack.id}`);
    if (!pack.js.every(isSafeAssetPath))
      throw new Error(`pack ${pack.id} contains an unsafe script path`);
    if (
      pack.id === "account-login" &&
      (pack.matches.includes("<all_urls>") ||
        pack.version !== "1.0.0" ||
        pack.js.length !== 1 ||
        pack.js[0] !== "packs/account-login/content.js" ||
        !isSafeAssetPath(pack.rule) ||
        pack.rule !== "packs/account-login/rules.json" ||
        pack.rule_version !== "1.0.0" ||
        pack.matches.length !== allowedMatches.size ||
        new Set(pack.matches).size !== allowedMatches.size ||
        !pack.matches.every(match => allowedMatches.has(match)))
    ) {
      throw new Error("account-login pack requires a fixed local rule manifest");
    }
    if (pack.id === "account-login") {
      const ruleResponse = await fetch(chrome.runtime.getURL(pack.rule));
      if (!ruleResponse.ok) throw new Error(`account-login rule returned HTTP ${ruleResponse.status}`);
      const rule = await ruleResponse.json();
      if (
        !rule ||
        rule.id !== "controlled-login-fixture" ||
        rule.version !== pack.rule_version ||
        !Array.isArray(rule.allowed_origins) ||
        rule.allowed_origins.length !== 2 ||
        new Set(rule.allowed_origins).size !== 2 ||
        !rule.allowed_origins.every((origin) =>
          ["http://127.0.0.1:49906", "http://localhost:49906"].includes(origin),
        ) ||
        !Array.isArray(rule.allowed_redirect_origins) ||
        rule.allowed_redirect_origins.length !== 2 ||
        new Set(rule.allowed_redirect_origins).size !== 2 ||
        !rule.allowed_redirect_origins.every((origin) =>
          ["http://127.0.0.1:49906", "http://localhost:49906"].includes(origin),
        ) ||
        rule.login_url !== "/login" ||
        !rule.auth ||
        rule.auth.identity_path !== "/identity" ||
        rule.auth.status_path !== "/auth-status"
      ) {
        throw new Error("account-login rule manifest is not the fixed deployed rule");
      }
      pack.ruleManifest = Object.freeze(rule);
      pack.ruleManifests = { [`${rule.id}@${rule.version}`]: pack.ruleManifest };
      if (!Array.isArray(pack.rules) || pack.rules.length !== catalogRules.size || new Set(pack.rules.map(rule=>rule.id)).size !== catalogRules.size)
        throw new Error('login rules must match fixed bundle4 registry');
      for (const entry of pack.rules) {
        const packaged = catalogRules.get(entry.id);
        if (!packaged || entry.version !== packaged.rule.version || entry.path !== packaged.path)
          throw new Error('login rule path is not packaged');
        const rule = packaged.rule;
        pack.ruleManifests[`${rule.id}@${rule.version}`] = Object.freeze(rule);
      }
    }
    const actionIds = new Set();
    for (const action of pack.actions) {
      if (
        !action ||
        typeof action.id !== "string" ||
        actionIds.has(action.id)
      ) {
        throw new Error(
          `pack ${pack.id} contains an invalid or duplicate action`,
        );
      }
      actionIds.add(action.id);
    }
    if (
      pack.id === "account-login" &&
      (actionIds.size !== 4 ||
        !["login.observe", "login.open", "login.refresh", "login.switch-mode"].every((id) =>
          actionIds.has(id),
        ))
    ) {
      throw new Error("account-login pack actions do not match the fixed rule manifest");
    }
    packs.set(pack.id, pack);
  }
}

async function registerPacks() {
  const registered = await chrome.scripting.getRegisteredContentScripts();
  const managedIds = registered
    .map((item) => item.id)
    .filter((id) => id.startsWith(SCRIPT_ID_PREFIX));
  if (managedIds.length > 0)
    await chrome.scripting.unregisterContentScripts({ ids: managedIds });

  const scripts = [...packs.values()].map((pack) => ({
    id: `${SCRIPT_ID_PREFIX}${pack.id}`,
    matches: pack.matches,
    js: pack.js,
    runAt: pack.run_at ?? "document_idle",
    world: "ISOLATED",
    persistAcrossSessions: true,
  }));
  if (scripts.length > 0)
    await chrome.scripting.registerContentScripts(scripts);
}

async function initialize() {
  startupReady = false;
  packs.clear();
  loginTargetRules.clear();
  startupError = null;
  try {
    await loadPackIndex();
    await registerPacks();
    startupReady = true;
  } catch (error) {
    startupError = error instanceof Error ? error.message : String(error);
    console.error("[opencli-script-host] startup failed:", startupError);
  }
}

let initialization = Promise.resolve();
function scheduleInitialize() {
  initialization = initialization.then(initialize);
  return initialization;
}

chrome.runtime.onInstalled.addListener(() => void scheduleInitialize());
chrome.runtime.onStartup.addListener(() => void scheduleInitialize());
void scheduleInitialize();

function health() {
  return {
    ok: startupReady && startupError === null,
    error: startupError,
    packs: [...packs.values()].map(({ id, version, actions }) => ({
      id,
      version,
      actions: actions.map((action) => action.id),
    })),
  };
}

function requireLoginTarget(args, tabId, rule) {
  const contractTarget = args?.target;
  if (!contractTarget || typeof contractTarget !== "object") {
    throw new Error("account-login actions require a claimed tab target");
  }
  const keys = Object.keys(contractTarget).sort().join(",");
  if (keys !== "document_id,frame_id,origin,tab_id,view_generation") {
    throw new Error("account-login actions require the canonical target fields");
  }
  if (!Number.isInteger(contractTarget.tab_id) || contractTarget.tab_id !== tabId) {
    throw new Error("account-login actions require the claimed tab target");
  }
  if (!Number.isInteger(contractTarget.frame_id) || contractTarget.frame_id < 0) {
    throw new Error("account-login actions require a concrete frame target");
  }
  if (!Number.isInteger(contractTarget.view_generation) || contractTarget.view_generation < 0) {
    throw new Error("account-login actions require a view generation");
  }
  if (
    !(
      (Number.isInteger(contractTarget.document_id) && contractTarget.document_id >= 0) ||
      (typeof contractTarget.document_id === "string" && contractTarget.document_id.length > 0)
    )
  ) {
    throw new Error("account-login actions require a document target");
  }
  if (typeof contractTarget.origin !== "string" || !contractTarget.origin) {
    throw new Error("account-login actions require an origin target");
  }
  let origin;
  try {
    const parsed = new URL(contractTarget.origin);
    if (
      parsed.origin !== contractTarget.origin ||
      parsed.pathname !== "/" ||
      parsed.search ||
      parsed.hash ||
      !rule.allowed_origins.includes(parsed.origin)
    ) {
      throw new Error("origin must be an exact allowed origin");
    }
    origin = parsed.origin;
  } catch {
    throw new Error("account-login actions require an exact allowed origin target");
  }
  return {
    tabId,
    frameId: contractTarget.frame_id,
    documentId: contractTarget.document_id,
    viewGeneration: contractTarget.view_generation,
    origin,
  };
}

async function verifyLoginTarget(tabId, target, pack) {
  const targetResult = await chrome.tabs.sendMessage(
    tabId,
    {
      type: "opencli-script-host.login-target",
      pack: pack.id,
      version: pack.version,
      rule: pack.ruleManifest,
    },
    { frameId: target.frameId },
  );
  if (
    !targetResult?.ok ||
    targetResult.target?.origin !== target.origin ||
    String(targetResult.target?.documentId) !== String(target.documentId) ||
    Number(targetResult.target?.viewGeneration) !== target.viewGeneration
  ) {
    throw new Error("account-login target generation is stale or unavailable");
  }
  return targetResult;
}

function loginRefreshKey(args, target) {
  return [args?.session_id, args?.epoch, args?.rule_id, args?.rule_version, target.tabId, target.frameId].join(":");
}

function waitForTabComplete(tabId, timeoutMs = 10_000) {
  return new Promise((resolve, reject) => {
    let timer = null;
    const cleanup = () => {
      chrome.tabs.onUpdated.removeListener(onUpdated);
      if (timer !== null) clearTimeout(timer);
    };
    const onUpdated = (updatedTabId, changeInfo) => {
      if (updatedTabId !== tabId || changeInfo.status !== "complete") return;
      cleanup();
      resolve();
    };
    chrome.tabs.onUpdated.addListener(onUpdated);
    timer = setTimeout(() => {
      cleanup();
      reject(new Error("account-login refresh did not finish navigation"));
    }, timeoutMs);
  });
}

async function refreshLogin(tabId, target, args, pack) {
  const refresh = pack.ruleManifest?.refresh;
  if (
    !refresh ||
    refresh.trigger !== "qr_expired" ||
    !Number.isInteger(refresh.interval) ||
    refresh.interval < 1 ||
    !Number.isInteger(refresh.max_attempts) ||
    refresh.max_attempts < 1
  ) {
    throw new Error("account-login refresh rule is unavailable");
  }
  if (args?.expected_view_generation !== target.viewGeneration) {
    throw new Error("account-login refresh target generation is stale");
  }
  if (!Number.isInteger(args?.expected_qr_generation) || args.expected_qr_generation < 0) {
    throw new Error("account-login refresh requires a QR generation");
  }
  if (args?.trigger !== refresh.trigger) {
    throw new Error("account-login refresh trigger is not allowed by the rule");
  }
  if (
    typeof args?.session_id !== "string" ||
    !Number.isInteger(args?.epoch) ||
    args.epoch < 0
  ) {
    throw new Error("account-login refresh requires a session epoch");
  }
  const key = loginRefreshKey(args, target);
  const now = Date.now();
  const previous = loginRefreshes.get(key);
  if (previous) {
    if (previous.attempts >= refresh.max_attempts) {
      throw new Error("account-login refresh attempt limit reached");
    }
    if (now - previous.lastAttempt < refresh.interval * 1000) {
      throw new Error("account-login refresh interval has not elapsed");
    }
  }
  loginRefreshes.set(key, {
    viewGeneration: target.viewGeneration,
    attempts: previous ? previous.attempts + 1 : 1,
    lastAttempt: now,
  });
  const beforeTarget = await verifyLoginTarget(tabId, target, pack);
  if (
    beforeTarget.target.qrGeneration !== null &&
    beforeTarget.target.qrGeneration !== args.expected_qr_generation
  ) {
    throw new Error("account-login refresh QR generation is stale");
  }
  if (pack.ruleManifest.id !== "controlled-login-fixture") {
    // Expiration observed by the node can become scanned/challenge while this
    // RPC is in flight. Re-read the fixed rule in the exact document; no timer,
    // missing QR, or stale screenshot is authority to reload a login page.
    const checked = await chrome.tabs.sendMessage(tabId, {
      type: "opencli-script-host.invoke", pack: pack.id, version: pack.version,
      action: "login.observe", args, target, rule: pack.ruleManifest,
    }, {frameId: target.frameId});
    const observed = checked?.result;
    if (checked?.ok !== true || observed?.state !== "refreshing" ||
        observed?.error_code !== "auth_required" || observed?.evidence_kind !== "unknown" ||
        observed.external_identity != null || observed.region_focus != null ||
        observed.session_id !== args.session_id || observed.epoch !== args.epoch ||
        observed.rule_id !== pack.ruleManifest.id || observed.rule_version !== pack.ruleManifest.version ||
        observed.view_generation !== target.viewGeneration ||
        observed.target?.tab_id !== target.tabId || observed.target?.frame_id !== target.frameId ||
        String(observed.target?.document_id) !== String(target.documentId) ||
        observed.target?.origin !== target.origin ||
        beforeTarget.target.qrGeneration !== args.expected_qr_generation) {
      throw new Error("account-login QR is no longer verifiably expired");
    }
  }
  const navigation = waitForTabComplete(tabId);
  await chrome.tabs.reload(tabId);
  await navigation;
  const afterTarget = await chrome.tabs.sendMessage(
    tabId,
    {
      type: "opencli-script-host.login-target",
      pack: pack.id,
      version: pack.version,
      rule: pack.ruleManifest,
    },
    { frameId: target.frameId },
  );
  if (
    !afterTarget?.ok ||
    afterTarget.target?.origin !== target.origin ||
    Number(afterTarget.target?.viewGeneration) <= target.viewGeneration ||
    String(afterTarget.target?.documentId) === String(target.documentId) ||
    !Number.isInteger(afterTarget.target?.qrGeneration) ||
    afterTarget.target.qrGeneration <= args.expected_qr_generation
  ) {
    throw new Error("account-login refresh did not rotate the target generation");
  }
  return {
    result: {
      state: "refreshing",
      view_generation: Number(afterTarget.target.viewGeneration),
    },
  };
}

async function invokePackAction({ pack: packId, action: actionId, args, tabId }) {
  await initialization;
  let pack = packs.get(packId);
  const action = pack?.actions.find((candidate) => candidate.id === actionId);
  if (!pack || !action) throw new Error("unknown pack action");
  let resolvedTabId = tabId;
  let loginTarget = null;
  let loginRule = null;
  if (packId === "account-login") {
    if (!Number.isInteger(resolvedTabId)) {
      throw new Error("account-login actions require an explicit tabId");
    }
    const ruleKey = args?.rule_id === undefined && args?.rule_version === undefined
      ? `${pack.ruleManifest.id}@${pack.ruleManifest.version}`
      : `${args?.rule_id}@${args?.rule_version}`;
    loginRule = pack.ruleManifests?.[ruleKey];
    pack = { ...pack, ruleManifest: loginRule };
    if (!loginRule) throw new Error("account-login fixed rule is unavailable");
    loginTarget = requireLoginTarget(args, resolvedTabId, loginRule);
    const tab = await chrome.tabs.get(resolvedTabId);
    let currentOrigin;
    try {
      currentOrigin = new URL(tab.url || "").origin;
    } catch {
      throw new Error("account-login target URL is unavailable");
    }
    if (currentOrigin !== loginTarget.origin) {
      throw new Error("account-login target origin changed");
    }
    await verifyLoginTarget(resolvedTabId, loginTarget, pack);
    if (actionId === "login.open") {
      const loginUrl = new URL(loginRule.login_url, loginTarget.origin);
      if (loginUrl.origin !== loginTarget.origin || loginUrl.pathname + loginUrl.search !== loginRule.login_url || loginUrl.hash) {
        throw new Error("account-login fixed login URL is not allowed");
      }
      if (args?.login_url !== undefined && args.login_url !== loginUrl.href) {
        throw new Error("account-login action cannot override the fixed login URL");
      }
      await chrome.tabs.update(resolvedTabId, { url: loginUrl.href });
      return {
        result: {
          ok: true,
          state: "opening",
          view_generation: loginTarget.viewGeneration,
        },
      };
    }
    if (actionId === "login.refresh") {
      return await refreshLogin(resolvedTabId, loginTarget, args, pack);
    }
  } else if (!Number.isInteger(resolvedTabId)) {
    const [activeTab] = await chrome.tabs.query({
      active: true,
      currentWindow: true,
    });
    resolvedTabId = activeTab?.id;
  }
  if (!Number.isInteger(resolvedTabId)) throw new Error("active tab is required");
  const before = loginTarget ? null : await chrome.tabs.get(resolvedTabId);
  const message = {
    type: "opencli-script-host.invoke",
    pack: pack.id,
    version: pack.version,
    action: action.id,
    args: args ?? {},
  };
  if (loginTarget) {
    message.target = loginTarget;
    message.rule = loginRule;
  }
  const sendOptions = loginTarget ? { frameId: loginTarget.frameId } : undefined;
  const result = await chrome.tabs.sendMessage(
    resolvedTabId,
    message,
    sendOptions,
  );
  if (loginTarget) {
    if (actionId === 'login.observe' && loginRule.id === 'xiaohongshu-qr' && result?.ok &&
        result.result?.state !== 'challenge' && result.result?.region_focus?.region_kind !== 'qr') {
      try {
        const probe = await probeXhsIdentity({target:loginTarget,ruleId:loginRule.id,ruleVersion:loginRule.version},
          {tab:{id:resolvedTabId},documentId:loginTarget.documentId});
        if (probe.evidence_kind === 'valid' && probe.external_identity) {
          result.result.evidence_kind = 'valid';
          result.result.external_identity = probe.external_identity;
          result.result.observed_at = probe.observed_at;
          result.result.state = 'verifying';
          delete result.result.region_focus; delete result.result.error_code;
        } else if (probe.browser_session_state === 'session_authenticated') {
          result.result.browser_session_state = 'session_authenticated';
          result.result.observed_at = probe.observed_at;
          delete result.result.error_code;
        } else if (probe.evidence_kind === 'invalid') {
          result.result.evidence_kind = 'invalid';
          result.result.error_code = 'auth_required';
          result.result.browser_session_state = 'unknown';
          result.result.observed_at = probe.observed_at;
          delete result.result.external_identity;
        }
      } catch {
        // Unavailable official verification leaves the page observation unknown.
      }
      await verifyLoginTarget(resolvedTabId,loginTarget,pack);
    }
    return { result };
  }
  const after = await chrome.tabs.get(resolvedTabId);
  return {
    result,
    page_before: { url: before.url ?? null, title: before.title ?? null },
    page_after: { url: after.url ?? null, title: after.title ?? null },
  };
}

async function verifyBoundLoginTarget(target) {
  await initialization;
  const pack = packs.get('account-login');
  const pinned = loginTargetRules.get(target?.tabId);
  const rules = pinned
    ? (pinned.documentId === target?.documentId ? [pinned.rule] : [])
    : Object.values(pack?.ruleManifests || {}).filter(
      rule => rule.allowed_origins.includes(target?.origin));
  if (rules.length !== 1 || !Number.isInteger(target?.tabId) || target.frameId !== 0)
    throw new Error('login target rule is missing or ambiguous');
  return verifyLoginTarget(target.tabId, target, {...pack, ruleManifest:rules[0]});
}

async function discoverLoginTarget({rule_id, rule_version, origin, tab_id}) {
  await initialization;
  const pack = packs.get('account-login');
  const rule = pack?.ruleManifests?.[`${rule_id}@${rule_version}`];
  if (!rule || !rule.allowed_origins.includes(origin)) throw new Error('login rule unavailable');
  const tabs = await chrome.tabs.query({});
  const matches = tabs.filter(tab => {
    try { const currentOrigin = new URL(tab.url || '').origin; return tab_id == null ? currentOrigin === origin : tab.id === tab_id && rule.allowed_origins.includes(currentOrigin); } catch { return false; }
  });
  if (matches.length !== 1 || !Number.isInteger(matches[0].id))
    throw new Error('login target is ambiguous');
  const tabId = matches[0].id;
  origin = new URL(matches[0].url).origin;
  const probeMessage = {
    type:'opencli-script-host.login-target', pack:pack.id, version:pack.version, rule,
  };
  let probe;
  try {
    probe = await chrome.tabs.sendMessage(tabId, probeMessage, {frameId:0});
  } catch (error) {
    // A page may have completed before dynamic registration at browser startup.
    // Inject only packaged code into that already selected official main frame.
    if (matches[0].status !== 'complete' ||
        !/Receiving end does not exist/.test(String(error?.message))) throw error;
  }
  // Another pack listener can leave this message unanswered without throwing.
  // Explicit negative replies must still fail without reinjecting or retrying.
  if (probe === undefined && matches[0].status === 'complete') {
    await chrome.scripting.executeScript({target:{tabId,frameIds:[0]},world:'ISOLATED',files:pack.js});
    probe = await chrome.tabs.sendMessage(tabId, probeMessage, {frameId:0});
  }
  if (!probe?.ok || probe.target?.origin !== origin) throw new Error('login target probe failed');
  const target = {tabId,frameId:0,documentId:probe.target.documentId,
    origin,viewGeneration:probe.target.viewGeneration};
  await verifyLoginTarget(tabId, target, {...pack,ruleManifest:rule});
  loginTargetRules.set(tabId, {documentId:target.documentId,rule});
  return {tab_id:tabId,frame_id:0,document_id:target.documentId,origin,
    view_generation:target.viewGeneration,qr_generation:probe.target.qrGeneration};
}

globalThis.opencliScriptHost = Object.freeze({
  health,
  invoke: invokePackAction,
  discoverLoginTarget,
  verifyLoginTarget: verifyBoundLoginTarget,
});

const probeXhsIdentity = createXhsIdentityProbe({verify: async (message) => {
  await initialization;
  const {target, ruleId, ruleVersion} = message;
  const pack = packs.get('account-login');
  const rule = pack?.ruleManifests?.[`${ruleId}@${ruleVersion}`];
  if (rule?.id !== 'xiaohongshu-qr' || target?.frameId !== 0 || target?.origin !== 'https://www.xiaohongshu.com')
    throw new Error('probe_rule_rejected');
  const tab = await chrome.tabs.get(target.tabId);
  if (new URL(tab.url).origin !== target.origin) throw new Error('probe_origin_changed');
  await verifyLoginTarget(target.tabId,target,{...pack,ruleManifest:rule});
}});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id) return false;
  if (message?.type === "script-host.health") {
    sendResponse(health());
    return false;
  }
  if (message?.type !== "script-host.invoke") return false;
  invokePackAction(message).then(
    (result) => sendResponse({ ok: true, ...result }),
    (error) => sendResponse({ ok: false, error: error.message }),
  );
  return true;
});
