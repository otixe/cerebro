# ---- Stage 1: build del frontend (Vite + React + TS) ----
FROM node:20-slim AS web
WORKDIR /build
COPY web/package.json web/package-lock.json* ./
RUN npm ci || npm install
COPY web/ ./
RUN npm run build

# ---- Stage 2: backend FastAPI fusionado (ex-visor + bff) ----
# Sirve el SPA estatico + endpoints de datos + /login + /api/image_bytes.
# Acceso LOCAL a Postgres (psycopg) y al vault montado en /vault/obsidian-vault.
FROM python:3.12-slim AS serve
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    DIST_DIR=/app/web/dist \
    VAULT_ROOT=/vault/obsidian-vault
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
# FUENTE UNICA de la notificacion tablero->bus (cambio de implementación). `common/` vive en
# el proyecto compose del Cerebro (/opt/cerebro/memory-gateway/common), que es OTRO
# arbol: se monta como build context ADICIONAL igual que hacen gateway/ y mcp/.
COPY --from=common bus_notify.py correspondencia_core.py correspondencia_notify.py tasks_rbac.py approval_commands.py ./
COPY backend/server.py backend/bootstrap.py ./
COPY --from=web /build/dist ./web/dist
EXPOSE 8090
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090"]

# Falla el BUILD si el modulo comun no importa o si alguien revierte el fix a una
# constante hardcodeada (la verdad de "quien es lider" es agents.is_lead).
RUN python -c "import bus_notify as b; assert hasattr(b, 'notify_assignment'); assert not hasattr(b, 'LEADERS'), 'bus_notify no debe hardcodear LEADERS'; print('bus_notify OK')"
# ── AVISO DE TABLERO -> CORRESPONDENCIA () ─────────────────────────
# Mismo patrón que los asserts de arriba: falla el BUILD, no el runtime.
RUN python -c "import correspondencia_core as c; assert c.ROOT_HEX_LENGTH == 16, 'el codigo raiz debe seguir siendo de 16 hex'; assert callable(c._registry) and callable(c.normalize_mailbox) and callable(c.individual_mailbox), 'falta el resolvedor de buzones'; assert callable(c._new_root) and callable(c._insert_deliveries); assert c.individual_mailbox({'name':'admin','team':None,'kind':'human','is_lead':False}) == '@admin', 'el buzon del humano dejo de resolver'; assert c.individual_mailbox({'name':'assistant-b-lurtz','team':'assistant-b','kind':'agent','is_lead':False}) == '@assistant-b/lurtz', 'el buzon del subagente dejo de resolver'; print('correspondencia_core OK')"
# RESILIENCIA DE `_registry` (F9). Convierte un revert SILENCIOSO del parche en un
# BUILD QUE FALLA. Es la contencion elegida porque el generador build_common_core.py
# esta roto y este archivo NO se puede regenerar: sin este assert, perder el
# try/except deja UNA fila inconsistente de `agents` tumbando el registro COMPLETO
# de buzones, o sea la flota entera sin avisos y sin sintoma en canal puro.
RUN python -c "import io,logging,correspondencia_core as c; assert hasattr(c,'log'), 'correspondencia_core.log NO existe: sin logger la fila salteada es INVISIBLE (parche de resiliencia revertido)'; buf=io.StringIO(); c.log.addHandler(logging.StreamHandler(buf)); ROWS=[{'name':'admin','team':None,'kind':'human','is_lead':False,'role':None},{'name':'zz-fila-malformada','team':None,'kind':'agent','is_lead':False,'role':None},{'name':'assistant-b','team':'assistant-b','kind':'agent','is_lead':True,'role':None}]; Cur=type('Cur',(),{'execute':lambda s,*a,**k:None,'fetchall':lambda s:ROWS}); ind,teams,aliases=c._registry(Cur()); assert '@admin' in ind and '@assistant-b/assistant-b' in ind, 'una fila malformada tumbo el registro COMPLETO: el fix de resiliencia de _registry fue revertido'; assert len(ind)==2, 'la fila malformada ENTRO al registro'; assert aliases.get('@assistant-b')=='@assistant-b/assistant-b', 'el barrido NO siguio despues de la fila malformada: se perdio el alias de equipo'; out=buf.getvalue(); assert 'SALTEADA' in out and 'zz-fila-malformada' in out, 'la fila se salteo en SILENCIO: sin el log.warning el defecto es invisible'; print('correspondencia_core resiliencia _registry OK', sorted(ind))"
RUN python -c "import correspondencia_notify as n; assert n.TIPO == 'inform', 'el aviso debe ser inform: un request abre hilo eterno (R4 de admin)'; assert n.ABIERTO is False; assert n.PRIORITY_MAP == {'high':'urgente','medium':'medio','low':'bajo'}, 'el mapeo de prioridad no es el declarado'; assert n.PRIORIDAD_DEFAULT == 'medio'; assert callable(n.notify_assignment_correspondencia) and callable(n.resolve_notify_mailbox); print('correspondencia_notify OK', n.TIPO, n.PRIORITY_MAP)"
RUN python -c "import bus_notify as b; assert callable(b.notify_assignment), 'la firma publica no puede cambiar: 7 call sites'; assert callable(b.notify_assignment_bus), 'la rama de rollback al bus NO se puede borrar'; assert b.NOTIFY_DEFAULT == 'correspondencia'; assert set(b.NOTIFY_MODES) == {'correspondencia','bus','both'}, 'se perdio un modo de reversion'; assert b.notify_mode({}) == 'correspondencia'; assert b.notify_mode({'CORRESPONDENCIA_NOTIFY':'bus'}) == 'bus'; assert b.notify_mode({'CORRESPONDENCIA_NOTIFY':'both'}) == 'both'; assert b.notify_mode({'CORRESPONDENCIA_NOTIFY':'basura'}) == 'correspondencia'; print('dispatcher OK', b.NOTIFY_MODES)"
RUN python -c "import ast,inspect,bus_notify as b; src=inspect.getsource(b.notify_assignment); assert 'INSERT INTO bus_messages' not in src, 'el dispatcher no puede escribir al bus inline'; legacy=inspect.getsource(b.notify_assignment_bus); assert 'INSERT INTO bus_messages' in legacy, 'el rollback al bus dejo de existir'; print('confinamiento del INSERT al bus OK')"
