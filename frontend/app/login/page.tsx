'use client'

import { Droplets, Grid3X3, LoaderCircle, SquareTerminal } from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'
import { useRouter, useSearchParams } from 'next/navigation'
import { Suspense, useEffect, useState } from 'react'
import { toast } from 'sonner'

import FaultyTerminal from '@/components/FaultyTerminal'
import Dither from '@/components/Dither'
import { useAuth } from '@/components/auth/auth-provider'
import { PixelLiquidBg } from '@/components/unlumen-ui/pixel-liquid-bg'
import { Button } from '@/components/ui/button'
import { GpuSurface } from '@/components/gpu/gpu-surface'
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Field, FieldDescription, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import RevealText from '@/components/ui/smoothui/reveal-text'
import { sanitizeReturnTo } from '@/lib/auth/oidc'

type LoginBackdrop = 'liquid' | 'terminal' | 'pixel'

const BACKDROPS: Array<{ id: LoginBackdrop; label: string; icon: typeof Droplets }> = [
  { id: 'liquid', label: '流体', icon: Droplets },
  { id: 'terminal', label: '终端', icon: SquareTerminal },
  { id: 'pixel', label: '像素', icon: Grid3X3 },
]
const HEADLINE_WORDS = ['系统', '工作流', '数据产品']
const LIQUID_DARK_PALETTE = [
  '#050302',
  '#1a0805',
  '#351008',
  '#5b160c',
  '#8f2112',
  '#bd2d17',
  '#f0441f',
  '#ff6324',
  '#ff8a2b',
  '#ffad42',
  '#ffd36a',
  '#ffe596',
  '#fff0b0',
]
const LIQUID_LIGHT_PALETTE = [
  '#fff7d6',
  '#fff0b0',
  '#ffe596',
  '#ffd36a',
  '#ffad42',
  '#ff8a2b',
  '#ff6324',
  '#f0441f',
  '#bd2d17',
  '#8f2112',
]

function StaticLoginBackground({ theme }: { theme: LoginBackdrop }) {
  const themeClassName = theme === 'liquid'
    ? 'bg-[radial-gradient(circle_at_20%_20%,#ff8a2b_0%,transparent_34%),radial-gradient(circle_at_80%_75%,#bd2d17_0%,transparent_38%),#070604]'
    : theme === 'terminal'
      ? 'bg-[linear-gradient(135deg,#0d0805_0%,#2a1008_48%,#070604_100%)]'
      : 'bg-[radial-gradient(circle_at_30%_20%,#f0441f_0%,transparent_25%),linear-gradient(145deg,#130604_0%,#020202_72%)]'

  return (
    <div
      aria-hidden="true"
      className={`relative size-full overflow-hidden ${themeClassName}`}
      data-login-static-background
      data-login-theme={theme}
    >
      {theme === 'terminal' ? (
        <div className="absolute inset-0 bg-[linear-gradient(rgba(249,115,22,0.13)_1px,transparent_1px),linear-gradient(90deg,rgba(249,115,22,0.13)_1px,transparent_1px)] bg-[size:28px_28px]" />
      ) : null}
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_center,transparent_0%,rgba(0,0,0,0.42)_100%)]" />
    </div>
  )
}

function LoginPendingShell() {
  return (
    <main className="relative min-h-screen overflow-hidden bg-[#070604] text-white">
      <div className="absolute inset-0 opacity-90" aria-hidden="true">
        <div
          className="absolute inset-0"
          data-gpu-surface="login-background"
          data-gpu-backend="pending"
          data-gpu-fallback-reason="probing"
        >
          <StaticLoginBackground theme="liquid" />
        </div>
      </div>
    </main>
  )
}

function LoginBackground({ theme, reduceMotion }: { theme: LoginBackdrop; reduceMotion: boolean }) {
  return (
    <GpuSurface
      surface="login-background"
      className="absolute inset-0"
      fallback={<StaticLoginBackground theme={theme} />}
    >
      <AnimatePresence mode="wait" initial={false}>
        <motion.div
          key={theme}
          className="absolute inset-0"
          initial={reduceMotion ? false : { opacity: 0, filter: 'blur(6px)', scale: 1.01 }}
          animate={{ opacity: 1, filter: 'blur(0px)', scale: 1 }}
          exit={reduceMotion ? { opacity: 0 } : { opacity: 0, filter: 'blur(3px)', scale: 0.995 }}
          transition={{ type: 'spring', duration: 0.45, bounce: 0 }}
        >
          {theme === 'liquid' ? (
            <div className="relative size-full overflow-hidden bg-[#070604]">
              <PixelLiquidBg
                className="absolute inset-0"
                darkPalette={LIQUID_DARK_PALETTE}
                lightPalette={LIQUID_LIGHT_PALETTE}
                pixelSize={11}
                resolution={0.34}
                mouseForce={5.5}
                cursorSize={150}
                viscosity={14}
                surfaceStrength={0.76}
                autoDemo={!reduceMotion}
              />
            </div>
          ) : theme === 'terminal' ? (
            <FaultyTerminal
              tint="#F97316"
              scale={1.35}
              gridMul={[2, 1]}
              digitSize={1.2}
              timeScale={0.22}
              pause={reduceMotion}
              scanlineIntensity={0.35}
              glitchAmount={0.65}
              flickerAmount={0.25}
              noiseAmp={0.85}
              chromaticAberration={0.5}
              curvature={0.08}
              mouseReact={!reduceMotion}
              mouseStrength={0.12}
              dpr={1}
              pageLoadAnimation={!reduceMotion}
              brightness={0.9}
            />
          ) : (
            <div className="relative size-full overflow-hidden bg-black">
              <div className="absolute inset-0 opacity-85">
                <Dither
                  waveSpeed={0.1}
                  waveFrequency={2.6}
                  waveAmplitude={0.38}
                  waveColor={[1, 0.27, 0.08]}
                  colorNum={7}
                  pixelSize={3}
                  disableAnimation={reduceMotion}
                  enableMouseInteraction={!reduceMotion}
                  mouseRadius={0.72}
                />
              </div>
            </div>
          )}
        </motion.div>
      </AnimatePresence>
    </GpuSurface>
  )
}

function LoginForm() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const { status, developmentLoginEnabled, signInWithPassword, enterDevelopmentMode } = useAuth()
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [rememberLogin, setRememberLogin] = useState(false)
  const [submitting, setSubmitting] = useState<'password' | 'development' | null>(null)
  const [reduceMotion, setReduceMotion] = useState(true)
  const [backdrop, setBackdrop] = useState<LoginBackdrop>('liquid')
  const [headlineWord, setHeadlineWord] = useState(0)
  const returnTo = sanitizeReturnTo(searchParams.get('returnTo'))

  useEffect(() => {
    const mediaQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
    const syncPreference = () => setReduceMotion(mediaQuery.matches)
    syncPreference()
    mediaQuery.addEventListener('change', syncPreference)
    return () => mediaQuery.removeEventListener('change', syncPreference)
  }, [])

  useEffect(() => {
    if (status === 'authenticated' && submitting !== 'development') router.replace(returnTo)
  }, [returnTo, router, status, submitting])

  useEffect(() => {
    router.prefetch(returnTo)
  }, [returnTo, router])

  useEffect(() => {
    if (reduceMotion) return
    const interval = window.setInterval(
      () => setHeadlineWord((current) => (current + 1) % HEADLINE_WORDS.length),
      2800,
    )
    return () => window.clearInterval(interval)
  }, [reduceMotion])

  async function handlePasswordLogin(event: React.FormEvent) {
    event.preventDefault()
    setSubmitting('password')
    try {
      const usingDefaultPassword = await signInWithPassword(username, password, rememberLogin)
      toast.success(
        usingDefaultPassword
          ? '登录成功。请在账户设置中修改安装器生成的初始密码。'
          : '登录成功',
      )
      router.replace(returnTo)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '用户名或密码错误')
      setSubmitting(null)
    }
  }

  function handleDevelopmentLogin() {
    setSubmitting('development')
    try {
      enterDevelopmentMode()
      window.setTimeout(() => router.replace(returnTo), reduceMotion ? 0 : 285)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '无法进入本地开发模式')
      setSubmitting(null)
    }
  }

  return (
    <motion.main
      className="relative min-h-screen overflow-hidden bg-[#070604] text-white"
      animate={
        submitting === 'development' && !reduceMotion ? { x: '-100%' } : { x: 0 }
      }
      transition={{ duration: 0.28, ease: [0.32, 0.72, 0, 1] }}
    >
      <div className="absolute inset-0 opacity-90" aria-hidden="true">
        <LoginBackground theme={backdrop} reduceMotion={reduceMotion} />
      </div>
      <div
        className="absolute inset-0 bg-[linear-gradient(90deg,rgba(7,6,4,0.08),rgba(7,6,4,0.62)_52%,rgba(7,6,4,0.94)),linear-gradient(0deg,rgba(7,6,4,0.7),transparent_45%)]"
        aria-hidden="true"
      />

      <div className="absolute right-4 top-4 z-20 flex rounded-full border border-white/12 bg-black/45 p-1 backdrop-blur-xl sm:right-6 sm:top-6" aria-label="登录背景主题">
        {BACKDROPS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            aria-label={`${label}背景`}
            aria-pressed={backdrop === id}
            onClick={() => setBackdrop(id)}
            className="flex h-8 items-center gap-1.5 rounded-full px-2.5 text-xs text-white/55 transition-[color,background-color,transform] duration-200 [transition-timing-function:var(--motion-ease-settle)] hover:text-white active:scale-[0.94] aria-pressed:bg-white aria-pressed:text-black"
          >
            <Icon className="size-3.5" aria-hidden />
            <span className="hidden sm:inline">{label}</span>
          </button>
        ))}
      </div>

      <div className="relative mx-auto grid min-h-screen w-full max-w-7xl items-center gap-12 px-4 py-10 sm:px-8 lg:grid-cols-[minmax(0,1fr)_26rem] lg:px-12">
        <section className="hidden max-w-2xl lg:block" aria-label="产品介绍">
          <div className="mb-10 flex items-start gap-5 font-mono text-orange-200">
            <motion.span
              className="grid size-14 shrink-0 place-items-center rounded-lg border border-orange-300/45 bg-orange-500/15 text-base font-bold tracking-[-0.06em] text-orange-100 shadow-[0_0_30px_rgba(255,99,36,0.14)]"
              initial={reduceMotion ? false : { opacity: 0, scale: 0.9, y: 8, filter: 'blur(5px)' }}
              animate={{ opacity: 1, scale: 1, y: 0, filter: 'blur(0px)' }}
              transition={{ type: 'spring', duration: 0.5, bounce: 0.08 }}
            >
              OC
            </motion.span>
            <div className="min-w-0 pt-0.5">
              <div className="flex overflow-hidden text-[clamp(2.8rem,4.5vw,4.4rem)] font-bold leading-[0.86] tracking-[-0.075em] text-white/95">
                {Array.from('OPENCLI').map((character, index) => (
                  <RevealText key={`${character}-${index}`} delay={80 + index * 45} direction="up">
                    {character}
                  </RevealText>
                ))}
                <span className="sr-only">OPENCLI</span>
              </div>
              <motion.div
                className="mt-3 flex items-center gap-3 pl-[clamp(1rem,4vw,4rem)] text-[11px] tracking-[0.34em] text-orange-200/75"
                initial={reduceMotion ? false : { opacity: 0, x: -18, filter: 'blur(4px)' }}
                animate={{ opacity: 1, x: 0, filter: 'blur(0px)' }}
                transition={{ type: 'spring', duration: 0.55, bounce: 0, delay: 0.42 }}
              >
                <span className="h-px w-10 bg-orange-300/55" aria-hidden="true" />
                CONTROL / PLANE
                <span className="text-orange-200">_</span>
              </motion.div>
            </div>
          </div>
          <p className="mb-4 font-mono text-xs tracking-[0.22em] text-orange-300/80">
            COLLECT · ORCHESTRATE · OPERATE
          </p>
          <h1 className="max-w-lg text-balance text-[clamp(2.25rem,3.5vw,3.5rem)] font-medium leading-[1.08] tracking-[-0.045em]">
            <RevealText delay={180} direction="up" className="block">
              把分散的数据能力，
            </RevealText>
            <span className="block">
              编排成持续运行的
              <span className="relative ml-[0.08em] inline-grid overflow-hidden align-bottom">
                <span className="invisible col-start-1 row-start-1">数据产品</span>
                <AnimatePresence mode="wait" initial={false}>
                  <motion.span
                    key={HEADLINE_WORDS[headlineWord]}
                    className="col-start-1 row-start-1 bg-[length:300%_100%] bg-clip-text text-transparent"
                    style={{
                      backgroundImage:
                        'linear-gradient(90deg,#ff8a48 0%,#ffb15c 28%,#f5a6d8 52%,#c9a7ff 72%,#ffd36a 100%)',
                    }}
                    initial={reduceMotion ? false : { opacity: 0, y: '48%', backgroundPosition: '100% 50%' }}
                    animate={{ opacity: 1, y: 0, backgroundPosition: '35% 50%' }}
                    exit={reduceMotion ? { opacity: 0 } : { opacity: 0, y: '-45%' }}
                    transition={{ duration: 0.62, ease: [0.22, 1, 0.36, 1] }}
                  >
                    {HEADLINE_WORDS[headlineWord]}
                  </motion.span>
                </AnimatePresence>
              </span>
            </span>
          </h1>
          <p className="mt-6 max-w-lg text-pretty text-base leading-7 text-white/60">
            从采集节点、自动化执行到交付与消费，在同一个运营控制台里观察、修复和扩展。
          </p>
        </section>

        <div className="mx-auto flex w-full max-w-md flex-col gap-6 lg:mx-0">
          <div className="flex items-center gap-3 lg:hidden">
            <span className="grid size-14 place-items-center rounded-lg border border-orange-400/40 bg-orange-500/15 font-mono text-base font-black tracking-[-0.06em] text-orange-100">
              OC
            </span>
            <div>
              <h1 className="text-xl font-black tracking-[-0.055em]">OPENCLI</h1>
              <p className="text-sm text-white/55">采集编排与运营控制台</p>
            </div>
          </div>

          <Card className="border-white/12 bg-background/92 shadow-2xl shadow-black/40 backdrop-blur-xl">
            <CardHeader>
              <CardTitle>登录控制台</CardTitle>
              <CardDescription>
                本地部署直接登录。首次使用请输入安装完成时显示的随机初始密码。
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form id="local-login" className="space-y-4" onSubmit={handlePasswordLogin}>
                <FieldGroup>
                  <Field>
                    <FieldLabel htmlFor="local-username">用户名</FieldLabel>
                    <Input
                      id="local-username"
                      autoComplete="username"
                      value={username}
                      onChange={(event) => setUsername(event.target.value)}
                      required
                    />
                  </Field>
                  <Field>
                    <FieldLabel htmlFor="local-password">密码</FieldLabel>
                    <Input
                      id="local-password"
                      type="password"
                      autoComplete="current-password"
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                      required
                    />
                    <FieldDescription>登录后可以在账户设置中修改密码。</FieldDescription>
                  </Field>
                </FieldGroup>
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={rememberLogin} onChange={(event) => setRememberLogin(event.target.checked)} disabled={submitting !== null} className="size-4 accent-primary" />
                  记住登录 30 天
                </label>
              </form>
            </CardContent>
            <CardFooter className="flex-col gap-2">
              <Button
                type="submit"
                form="local-login"
                className={`w-full overflow-hidden transition-[height,border-radius,background-color] duration-300 ${submitting === 'password' ? 'h-16 rounded-2xl' : 'h-10'}`}
                data-triggered={submitting === 'password'}
                disabled={submitting !== null}
              >
                {submitting === 'password' ? (
                  <span className="flex items-center gap-3 text-left">
                    <LoaderCircle className="size-5 animate-spin" />
                    <span className="grid">
                      <span>正在登录</span>
                      <span className="text-xs font-normal opacity-65">建立本地会话</span>
                    </span>
                  </span>
                ) : (
                  '登录'
                )}
              </Button>
              {developmentLoginEnabled ? (
                <Button
                  type="button"
                  variant="ghost"
                  className={`w-full overflow-hidden text-muted-foreground transition-[height,border-radius,background-color] duration-300 ${submitting === 'development' ? 'h-16 rounded-2xl bg-muted' : 'h-10'}`}
                  data-triggered={submitting === 'development'}
                  disabled={submitting !== null}
                  onClick={handleDevelopmentLogin}
                >
                  {submitting === 'development' ? (
                    <span className="flex items-center gap-3 text-left text-foreground">
                      <LoaderCircle className="size-5 animate-spin" />
                      <span className="grid">
                        <span>正在进入本地开发模式</span>
                        <span className="text-xs font-normal text-muted-foreground">正在建立开发会话</span>
                      </span>
                    </span>
                  ) : (
                    '进入本地开发模式'
                  )}
                </Button>
              ) : null}
            </CardFooter>
          </Card>
          <p className="text-center font-mono text-[11px] tracking-wide text-white/35">
            LOCAL-FIRST · AUDITABLE · NODE-NATIVE
          </p>
        </div>
      </div>
    </motion.main>
  )
}

export default function LoginPage() {
  return (
    <Suspense fallback={<LoginPendingShell />}>
      <LoginForm />
    </Suspense>
  )
}
