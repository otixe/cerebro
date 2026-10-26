/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: 'var(--bg)',
        panel: 'var(--panel)',
        surface: {
          DEFAULT: 'var(--surface)',
          hover: 'var(--surface-hover)',
        },
        fg: {
          DEFAULT: 'var(--fg)',
          muted: 'var(--fg-muted)',
          subtle: 'var(--fg-subtle)',
          faint: 'var(--fg-faint)',
        },
        accent: {
          DEFAULT: 'var(--accent)',
          bg: 'var(--accent-bg)',
          hover: 'var(--accent-hover)',
        },
        success: 'var(--success)',
        warn: 'var(--warn)',
        danger: 'var(--danger)',
      },
      borderColor: {
        strong: 'rgba(255,255,255,0.08)',
        subtle: 'rgba(255,255,255,0.05)',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      fontWeight: {
        normal: '400',
        medium: '510',
        semibold: '590',
      },
      borderRadius: {
        sm: '4px',
        DEFAULT: '6px',
        md: '6px',
        lg: '8px',
        xl: '12px',
      },
      transitionDuration: {
        DEFAULT: '150ms',
      },
    },
  },
  plugins: [],
}
