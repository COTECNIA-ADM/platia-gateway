// Preparación común de las pruebas de pantallas (Vitest + Testing Library).
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'
import { setAccessToken } from '../lib/api'

afterEach(async () => {
  cleanup()
  // lib/api.ts suelta la renovación en curso en la siguiente vuelta del bucle de eventos
  await new Promise((resolve) => setTimeout(resolve, 0))
  setAccessToken(null)
  vi.unstubAllGlobals()
})

// jsdom no implementa <dialog>.showModal(): lo usa <Modal>
if (!HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.open = true
  }
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.open = false
  }
}

// jsdom tampoco tiene ResizeObserver: lo usan los gráficos para medir su ancho
if (!('ResizeObserver' in globalThis)) {
  class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  Object.assign(globalThis, { ResizeObserver: ResizeObserverStub })
}
