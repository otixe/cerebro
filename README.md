# Cerebro

**Your AI agents forget everything between sessions and can't coordinate. Cerebro gives them shared persistent memory, a task board, a mailbox and shared skills — over MCP and REST, with local embeddings.**

Self-hosted · MIT · works with Claude Code, Codex, Cursor, pi and any MCP client · no embedding API costs by default · [Español](README.es.md)

## Sound familiar?

- **Every session starts from zero.** You re-explain the project, the decisions and the last bug again and again.
- **Two agents, two truths.** One agent learns something and the other never finds out. They redo each other's work or overwrite it.
- **Nobody checks the work.** An agent says "done", and nothing makes sure someone else verified it.
- **Agents can't talk to each other.** Handoffs go through you, copy-pasting between windows.
- **Search that misses.** Keyword search misses paraphrases, vector search misses exact ids and paths, and paid embedding APIs add cost to every note.

## How Cerebro fixes it

| Pain | What you get |
|---|---|
| Starts from zero | **Persistent memory.** Agents write what they learn and search it next session. Memories can be superseded without losing history. |
| Two truths | **Fewer collisions between agents.** One key per agent, teams and roles. Every memory and change is attributed to its author by the key, not by what the client claims, and that attribution plus the shared board help reduce conflicts. |
| Nobody checks | **Task board with QA.** Claim → submit → review, with dependencies. Self-approval is blocked: work is closed by someone else. |
| Can't talk | **Agent correspondence.** Requests and notices between agents, with acknowledgements and threads. |
| Search that misses | **Hybrid search.** Semantic plus lexical (BM25), fused with RRF, with an explicit low-confidence signal instead of a confident wrong answer. |
| Embedding bills | **No embedding API costs by default.** EmbeddingGemma runs locally through Ollama, on your own CPU, RAM and disk. Cloud embeddings through AWS Bedrock (Titan or Cohere) are optional. |
| Lost know-how | **Shared skills.** Reusable procedures that any agent can find by task, plus a markdown vault that is indexed automatically. |

## Quickstart

```bash
git clone https://github.com/otixe/cerebro && cd cerebro
./setup.sh                    # creates .env with random secrets and your admin key
docker compose up -d --wait   # Postgres, Qdrant, Ollama (EmbeddingGemma) and Cerebro
```

Health check: `curl http://127.0.0.1:8765/health`. End-to-end check (write → semantic search, in a throwaway stack): `tests/smoke.sh --down`.

Requirements: Docker with Compose v2, about 4 GB of free RAM and about 3 GB of disk. Everything binds to localhost; put a TLS reverse proxy in front before exposing it.

## Works with

Each agent uses its **own key**: the admin key creates team leaders, and leaders provision their agents. Keep keys in the environment, not in project files.

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

**Any client, over REST**
```bash
curl -s http://127.0.0.1:8765/memory/add -H "X-Merlin-Key: $CEREBRO_KEY" -H 'Content-Type: application/json' \
  -d '{"text":"Deploys go out on Tuesdays after QA.","source_path":"memoria/me/deploy-day","category":"convention"}'
curl -s http://127.0.0.1:8765/memory/search -H "X-Merlin-Key: $CEREBRO_KEY" -H 'Content-Type: application/json' \
  -d '{"query":"when do we ship?","top_k":5}'
```

## Architecture

```
 Claude Code · Codex · Cursor · pi ──MCP──► mcp / mcp-sse ─┐
 scripts · services ────────────────REST───────────────────►│ gateway (FastAPI) ──► Postgres  source of truth: memory, agents, tasks, mail, audit
                                                            │                   ├─► Qdrant    vectors (rebuildable from Postgres)
 vault/*.md ◄──── indexer (watch → chunk → embed) ──────────┘                   └─► Ollama    EmbeddingGemma, local
```

## Cerebro, Engram or Mem0?

All three are good projects. They solve different problems:

| | [Engram](https://github.com/Gentleman-Programming/engram) | [Mem0](https://github.com/mem0ai/mem0) | **Cerebro** |
|---|---|---|---|
| Best for | One developer's coding agent | Adding memory to your app (SDK, server or cloud) | A team of agents that share memory and coordinate work |
| Shape | One Go binary + SQLite | Python/TS library, self-hosted server or managed cloud | Docker stack: Postgres + Qdrant + Ollama |
| Search | Full-text (FTS5) | Vectors; optional BM25 and entity boosting | Hybrid vectors + BM25, low-confidence signal |
| Writes | Stored as the agent sends them | An LLM extracts facts (OpenAI by default) | Stored as the agent sends them; no LLM at write time |
| Embeddings | Not needed | OpenAI by default, configurable | Local EmbeddingGemma by default |
| Multi-agent | Project scoping; Git sync and optional cloud | Users, agents and sessions as scopes | Per-agent keys, teams, roles, execution classes |
| Beyond memory | CLI, HTTP, MCP, TUI | Graph and entity features | Task board with QA, correspondence, shared skills, retrieval evaluation |
| Footprint | Minimal | Library: light; server: a stack | A few GB of RAM and disk |


## License

MIT — see [LICENSE](LICENSE).
