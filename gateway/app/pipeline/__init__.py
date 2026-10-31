"""Pipeline de etapas (cap. 3, ADR-003).

Cada etapa es una función ``run(ctx, deps)`` que lee y escribe el contexto compartido y
puede cortar la petición lanzando un `GatewayError`. Ninguna etapa llama a otra: el orden
lo fija `runner.py`.

Etapas en G3: auth → policy → route → audit (apertura) → execute → audit (cierre).
"""
