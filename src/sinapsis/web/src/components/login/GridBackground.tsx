import { useEffect, useRef } from 'react'

// SINAPSIS_LOGIN_SPHERE — orbe de particulas del login (chunk lazy, Zoey-style violeta).
// SINAPSIS_LOGIN_INTERACTIVE — parallax de mouse (pointermove + lerp) y grafo vivo
// (puntos transitorios que entran/se unen/salen con aristas violeta).
// Los marcadores viven tambien como string literal (data-sphere) para sobrevivir la minificacion.
const SPHERE_MARKER = 'SINAPSIS_LOGIN_SPHERE'
const INTERACTIVE_MARKER = 'SINAPSIS_LOGIN_INTERACTIVE'
// SINAPSIS_LOGIN_JOINLEAVE_V2 — puntos que APARECEN en la superficie (scale 0->1 + fade-in),
// se unen con aristas, y se DISUELVEN en lugar (fade-out + scale-down). Sin trayectoria "meteorito".
const JOINLEAVE_MARKER = 'SINAPSIS_LOGIN_JOINLEAVE_V2'
const SPHERE_TAGS = `${SPHERE_MARKER}:fibonacci:rotation:lighter:${INTERACTIVE_MARKER}:pointermove:lerp:transient:spawn:edges:${JOINLEAVE_MARKER}:appear:scale:dissolve`

// Paleta morada SINAPSIS (nada de naranja, nada de azul).
const VIOLET = '168,85,247' // rgb base (morado --accent)
const LAVENDER = '216,180,254' // chispa mas clara para ~12% de los puntos

const N_POINTS = 2000
const ROT_Y_PER_FRAME = 0.0015 // rad/frame, auto-rotacion lenta (idle) eje Y
const BASE_TILT_X = 0.35 // rad, inclinacion base eje X

// SINAPSIS_LOGIN_INTERACTIVE — parametros del parallax de mouse.
const MAX_YAW = 0.6 // rad, desviacion max de rotacion Y siguiendo el mouse
const MAX_PITCH = 0.45 // rad, desviacion max de tilt X siguiendo el mouse
const MOUSE_LERP = 0.05 // easing por frame hacia el objetivo (mouse) / idle
const IDLE_MS = 1800 // ms sin mover el mouse => vuelve al idle

// SINAPSIS_LOGIN_JOINLEAVE_V2 — pool de puntos transitorios (grafo vivo, sin "meteoritos").
const MAX_TRANSIENT = 10 // <=10 vivos
const SPAWN_PER_SEC = 1.5 // ~1-2 spawns por segundo
const APPEAR_MS = 600 // aparece EN la superficie: scale 0->1 + fade-in (easeOutCubic)
const JOIN_MIN_MS = 1500 // permanece unido ~1.5-2.5s
const JOIN_MAX_MS = 2500
const DISSOLVE_MS = 700 // se disuelve en lugar: fade-out + leve scale-down (easeOutCubic)
const LEAVE_SCALE = 0.7 // scale final al disolverse

// easeOutCubic — arranque rapido, cierre suave.
const easeOutCubic = (x: number) => 1 - Math.pow(1 - x, 3)

interface P3 {
  x: number
  y: number
  z: number
  light: boolean
}

type Phase = 'entering' | 'joined' | 'leaving'

interface Transient {
  // Direccion unitaria objetivo sobre la esfera.
  x: number
  y: number
  z: number
  neighbors: number[] // indices en pts base (2-3 vecinos mas cercanos)
  phase: Phase
  t: number // ms acumulados en la fase actual
  joinDur: number // ms de la fase UNIDO (aleatoria)
  scale: number // 0 -> 1 (aparece) -> LEAVE_SCALE (se disuelve)
  alpha: number // 0..1
}

/**
 * Fondo del login: ESFERA/GLOBO de particulas (canvas 2D, sin three.js).
 * ~2000 puntos distribuidos con secuencia de Fibonacci sobre la superficie de
 * una esfera. Auto-rotacion lenta en idle; sigue el mouse con parallax suave
 * (lerp) y vuelve al idle al salir el puntero. Encima, un grafo vivo de puntos
 * transitorios que APARECEN sobre la superficie (scale 0->1 + fade-in), se UNEN
 * dibujando aristas moradas a sus vecinos, y se DISUELVEN en lugar (fade-out +
 * leve scale-down). Sin trayectorias tipo "meteorito". Debajo, una grilla de piso.
 * Respeta prefers-reduced-motion (render estatico, sin rotacion/mouse/spawns).
 */
export default function GridBackground() {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const cv = canvasRef.current
    if (!cv) return
    const ctx = cv.getContext('2d')
    if (!ctx) return

    // Distribucion FIBONACCI (espiral de angulo aureo) => puntos uniformes en la esfera.
    const golden = Math.PI * (3 - Math.sqrt(5))
    const pts: P3[] = []
    for (let i = 0; i < N_POINTS; i++) {
      const y = 1 - (i / (N_POINTS - 1)) * 2 // y en [-1, 1]
      const r = Math.sqrt(Math.max(0, 1 - y * y))
      const theta = i * golden
      pts.push({
        x: Math.cos(theta) * r,
        y,
        z: Math.sin(theta) * r,
        light: Math.random() < 0.12, // ~12% lavanda
      })
    }

    let raf = 0
    let w = 0
    let h = 0
    let dpr = 1
    let angle = 0 // auto-rotacion base (idle)

    // SINAPSIS_LOGIN_INTERACTIVE — estado del parallax de mouse.
    let targetYaw = 0 // objetivo de desviacion Y (mouse)
    let yaw = 0 // valor actual (lerp)
    let targetTilt = BASE_TILT_X
    let tilt = BASE_TILT_X
    let lastMove = 0 // timestamp del ultimo pointermove

    // SINAPSIS_LOGIN_INTERACTIVE — grafo vivo.
    const transients: Transient[] = []
    let spawnAcc = 0

    const resize = () => {
      dpr = Math.min(window.devicePixelRatio || 1, 2)
      w = cv.clientWidth
      h = cv.clientHeight
      cv.width = Math.floor(w * dpr)
      cv.height = Math.floor(h * dpr)
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    resize()
    window.addEventListener('resize', resize)

    // SINAPSIS_LOGIN_INTERACTIVE — tracking del puntero sobre el area del login.
    const onPointerMove = (e: PointerEvent | MouseEvent) => {
      const nx = (e.clientX / window.innerWidth) * 2 - 1 // [-1, 1]
      const ny = (e.clientY / window.innerHeight) * 2 - 1 // [-1, 1]
      targetYaw = nx * MAX_YAW
      targetTilt = BASE_TILT_X + ny * MAX_PITCH
      lastMove = performance.now()
    }
    const onPointerLeave = () => {
      // Volver suave al idle.
      targetYaw = 0
      targetTilt = BASE_TILT_X
      lastMove = 0
    }

    if (!reduce) {
      window.addEventListener('pointermove', onPointerMove, { passive: true })
      window.addEventListener('mousemove', onPointerMove, { passive: true })
      document.addEventListener('pointerleave', onPointerLeave)
      window.addEventListener('blur', onPointerLeave)
    }

    // Proyecta una direccion unitaria (dir*rf) a pantalla usando yaw+tilt actuales.
    const project = (
      ux: number,
      uy: number,
      uz: number,
      rf: number,
      R: number,
      cx: number,
      cy: number,
      sinA: number,
      cosA: number,
      sinT: number,
      cosT: number,
    ) => {
      const x = ux * rf
      const y = uy * rf
      const z = uz * rf
      const x1 = x * cosA + z * sinA
      const z1 = -x * sinA + z * cosA
      const y2 = y * cosT - z1 * sinT
      const z2 = y * sinT + z1 * cosT
      const d = (z2 / rf + 1) / 2 // profundidad normalizada 0 (atras) .. 1 (frente)
      return { sx: cx + x1 * R, sy: cy + y2 * R, d }
    }

    // Grilla de piso tenue en perspectiva (~30% inferior) que recede.
    const drawFloor = () => {
      const horizon = h * 0.72
      const cx = w / 2
      ctx.globalCompositeOperation = 'source-over'
      ctx.lineWidth = 1
      for (let i = 1; i <= 10; i++) {
        const t = i / 10
        const yy = horizon + (h - horizon) * (t * t)
        const a = 0.05 * (1 - t) + 0.01
        ctx.strokeStyle = `rgba(${VIOLET},${a.toFixed(3)})`
        ctx.beginPath()
        ctx.moveTo(0, yy)
        ctx.lineTo(w, yy)
        ctx.stroke()
      }
      for (let i = -6; i <= 6; i++) {
        const a = 0.04
        ctx.strokeStyle = `rgba(${VIOLET},${a})`
        ctx.beginPath()
        ctx.moveTo(cx + (i * (w * 0.5)) / 6, h)
        ctx.lineTo(cx + i * (w * 0.06), horizon)
        ctx.stroke()
      }
    }

    // SINAPSIS_LOGIN_JOINLEAVE_V2 — crea un punto transitorio con vecinos precalculados.
    const spawnTransient = () => {
      if (transients.length >= MAX_TRANSIENT) return
      // Direccion aleatoria uniforme sobre la esfera.
      const u = Math.random() * 2 - 1
      const phi = Math.random() * Math.PI * 2
      const s = Math.sqrt(Math.max(0, 1 - u * u))
      const dx = s * Math.cos(phi)
      const dy = u
      const dz = s * Math.sin(phi)

      // 2-3 vecinos mas cercanos entre una muestra de puntos base.
      const want = 2 + (Math.random() < 0.5 ? 1 : 0)
      const cand: { idx: number; dd: number }[] = []
      for (let k = 0; k < 240; k++) {
        const idx = (Math.random() * pts.length) | 0
        const p = pts[idx]
        const ex = p.x - dx
        const ey = p.y - dy
        const ez = p.z - dz
        cand.push({ idx, dd: ex * ex + ey * ey + ez * ez })
      }
      cand.sort((a, b) => a.dd - b.dd)
      const neighbors = cand.slice(0, want).map((c) => c.idx)

      transients.push({
        x: dx,
        y: dy,
        z: dz,
        neighbors,
        phase: 'entering',
        t: 0,
        joinDur: JOIN_MIN_MS + Math.random() * (JOIN_MAX_MS - JOIN_MIN_MS),
        scale: 0,
        alpha: 0,
      })
    }

    // Avanza fases del grafo vivo segun dt (ms).
    const stepTransients = (dt: number) => {
      spawnAcc += (dt / 1000) * SPAWN_PER_SEC
      while (spawnAcc >= 1) {
        spawnAcc -= 1
        spawnTransient()
      }
      for (let i = transients.length - 1; i >= 0; i--) {
        const tr = transients[i]
        tr.t += dt
        if (tr.phase === 'entering') {
          // APARECE en la superficie: scale 0->1 + fade-in (easeOutCubic). Sin viaje.
          const k = easeOutCubic(Math.min(1, tr.t / APPEAR_MS))
          tr.scale = k
          tr.alpha = k
          if (tr.t >= APPEAR_MS) {
            tr.phase = 'joined'
            tr.t = 0
            tr.scale = 1
            tr.alpha = 1
          }
        } else if (tr.phase === 'joined') {
          tr.scale = 1
          tr.alpha = 1
          if (tr.t >= tr.joinDur) {
            tr.phase = 'leaving'
            tr.t = 0
          }
        } else {
          // Se DISUELVE en lugar: fade-out + leve scale-down (easeOutCubic).
          const k = easeOutCubic(Math.min(1, tr.t / DISSOLVE_MS))
          tr.scale = 1 - (1 - LEAVE_SCALE) * k
          tr.alpha = 1 - k
          if (tr.t >= DISSOLVE_MS) {
            transients.splice(i, 1)
          }
        }
      }
    }

    // Dibuja el grafo vivo: aristas violeta (glow multi-capa) + nodo transitorio.
    const drawTransients = (
      R: number,
      cx: number,
      cy: number,
      sinA: number,
      cosA: number,
      sinT: number,
      cosT: number,
    ) => {
      ctx.globalCompositeOperation = 'lighter'
      for (let i = 0; i < transients.length; i++) {
        const tr = transients[i]
        if (tr.alpha <= 0.01) continue
        // rf=1 => el nodo vive EN la superficie de la esfera (sin viaje radial).
        const node = project(tr.x, tr.y, tr.z, 1, R, cx, cy, sinA, cosA, sinT, cosT)

        // ARISTAS a los vecinos (efecto 'se une al grafo'). Glow multi-capa violeta.
        for (let n = 0; n < tr.neighbors.length; n++) {
          const p = pts[tr.neighbors[n]]
          const nb = project(p.x, p.y, p.z, 1, R, cx, cy, sinA, cosA, sinT, cosT)
          const ea = tr.alpha * (0.35 + 0.35 * nb.d)
          // Capa ancha tenue.
          ctx.strokeStyle = `rgba(${VIOLET},${(ea * 0.25).toFixed(3)})`
          ctx.lineWidth = 2.4
          ctx.beginPath()
          ctx.moveTo(node.sx, node.sy)
          ctx.lineTo(nb.sx, nb.sy)
          ctx.stroke()
          // Capa fina brillante.
          ctx.strokeStyle = `rgba(${VIOLET},${ea.toFixed(3)})`
          ctx.lineWidth = 0.9
          ctx.beginPath()
          ctx.moveTo(node.sx, node.sy)
          ctx.lineTo(nb.sx, nb.sy)
          ctx.stroke()
        }

        // NODO transitorio (mas brillante que la esfera base); su tamaño sigue el scale.
        const size = Math.max(0.01, (1.6 + 2.2 * node.d) * tr.scale)
        const g = ctx.createRadialGradient(node.sx, node.sy, 0, node.sx, node.sy, size * 3)
        g.addColorStop(0, `rgba(${LAVENDER},${(tr.alpha * 0.95).toFixed(3)})`)
        g.addColorStop(0.4, `rgba(${VIOLET},${(tr.alpha * 0.6).toFixed(3)})`)
        g.addColorStop(1, `rgba(${VIOLET},0)`)
        ctx.fillStyle = g
        ctx.beginPath()
        ctx.arc(node.sx, node.sy, size * 3, 0, Math.PI * 2)
        ctx.fill()
      }
      ctx.globalCompositeOperation = 'source-over'
    }

    const draw = () => {
      const R = Math.min(w, h) * 0.42
      const cx = w / 2
      const cy = h * 0.45
      const rotY = angle + yaw
      const sinA = Math.sin(rotY)
      const cosA = Math.cos(rotY)
      const sinT = Math.sin(tilt)
      const cosT = Math.cos(tilt)

      ctx.clearRect(0, 0, w, h)
      drawFloor()

      // Glow aditivo: los puntos se suman => la silueta queda mas densa.
      ctx.globalCompositeOperation = 'lighter'
      for (let i = 0; i < pts.length; i++) {
        const p = pts[i]
        const x1 = p.x * cosA + p.z * sinA
        const z1 = -p.x * sinA + p.z * cosA
        const y2 = p.y * cosT - z1 * sinT
        const z2 = p.y * sinT + z1 * cosT

        const d = (z2 + 1) / 2 // profundidad 0 (atras) .. 1 (frente)
        const sx = cx + x1 * R
        const sy = cy + y2 * R
        const size = 0.6 + 2.4 * d
        const alpha = 0.12 + 0.9 * d
        const color = p.light ? LAVENDER : VIOLET

        const g = ctx.createRadialGradient(sx, sy, 0, sx, sy, size * 2)
        g.addColorStop(0, `rgba(${color},${alpha.toFixed(3)})`)
        g.addColorStop(1, `rgba(${color},0)`)
        ctx.fillStyle = g
        ctx.beginPath()
        ctx.arc(sx, sy, size * 2, 0, Math.PI * 2)
        ctx.fill()
      }
      ctx.globalCompositeOperation = 'source-over'

      // Grafo vivo encima de la esfera base.
      drawTransients(R, cx, cy, sinA, cosA, sinT, cosT)
    }

    if (reduce) {
      // Render estatico: sin rotacion, sin mouse, sin spawns.
      yaw = 0
      tilt = BASE_TILT_X
      draw()
      return () => {
        window.removeEventListener('resize', resize)
      }
    }

    let prev = performance.now()
    const loop = (now: number) => {
      const dt = Math.min(64, now - prev) // clamp para pestañas en background
      prev = now

      // Idle: si no hay movimiento reciente, relaja hacia el idle.
      if (lastMove && now - lastMove > IDLE_MS) {
        targetYaw = 0
        targetTilt = BASE_TILT_X
      }
      // Parallax con easing (lerp ~0.05/frame).
      yaw += (targetYaw - yaw) * MOUSE_LERP
      tilt += (targetTilt - tilt) * MOUSE_LERP

      // Auto-rotacion lenta siempre presente (idle).
      angle += ROT_Y_PER_FRAME

      stepTransients(dt)
      draw()
      raf = requestAnimationFrame(loop)
    }
    raf = requestAnimationFrame(loop)

    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', resize)
      window.removeEventListener('pointermove', onPointerMove)
      window.removeEventListener('mousemove', onPointerMove)
      document.removeEventListener('pointerleave', onPointerLeave)
      window.removeEventListener('blur', onPointerLeave)
    }
  }, [])

  return (
    <div
      className="login-grid"
      data-sphere={SPHERE_TAGS}
      aria-hidden="true"
    >
      <canvas ref={canvasRef} className="login-grid-canvas" />
      <div className="login-grid-glow login-grid-glow-tl" />
      <div className="login-grid-glow login-grid-glow-br" />
      <div className="login-grid-vignette" />
    </div>
  )
}
