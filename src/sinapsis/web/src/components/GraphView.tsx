import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import ForceGraph2D, {
  type ForceGraphMethods,
  type LinkObject,
  type NodeObject,
} from 'react-force-graph-2d'
import { Network, RefreshCw } from 'lucide-react'
import { fetchGraph, refreshGraph, type GraphNode } from '../lib/api'
import { useToast } from './Toast'

/** Paleta por tipo de nodo (tokens del diseño Linear-first). */
const TYPE_COLORS: Record<GraphNode['type'], string> = {
  note: '#7170ff',
  task: '#f2c94c',
  project: '#27a644',
  agent: '#56ccf2',
  memory: '#8a8f98',
}
const DEFAULT_COLOR = '#8a8f98'

const TYPE_LABEL: Record<GraphNode['type'], string> = {
  note: 'Notas',
  task: 'Tareas',
  project: 'Proyectos',
  agent: 'Agentes',
  memory: 'Memoria',
}

type FgNode = NodeObject<GraphNode>

/**
 * Vista de grafo global del Cerebro. Renderiza nodos/aristas de /api/graph
 * en WebGL-canvas (react-force-graph-2d), coloreando por `type`, escalando
 * por `val`, y navegando a la nota al hacer click en un nodo con `path`.
 */
export default function GraphView({ onOpenNote }: { onOpenNote: (path: string) => void }) {
  const qc = useQueryClient()
  const toast = useToast()
  const { data, isLoading, isError } = useQuery({ queryKey: ['graph'], queryFn: fetchGraph })

  const fgRef = useRef<ForceGraphMethods<FgNode, LinkObject<GraphNode>> | undefined>(undefined)
  const wrapRef = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ width: 0, height: 0 })

  // Medir el contenedor: el grafo ocupa todo el área de contenido (full-width móvil).
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect
      if (r) setSize({ width: Math.floor(r.width), height: Math.floor(r.height) })
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // Copia superficial de nodes/links: force-graph MUTA los objetos (agrega x/y/vx…).
  // No debemos alterar la data cacheada por TanStack Query.
  const graphData = useMemo(() => {
    if (!data) return { nodes: [] as GraphNode[], links: [] as { source: string; target: string }[] }
    return {
      nodes: data.nodes.map((n) => ({ ...n })),
      links: data.links.map((l) => ({ ...l })),
    }
  }, [data])

  const refresh = useMutation({
    mutationFn: refreshGraph,
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ['graph'] })
      toast(`Grafo reconstruido: ${res.nodes} nodos, ${res.links} enlaces`)
    },
    onError: () => toast('No se pudo reconstruir el grafo', 'err'),
  })

  const isEmpty = !isLoading && !isError && graphData.nodes.length === 0
  const showGraph = !isLoading && !isError && !isEmpty && size.width > 0

  return (
    <div ref={wrapRef} className="relative h-full w-full overflow-hidden glass-strong">
      {/* Control flotante: reconstruir grafo (táctil ≥44px). */}
      <div className="absolute right-3 top-3 z-10 flex items-center gap-2 pr-[env(safe-area-inset-right)]">
        <button
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
          title="Reconstruir grafo"
          className="glass-strong flex h-11 min-h-[44px] items-center gap-2 rounded-md border border-[var(--border-strong)] px-3 text-[13px] text-fg-muted transition hover:bg-surface-hover disabled:opacity-50"
        >
          <RefreshCw size={15} className={refresh.isPending ? 'animate-spin' : ''} />
          <span className="hidden sm:inline">Refrescar</span>
        </button>
      </div>

      {/* Leyenda por tipo. */}
      {showGraph && (
        <div className="glass-strong pointer-events-none absolute bottom-3 left-3 z-10 flex flex-wrap gap-x-3 gap-y-1 rounded-md border border-[var(--border-subtle)] px-3 py-2 pb-[env(safe-area-inset-bottom)] pl-[env(safe-area-inset-left)] text-[11px] text-fg-subtle">
          {(Object.keys(TYPE_COLORS) as GraphNode['type'][]).map((t) => (
            <span key={t} className="flex items-center gap-1.5">
              <span
                className="inline-block h-2 w-2 rounded-full"
                style={{ backgroundColor: TYPE_COLORS[t] }}
              />
              {TYPE_LABEL[t]}
            </span>
          ))}
        </div>
      )}

      {isLoading && (
        <div className="flex h-full items-center justify-center">
          <p className="text-[13px] text-fg-subtle">Cargando grafo…</p>
        </div>
      )}

      {isError && (
        <div className="flex h-full items-center justify-center px-6">
          <p className="text-[13px] text-danger">No se pudo cargar el grafo.</p>
        </div>
      )}

      {isEmpty && (
        <div className="flex h-full items-center justify-center px-6">
          <div className="flex flex-col items-center gap-3 text-center">
            <div className="flex h-14 w-14 items-center justify-center rounded-full bg-surface">
              <Network size={24} className="text-fg-faint" />
            </div>
            <h2 className="text-[15px] font-medium text-fg-muted">Grafo vacío</h2>
            <p className="max-w-[360px] text-[13px] text-fg-subtle">
              Todavía no hay nodos ni enlaces para mostrar.
            </p>
          </div>
        </div>
      )}

      {showGraph && (
        <ForceGraph2D<GraphNode>
          ref={fgRef}
          width={size.width}
          height={size.height}
          graphData={graphData}
          backgroundColor="rgba(0,0,0,0)"
          nodeId="id"
          nodeRelSize={4}
          nodeVal={(n) => Math.max(1, n.val ?? 1)}
          nodeColor={(n) => TYPE_COLORS[n.type] ?? DEFAULT_COLOR}
          nodeLabel={(n) => n.title || String(n.id ?? '')}
          linkColor={() => 'rgba(255,255,255,0.08)'}
          linkWidth={0.5}
          linkDirectionalArrowLength={2}
          linkDirectionalArrowRelPos={1}
          minZoom={0.15}
          maxZoom={8}
          cooldownTicks={100}
          onEngineStop={() => fgRef.current?.zoomToFit(400, 48)}
          onNodeClick={(n) => {
            if (n.path) onOpenNote(n.path)
          }}
        />
      )}
    </div>
  )
}
