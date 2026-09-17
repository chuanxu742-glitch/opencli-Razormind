import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { registerHooks, stripTypeScriptTypes } from 'node:module'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const mock = `data:text/javascript,${encodeURIComponent(`
export const useRef = (value) => ({current:value})
export const useBrowserAccountPortal = () => globalThis.__portalState
export const Wifi = 'wifi'; export const X = 'close'; export const Badge = 'badge';
export const Button = 'button'; export const Input = 'input';
export const jsx = (type, props) => ({ type, props }); export const jsxs = jsx;
`)}`
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'react' || specifier === 'react/jsx-runtime' || specifier === 'lucide-react' || specifier.startsWith('@/components/ui/') || specifier === '@/hooks/use-browser-account-portal') return { url: mock, shortCircuit: true }
    if (specifier === '@/lib/browser-accounts/portal-protocol') return { url: new URL('../lib/browser-accounts/portal-protocol.ts', import.meta.url).href, shortCircuit: true }
    return nextResolve(specifier, context)
  },
  load(url, context, nextLoad) {
    if (url.endsWith('.tsx')) return { format: 'module', shortCircuit: true, source: ts.transpileModule(readFileSync(fileURLToPath(url), 'utf8'), { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.ESNext } }).outputText }
    if (url.endsWith('.ts')) return { format: 'module', shortCircuit: true, source: stripTypeScriptTypes(readFileSync(fileURLToPath(url), 'utf8')) }
    return nextLoad(url, context)
  },
})
const { BrowserAccountPortal } = await import('../components/browsers/browser-account-portal.tsx')

function elements(node) {
  if (!node || typeof node !== 'object') return []
  if (Array.isArray(node)) return node.flatMap(elements)
  return [node, ...elements(node.props?.children)]
}

function render(frameKind) {
  const controls = []
  globalThis.__portalState = {
    transport: 'connected', message: '', frameUrl: 'blob:test', frameKind,
    frameClip: { x: 100, y: 200, width: 1000, height: 500 },
    input: '', focusedFieldRef: null, setInput: () => {}, sendInput: () => {},
    requestTakeover: () => controls.push(['takeover']),
    sendPointer: (...args) => controls.push(['pointer', ...args]),
    sendKey: (...args) => controls.push(['key', ...args]),
  }
  const tree = BrowserAccountPortal({ workspaceId: 'w', accountId: 'a', session: {}, onClose: () => {} })
  return { controls, nodes: elements(tree) }
}

test('QR image has no pointer or keyboard control and offers explicit takeover', () => {
  const { controls, nodes } = render('qr')
  const image = nodes.find((node) => node.type === 'img')
  assert.equal(image.props.onPointerDown, undefined)
  assert.equal(image.props.onPointerMove, undefined)
  assert.equal(image.props.onKeyDown, undefined)
  assert.equal(image.props.tabIndex, undefined)
  nodes.find((node) => node.type === 'button' && node.props.children === '需要其他验证').props.onClick()
  assert.deepEqual(controls, [['takeover']])
})

test('approved images wire dragging and control keys through the actual displayed image bounds', () => {
  for (const kind of ['form', 'approved']) {
    const { controls, nodes } = render(kind)
    const image = nodes.find((node) => node.type === 'img')
    let captured = false
    let blurred = false
    const target = {
      focus: () => {}, blur: () => { blurred = true },
      setPointerCapture: () => { captured = true },
      hasPointerCapture: () => captured,
      releasePointerCapture: () => { captured = false },
      getBoundingClientRect: () => ({ left: 10, top: 20, width: 250, height: 125 }),
    }
    const event = { currentTarget: target, pointerId: 1, isPrimary: true, button: 0, clientX: 35, clientY: 45, preventDefault: () => {} }
    image.props.onPointerMove(event)
    assert.deepEqual(controls, [], 'hover is not a drag')
    image.props.onPointerDown(event)
    assert.equal(captured, true)
    image.props.onPointerMove({ ...event, clientX: 135 })
    image.props.onPointerUp({ ...event, clientX: 235 })
    assert.equal(captured, false)
    assert.deepEqual(controls, [['pointer', 'down', 200, 300], ['pointer', 'move', 600, 300], ['pointer', 'up', 1000, 300]])
    for (const key of ['Tab', 'Enter', 'Backspace', 'Escape']) image.props.onKeyDown({ ...event, key })
    assert.equal(blurred, true)
    assert.deepEqual(controls.slice(3), [['key', 'Tab'], ['key', 'Enter'], ['key', 'Backspace'], ['key', 'Escape']])
    image.props.onKeyDown({ ...event, key: '1' })
    image.props.onKeyDown({ ...event, key: 'Tab', shiftKey: true })
    assert.equal(controls.length, 7, 'text stays in the sensitive field, Shift+Tab can leave the projection')
    image.props.onPointerDown(event)
    image.props.onPointerCancel({ ...event, clientX: 600 })
    assert.deepEqual(controls.at(-1), ['pointer', 'up', 1099, 300])
  }
})
