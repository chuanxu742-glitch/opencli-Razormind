const SCRIPT_ID_PREFIX = "opencli-pack-";
const packs = new Map();
let startupError = null;

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
  packs.clear();
  startupError = null;
  try {
    await loadPackIndex();
    await registerPacks();
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
    ok: startupError === null,
    error: startupError,
    packs: [...packs.values()].map(({ id, version, actions }) => ({
      id,
      version,
      actions: actions.map((action) => action.id),
    })),
  };
}

async function invokePackAction({ pack: packId, action: actionId, args, tabId }) {
  const pack = packs.get(packId);
  const action = pack?.actions.find((candidate) => candidate.id === actionId);
  if (!pack || !action) throw new Error("unknown pack action");
  let resolvedTabId = tabId;
  if (!Number.isInteger(resolvedTabId)) {
    const [activeTab] = await chrome.tabs.query({
      active: true,
      currentWindow: true,
    });
    resolvedTabId = activeTab?.id;
  }
  if (!Number.isInteger(resolvedTabId)) throw new Error("active tab is required");
  const before = await chrome.tabs.get(resolvedTabId);
  const result = await chrome.tabs.sendMessage(resolvedTabId, {
    type: "opencli-script-host.invoke",
    pack: pack.id,
    version: pack.version,
    action: action.id,
    args: args ?? {},
  });
  const after = await chrome.tabs.get(resolvedTabId);
  return {
    result,
    page_before: { url: before.url ?? null, title: before.title ?? null },
    page_after: { url: after.url ?? null, title: after.title ?? null },
  };
}

globalThis.opencliScriptHost = Object.freeze({
  health,
  invoke: invokePackAction,
});

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
