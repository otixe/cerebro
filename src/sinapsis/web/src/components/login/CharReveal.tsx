import { motion, useReducedMotion } from 'framer-motion'

interface CharRevealProps {
  text: string
  className?: string
}

/**
 * Revela el texto caracter por caracter desde el centro hacia afuera
 * (mitad izquierda entra por -x, mitad derecha por +x), con blur→0.
 * Es la única animación de entrada tipo "hero" del login (one-shot).
 */
export default function CharReveal({ text, className }: CharRevealProps) {
  const reduce = useReducedMotion()
  const chars = text.split('')
  const center = (chars.length - 1) / 2

  if (reduce) {
    return <span className={className}>{text}</span>
  }

  return (
    <span className={className} aria-label={text}>
      {chars.map((ch, i) => {
        const dist = Math.abs(i - center)
        const fromLeft = i < center
        return (
          <motion.span
            key={i}
            aria-hidden="true"
            style={{ display: 'inline-block' }}
            initial={{ opacity: 0, x: fromLeft ? -40 : 40, filter: 'blur(6px)' }}
            animate={{ opacity: 1, x: 0, filter: 'blur(0px)' }}
            transition={{ duration: 0.5, delay: dist * 0.05, ease: [0.16, 1, 0.3, 1] }}
          >
            {ch === ' ' ? '\u00A0' : ch}
          </motion.span>
        )
      })}
    </span>
  )
}
