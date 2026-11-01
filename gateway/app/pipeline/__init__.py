"""Pipeline de etapas (cap. 3, ADR-003).

Cada etapa es una función ``run(ctx, deps)`` que lee y escribe el contexto compartido y
puede cortar la petición lanzando un `GatewayError`. Ninguna etapa llama a otra: el orden
lo fija `runner.py`.

Orden actual: auth → security_input → filter → policy → cache → quota → route → audit
(apertura) → execute → security_output → audit (cierre). Al terminar, `telemetry.finish`
registra las métricas y los eventos (G12), sin ser una etapa.
"""
