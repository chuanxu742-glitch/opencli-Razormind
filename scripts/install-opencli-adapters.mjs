#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { createRequire } from 'node:module';

const VERSION = '1.8.7';
const SITES = new Set(['amazon', 'taobao', 'coupang', 'ebay']);
const runtimeJson = `${JSON.stringify({ name: 'opencli-user-runtime', private: true, type: 'module' }, null, 2)}\n`;
const fail = message => { throw new Error(message); };
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const json = value => `${JSON.stringify(value, null, 2)}\n`;
const exists = file => { try { return fs.lstatSync(file); } catch (e) { if (e.code === 'ENOENT') return null; throw e; } };
const readJson = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);

function safeRelative(file, restricted = false) {
  if (typeof file !== 'string' || !file || file.includes('\\') || file.includes(':') || file.includes('\0') || path.posix.isAbsolute(file)
      || file.split('/').some(part => !part || part === '.' || part === '..' || /[. ]$/.test(part))) fail(`Unsafe relative path: ${file}`);
  if (restricted && (!SITES.has(file.split('/')[0]) || !/^[a-z0-9-]+\/[a-z0-9-]+\.js$/.test(file))) fail(`File outside adapter allowlist: ${file}`);
  return file;
}

function checkPath(file, kind) {
  const parent = path.dirname(file);
  if (parent !== file) checkPath(parent, 'directory');
  const stat = exists(file);
  if (!stat) return;
  if (stat.isSymbolicLink()) fail(`Symlink/junction is not permitted: ${file}`);
  if (kind === 'directory' ? !stat.isDirectory() : !stat.isFile()) fail(`Unexpected ${kind}: ${file}`);
  fs.accessSync(file, fs.constants.R_OK);
}

function writable(file) {
  let target = file;
  while (!exists(target)) target = path.dirname(target);
  fs.accessSync(target, fs.constants.W_OK);
  if (process.platform !== 'win32' && !(fs.statSync(target).mode & 0o222)) fail(`Not writable: ${target}`);
  if (exists(file)) {
    fs.accessSync(path.dirname(file), fs.constants.W_OK);
    if (process.platform !== 'win32' && !(fs.statSync(path.dirname(file)).mode & 0o222)) fail(`Not writable: ${path.dirname(file)}`);
  }
}

function commandKeys(entry) {
  if (!plain(entry) || !/^[\w-]+$/.test(entry.site ?? '') || !/^[\w-]+$/.test(entry.name ?? '')
      || !['read', 'write'].includes(entry.access) || typeof entry.modulePath !== 'string') fail('Invalid CLI manifest command');
  safeRelative(entry.modulePath);
  if (entry.sourceFile !== undefined) safeRelative(entry.sourceFile);
  if (entry.aliases !== undefined && (!Array.isArray(entry.aliases) || entry.aliases.some(alias => typeof alias !== 'string' || !/^[\w-]+$/.test(alias)))) fail('Invalid CLI aliases');
  if (entry.args !== undefined && !Array.isArray(entry.args)) fail('Invalid CLI arguments');
  return [...new Set([entry.name, ...(entry.aliases ?? [])])].map(name => `${entry.site}/${name}`);
}

function validateCommands(entries, inventoryFiles) {
  if (!Array.isArray(entries)) fail('Commands must be an array');
  const keys = new Set();
  for (const entry of entries) {
    if (inventoryFiles && (!SITES.has(entry.site) || !inventoryFiles.has(entry.modulePath)
        || (entry.sourceFile && !inventoryFiles.has(entry.sourceFile)))) fail('Command module absent from inventory');
    for (const key of commandKeys(entry)) {
      if (keys.has(key)) fail(`Duplicate CLI command/alias: ${key}`);
      keys.add(key);
    }
  }
  return keys;
}

function maskSource(source) {
  const masked = source.split('');
  const blank = index => {
    if (masked[index] !== '\n' && masked[index] !== '\r') masked[index] = ' ';
  };
  const blankRange = (start, end) => {
    for (let index = start; index < end; index++) blank(index);
  };
  const canStartRegex = index => {
    let cursor = index - 1;
    while (cursor >= 0 && /\s/.test(source[cursor])) cursor--;
    if (cursor < 0) return true;
    if ('([{=,:;!&|?+-*%^~<>'.includes(source[cursor])) return true;
    return /\b(?:case|delete|do|else|in|instanceof|of|return|throw|typeof|void)\s*$/
      .test(source.slice(Math.max(0, cursor - 12), cursor + 1));
  };
  for (let index = 0; index < source.length;) {
    const character = source[index];
    const next = source[index + 1];
    if (character === '/' && next === '/') {
      const start = index;
      index += 2;
      while (index < source.length && source[index] !== '\n' && source[index] !== '\r') index++;
      blankRange(start, index);
      continue;
    }
    if (character === '/' && next === '*') {
      const start = index;
      index += 2;
      while (index < source.length && !(source[index] === '*' && source[index + 1] === '/')) index++;
      index = Math.min(source.length, index + 2);
      blankRange(start, index);
      continue;
    }
    if (character === "'" || character === '"' || character === '`') {
      const start = index;
      const quote = character;
      index++;
      while (index < source.length) {
        if (source[index] === '\\') {
          index += 2;
          continue;
        }
        if (source[index] === quote) {
          index++;
          break;
        }
        index++;
      }
      blankRange(start, index);
      continue;
    }
    if (character === '/' && next !== '/' && next !== '*' && canStartRegex(index)) {
      const start = index;
      let inClass = false;
      index++;
      while (index < source.length) {
        if (source[index] === '\\') {
          index += 2;
          continue;
        }
        if (source[index] === '[') inClass = true;
        else if (source[index] === ']') inClass = false;
        else if (source[index] === '/' && !inClass) {
          index++;
          while (/[A-Za-z]/.test(source[index] ?? '')) index++;
          break;
        }
        index++;
      }
      blankRange(start, index);
      continue;
    }
    index++;
  }
  return masked.join('');
}

function maskComments(source) {
  const masked = source.split('');
  const blank = index => {
    if (masked[index] !== '\n' && masked[index] !== '\r') masked[index] = ' ';
  };
  const blankRange = (start, end) => {
    for (let index = start; index < end; index++) blank(index);
  };
  for (let index = 0; index < source.length;) {
    if (source[index] === '/' && source[index + 1] === '/') {
      const start = index;
      index += 2;
      while (index < source.length && source[index] !== '\n' && source[index] !== '\r') index++;
      blankRange(start, index);
      continue;
    }
    if (source[index] === '/' && source[index + 1] === '*') {
      const start = index;
      index += 2;
      while (index < source.length && !(source[index] === '*' && source[index + 1] === '/')) index++;
      index = Math.min(source.length, index + 2);
      blankRange(start, index);
      continue;
    }
    if (source[index] === "'" || source[index] === '"' || source[index] === '`') {
      const quote = source[index];
      index++;
      while (index < source.length) {
        if (source[index] === '\\') index += 2;
        else if (source[index++] === quote) break;
      }
      continue;
    }
    index++;
  }
  return masked.join('');
}


function balancedEnd(source, start, opening, closing, relative) {
  let depth = 0;
  for (let index = start; index < source.length; index++) {
    if (source[index] === opening) depth++;
    else if (source[index] === closing && --depth === 0) return index;
  }
  fail(`Unrecognized unmanaged registration: ${relative}`);
}

function literalString(value, maskedValue, relative, field) {
  let start = 0;
  while (start < maskedValue.length && /\s/.test(maskedValue[start])) start++;
  const quote = value[start];
  if (quote !== "'" && quote !== '"' && quote !== '`') {
    fail(`Dynamic ${field} requires a root cli-manifest.json: ${relative}`);
  }
  let end = start + 1;
  while (end < value.length) {
    if (value[end] === '\\') end += 2;
    else if (value[end] === quote) break;
    else end++;
  }
  if (value[end] !== quote || maskedValue.slice(end + 1).trim()) {
    fail(`Dynamic ${field} requires a root cli-manifest.json: ${relative}`);
  }
  const result = value.slice(start + 1, end);
  if (quote === '`' && /\$\{/.test(result)) {
    fail(`Dynamic ${field} requires a root cli-manifest.json: ${relative}`);
  }
  if (!/^[\w-]+$/.test(result)) fail(`Invalid ${field}: ${relative}`);
  return result;
}

function literalAliases(value, relative) {
  const masked = maskSource(value);
  let start = 0;
  while (start < masked.length && /\s/.test(masked[start])) start++;
  if (value[start] !== '[') fail(`Dynamic aliases require a root cli-manifest.json: ${relative}`);
  const end = balancedEnd(masked, start, '[', ']', relative);
  if (masked.slice(end + 1).trim()) fail(`Dynamic aliases require a root cli-manifest.json: ${relative}`);
  const aliases = [];
  let itemStart = start + 1;
  let depth = 0;
  for (let index = start + 1; index <= end; index++) {
    const character = masked[index];
    if ('([{'.includes(character)) depth++;
    else if (')]}'.includes(character)) depth--;
    if ((character === ',' && depth === 0) || index === end) {
      const itemEnd = index === end ? index : index;
      const item = value.slice(itemStart, itemEnd);
      const itemMasked = maskComments(item);
      if (itemMasked.trim()) aliases.push(literalString(item, itemMasked, relative, 'alias'));
      itemStart = index + 1;
    }
  }
  return aliases;
}

function literalCommandFields(object, maskedObject, relative) {
  const fields = new Map();
  let start = 0;
  let depth = 0;
  const consume = end => {
    const maskedProperty = maskedObject.slice(start, end);
    if (!maskedProperty.trim()) return;
    const property = object.slice(start, end);
    const keyMatch = maskedProperty.match(/^\s*([A-Za-z_$][\w$]*)\s*:/);
    if (keyMatch) {
      const key = keyMatch[1];
      if (fields.has(key)) fail(`Dynamic/duplicate command fields require a root cli-manifest.json: ${relative}`);
      fields.set(key, property.slice(keyMatch[0].length));
      return;
    }
    if (/^\s*\.\.\./.test(maskedProperty)
        || !/^\s*(?:(?:async|get|set)\s+)?[A-Za-z_$][\w$]*\s*\(/.test(maskedProperty)) {
      fail(`Dynamic/duplicate command fields require a root cli-manifest.json: ${relative}`);
    }
  };
  for (let index = 0; index <= maskedObject.length; index++) {
    const character = maskedObject[index];
    if ('([{'.includes(character ?? '')) depth++;
    else if (')]}'.includes(character ?? '')) depth--;
    if ((character === ',' && depth === 0) || index === maskedObject.length) {
      consume(index);
      start = index + 1;
    }
  }
  return fields;
}

function commandKeysFromSource(source, relative) {
  const masked = maskSource(source);
  const keys = new Set();
  const dynamicHook = masked.match(/\b(?:registerSiteAuthCommands|onStartup|onBeforeExecute|onAfterExecute)\s*\(/);
  if (dynamicHook) fail(`Dynamic unmanaged registration requires a root cli-manifest.json: ${relative}`);
  const calls = /\bcli\s*\(/g;
  let match;
  while ((match = calls.exec(masked))) {
    const callStart = match.index;
    const callEnd = balancedEnd(masked, callStart + match[0].length - 1, '(', ')', relative);
    let objectStart = callStart + match[0].length;
    while (objectStart < callEnd && /\s/.test(masked[objectStart])) objectStart++;
    if (masked[objectStart] !== '{') fail(`Dynamic CLI registration requires a root cli-manifest.json: ${relative}`);
    const objectEnd = balancedEnd(masked, objectStart, '{', '}', relative);
    const fields = literalCommandFields(
      source.slice(objectStart + 1, objectEnd),
      masked.slice(objectStart + 1, objectEnd),
      relative,
    );
    const siteValue = fields.get('site');
    const nameValue = fields.get('name');
    if (siteValue === undefined || nameValue === undefined) {
      fail(`Cannot safely identify unmanaged command: ${relative}; supply a root cli-manifest.json`);
    }
    const siteName = literalString(siteValue, maskComments(siteValue), relative, 'site');
    const name = literalString(nameValue, maskComments(nameValue), relative, 'name');
    keys.add(`${siteName}/${name}`);
    if (fields.has('aliases')) {
      for (const alias of literalAliases(fields.get('aliases'), relative)) keys.add(`${siteName}/${alias}`);
    }
    calls.lastIndex = callEnd + 1;
  }
  return keys;
}

// Do not execute arbitrary user modules during preflight. Dynamic registrations
// need the upstream root manifest to declare their keys without running code.
function scannedKeys(clis, owned) {
  const keys = new Set();
  if (!exists(clis)) return keys;
  for (const site of fs.readdirSync(clis, { withFileTypes: true })) {
    const dir = path.join(clis, site.name);
    if (site.isSymbolicLink()) fail(`User adapter directory is a link: ${dir}`);
    if (!site.isDirectory()) continue;
    // OpenCLI 1.8.7 scans only direct files below each site directory.
    for (const file of fs.readdirSync(dir)) {
      if (!file.endsWith('.js') || file.endsWith('.test.js') || file.endsWith('.d.js')) continue;
      const relative = `${site.name}/${file}`;
      if (owned.has(relative)) continue;
      const target = path.join(dir, file);
      checkPath(target, 'file');
      for (const key of commandKeysFromSource(fs.readFileSync(target, 'utf8'), relative)) keys.add(key);
    }
  }
  return keys;
}

function install() {
  const options = {};
  const args = process.argv.slice(2);
  for (let i = 0; i < args.length; i += 2) {
    const key = args[i];
    if (!['--source', '--prefix', '--home'].includes(key) || options[key] || !args[i + 1]) fail('Usage: --source <absolute> --prefix <absolute> --home <absolute>');
    options[key] = args[i + 1];
  }
  for (const key of ['--source', '--prefix', '--home']) {
    if (!options[key] || !path.isAbsolute(options[key])) fail(`${key} must be explicit and absolute`);
    options[key] = path.resolve(options[key]);
    checkPath(options[key], 'directory');
  }
  const source = options['--source'];
  const home = options['--home'];
  const prefix = options['--prefix'];
  if (!exists(source) || !exists(prefix) || !exists(home)) fail('Source, prefix and HOME must exist');
  const packages = ['lib/node_modules', 'node_modules'].map(layout => path.join(prefix, layout, '@jackwener/opencli')).filter(file => exists(file));
  if (packages.length !== 1) fail('Expected exactly one OpenCLI installation in the explicit prefix');
  const packageRoot = packages[0];
  checkPath(path.join(packageRoot, 'package.json'), 'file');
  const pkg = readJson(path.join(packageRoot, 'package.json'));
  if (pkg.name !== '@jackwener/opencli' || pkg.version !== VERSION) fail(`Official OpenCLI ${VERSION} is required`);
  for (const name of ['registry', 'errors']) {
    const exported = pkg.exports?.[`./${name}`];
    if (typeof exported !== 'string' || !exported.startsWith('./')) fail(`Missing public export: ${name}`);
    checkPath(path.join(packageRoot, safeRelative(exported.slice(2))), 'file');
    fs.accessSync(path.join(packageRoot, exported));
  }
  checkPath(path.join(packageRoot, 'dist/src/main.js'), 'file');
  fs.accessSync(path.join(packageRoot, 'dist/src/main.js'));
  const require = createRequire(path.join(packageRoot, 'package.json'));
  for (const dependency of Object.keys(pkg.dependencies ?? {})) {
    const resolved = require.resolve(dependency);
    const relative = path.relative(prefix, resolved);
    if (relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) fail(`Dependency is outside the managed prefix: ${dependency}`);
    checkPath(resolved, 'file');
  }

  const inventoryFile = path.join(source, 'adapter-pack.json');
  checkPath(inventoryFile, 'file');
  const inventory = readJson(inventoryFile);
  if (inventory.schemaVersion !== 1 || inventory.opencliVersion !== VERSION || !Array.isArray(inventory.files) || !inventory.files.length) fail('Invalid adapter inventory');
  const files = new Set(inventory.files.map(file => safeRelative(file, true)));
  if (files.size !== inventory.files.length) fail('Duplicate inventory files');
  const newKeys = validateCommands(inventory.commands, files);
  if (!newKeys.size) fail('Empty command inventory');
  checkPath(path.join(source, 'LICENSE.opencli'), 'file');
  if (!fs.readFileSync(path.join(source, 'LICENSE.opencli')).length) fail('Empty upstream license');
  const payload = new Map();
  for (const file of files) {
    checkPath(path.join(source, file), 'file');
    const bytes = fs.readFileSync(path.join(source, file));
    if (!bytes.length) fail(`Empty adapter: ${file}`);
    payload.set(file, bytes);
  }
  for (const [file, bytes] of payload) {
    const text = bytes.toString('utf8');
    if (/\bimport\s*\(/.test(text)) fail(`Dynamic adapter imports are outside the fixed payload contract: ${file}`);
    for (const match of text.matchAll(/^\s*(?:import|export)\s+(?:[^'";]*?\s+from\s*)?['"]([^'"]+)['"]/gm)) {
      const specifier = match[1];
      if (['@jackwener/opencli/registry', '@jackwener/opencli/errors'].includes(specifier)) continue;
      if (!specifier.startsWith('./')) fail(`Non-public adapter dependency: ${file}`);
      const dependency = path.posix.join(path.posix.dirname(file), specifier);
      if (!files.has(dependency)) fail(`Missing inventoried helper ${dependency} required by ${file}`);
    }
  }
  const root = path.join(home, '.opencli');
  const clis = path.join(root, 'clis');
  const receiptPath = path.join(root, '.opencli-admin-adapters.json');
  const manifestPath = path.join(root, 'cli-manifest.json');
  const runtimePath = path.join(root, 'package.json');
  const link = path.join(root, 'node_modules/@jackwener/opencli');
  for (const file of [receiptPath, manifestPath, runtimePath]) checkPath(file, 'file');
  checkPath(clis, 'directory');
  checkPath(path.dirname(link), 'directory');
  for (const directory of [clis, ...[...SITES].map(site => path.join(clis, site))]) {
    const localPackage = path.join(directory, 'package.json');
    checkPath(localPackage, 'file');
    if (exists(localPackage) && readJson(localPackage).type !== 'module') fail(`Conflicting adapter ESM boundary: ${localPackage}`);
    const shadow = path.join(directory, 'node_modules/@jackwener/opencli');
    if (exists(shadow)) fail(`User dependency shadows the managed public runtime: ${shadow}`);
  }
  let previous = { files: {}, commands: [], runtime: null };
  if (exists(receiptPath)) {
    previous = readJson(receiptPath);
    if (previous.schemaVersion !== 1 || previous.opencliVersion !== VERSION || !plain(previous.files)
        || !plain(previous.runtime) || previous.runtime.packageHash !== hash(runtimeJson)
        || typeof previous.runtime.packageRoot !== 'string' || !path.isAbsolute(previous.runtime.packageRoot)) fail('Damaged adapter ownership record');
    validateCommands(previous.commands, new Set(Object.keys(previous.files)));
    for (const [file, digest] of Object.entries(previous.files)) {
      safeRelative(file, true);
      if (typeof digest !== 'string' || !/^[a-f0-9]{64}$/.test(digest)) fail('Damaged ownership hash');
      const target = path.join(clis, file);
      checkPath(target, 'file');
      if (!exists(target) || hash(fs.readFileSync(target)) !== digest) fail(`Managed file drift: ${file}`);
    }
  }
  if (exists(runtimePath) && fs.readFileSync(runtimePath, 'utf8') !== runtimeJson) fail('Conflicting user runtime package.json; upstream loader would overwrite it');
  const linkStat = exists(link);
  if (linkStat) {
    if (!linkStat.isSymbolicLink()) fail('Conflicting runtime package path; expected a symlink/junction');
    const target = path.resolve(path.dirname(link), fs.readlinkSync(link));
    if (target !== packageRoot && target !== previous.runtime?.packageRoot) fail('Conflicting unmanaged OpenCLI runtime link');
  }
  const owned = new Set(Object.keys(previous.files));
  for (const file of files) {
    const target = path.join(clis, file);
    checkPath(target, 'file');
    if (exists(target) && !owned.has(file)) fail(`Unmanaged file conflict: ${file}`);
  }
  let merged;
  let unmanagedKeys;
  if (exists(manifestPath)) {
    const manifest = readJson(manifestPath);
    validateCommands(manifest);
    const previousByKey = new Map(previous.commands.map(entry => [`${entry.site}/${entry.name}`, entry]));
    const unmanaged = [];
    const observed = new Set();
    for (const entry of manifest) {
      const key = `${entry.site}/${entry.name}`;
      const old = previousByKey.get(key);
      if (old) {
        if (JSON.stringify(entry) !== JSON.stringify(old)) fail(`Managed manifest drift: ${key}`);
        observed.add(key);
      } else unmanaged.push(entry);
    }
    if (previous.manifestManaged && observed.size !== previousByKey.size) fail('Managed manifest commands are missing');
    unmanagedKeys = validateCommands(unmanaged);
    merged = [...unmanaged, ...inventory.commands];
  } else {
    if (previous.manifestManaged) fail('Managed root manifest was removed');
    unmanagedKeys = scannedKeys(clis, owned);
  }
  for (const key of newKeys) if (unmanagedKeys.has(key)) fail(`Unmanaged command/alias conflict: ${key}`);

  const changes = new Map();
  for (const [file, bytes] of payload) changes.set(path.join(clis, file), bytes);
  for (const file of owned) if (!files.has(file)) changes.set(path.join(clis, file), null);
  if (!exists(runtimePath)) changes.set(runtimePath, Buffer.from(runtimeJson));
  if (merged) changes.set(manifestPath, Buffer.from(json(merged)));
  const receipt = {
    schemaVersion: 1, opencliVersion: VERSION,
    files: Object.fromEntries([...payload].map(([file, bytes]) => [file, hash(bytes)])),
    commands: inventory.commands, manifestManaged: Boolean(merged),
    runtime: { packageHash: hash(runtimeJson), packageRoot },
  };
  changes.set(receiptPath, Buffer.from(json(receipt)));
  for (const file of changes.keys()) writable(file);
  writable(path.dirname(link));

  // Stage all bytes before publishing. Roll back every published path on errors;
  // the receipt is committed last and never reports a partly installed pack.
  const madeDirs = [];
  const mkdir = dir => {
    if (exists(dir)) return;
    mkdir(path.dirname(dir));
    fs.mkdirSync(dir);
    madeDirs.push(dir);
  };
  const backups = new Map();
  const applied = [];
  const oldLink = linkStat ? fs.readlinkSync(link) : null;
  let linkChanged = false;
  let staging;
  try {
    mkdir(root);
    staging = fs.mkdtempSync(path.join(root, '.adapter-install-'));
    let index = 0;
    const staged = new Map();
    for (const [file, bytes] of changes) {
      backups.set(file, exists(file) ? fs.readFileSync(file) : null);
      if (bytes !== null) {
        const stage = path.join(staging, String(index++));
        fs.writeFileSync(stage, bytes, { flag: 'wx', mode: 0o644 });
        staged.set(file, stage);
      }
    }
    if (!linkStat || path.resolve(path.dirname(link), oldLink) !== packageRoot) {
      const stagedLink = path.join(staging, 'runtime-link');
      fs.symlinkSync(packageRoot, stagedLink, process.platform === 'win32' ? 'junction' : 'dir');
      mkdir(path.dirname(link));
      if (linkStat) fs.unlinkSync(link);
      linkChanged = true;
      fs.renameSync(stagedLink, link);
    }
    for (const [file, bytes] of changes) {
      mkdir(path.dirname(file));
      applied.push(file);
      if (bytes === null) fs.unlinkSync(file);
      else fs.renameSync(staged.get(file), file);
    }
  } catch (error) {
    for (const file of applied.reverse()) {
      const bytes = backups.get(file);
      if (bytes === null) { if (exists(file)) fs.unlinkSync(file); }
      else fs.writeFileSync(file, bytes);
    }
    if (linkChanged) {
      if (exists(link)) fs.unlinkSync(link);
      if (oldLink) fs.symlinkSync(oldLink, link, process.platform === 'win32' ? 'junction' : 'dir');
    }
    throw error;
  } finally {
    if (staging) fs.rmSync(staging, { recursive: true, force: true });
    for (const dir of madeDirs.reverse()) {
      try { fs.rmdirSync(dir); } catch (error) { if (!['ENOTEMPTY', 'EEXIST', 'ENOENT'].includes(error.code)) throw error; }
    }
  }
  console.log(`Installed OpenCLI ${VERSION} adapter pack in ${clis} (${files.size} files; ${merged ? 'merged root manifest' : 'native scan retained'})`);
}

try { install(); } catch (error) {
  console.error(`OpenCLI adapter installation failed: ${error.message}`);
  process.exitCode = 1;
}
