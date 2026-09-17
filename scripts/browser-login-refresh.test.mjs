import {createXhsIdentityProbe} from '../chrome/platform-login-bundle4/xhs-identity.js';
import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync, existsSync} from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';

const base = new URL('../chrome/platform-login-bundle4/', import.meta.url);
const source = readFileSync(new URL('background.js', base), 'utf8').replace(/^import .*;\r?\n/, '');

async function worker(platform, mutate = () => {}) {
  const rule = JSON.parse(readFileSync(new URL(`packs/account-login/${platform}-qr.json`, base), 'utf8'));
  const state = {now: 0, generation: 1, reloads: 0, state: 'refreshing', evidence: 'unknown', listener: null};
  const origin = rule.allowed_origins[0];
  const ctx = vm.createContext({createXhsIdentityProbe,URL, Map, Set, console, crypto: webcrypto, TextEncoder, setTimeout, clearTimeout, Date: {now: () => state.now},
    fetch: async path => ({ok: true, json: async () => {
      let file = new URL(path, base);
      if (!existsSync(file)) file = new URL('../chrome/script-host/' + path, import.meta.url);
      const value = JSON.parse(readFileSync(file, 'utf8'));
      mutate(path, value);
      return value;
    }}),
    chrome: {
      runtime: {getURL: path => path, onInstalled: {addListener() {}}, onStartup: {addListener() {}}, onMessage: {addListener() {}}},
      scripting: {getRegisteredContentScripts: async () => [], registerContentScripts: async () => {}},
      tabs: {
        get: async id => ({id, url: origin + rule.login_url}),
        onUpdated: {addListener: fn => {state.listener = fn;}, removeListener: () => {state.listener = null;}},
        reload: async id => {state.reloads++; state.generation++; state.listener(id, {status: 'complete'});},
        sendMessage: async (id, message) => {
          assert.equal(message.rule.id, rule.id);
          assert.equal(message.rule.version, rule.version);
          if (message.type === 'opencli-script-host.login-target') return {ok: true, target: {
            documentId: `d${state.generation}`, viewGeneration: state.generation, origin, qrGeneration: state.generation,
          }};
          assert.equal(message.action, 'login.observe');
          return {ok: true, result: {
            session_id: 's', epoch: 1, rule_id: rule.id, rule_version: rule.version,
            state: state.state, evidence_kind: state.evidence, error_code: 'auth_required',
            target: {tab_id: id, frame_id: 0, document_id: `d${state.generation}`, origin}, view_generation: state.generation,
          }};
        },
      },
    },
  });
  vm.runInContext(source, ctx);
  await vm.runInContext('initialization', ctx);
  const refresh = () => {
    ctx.request = {pack: 'account-login', action: 'login.refresh', tabId: 7, args: {
      session_id: 's', epoch: 1, rule_id: rule.id, rule_version: rule.version, trigger: 'qr_expired',
      expected_view_generation: state.generation, expected_qr_generation: state.generation,
      target: {tab_id: 7, frame_id: 0, document_id: `d${state.generation}`, origin, view_generation: state.generation},
    }};
    return vm.runInContext('globalThis.opencliScriptHost.invoke(request)', ctx);
  };
  return {state, refresh, startupError: vm.runInContext('startupError', ctx)};
}

for (const [label, mutate] of [
  ['rule contents', (path, value) => {if (path.endsWith('/bilibili-qr.json')) value.login_url = '/other';}],
  ['index origin', (path, value) => {if (path === 'packs/index.json') value.find(pack => pack.id === 'account-login').matches.push('https://example.org/*');}],
  ['index rule path', (path, value) => {if (path === 'packs/index.json') value.find(pack => pack.id === 'account-login').rules[0].path = 'packs/account-login/rules.json';}],
]) {
  test(`packaged catalog rejects changed ${label}`, async () => {
    const {startupError, refresh, state} = await worker('bilibili', mutate);
    assert.ok(startupError);
    await assert.rejects(refresh());
    assert.equal(state.reloads, 0);
  });
}

for (const platform of ['xiaohongshu', 'bilibili', 'douyin']) {
  test(`${platform} dynamic fixed rule rotates exact expired tab and retains budget across documents`, async () => {
    const {state, refresh} = await worker(platform);
    await refresh();
    assert.equal(state.reloads, 1);
    await assert.rejects(refresh(), /interval/);
    state.now = 30000; await refresh();
    state.now = 60000; await refresh();
    state.now = 90000; await assert.rejects(refresh(), /attempt limit/);
    assert.equal(state.reloads, 3);
  });
  for (const stateName of ['challenge', 'verifying', 'presenting', 'unknown']) {
    test(`${platform} rejects expiration superseded by ${stateName} before reload`, async () => {
      const {state, refresh} = await worker(platform);
      state.state = stateName;
      await assert.rejects(refresh(), /no longer verifiably expired/);
      assert.equal(state.reloads, 0);
    });
  }
}
