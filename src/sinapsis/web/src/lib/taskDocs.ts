import type { TreeNode } from './api'

/**
 * Extrae de un texto libre (la descripcion de una tarea) las referencias a
 * archivos del vault, y las resuelve contra el arbol real.
 *
 * MOTIVO (pedido de admin ): "como es una tarea que me estas pidiendo
 * que revise documentacion, ideal seria una forma que pueda ir a ver la
 * documentacion o el archivo que esta relacionada esta tarea".
 *
 * SOPORTA LAS DOS FORMAS, y el orden importa por frecuencia MEDIDA sobre las
 * 1019 descripciones no vacias del tablero:
 *   · rutas `.md` sueltas en prosa  -> 312 tareas  <- EL CASO DOMINANTE
 *   · [[wikilinks]]                 -> 128 tareas
 *   · ambas                         ->  24 tareas
 * Detectar solo wikilinks habria dejado afuera el caso mas frecuente.
 *
 * ⚠️ EL PROBLEMA LEXICO Y POR QUE NO SE RESUELVE CON REGEX:
 * las carpetas reales del vault TIENEN ESPACIOS (`30 Projects`, `50 Agents`,
 * `99 Archive`, `95 Secrets`). Un regex que permita espacios se come la prosa
 * de la izquierda: de "Detalle completo en vault ExampleOrg/x.md" matchea
 * "en vault ExampleOrg/x.md". No hay forma puramente lexica de saber donde
 * empieza la ruta y donde termina la oracion.
 * SOLUCION: recortar el candidato contra el indice de nombres de carpeta REALES
 * del arbol. Se busca, dentro del match, el ultimo punto donde arranca una
 * carpeta conocida, y se descarta todo lo anterior. Es una decision guiada por
 * DATOS del vault, no por gramatica.
 */

export interface TaskDocRef {
  /** Ruta relativa al vault, tal como la espera openNote(). */
  path: string
  /** Texto para mostrar (el nombre del archivo, sin la ruta completa). */
  label: string
}

/**
 * ⚠️ CACHE OBLIGATORIA. Recorrer el arbol cuesta: el vault real tiene 3.322 .md y
 * 862 carpetas. Sin cache, `indexTree` corria en CADA evaluacion del useMemo (o sea
 * en cada keystroke de la descripcion) y ademas construia un Map de stems de 3.322
 * entradas. Eso colgaba la UI: el tablero dejaba de pintar (regresion medida
 * , reportada por admin: "ahora no salen las tareas en el tablero").
 * La cache es por IDENTIDAD del objeto `tree`: react-query devuelve la MISMA
 * referencia mientras el query no se invalide, asi que un WeakMap alcanza y se
 * libera solo cuando el arbol cambia.
 */
const IDX_CACHE = new WeakMap<object, { files: Set<string>; byStem: Map<string, string>; dirs: Set<string> }>()

/** Junta todos los paths de archivo del arbol + el set de nombres de carpeta. */
function indexTree(tree: TreeNode[]): { files: Set<string>; byStem: Map<string, string>; dirs: Set<string> } {
  const key = (tree.length ? tree[0] : tree) as object
  const hit = IDX_CACHE.get(key)
  if (hit) return hit
  const files = new Set<string>()
  const dirs = new Set<string>()
  // Indice por STEM (nombre sin .md, minuscula) -> path. Se arma en la MISMA pasada.
  // Antes la resolucion de wikilinks hacia `for (const f of files)` DENTRO del loop de
  // matches => O(n*m) con n=3.322 archivos. Un Map lo vuelve O(1) por wikilink.
  const byStem = new Map<string, string>()
  const walk = (nodes: TreeNode[]) => {
    for (const n of nodes) {
      if (n.type === 'dir') {
        // el nombre suelto de la carpeta (ultimo segmento) es lo que sirve de ancla
        const seg = n.path.split('/').filter(Boolean).pop()
        if (seg) dirs.add(seg)
        if (n.children) walk(n.children)
      } else {
        files.add(n.path)
        const fname = (n.path.split('/').pop() || '').replace(/\.md$/i, '').toLowerCase()
        if (fname && !byStem.has(fname)) byStem.set(fname, n.path)
      }
    }
  }
  walk(tree)
  const idx = { files, byStem, dirs }
  IDX_CACHE.set(key, idx)
  return idx
}

/**
 * Recorta un candidato para que arranque en una carpeta real del vault.
 * "Detalle en vault ExampleOrg/example-suite/x.md" -> "ExampleOrg/example-suite/x.md"
 * Si ningun segmento coincide con una carpeta conocida, devuelve null: preferimos
 * NO linkear antes que mostrar un link roto o una ruta con prosa pegada.
 */
function trimToRealFolder(cand: string, dirs: Set<string>): string | null {
  const segs = cand.split('/')
  if (segs.length < 2) return cand // archivo suelto en la raiz, sin carpeta que anclar
  // El primer segmento es el sospechoso (puede traer prosa). Se prueban sufijos
  // de palabras de ese primer segmento, del mas largo al mas corto.
  const words = segs[0].split(/\s+/).filter(Boolean)
  for (let i = 0; i < words.length; i++) {
    const head = words.slice(i).join(' ')
    if (dirs.has(head)) return [head, ...segs.slice(1)].join('/')
  }
  return null
}

// NO-GREEDY (`*?`) a proposito: con greedy, un texto como 'A.md vs B.md' se captura
// como UN solo candidato y se pierden las dos rutas. Medido sobre las 312 descripciones
// reales: greedy resuelve 239, no-greedy resuelve 244.
const MD_RE = /([A-Za-z0-9][A-Za-z0-9 ._&()\-]*?(?:\/[A-Za-z0-9][A-Za-z0-9 ._&()\-]*?)+\.md)/g
const WIKI_RE = /\[\[([^\]|#\n]+?)(?:#[^\]|\n]+?)?(?:\|[^\]\n]+?)?\]\]/g

export function extractTaskDocs(text: string, tree: TreeNode[]): TaskDocRef[] {
  // Salidas tempranas ANTES de tocar el arbol: la mayoria de las descripciones no
  // tienen ninguna referencia, y no hay razon para indexar 3.322 archivos para eso.
  if (!text || !tree.length) return []
  if (!text.includes('.md') && !text.includes('[[')) return []
  const { files, byStem, dirs } = indexTree(tree)
  const out: TaskDocRef[] = []
  const seen = new Set<string>()

  const push = (p: string) => {
    if (!p || seen.has(p)) return
    seen.add(p)
    out.push({ path: p, label: p.split('/').pop() || p })
  }

  // (1) rutas .md sueltas — el caso dominante
  MD_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = MD_RE.exec(text)) !== null) {
    const trimmed = trimToRealFolder(m[1].trim(), dirs)
    if (!trimmed) continue
    // ⚠️ SOLO se linkea si el archivo EXISTE. Un link roto es peor que texto plano:
    // manda al lector a buscar algo que no esta y el 404 se lee como "me falta acceso".
    if (files.has(trimmed)) push(trimmed)
    else if (files.has('obsidian-vault/' + trimmed)) push('obsidian-vault/' + trimmed)
  }

  // (2) [[wikilinks]] — resueltos por stem contra el arbol
  WIKI_RE.lastIndex = 0
  while ((m = WIKI_RE.exec(text)) !== null) {
    const target = m[1].trim()
    if (!target) continue
    const direct = target.endsWith('.md') ? target : target + '.md'
    if (files.has(direct)) { push(direct); continue }
    // por stem, O(1) contra el indice precomputado (antes era un for-of sobre 3.322)
    const stem = (target.split('/').pop() || target).toLowerCase()
    const byS = byStem.get(stem)
    if (byS) push(byS)
  }

  return out
}
