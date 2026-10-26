import { useEffect, useState } from 'react'

/**
 * Corte responsive de SINAPSIS. En `lg` (>=1024px) se restaura el layout de
 * 3 columnas de escritorio; por debajo, Sidebar y RightPanel se comportan como
 * drawers off-canvas. Se centraliza acá para que AppShell y Sidebar compartan
 * exactamente el mismo umbral que las utilidades `lg:` de Tailwind.
 */
const DESKTOP_QUERY = '(min-width: 1024px)'

export function useIsDesktop(): boolean {
  const [isDesktop, setIsDesktop] = useState<boolean>(() =>
    typeof window !== 'undefined' ? window.matchMedia(DESKTOP_QUERY).matches : true
  )

  useEffect(() => {
    const mq = window.matchMedia(DESKTOP_QUERY)
    const onChange = (e: MediaQueryListEvent) => setIsDesktop(e.matches)
    // Sincronizar por si el valor cambió entre el render inicial y el efecto.
    setIsDesktop(mq.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  return isDesktop
}
