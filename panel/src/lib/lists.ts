// Utilidades para los formularios: listas escritas a mano (separadas por comas o líneas).

export function splitList(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

export function joinList(values: string[] | null | undefined): string {
  return (values ?? []).join(', ')
}
