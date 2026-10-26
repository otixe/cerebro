import type { TreeNode } from './api'

/**
 * Soporte de wikilinks estilo Obsidian: [[Nombre]], [[Nombre|alias]],
 * [[Carpeta/Nombre]], [[Nombre#seccion]].
 *
 * Estrategia: un plugin remark reescribe los text-nodes del mdast,
 * convirtiendo cada wikilink en un `link` con url `wikilink:<target>`.
 * El componente <a> de react-markdown detecta ese esquema y navega
 * a la nota destino (resuelta por stem contra el árbol del vault).
 */

// [[ target (#heading)? (|alias)? ]]
const WIKILINK_RE = /\[\[([^\]|#\n]+?)(?:#[^\]|\n]+?)?(?:\|([^\]\n]+?))?\]\]/g

interface MdNode {
  type: string
  value?: string
  url?: string
  title?: string | null
  children?: MdNode[]
}

function splitWikilinks(value: string): MdNode[] {
  const nodes: MdNode[] = []
  let last = 0
  WIKILINK_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = WIKILINK_RE.exec(value)) !== null) {
    if (m.index > last) {
      nodes.push({ type: 'text', value: value.slice(last, m.index) })
    }
    const target = m[1].trim()
    const alias = (m[2] || m[1]).trim()
    nodes.push({
      type: 'link',
      url: 'wikilink:' + encodeURIComponent(target),
      title: null,
      children: [{ type: 'text', value: alias }],
    })
    last = m.index + m[0].length
  }
  if (last < value.length) {
    nodes.push({ type: 'text', value: value.slice(last) })
  }
  return nodes
}

function transform(node: MdNode): void {
  if (!Array.isArray(node.children)) return
  const out: MdNode[] = []
  for (const child of node.children) {
    if (
      child.type === 'text' &&
      typeof child.value === 'string' &&
      child.value.includes('[[')
    ) {
      out.push(...splitWikilinks(child.value))
    } else {
      // code / inlineCode no tienen hijos `text`, así que no se tocan
      if (Array.isArray(child.children)) transform(child)
      out.push(child)
    }
  }
  node.children = out
}

/** Plugin remark: transforma wikilinks en enlaces `wikilink:`. */
export function remarkWikilink() {
  return (tree: MdNode): void => {
    transform(tree)
  }
}

/** Construye un índice stem -> ruta relativa a partir del árbol del vault. */
export function buildWikiIndex(tree: TreeNode | undefined): Map<string, string> {
  const map = new Map<string, string>()
  if (!tree) return map
  const walk = (n: TreeNode): void => {
    if (n.type === 'file' && n.path.endsWith('.md')) {
      const stem = n.name.replace(/\.md$/, '')
      if (!map.has(stem)) map.set(stem, n.path)
    }
    n.children?.forEach(walk)
  }
  walk(tree)
  return map
}

/**
 * Resuelve un target de wikilink (ya URL-decodificado o no) a una ruta del
 * vault. Match por stem (último segmento, sin `.md`); si hay varias, la
 * primera indexada. Devuelve null si no existe.
 */
export function resolveWikilink(
  target: string,
  index: Map<string, string>
): string | null {
  let t = target
  try {
    t = decodeURIComponent(target)
  } catch {
    t = target
  }
  t = t.replace(/\.md$/i, '').trim()
  const stem = (t.split('/').pop() ?? t).trim()
  return index.get(stem) ?? index.get(t) ?? null
}
