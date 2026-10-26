# Cerebro

**Tus agentes de IA se olvidan de todo entre sesiones y no se pueden coordinar. Cerebro les da memoria persistente compartida, un tablero de tareas, un buzón y skills compartidas, por MCP y REST, con embeddings locales.**

Autoalojado · MIT · funciona con Claude Code, Codex, Cursor, pi y cualquier cliente MCP · sin costo de API de embeddings por defecto · [English](README.md)

## ¿Te pasa esto?

- **Cada sesión arranca de cero.** Le volvés a explicar el proyecto, las decisiones y el último bug, una y otra vez.
- **Dos agentes, dos verdades.** Uno aprende algo y el otro nunca se entera. Rehacen el trabajo del otro o lo pisan.
- **Nadie revisa.** Un agente dice «listo» y nada asegura que otro lo haya verificado.
- **Los agentes no se hablan.** Los traspasos pasan por vos, copiando y pegando entre ventanas.
- **Búsquedas que fallan.** La búsqueda por palabras no encuentra paráfrasis, la vectorial no encuentra ids ni paths exactos, y las APIs pagas de embeddings le suman costo a cada nota.

## Cómo lo resuelve Cerebro

| Dolor | Qué ganás |
|---|---|
| Arranca de cero | **Memoria persistente.** Los agentes guardan lo que aprenden y lo encuentran en la próxima sesión. Una memoria se puede reemplazar sin perder la historia. |
| Dos verdades | **Menos conflictos entre agentes.** Una key por agente, equipos y roles. Cada memoria y cada cambio quedan atribuidos a su autor por la key, no por lo que diga el cliente, y esa atribución, junto con el tablero compartido, ayuda a reducir conflictos. |
| Nadie revisa | **Tablero de tareas con QA.** Tomar → entregar → revisar, con dependencias. La autoaprobación está bloqueada: el trabajo lo cierra otro. |
| No se hablan | **Correspondencia entre agentes.** Pedidos y avisos entre agentes, con acuses e hilos. |
| Búsquedas que fallan | **Búsqueda híbrida.** Semántica más léxica (BM25), fusionadas con RRF, con una señal explícita de baja confianza en vez de una respuesta equivocada segura. |
| Costo de embeddings | **Sin costo de API de embeddings por defecto.** EmbeddingGemma corre local con Ollama, con tu propia CPU, RAM y disco. Los embeddings en la nube por AWS Bedrock (Titan o Cohere) son opcionales. |
| Conocimiento que se pierde | **Skills compartidas.** Procedimientos reutilizables que cualquier agente encuentra por tarea, más un vault de markdown que se indexa solo. |

## Arranque rápido

```bash
git clone https://github.com/otixe/cerebro && cd cerebro
./setup.sh                    # crea el .env con secretos aleatorios y tu key de admin
docker compose up -d --wait   # Postgres, Qdrant, Ollama (EmbeddingGemma) y Cerebro
```

Chequeo: `curl http://127.0.0.1:8765/health`. Prueba de punta a punta (escritura → búsqueda semántica, en un stack descartable): `tests/smoke.sh --down`.

Requisitos: Docker con Compose v2, unos 4 GB de RAM libres y unos 3 GB de disco. Todo escucha en localhost; antes de exponerlo, poné adelante un reverse proxy con TLS.

## Funciona con

Cada agente usa **su propia key**: la key de admin crea líderes de equipo, y los líderes provisionan a sus agentes. Las keys van en el entorno, no en archivos del proyecto.

**Claude Code**
```bash
claude mcp add --transport http cerebro http://127.0.0.1:8766/mcp --header "X-Merlin-Key: ${CEREBRO_KEY}"
```

**Codex** (`~/.codex/config.toml`)
```toml
[mcp_servers.cerebro]
url = "http://127.0.0.1:8766/mcp"
env_http_headers = { "X-Merlin-Key" = "CEREBRO_KEY" }
```

**Cursor** (`~/.cursor/mcp.json`)
```json
{ "mcpServers": { "cerebro": {
  "url": "http://127.0.0.1:8766/mcp",
  "headers": { "X-Merlin-Key": "${env:CEREBRO_KEY}" } } } }
```

**pi**
```bash
pi mcp add cerebro --url http://127.0.0.1:8766/mcp --header 'X-Merlin-Key=${CEREBRO_KEY}'
```

**Cualquier cliente, por REST**
```bash
curl -s http://127.0.0.1:8765/memory/add -H "X-Merlin-Key: $CEREBRO_KEY" -H 'Content-Type: application/json' \
  -d '{"text":"Los deploys salen los martes, después de QA.","source_path":"memoria/yo/dia-de-deploy","category":"convention"}'
curl -s http://127.0.0.1:8765/memory/search -H "X-Merlin-Key: $CEREBRO_KEY" -H 'Content-Type: application/json' \
  -d '{"query":"¿cuándo publicamos?","top_k":5}'
```

## Arquitectura

```
 Claude Code · Codex · Cursor · pi ──MCP──► mcp / mcp-sse ─┐
 scripts · servicios ───────────────REST───────────────────►│ gateway (FastAPI) ──► Postgres  fuente de verdad: memoria, agentes, tareas, correo, auditoría
                                                            │                   ├─► Qdrant    vectores (se reconstruyen desde Postgres)
 vault/*.md ◄──── indexer (vigila → trocea → vectoriza) ────┘                   └─► Ollama    EmbeddingGemma, local
```

## ¿Cerebro, Engram o Mem0?

Los tres son buenos proyectos. Resuelven problemas distintos:

| | [Engram](https://github.com/Gentleman-Programming/engram) | [Mem0](https://github.com/mem0ai/mem0) | **Cerebro** |
|---|---|---|---|
| Ideal para | El agente de código de un desarrollador | Sumarle memoria a tu app (SDK, servidor o nube) | Un equipo de agentes que comparte memoria y coordina trabajo |
| Forma | Un binario Go + SQLite | Librería Python/TS, servidor autoalojado o nube administrada | Stack Docker: Postgres + Qdrant + Ollama |
| Búsqueda | Texto completo (FTS5) | Vectores; BM25 y realce por entidades opcionales | Híbrida vectores + BM25, con señal de baja confianza |
| Escritura | Se guarda como la manda el agente | Un LLM extrae hechos (OpenAI por defecto) | Se guarda como la manda el agente; sin LLM al escribir |
| Embeddings | No necesita | OpenAI por defecto, configurable | EmbeddingGemma local por defecto |
| Multiagente | Alcance por proyecto; Git sync y nube opcional | Usuarios, agentes y sesiones como alcances | Key por agente, equipos, roles y clases de ejecución |
| Más allá de la memoria | CLI, HTTP, MCP y TUI | Grafo y entidades | Tablero con QA, correspondencia, skills compartidas y evaluación del retrieval |
| Huella | Mínima | Librería: liviana; servidor: un stack | Algunos GB de RAM y disco |


## Licencia

MIT. Ver [LICENSE](LICENSE).
