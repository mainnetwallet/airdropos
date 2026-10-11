/** @type {import('tailwindcss').Config} */

// Colors that change between the desktop (dark) and mobile (light) themes are
// CSS variables holding "R G B" channels, defined in src/styles/globals.css.
const channel = (name) => `rgb(var(--${name}) / <alpha-value>)`
const SLATE_SHADES = [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950]

export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        surface: {
          50: channel('surface-50'),
          100: channel('surface-100'),
          200: channel('surface-200'),
          300: channel('surface-300'),
        },
        slate: Object.fromEntries(SLATE_SHADES.map((s) => [s, channel(`slate-${s}`)])),
        accent: {
          DEFAULT: '#3b82f6',
          hover: '#2563eb',
        },
        status: {
          success: '#22c55e',
          warning: '#f59e0b',
          error: '#ef4444',
          info: '#06b6d4',
          idle: '#64748b',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'monospace'],
      },
      boxShadow: {
        card: '0 1px 2px rgba(15, 23, 42, 0.04), 0 8px 24px rgba(37, 99, 235, 0.07)',
        nav: '0 -8px 28px rgba(15, 23, 42, 0.12)',
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
      },
    },
  },
  plugins: [],
}
