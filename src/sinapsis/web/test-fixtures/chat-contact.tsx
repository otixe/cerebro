import React from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ChatView from '../src/components/ChatView'

const agents = ['agent-a', 'agent-b'].map(name => ({ name, status: 'respondiendo', detail: 'online', accepting_sends: true }))
const requests: Array<{ agent: string; message: string; images?: unknown[] }> = []
const waiting: Array<() => void> = []
;(window as any).fixture = { requests, resolve: () => waiting.shift()?.() }
const originalFetch = window.fetch.bind(window)
window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = new URL(typeof input === 'string' ? input : input.toString(), location.href)
  const reply = (data: unknown, status = 200) => new Response(JSON.stringify(data), {status, headers: {'content-type': 'application/json'}})
  if (url.pathname === '/api/chat/agents') return reply({ agents, states: [] })
  if (url.pathname === '/api/chat/capabilities') return reply({agent:url.searchParams.get('agent'), images:{supported:true, max_b64:8*1024*1024, mimes:['image/png','image/jpeg','image/gif','image/webp']}})
  if (url.pathname === '/api/chat/messages') return reply({messages: []})
  if (url.pathname === '/api/chat/state') return reply({isStreaming:false})
  if (url.pathname === '/api/chat/stats') return reply({})
  if (url.pathname === '/api/chat/commands') return reply({commands: []})
  if (url.pathname === '/api/chat/pending') return reply({pending: []})
  if (url.pathname === '/api/chat/subagents') return reply({subagents: []})
  if (url.pathname === '/api/chat/send') {
    requests.push(JSON.parse(String(init?.body)))
    return new Promise<Response>(resolve => waiting.push(() => resolve(reply({accepted:true}, 202))))
  }
  return originalFetch(input, init)
}) as typeof window.fetch
createRoot(document.getElementById('root')!).render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><ChatView /></QueryClientProvider>)
