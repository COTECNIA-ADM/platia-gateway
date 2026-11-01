// Reglas del panel que se pueden comprobar leyendo el código (CLAUDE.md, ADR-017).
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const ROOT = join(__dirname, '..')

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return files(path)
    return /\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name) && !path.includes('/test/') ? [path] : []
  })
}

// Sin comentarios: una regla puede nombrar lo que prohíbe
const strip = (text: string) => text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
const sources = files(ROOT).map((path) => ({ path, text: strip(readFileSync(path, 'utf8')) }))

describe('convenciones del panel', () => {
  it('el token nunca va al almacenamiento del navegador', () => {
    expect(sources.filter((s) => /localStorage|sessionStorage|indexedDB/.test(s.text)).map((s) => s.path)).toEqual([])
  })

  it('nada de HTML sin escapar', () => {
    expect(sources.filter((s) => /dangerouslySetInnerHTML|innerHTML/.test(s.text)).map((s) => s.path)).toEqual([])
  })

  it('las llamadas a la API pasan por lib/api.ts', () => {
    const offenders = sources.filter((s) => !s.path.endsWith('lib/api.ts') && /\bfetch\(/.test(s.text))
    expect(offenders.map((s) => s.path)).toEqual([])
  })

  it('el token del WebSocket va en el subprotocolo, no en la URL', () => {
    const realtime = sources.find((s) => s.path.endsWith('lib/realtime.ts'))!.text
    expect(realtime).toContain("['platia.v1', `bearer.${token}`]")
    expect(realtime).not.toMatch(/stream\?.*token/)
  })
})
