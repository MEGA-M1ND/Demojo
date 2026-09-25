import { useEffect, useState } from 'react'
import { api, type AppConfig } from './api'
import { ProjectList } from './components/ProjectList'
import { Workspace, type Step } from './components/Workspace'

type Route = { name: 'home' } | { name: 'project'; id: string; step: Step }

function parseHash(): Route {
  const m = window.location.hash.match(/^#\/p\/(prj_[a-z0-9]+)(?:\/(upload|describe|review|export))?/)
  if (m) return { name: 'project', id: m[1], step: (m[2] as Step) || 'upload' }
  return { name: 'home' }
}

export function navigate(path: string) {
  window.location.hash = path
}

export default function App() {
  const [route, setRoute] = useState<Route>(parseHash)
  const [config, setConfig] = useState<AppConfig | null>(null)

  useEffect(() => {
    const onHash = () => setRoute(parseHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    api.config().then(setConfig).catch(() => setConfig(null))
  }, [])

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href="#/">
          <span className="brand-mark" aria-hidden>
            <svg viewBox="0 0 64 64" width="26" height="26">
              <rect width="64" height="64" rx="16" fill="currentColor" />
              <path d="M25 19.5v25c0 1.2 1.3 1.9 2.3 1.3l19.4-12.5a1.5 1.5 0 0 0 0-2.6L27.3 18.2c-1-.6-2.3.1-2.3 1.3z" fill="#fff" />
            </svg>
          </span>
          Demojo
        </a>
        <span className="tagline">Screenshots, photos &amp; recordings → a narrated product demo</span>
        <span className="spacer" />
        {config?.fixture_mode && (
          <span className="mode-badge mode-fixture" title="DEMOJO_PROVIDER_MODE=fixture: no AI calls; outputs are deterministic templates.">
            Fixture mode · not AI
          </span>
        )}
        {config && !config.fixture_mode && (
          <span className="mode-badge" title={`Vision ${config.models.vision} · Story ${config.models.story} · Voice ${config.models.tts} (${config.models.voice})`}>
            OpenRouter
          </span>
        )}
      </header>
      <main className="main">
        {route.name === 'home' ? (
          <ProjectList onOpen={(id) => navigate(`/p/${id}/upload`)} />
        ) : (
          <Workspace key={route.id} projectId={route.id} step={route.step} config={config} onStep={(s) => navigate(`/p/${route.id}/${s}`)} />
        )}
      </main>
    </div>
  )
}
