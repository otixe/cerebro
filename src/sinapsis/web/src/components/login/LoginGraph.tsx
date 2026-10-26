import { useEffect, useRef, type CSSProperties } from 'react'

// SINAPSIS_LOGIN_GRAPH — grafo de nodos + aristas del login (chunk lazy, Zoey-style violeta).
// El marcador vive tambien como string literal (data-graph) para sobrevivir la minificacion.
const GRAPH_MARKER = 'SINAPSIS_LOGIN_GRAPH'
const GLOW = '168, 85, 247'

// Filas que se alejan en profundidad sobre el plano 3D.
// gy mayor = fila cercana (abajo); gy menor = recede (arriba). gx repartido ~30-70%.
const ROWS: { gy: number; xs: number[] }[] = [
  { gy: 84, xs: [30, 40, 50, 60, 70] }, // fila cercana (5 nodos)
  { gy: 75, xs: [34, 45, 56, 67] },     // 4
  { gy: 68, xs: [31, 43, 55, 67] },     // 4
  { gy: 60, xs: [35, 46, 57, 66] },     // fila lejana (4)
]

interface GNode {
  gx: number
  gy: number
  row: number
}

// Nodos aplanados desde las filas.
const NODES: GNode[] = ROWS.flatMap((r, ri) => r.xs.map((gx) => ({ gx, gy: r.gy, row: ri })))

// Indices agrupados por fila.
const ROW_INDICES: number[][] = ROWS.map(() => [])
NODES.forEach((n, i) => ROW_INDICES[n.row].push(i))

// Aristas: cada nodo se conecta con 1-2 vecinos mas cercanos (por gx) de la fila siguiente.
const EDGES: [number, number][] = []
for (let r = 0; r < ROW_INDICES.length - 1; r++) {
  for (const i of ROW_INDICES[r]) {
    const next = [...ROW_INDICES[r + 1]].sort(
      (a, b) => Math.abs(NODES[a].gx - NODES[i].gx) - Math.abs(NODES[b].gx - NODES[i].gx),
    )
    const k = 1 + (i % 2) // alterna 1 o 2 vecinos -> malla que recede
    for (let j = 0; j < Math.min(k, next.length); j++) EDGES.push([i, next[j]])
  }
}

interface Pt {
  x: number
  y: number
}

/**
 * Grafo del login: nodos abstractos violeta que respiran sobre el plano 3D y
 * un canvas que lee sus posiciones (getBoundingClientRect) y dibuja haces de
 * luz multi-capa conectandolos. Respeta prefers-reduced-motion.
 */
export default function LoginGraph() {
  const wrapRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const nodeRefs = useRef<(HTMLDivElement | null)[]>([])

  useEffect(() => {
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const cv = canvasRef.current
    const wrap = wrapRef.current
    if (!cv || !wrap) return
    const ctx = cv.getContext('2d')
    if (!ctx) return

    let raf = 0
    let w = 0
    let h = 0
    let centers: Pt[] = []

    // Lee el centro de cada nodo relativo al wrapper (refleja la proyeccion 3D real).
    const measure = () => {
      const wr = wrap.getBoundingClientRect()
      centers = nodeRefs.current.map((el) => {
        if (!el) return { x: 0, y: 0 }
        const r = el.getBoundingClientRect()
        return { x: r.left + r.width / 2 - wr.left, y: r.top + r.height / 2 - wr.top }
      })
    }

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      w = wrap.clientWidth
      h = wrap.clientHeight
      cv.width = Math.floor(w * dpr)
      cv.height = Math.floor(h * dpr)
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      measure()
    }

    const line = (a: Pt, b: Pt, width: number, alpha: number) => {
      ctx.strokeStyle = `rgba(${GLOW},${alpha})`
      ctx.lineWidth = width
      ctx.beginPath()
      ctx.moveTo(a.x, a.y)
      ctx.lineTo(b.x, b.y)
      ctx.stroke()
    }

    // Un haz multi-capa: glow ancho + linea media + core brillante (estilo Zoey).
    const beam = (a: Pt, b: Pt, depth: number) => {
      // depth 0 (lejos) -> mas tenue; 1 (cerca) -> mas brillante.
      const k = 0.45 + 0.55 * depth
      ctx.lineCap = 'round'
      line(a, b, 4, 0.06 * k) // (a) glow ancho
      line(a, b, 1.5, 0.2 * k) // (b) linea media
      line(a, b, 0.5, 0.5 * k) // (c) core brillante
    }

    // Shimmer: un punto de luz viaja por la arista.
    const shimmer = (a: Pt, b: Pt, t: number, depth: number) => {
      const x = a.x + (b.x - a.x) * t
      const y = a.y + (b.y - a.y) * t
      const rad = 5
      const g = ctx.createRadialGradient(x, y, 0, x, y, rad)
      g.addColorStop(0, `rgba(${GLOW},${0.85 * (0.5 + 0.5 * depth)})`)
      g.addColorStop(1, `rgba(${GLOW},0)`)
      ctx.fillStyle = g
      ctx.beginPath()
      ctx.arc(x, y, rad, 0, Math.PI * 2)
      ctx.fill()
    }

    const paint = (now: number) => {
      ctx.clearRect(0, 0, w, h)
      for (let e = 0; e < EDGES.length; e++) {
        const [ia, ib] = EDGES[e]
        const a = centers[ia]
        const b = centers[ib]
        if (!a || !b) continue
        const depth = Math.max(0, Math.min(1, (a.y + b.y) / 2 / (h || 1)))
        beam(a, b, depth)
        if (!reduce) {
          const t = (now / 1600 + e * 0.13) % 1
          shimmer(a, b, t, depth)
        }
      }
      if (!reduce) raf = requestAnimationFrame(paint)
    }

    resize()
    window.addEventListener('resize', resize)
    // Un par de re-medidas tras el layout/entrada para anclar bien las posiciones.
    const t1 = window.setTimeout(measure, 60)
    const t2 = window.setTimeout(measure, 400)
    raf = requestAnimationFrame(paint)

    return () => {
      cancelAnimationFrame(raf)
      window.clearTimeout(t1)
      window.clearTimeout(t2)
      window.removeEventListener('resize', resize)
    }
  }, [])

  return (
    <div ref={wrapRef} className="login-graph" data-graph={GRAPH_MARKER} aria-hidden="true">
      <canvas ref={canvasRef} className="login-graph-canvas" />
      <div className="login-graph-plane">
        {NODES.map((n, i) => (
          <div
            key={i}
            ref={(el) => {
              nodeRefs.current[i] = el
            }}
            className="login-node"
            style={{ '--gx': `${n.gx}%`, '--gy': `${n.gy}%`, '--d': `${(i % 6) * 0.32}s` } as CSSProperties}
          >
            <span className="login-node-dot" />
          </div>
        ))}
      </div>
    </div>
  )
}
