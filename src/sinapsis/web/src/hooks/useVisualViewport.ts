import { useEffect, useState } from 'react'

/**
 * ALTO REALMENTE VISIBLE del viewport, para el teclado virtual.
 *
 * POR QUÉ EXISTE, y no es lo mismo que `100dvh`: ninguna unidad CSS informa el
 * teclado. `dvh` se adapta a las barras dinámicas del navegador, pero al abrir el
 * teclado el viewport de LAYOUT no cambia, así que el layout sigue creyendo que
 * tiene el alto completo y el composer del chat termina fuera de la zona visible.
 * `window.visualViewport` es la ÚNICA API que reporta lo que se ve de verdad.
 *
 * ⚠️ Y los dos sistemas se comportan DISTINTO, así que hay que leer las dos cosas:
 *   - Android/Chrome: REDUCE `visualViewport.height` (el teclado recorta abajo).
 *   - Safari iOS: DESPLAZA la página hacia arriba ⇒ lo que cambia es
 *     `visualViewport.offsetTop`, y `height` puede quedar casi igual. Un fix que
 *     sólo mire el alto no reacomoda NADA en iOS.
 * Por eso se expone `height` Y `offsetTop`, y se escucha `resize` Y `scroll` del
 * visualViewport: en iOS el evento que llega al abrir el teclado suele ser
 * `scroll`, no `resize`.
 *
 * ANTI-LOOP DE LAYOUT: el estado se actualiza sólo cuando alguno de los valores
 * REDONDEADOS cambia (`Math.round`), y el setter devuelve el objeto anterior
 * cuando son iguales. Así un `resize` que resulta del propio re-render no vuelve a
 * disparar otro re-render: la secuencia converge en un paso.
 *
 * `enabled=false` (escritorio) deja el hook INERTE: no registra listeners y
 * reporta `keyboardOpen: false`. Si el navegador no tiene la API, mismo camino:
 * fallback al comportamiento actual, nunca un crash.
 */

/** Fracción del alto de la ventana que hay que perder para considerarlo teclado.
 *  Un umbral evita confundir el teclado con la barra de direcciones de Safari
 *  (que se contrae ~60-100px al scrollear y NO debe reacomodar el chat). */
const KEYBOARD_RATIO = 0.15
/** Desplazamiento mínimo (px) que cuenta como teclado en iOS. */
const OFFSET_THRESHOLD = 30

export type VisualViewportState = {
  /** Alto visible en px. Sin API o deshabilitado: el alto de la ventana. */
  height: number
  /** Desplazamiento vertical que el navegador le aplicó a la página (iOS). */
  offsetTop: number
  /** Heurística: hay teclado (o algo que recorta/desplaza) sobre la ventana. */
  keyboardOpen: boolean
  /** La API existe y está en uso. Falso ⇒ los valores son el fallback. */
  supported: boolean
}

function readState(enabled: boolean): VisualViewportState {
  if (typeof window === 'undefined') {
    return { height: 0, offsetTop: 0, keyboardOpen: false, supported: false }
  }
  const vv = window.visualViewport
  const winH = window.innerHeight
  if (!enabled || !vv) {
    return { height: winH, offsetTop: 0, keyboardOpen: false, supported: !!vv }
  }
  const height = Math.round(vv.height)
  const offsetTop = Math.round(vv.offsetTop)
  const shrunk = winH - height > winH * KEYBOARD_RATIO
  const shifted = offsetTop > OFFSET_THRESHOLD
  return { height, offsetTop, keyboardOpen: shrunk || shifted, supported: true }
}

export function useVisualViewport(enabled = true): VisualViewportState {
  const [state, setState] = useState<VisualViewportState>(() => readState(enabled))

  useEffect(() => {
    const vv = typeof window !== 'undefined' ? window.visualViewport : undefined

    // Deshabilitado o sin API: se fija el fallback UNA vez y no se escucha nada.
    if (!enabled || !vv) {
      setState(readState(enabled))
      return
    }

    const sync = () => {
      const next = readState(true)
      setState((prev) =>
        prev.height === next.height &&
        prev.offsetTop === next.offsetTop &&
        prev.keyboardOpen === next.keyboardOpen &&
        prev.supported === next.supported
          ? prev // mismo objeto ⇒ React no re-renderiza ⇒ no hay loop de layout
          : next
      )
    }

    sync()
    vv.addEventListener('resize', sync)
    vv.addEventListener('scroll', sync)
    return () => {
      vv.removeEventListener('resize', sync)
      vv.removeEventListener('scroll', sync)
    }
  }, [enabled])

  return state
}
