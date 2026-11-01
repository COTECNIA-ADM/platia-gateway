// Eventos en tiempo real del gateway (G12, ADR-016) por WebSocket.
// El token va en el subprotocolo (`platia.v1, bearer.<token>`), nunca en la URL, que acaba en
// los logs. Si la conexión se corta, se vuelve a abrir con espera creciente de 1 s a 30 s;
// si el gateway la cierra por sesión caducada (4401), se renueva el token antes.
// Idea de reconexión adaptada de machinebooks.ai/ES/aigateway/cap-27/block-11 (MIT).
import { useEffect, useRef, useState } from 'react'
import { getAccessToken, refreshSession } from './api'
import type { LiveEvent } from './types'

export type LiveState = 'connecting' | 'open' | 'closed' | 'denied'

const MAX_EVENTS = 50
export const MIN_DELAY = 1000
export const MAX_DELAY = 30_000

export function nextDelay(previous: number): number {
  return Math.min(MAX_DELAY, Math.max(MIN_DELAY, previous * 2))
}

export function streamUrl(location: Pick<Location, 'protocol' | 'host'> = window.location): string {
  return `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/events/stream`
}

export function useLiveEvents(enabled = true) {
  const [events, setEvents] = useState<LiveEvent[]>([])
  const [state, setState] = useState<LiveState>('connecting')
  const delay = useRef(MIN_DELAY / 2)

  useEffect(() => {
    if (!enabled) return
    let socket: WebSocket | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    let stopped = false

    const connect = () => {
      const token = getAccessToken()
      if (stopped || !token) return
      setState('connecting')
      socket = new WebSocket(streamUrl(), ['platia.v1', `bearer.${token}`])
      socket.onopen = () => {
        delay.current = MIN_DELAY / 2
        setState('open')
      }
      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(String(message.data)) as LiveEvent
          setEvents((list) => [event, ...list].slice(0, MAX_EVENTS))
        } catch {
          // Un mensaje mal formado no corta la conexión
        }
      }
      socket.onclose = async (close) => {
        if (stopped) return
        if (close.code === 4403) {
          setState('denied')
          return
        }
        setState('closed')
        if (close.code === 4401) await refreshSession()
        delay.current = nextDelay(delay.current)
        timer = setTimeout(connect, delay.current)
      }
    }

    connect()
    return () => {
      stopped = true
      clearTimeout(timer)
      socket?.close()
    }
  }, [enabled])

  return { events, state }
}
