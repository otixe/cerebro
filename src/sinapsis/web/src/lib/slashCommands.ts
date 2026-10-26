/**
 * Comandos del slash-menu sobre CodeMirror (Ola 2). Cada comando inserta
 * markdown en la posición del cursor. `insert` devuelve el texto a insertar y
 * el desplazamiento donde debe quedar el cursor (relativo al inicio del texto).
 * `upload: true` marca el comando de imagen, que delega en el uploader de
 * adjuntos existente en vez de insertar texto directo.
 */

export interface SlashInsert {
  text: string
  /** Offset del cursor dentro de `text` tras insertar. Default: final. */
  cursor?: number
}

export interface SlashCommand {
  id: string
  label: string
  hint: string
  /** Icono lucide (nombre). Se resuelve en el componente. */
  icon: string
  keywords: string[]
  /** Si true, dispara el uploader de imagen en vez de insertar texto. */
  upload?: boolean
  insert?: () => SlashInsert
}

function today(): string {
  // Fecha local del navegador del usuario.
  const d = new Date()
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

export const SLASH_COMMANDS: SlashCommand[] = [
  {
    id: 'h1',
    label: 'Encabezado 1',
    hint: '# Título',
    icon: 'Heading1',
    keywords: ['h1', 'heading', 'titulo', 'título', 'encabezado'],
    insert: () => ({ text: '# ' }),
  },
  {
    id: 'h2',
    label: 'Encabezado 2',
    hint: '## Subtítulo',
    icon: 'Heading2',
    keywords: ['h2', 'heading', 'subtitulo', 'encabezado'],
    insert: () => ({ text: '## ' }),
  },
  {
    id: 'h3',
    label: 'Encabezado 3',
    hint: '### Sección',
    icon: 'Heading3',
    keywords: ['h3', 'heading', 'seccion', 'sección', 'encabezado'],
    insert: () => ({ text: '### ' }),
  },
  {
    id: 'bullet',
    label: 'Lista',
    hint: '- Elemento',
    icon: 'List',
    keywords: ['lista', 'bullet', 'ul', 'viñeta', 'vineta'],
    insert: () => ({ text: '- ' }),
  },
  {
    id: 'todo',
    label: 'Lista de tareas',
    hint: '- [ ] Tarea',
    icon: 'ListChecks',
    keywords: ['tarea', 'todo', 'checkbox', 'check', 'pendiente'],
    insert: () => ({ text: '- [ ] ' }),
  },
  {
    id: 'quote',
    label: 'Cita',
    hint: '> Texto citado',
    icon: 'Quote',
    keywords: ['cita', 'quote', 'blockquote'],
    insert: () => ({ text: '> ' }),
  },
  {
    id: 'code',
    label: 'Bloque de código',
    hint: '``` … ```',
    icon: 'Code',
    keywords: ['codigo', 'código', 'code', 'fence', 'bloque'],
    insert: () => ({ text: '```\n\n```\n', cursor: 4 }),
  },
  {
    id: 'table',
    label: 'Tabla',
    hint: 'Tabla 2×2',
    icon: 'Table',
    keywords: ['tabla', 'table', 'grid'],
    insert: () => ({
      text: '| Columna 1 | Columna 2 |\n| --- | --- |\n| Celda | Celda |\n',
      cursor: 2,
    }),
  },
  {
    id: 'callout',
    label: 'Callout',
    hint: '> [!note]',
    icon: 'Info',
    keywords: ['callout', 'nota', 'note', 'admonition', 'aviso'],
    insert: () => ({ text: '> [!note] ', cursor: 10 }),
  },
  {
    id: 'date',
    label: 'Fecha de hoy',
    hint: today(),
    icon: 'Calendar',
    keywords: ['fecha', 'hoy', 'date', 'today'],
    insert: () => ({ text: today() }),
  },
  {
    id: 'link',
    label: 'Enlace',
    hint: '[texto](url)',
    icon: 'Link',
    keywords: ['link', 'enlace', 'url', 'href'],
    insert: () => ({ text: '[texto](url)', cursor: 1 }),
  },
  {
    id: 'image',
    label: 'Imagen (subir)',
    hint: 'Adjuntar imagen',
    icon: 'Image',
    keywords: ['imagen', 'image', 'foto', 'subir', 'upload', 'adjunto'],
    upload: true,
  },
]

export function filterCommands(query: string): SlashCommand[] {
  const q = query.trim().toLowerCase()
  if (!q) return SLASH_COMMANDS
  return SLASH_COMMANDS.filter((c) => {
    if (c.label.toLowerCase().includes(q)) return true
    return c.keywords.some((k) => k.includes(q))
  })
}
