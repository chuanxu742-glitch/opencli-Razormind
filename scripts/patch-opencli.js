#!/usr/bin/env node
/**
 * Post-install patch for @jackwener/opencli (CommonJS, works on Node 18+).
 *
 * Usage:
 *   node patch-opencli.js               # patch the default global install
 *   node patch-opencli.js /opt/my-dir   # patch a specific npm prefix dir
 *
 * Adds two env-var hooks that the published package lacks:
 *
 *   OPENCLI_DAEMON_LISTEN  (daemon.js)
 *     Default: 127.0.0.1 — set to 0.0.0.0 so the API container can reach
 *     the daemon running inside the chrome-N container.
 *
 *   OPENCLI_DAEMON_HOST  (daemon-client.js + browser/bridge.js)
 *     Default: 127.0.0.1 — set to chrome-1 (or chrome-N) so the CLI in the
 *     API container contacts the remote daemon instead of spawning one locally.
 *
 * v1.7.0 migration: mcp.js renamed to browser/bridge.js
 */

'use strict';

const fs = require('fs');
const path = require('path');
const staged = new Map();

function readPlanned(filePath) {
  return staged.has(filePath) ? staged.get(filePath) : fs.readFileSync(filePath, 'utf8');
}

function resolvePackageDir(prefixDir) {
  if (prefixDir) {
    // Explicit prefix supplied (e.g. /opt/opencli-bridge)
    const candidate = path.join(prefixDir, 'lib', 'node_modules', '@jackwener', 'opencli');
    if (fs.existsSync(candidate)) return candidate;
    // Some npm versions omit the 'lib/' level
    const candidate2 = path.join(prefixDir, 'node_modules', '@jackwener', 'opencli');
    if (fs.existsSync(candidate2)) return candidate2;
    throw new Error('Could not find @jackwener/opencli under prefix: ' + prefixDir);
  }
  try {
    return path.dirname(require.resolve('@jackwener/opencli/package.json'));
  } catch (_) {
    // Fallback: ask npm where its global root is (works with NodeSource installs)
    const { execSync } = require('child_process');
    const npmRoot = execSync('npm root -g').toString().trim();
    return path.join(npmRoot, '@jackwener', 'opencli');
  }
}

function patch(filePath, search, replace, label) {
  if (!fs.existsSync(filePath)) {
    throw new Error('Required runtime file missing for ' + label + ': ' + filePath);
  }
  let content = readPlanned(filePath);
  if (label === 'execution.js: managed web-adapter CDP routing' &&
      content.includes('OPENCLI_ADMIN_MANAGED_CDP_ROUTING_V2')) {
    console.log('  [skip] ' + label + ' superseded by V2');
    return;
  }
  if (content.split(replace).length === 2) {
    console.log('  [skip] ' + label + ' already patched');
    return;
  }
  if (content.split(search).length !== 2) {
    throw new Error('Unsupported runtime structure for ' + label + ': ' + filePath);
  }
  content = content.replace(search, replace);
  staged.set(filePath, content);
  console.log('  [ok]   ' + label);
}

const prefixDir = process.argv[2] || null;
const pkgDir = resolvePackageDir(prefixDir);
const packageVersion = JSON.parse(fs.readFileSync(path.join(pkgDir, 'package.json'), 'utf8')).version;
if (packageVersion !== '1.8.7') {
  throw new Error('Runtime patch requires @jackwener/opencli 1.8.7');
}
console.log('Patching opencli at ' + pkgDir + ' ...');

// ── 1. daemon.js: honour OPENCLI_DAEMON_LISTEN ───────────────────────────────
patch(
  path.join(pkgDir, 'dist', 'src', 'daemon.js'),
  "httpServer.listen(PORT, '127.0.0.1', () => {",
  "const DAEMON_LISTEN = process.env.OPENCLI_DAEMON_LISTEN ?? '127.0.0.1';\nhttpServer.listen(PORT, DAEMON_LISTEN, () => {",
  'daemon.js: OPENCLI_DAEMON_LISTEN'
);

// ── 2. daemon-client.js: honour OPENCLI_DAEMON_HOST ─────────────────────────
patch(
  path.join(pkgDir, 'dist', 'src', 'browser', 'daemon-transport.js'),
  'const DAEMON_URL = `http://127.0.0.1:${DAEMON_PORT}`;',
  "const DAEMON_HOST = process.env.OPENCLI_DAEMON_HOST ?? '127.0.0.1';\nconst DAEMON_URL = `http://${DAEMON_HOST}:${DAEMON_PORT}`;",
  'daemon-client.js: OPENCLI_DAEMON_HOST'
);

// ── 3. browser/bridge.js: skip local auto-spawn when daemon is remote ────────
// v1.7.0+: _ensureDaemon moved from mcp.js to browser/bridge.js.
// When OPENCLI_DAEMON_HOST is set to a remote address, probe that daemon
// instead of spawning a process inside the caller's container.
patch(
  path.join(pkgDir, 'dist', 'src', 'browser', 'bridge.js'),
  "import { ensureBrowserBridgeReady } from './daemon-lifecycle.js';",
  "// OPENCLI_ADMIN_REMOTE_DAEMON_IMPORT_V1\nimport { ensureBrowserBridgeReady } from './daemon-lifecycle.js';\nimport { fetchDaemonStatus } from './daemon-transport.js';",
  'browser/bridge.js: import remote daemon status probe'
);

patch(
  path.join(pkgDir, 'dist', 'src', 'browser', 'bridge.js'),
  `    async _ensureDaemon(timeoutSeconds, contextId, preferredContextId) {
        await ensureBrowserBridgeReady({
            timeoutSeconds: timeoutSeconds ?? Math.ceil(DAEMON_SPAWN_TIMEOUT / 1000),
            contextId,
            preferredContextId,
        });
    }`,
  `    // OPENCLI_ADMIN_REMOTE_DAEMON_ROUTE_V2
    async _ensureDaemon(timeoutSeconds, contextId, preferredContextId) {
        const daemonHost = process.env.OPENCLI_DAEMON_HOST;
        if (daemonHost && daemonHost !== '127.0.0.1' && daemonHost !== 'localhost') {
            const remoteStatus = await fetchDaemonStatus({
                timeout: (timeoutSeconds ?? Math.ceil(DAEMON_SPAWN_TIMEOUT / 1000)) * 1000,
                contextId,
                preferredContextId,
            });
            if (!remoteStatus) {
                throw new Error('Remote Browser Bridge daemon at ' + daemonHost + ' is not reachable. Ensure BROWSER_BRIDGE_ENABLED=true on the chrome container.');
            }
            return;
        }
        await ensureBrowserBridgeReady({
            timeoutSeconds: timeoutSeconds ?? Math.ceil(DAEMON_SPAWN_TIMEOUT / 1000),
            contextId,
            preferredContextId,
        });
    }`,
  'browser/bridge.js: route remote daemon health checks'
);

// ── 4. execution.js: honour explicit CDP endpoint for web adapters ──────────
// OpenCLI 1.8.7 only reads OPENCLI_CDP_ENDPOINT inside the Electron branch.
// A normal web adapter therefore silently falls back to Browser Bridge and can
// escape Admin's selected profile. Managed acquisition must fail closed at the
// requested endpoint instead.
patch(
  path.join(pkgDir, 'dist', 'src', 'execution.js'),
  `            let cdpEndpoint;
            if (electron) {
                // Electron apps: respect manual endpoint override, then try auto-detect
                const manualEndpoint = process.env.OPENCLI_CDP_ENDPOINT;
                if (manualEndpoint) {
                    const port = Number(new URL(manualEndpoint).port);
                    if (!await probeCDP(port)) {
                        throw new CommandExecutionError(\`CDP not reachable at \${manualEndpoint}\`, 'Check that the app is running with --remote-debugging-port and the endpoint is correct.');
                    }
                    cdpEndpoint = manualEndpoint;
                }
                else {
                    cdpEndpoint = await resolveElectronEndpoint(cmd.site);
                }
            }`,
  `            // OPENCLI_ADMIN_MANAGED_CDP_ROUTING_V1
            const manualEndpoint = process.env.OPENCLI_CDP_ENDPOINT;
            let cdpEndpoint;
            if (manualEndpoint) {
                const port = Number(new URL(manualEndpoint).port);
                if (!await probeCDP(port)) {
                    throw new CommandExecutionError(\`CDP not reachable at \${manualEndpoint}\`, 'Check that the managed browser profile is running and the endpoint is correct.');
                }
                cdpEndpoint = manualEndpoint;
            }
            else if (electron) {
                cdpEndpoint = await resolveElectronEndpoint(cmd.site);
            }`,
  'execution.js: managed web-adapter CDP routing'
);

// Upgrade the first managed-routing patch so remote/container endpoints are
// probed at their real host. OpenCLI's probeCDP(port) is intentionally local
// to Electron discovery and always calls 127.0.0.1.
patch(
  path.join(pkgDir, 'dist', 'src', 'execution.js'),
  `            // OPENCLI_ADMIN_MANAGED_CDP_ROUTING_V1
            const manualEndpoint = process.env.OPENCLI_CDP_ENDPOINT;
            let cdpEndpoint;
            if (manualEndpoint) {
                const port = Number(new URL(manualEndpoint).port);
                if (!await probeCDP(port)) {
                    throw new CommandExecutionError(\`CDP not reachable at \${manualEndpoint}\`, 'Check that the managed browser profile is running and the endpoint is correct.');
                }
                cdpEndpoint = manualEndpoint;
            }
            else if (electron) {
                cdpEndpoint = await resolveElectronEndpoint(cmd.site);
            }`,
  `// OPENCLI_ADMIN_MANAGED_CDP_ROUTING_V2
            const manualEndpoint = process.env.OPENCLI_CDP_ENDPOINT?.trim();
            let cdpEndpoint;
            if (manualEndpoint) {
                let reachable = true;
                try {
                    const probeUrl = new URL(manualEndpoint);
                    if (probeUrl.protocol === 'http:' || probeUrl.protocol === 'https:') {
                        probeUrl.pathname = probeUrl.pathname.replace(/\\/$/, '') + '/json';
                        const response = await fetch(probeUrl, { signal: AbortSignal.timeout(3000) });
                        reachable = response.ok;
                    }
                }
                catch {
                    reachable = false;
                }
                if (!reachable) {
                    throw new CommandExecutionError(\`CDP not reachable at \${manualEndpoint}\`, 'Check that the managed browser profile is running and the endpoint is correct.');
                }
                cdpEndpoint = manualEndpoint;
            }
            else if (electron) {
                cdpEndpoint = await resolveElectronEndpoint(cmd.site);
            }`,
  'execution.js: probe the actual managed CDP host'
);

patch(
  path.join(pkgDir, 'dist', 'src', 'execution.js'),
  '            const BrowserFactory = getBrowserFactory(cmd.site);',
  '            // OPENCLI_ADMIN_FACTORY_SELECTION_V1\n            const BrowserFactory = getBrowserFactory(cmd.site, { cdpEndpoint });',
  'execution.js: select CDP factory for managed endpoint'
);

patch(
  path.join(pkgDir, 'dist', 'src', 'runtime.js'),
  `export function getBrowserFactory(site) {
    if (site && isElectronApp(site))
        return CDPBridge;
    return BrowserBridge;
}`,
  `// OPENCLI_ADMIN_RUNTIME_FACTORY_V1
export function getBrowserFactory(site, opts = {}) {
    if (opts.cdpEndpoint || (site && isElectronApp(site)))
        return CDPBridge;
    return BrowserBridge;
}`,
  'runtime.js: explicit endpoint selects CDPBridge'
);

const executionSource = readPlanned(path.join(pkgDir, 'dist', 'src', 'execution.js'));
const runtimeSource = readPlanned(path.join(pkgDir, 'dist', 'src', 'runtime.js'));
const bridgeSource = readPlanned(path.join(pkgDir, 'dist', 'src', 'browser', 'bridge.js'));
if (!executionSource.includes('OPENCLI_ADMIN_MANAGED_CDP_ROUTING_V2') ||
    !executionSource.includes('OPENCLI_ADMIN_FACTORY_SELECTION_V1') ||
    !runtimeSource.includes('OPENCLI_ADMIN_RUNTIME_FACTORY_V1') ||
    !bridgeSource.includes('OPENCLI_ADMIN_REMOTE_DAEMON_ROUTE_V2')) {
  throw new Error('Managed CDP routing patch verification failed');
}

// Commit only after every required runtime transformation passed preflight.
for (const [target, content] of staged) {
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, content);
}
console.log('Done.');
