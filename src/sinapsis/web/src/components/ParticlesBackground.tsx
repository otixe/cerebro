import { useEffect, useRef } from 'react'

// SINAPSIS_PARTICLES_BACKGROUND — campo de particulas Canvas 2D portado del
// prototipo aprobado (PARTICLES-REFERENCE.html): deriva continua, conexiones
// por proximidad via grid espacial O(n), halo/atraccion de mouse no
// acumulativa, twinkle aleatorio y auto-degradacion por presupuesto de frame.
//
// variant='login' replica densidad/opacidad/interaccion plena del prototipo.
// variant='ambient' reduce densidad y opacidad para vivir DETRAS del
// contenido de la app (todas las vistas) sin danar legibilidad, y desactiva
// el halo de mouse (puramente atmosferico, sin distraer sobre texto/paneles).
//
// Colores se leen de custom properties (--bg/--accent/--accent-soft/--fg)
// via getComputedStyle + MutationObserver sobre la clase de <html>, asi que
// sigue el tema claro/oscuro (toggle Cmd+Shift+L) sin reiniciar el efecto.
// Respeta prefers-reduced-motion (un solo frame estatico, sin RAF), pausa en
// visibilitychange, recalcula en resize (debounced) y hace cleanup completo
// (listeners + RAF + timers + observer) al desmontar.
//
// Nota tecnica: los closures internos usan `const fn = (...) => {}` (no
// `function fn() {}`) para que TypeScript pueda seguir el narrowing de
// `canvas`/`ctx` tras los early-return; con function declarations hoisted,
// TS no propaga esa narrowing y marca ambos como "possibly null".

type ParticlesVariant = 'login' | 'ambient'

export interface ParticlesStats {
  mode: 'animado' | 'estatico'
  dpr: number
  deviceLabel: 'mobile' | 'desktop'
  count: number
  target: number
  maxDist: number
  frameMs: number
}

interface ParticlesBackgroundProps {
  variant?: ParticlesVariant
  /**
   * Callback opcional de telemetria (throttled a ~4/s en modo animado, una
   * vez por resize/reduced-motion en modo estatico). Se lee via ref interna
   * (onStatsRef) para que un onStats inline nuevo en cada render NO reinicie
   * el efecto/loop de particulas (evita reseed/parpadeo).
   */
  onStats?: (stats: ParticlesStats) => void
}

interface FieldParticle {
  x: number
  y: number
  vx: number
  vy: number
  r: number
  a: number
  depth: number
  tw: number
}

interface ThemeColors {
  bg: string
  line: string
  dot: string
  twinkle: string
}

// Battery Status API no esta en lib.dom (deprecada/parcial: Chromium+Android,
// sin soporte en Firefox/Safari). Se tipa de forma minima y se accede via
// navigator.getBattery?.() con guard de existencia; si no esta, el modo
// ahorro de bateria simplemente nunca se activa (comportamiento normal).
interface BatteryManager {
  level: number
  charging: boolean
  addEventListener(type: 'levelchange' | 'chargingchange', listener: () => void): void
  removeEventListener(type: 'levelchange' | 'chargingchange', listener: () => void): void
}

interface NavigatorWithBattery extends Navigator {
  getBattery?: () => Promise<BatteryManager>
}

const BATTERY_SAVER_LEVEL = 0.2
const BATTERY_SAVER_DENSITY_MULT = 0.5
const BATTERY_SAVER_FRAME_INTERVAL_MS = 100 // ~10fps en modo ahorro

const TWINKLE_IGNITE_P = 0.0009
const TWINKLE_DECAY = 0.9
const RESIZE_DEBOUNCE_MS = 150
const DEGRADE_THROTTLE_MS = 2000
const FLOOR_FACTOR = 0.4
const FRAME_SAMPLE = 60

function clamp(value: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, value))
}

function hexToRgb(hex: string, fallback: string): string {
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex.trim())
  if (!m) return fallback
  return `${parseInt(m[1], 16)},${parseInt(m[2], 16)},${parseInt(m[3], 16)}`
}

function readThemeColors(): ThemeColors {
  const cs = getComputedStyle(document.documentElement)
  const bg = cs.getPropertyValue('--bg').trim() || '#08090a'
  const line = hexToRgb(cs.getPropertyValue('--accent'), '168,85,247')
  const dot = hexToRgb(cs.getPropertyValue('--accent-soft'), '192,132,252')
  const twinkle = hexToRgb(cs.getPropertyValue('--fg'), '247,248,248')
  return { bg, line, dot, twinkle }
}

export default function ParticlesBackground({ variant = 'ambient', onStats }: ParticlesBackgroundProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const onStatsRef = useRef(onStats)
  onStatsRef.current = onStats

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d', { alpha: false })
    if (!ctx) return

    const isLogin = variant === 'login'
    const densityScale = isLogin ? 1 : 0.4
    const dotAlphaScale = isLogin ? 1 : 0.55
    const lineAlphaScale = isLogin ? 1 : 0.5
    const haloEnabled = isLogin

    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    let isMobile = window.matchMedia('(pointer: coarse)').matches || window.innerWidth < 768

    let colors = readThemeColors()

    let W = 0
    let H = 0
    let DPR = 1
    let particles: FieldParticle[] = []
    let grid: number[][] = []
    let gridCols = 1
    let gridRows = 1
    let maxDist = 130
    let cellSize = 130
    let haloRadius = 170
    let haloDisp = 26
    let baseTargetCount = 0
    let batterySaverActive = false
    let batteryManager: BatteryManager | null = null
    let disposed = false

    const cursor = { x: -9999, y: -9999, active: false }
    let raf = 0
    let running = false
    const frameTimes: number[] = []
    let lastFrameAt = 0
    let lastDegradeAt = 0

    const budgetMs = (): number => (isMobile ? 20 : 16.6)

    let lastStatsAt = 0
    const emitStats = (mode: 'animado' | 'estatico', avgMs: number): void => {
      const cb = onStatsRef.current
      if (!cb) return
      const now = performance.now()
      if (mode === 'animado' && now - lastStatsAt < 250) return
      lastStatsAt = now
      cb({
        mode,
        dpr: DPR,
        deviceLabel: isMobile ? 'mobile' : 'desktop',
        count: particles.length,
        target: baseTargetCount,
        maxDist,
        frameMs: avgMs,
      })
    }

    const computeDensity = (w: number, h: number, mobile: boolean): number => {
      const divisor = mobile ? 6000 : 1250
      const raw = (w * h) / divisor
      const capMin = mobile ? 140 : 320
      const capMax = mobile ? 340 : 2000
      const base = clamp(raw, capMin, capMax)
      const saverMult = batterySaverActive ? BATTERY_SAVER_DENSITY_MULT : 1
      return Math.max(20, Math.round(base * densityScale * saverMult))
    }

    const computeDistances = (mobile: boolean): void => {
      maxDist = mobile ? 85 : 130
      cellSize = maxDist
      haloRadius = mobile ? 130 : 170
      haloDisp = mobile ? 18 : 26
    }

    const makeParticle = (): FieldParticle => {
      const depth = Math.random()
      return {
        x: Math.random() * W,
        y: Math.random() * H,
        vx: (Math.random() - 0.5) * (0.18 + depth * 0.18),
        vy: (Math.random() - 0.5) * (0.18 + depth * 0.18),
        r: 0.8 + depth * 1.7,
        a: (0.3 + depth * 0.55) * dotAlphaScale,
        depth,
        tw: 0,
      }
    }

    const seedParticles = (): void => {
      const target = computeDensity(W, H, isMobile)
      baseTargetCount = target
      particles = new Array(target)
      for (let i = 0; i < target; i++) particles[i] = makeParticle()
    }

    // Ajusta la cantidad de particulas al entrar/salir de modo ahorro sin
    // recrear todo el campo (mismo patron que resize, pero incremental: no
    // reposiciona lo que ya existe, solo agrega/recorta para llegar al
    // nuevo target). Evita el parpadeo de un reseed completo.
    const reseedForBatteryChange = (): void => {
      const target = computeDensity(W, H, isMobile)
      baseTargetCount = target
      if (particles.length > target) {
        particles.length = target
      } else if (particles.length < target) {
        const extra = target - particles.length
        for (let i = 0; i < extra; i++) particles.push(makeParticle())
      }
      if (reducedMotion) {
        draw()
        emitStats('estatico', 0)
      }
    }

    const resize = (): void => {
      W = window.innerWidth
      H = window.innerHeight
      DPR = Math.min(window.devicePixelRatio || 1, 2)
      canvas.width = Math.round(W * DPR)
      canvas.height = Math.round(H * DPR)
      canvas.style.width = `${W}px`
      canvas.style.height = `${H}px`
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0)

      computeDistances(isMobile)
      gridCols = Math.max(1, Math.ceil(W / cellSize))
      gridRows = Math.max(1, Math.ceil(H / cellSize))
      seedParticles()
    }

    const cellIndex = (cx: number, cy: number): number | null => {
      if (cx < 0 || cy < 0 || cx >= gridCols || cy >= gridRows) return null
      return cy * gridCols + cx
    }

    const buildGrid = (): void => {
      grid = new Array(gridCols * gridRows)
      for (let i = 0; i < grid.length; i++) grid[i] = []
      for (let p = 0; p < particles.length; p++) {
        const pt = particles[p]
        const cx = clamp(Math.floor(pt.x / cellSize), 0, gridCols - 1)
        const cy = clamp(Math.floor(pt.y / cellSize), 0, gridRows - 1)
        grid[cy * gridCols + cx].push(p)
      }
    }

    const updateCursorFromEvent = (clientX: number, clientY: number): void => {
      cursor.x = clientX
      cursor.y = clientY
      cursor.active = true
    }

    const onMouseMove = (e: MouseEvent): void => {
      updateCursorFromEvent(e.clientX, e.clientY)
    }
    const onMouseLeave = (): void => {
      cursor.active = false
    }
    const onTouchMove = (e: TouchEvent): void => {
      if (!e.touches || !e.touches.length) return
      const t = e.touches[0]
      updateCursorFromEvent(t.clientX, t.clientY)
    }
    const onTouchEnd = (): void => {
      cursor.active = false
    }

    const step = (x: number, y: number, vx: number, vy: number): [number, number] => {
      let nx = x + vx
      let ny = y + vy
      if (nx < -10) nx = W + 10
      else if (nx > W + 10) nx = -10
      if (ny < -10) ny = H + 10
      else if (ny > H + 10) ny = -10
      return [nx, ny]
    }

    const connectPairs = (listA: number[], listB: number[]): void => {
      for (let a = 0; a < listA.length; a++) {
        const i = listA[a]
        for (let b = 0; b < listB.length; b++) {
          const j = listB[b]
          if (j <= i) continue
          const pi = particles[i]
          const pj = particles[j]
          const dx = pi.x - pj.x
          const dy = pi.y - pj.y
          const d2 = dx * dx + dy * dy
          if (d2 >= maxDist * maxDist) continue
          const d = Math.sqrt(d2)
          const t = 1 - d / maxDist
          const alpha = t * t * 0.5 * lineAlphaScale
          ctx.strokeStyle = `rgba(${colors.line},${alpha.toFixed(3)})`
          ctx.beginPath()
          ctx.moveTo(pi.x, pi.y)
          ctx.lineTo(pj.x, pj.y)
          ctx.stroke()
        }
      }
    }

    const draw = (): void => {
      ctx.fillStyle = colors.bg
      ctx.fillRect(0, 0, W, H)

      buildGrid()

      for (let i = 0; i < particles.length; i++) {
        const pt = particles[i]
        const [nx, ny] = step(pt.x, pt.y, pt.vx, pt.vy)
        pt.x = nx
        pt.y = ny

        if (!reducedMotion) {
          if (pt.tw > 0) {
            pt.tw *= TWINKLE_DECAY
            if (pt.tw < 0.02) pt.tw = 0
          } else if (!batterySaverActive && Math.random() < TWINKLE_IGNITE_P) {
            pt.tw = 1
          }
        }
      }

      ctx.lineWidth = 1
      for (let cy = 0; cy < gridRows; cy++) {
        for (let cx = 0; cx < gridCols; cx++) {
          const here = grid[cy * gridCols + cx]
          if (!here || !here.length) continue
          for (let oy = 0; oy <= 1; oy++) {
            for (let ox = oy === 0 ? 1 : -1; ox <= 1; ox++) {
              const ni = cellIndex(cx + ox, cy + oy)
              if (ni === null) continue
              const there = grid[ni]
              if (!there || !there.length) continue
              connectPairs(here, there)
            }
          }
          connectPairs(here, here)
        }
      }

      if (haloEnabled && !batterySaverActive && cursor.active) {
        const r2 = haloRadius * haloRadius
        ctx.lineWidth = 1
        for (let m = 0; m < particles.length; m++) {
          const pm = particles[m]
          const dx = pm.x - cursor.x
          const dy = pm.y - cursor.y
          const d2 = dx * dx + dy * dy
          if (d2 < r2) {
            const d = Math.sqrt(d2) || 0.0001
            const t = 1 - d / haloRadius
            const alpha = t * 0.55
            ctx.strokeStyle = `rgba(${colors.dot},${alpha.toFixed(3)})`
            ctx.beginPath()
            ctx.moveTo(cursor.x, cursor.y)
            ctx.lineTo(pm.x, pm.y)
            ctx.stroke()
          }
        }
      }

      for (let k = 0; k < particles.length; k++) {
        const q = particles[k]
        let rx = q.x
        let ry = q.y
        if (haloEnabled && !batterySaverActive && cursor.active) {
          const ddx = q.x - cursor.x
          const ddy = q.y - cursor.y
          const dd = Math.sqrt(ddx * ddx + ddy * ddy)
          if (dd < haloRadius && dd > 0.001) {
            const pull = (1 - dd / haloRadius) * haloDisp
            rx = q.x - (ddx / dd) * pull
            ry = q.y - (ddy / dd) * pull
          }
        }
        ctx.beginPath()
        ctx.fillStyle = `rgba(${colors.dot},${q.a.toFixed(3)})`
        ctx.arc(rx, ry, q.r, 0, Math.PI * 2)
        ctx.fill()

        if (q.tw > 0) {
          const twAlpha = q.tw * 0.9 * dotAlphaScale
          const twR = q.r * (1 + q.tw * 1.8)
          ctx.beginPath()
          ctx.fillStyle = `rgba(${colors.twinkle},${twAlpha.toFixed(3)})`
          ctx.arc(rx, ry, twR, 0, Math.PI * 2)
          ctx.fill()
        }
      }
    }

    const maybeDegrade = (avgMs: number): void => {
      const now = performance.now()
      if (now - lastDegradeAt < DEGRADE_THROTTLE_MS) return
      const budget = budgetMs()
      if (avgMs > budget * 1.15 && particles.length > baseTargetCount * FLOOR_FACTOR) {
        const newLen = Math.max(
          Math.floor(baseTargetCount * FLOOR_FACTOR),
          Math.floor(particles.length * 0.85),
        )
        particles.length = newLen
        maxDist = Math.max(60, maxDist * 0.92)
        cellSize = maxDist
        gridCols = Math.max(1, Math.ceil(W / cellSize))
        gridRows = Math.max(1, Math.ceil(H / cellSize))
        lastDegradeAt = now
      }
    }

    // --- Ahorro de bateria (Battery Status API) ---------------------------
    // Chromium/Android exponen navigator.getBattery(); Firefox/Safari no la
    // implementan, en cuyo caso getBattery es undefined y este bloque nunca
    // hace nada (comportamiento normal intacto, sin errores).
    const updateBatterySaverState = (): void => {
      if (!batteryManager) return
      const shouldSave = batteryManager.level <= BATTERY_SAVER_LEVEL && !batteryManager.charging
      if (shouldSave === batterySaverActive) return
      batterySaverActive = shouldSave
      reseedForBatteryChange()
    }
    const onBatteryLevelChange = (): void => updateBatterySaverState()
    const onBatteryChargingChange = (): void => updateBatterySaverState()

    const initBatterySaver = (): void => {
      const nav = navigator as NavigatorWithBattery
      if (typeof nav.getBattery !== 'function') return
      nav
        .getBattery()
        .then((battery) => {
          if (disposed) return
          batteryManager = battery
          updateBatterySaverState()
          battery.addEventListener('levelchange', onBatteryLevelChange)
          battery.addEventListener('chargingchange', onBatteryChargingChange)
        })
        .catch(() => {
          // API presente pero rechazada (permisos/entorno) - modo normal.
        })
    }

    const loop = (_ts: number): void => {
      if (!running) return

      // Modo ahorro de bateria: cap de ~10fps sin abandonar requestAnimationFrame
      // (mantiene el frame minimo/casi estatico pedido, sin timers separados).
      if (batterySaverActive) {
        const nowGate = performance.now()
        if (nowGate - lastFrameAt < BATTERY_SAVER_FRAME_INTERVAL_MS) {
          raf = window.requestAnimationFrame(loop)
          return
        }
        lastFrameAt = nowGate
      }

      const t0 = performance.now()
      draw()
      const t1 = performance.now()

      frameTimes.push(t1 - t0)
      if (frameTimes.length > FRAME_SAMPLE) frameTimes.shift()
      const avg = frameTimes.reduce((s, v) => s + v, 0) / frameTimes.length

      maybeDegrade(avg)
      emitStats('animado', avg)
      raf = window.requestAnimationFrame(loop)
    }

    const startLoop = (): void => {
      if (running) return
      running = true
      raf = window.requestAnimationFrame(loop)
    }

    const stopLoop = (): void => {
      running = false
      if (raf) window.cancelAnimationFrame(raf)
      raf = 0
    }

    const onVisibilityChange = (): void => {
      if (reducedMotion) return
      if (document.hidden) stopLoop()
      else startLoop()
    }

    let resizeTimer: ReturnType<typeof setTimeout> | null = null
    const onResize = (): void => {
      if (resizeTimer) clearTimeout(resizeTimer)
      resizeTimer = setTimeout(() => {
        isMobile = window.matchMedia('(pointer: coarse)').matches || window.innerWidth < 768
        stopLoop()
        resize()
        if (reducedMotion) {
          draw()
          emitStats('estatico', 0)
        } else startLoop()
      }, RESIZE_DEBOUNCE_MS)
    }

    const themeObserver = new MutationObserver(() => {
      colors = readThemeColors()
    })
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })

    window.addEventListener('resize', onResize, { passive: true })
    document.addEventListener('visibilitychange', onVisibilityChange)
    if (!reducedMotion && haloEnabled) {
      window.addEventListener('mousemove', onMouseMove, { passive: true })
      window.addEventListener('mouseleave', onMouseLeave, { passive: true })
      window.addEventListener('touchmove', onTouchMove, { passive: true })
      window.addEventListener('touchend', onTouchEnd, { passive: true })
    }

    resize()
    if (reducedMotion) {
      draw()
      emitStats('estatico', 0)
    } else startLoop()
    initBatterySaver()

    return () => {
      disposed = true
      stopLoop()
      if (resizeTimer) clearTimeout(resizeTimer)
      themeObserver.disconnect()
      window.removeEventListener('resize', onResize)
      document.removeEventListener('visibilitychange', onVisibilityChange)
      window.removeEventListener('mousemove', onMouseMove)
      window.removeEventListener('mouseleave', onMouseLeave)
      window.removeEventListener('touchmove', onTouchMove)
      window.removeEventListener('touchend', onTouchEnd)
      if (batteryManager) {
        batteryManager.removeEventListener('levelchange', onBatteryLevelChange)
        batteryManager.removeEventListener('chargingchange', onBatteryChargingChange)
      }
    }
  }, [variant])

  const wrapperClass =
    variant === 'login'
      ? 'pointer-events-none absolute inset-0 z-0'
      : 'pointer-events-none fixed inset-0 z-[-1]'

  return (
    <div className={wrapperClass} aria-hidden="true">
      <canvas ref={canvasRef} className="block h-full w-full" />
    </div>
  )
}
