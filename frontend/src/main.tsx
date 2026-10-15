import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './styles/globals.css'

// Dev-only: say why the page just (re)loaded. On a phone this tells apart "Chrome discarded the tab
// to free memory" (document.wasDiscarded) from a real reload triggered by the page or dev server.
if (import.meta.env.DEV) {
  const nav = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined
  const discarded = (document as Document & { wasDiscarded?: boolean }).wasDiscarded === true
  if (discarded || nav?.type === 'reload') {
    const el = document.createElement('div')
    el.textContent = discarded
      ? 'Reloaded: Chrome discarded this tab (low memory)'
      : 'Reloaded: page reload (not a Chrome tab discard)'
    el.style.cssText =
      'position:fixed;left:8px;right:8px;bottom:8px;z-index:99999;padding:8px 12px;border-radius:8px;background:#b45309;color:#fff;font:12px sans-serif'
    document.body.appendChild(el)
    window.setTimeout(() => el.remove(), 10000)
  }
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
