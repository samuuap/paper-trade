# PLAN — Sistema de paper trading con agentes IA en competición

> **Documento maestro del proyecto.** Contiene el diseño, el estado real del
> código y el trabajo pendiente. Está escrito para que cualquiera —incluido tu
> yo de dentro de tres meses, o un agente que retome el hilo en frío— pueda
> continuar sin depender de la memoria de una conversación.
>
> **Si vuelves después de una pausa, lee en este orden:** §7 (montar el entorno),
> §5 (qué hay hecho), §6 (qué toca ahora). El resto es referencia.
>
> Idioma: la documentación va en castellano; el código, los docstrings y los
> comentarios van en inglés. Mantén esa separación.

**Última actualización:** 2026-09-28 (Fases 1-3 completas)

---

## 0. Cómo usar y mantener este documento

Tres reglas para que no se convierta en ficción:

1. **§5 (Estado actual) se actualiza en el mismo cambio que toca el código.** Si
   escribes un módulo, marcas su casilla. Un documento que miente es peor que no
   tenerlo.
2. **§4 (Principios) no se cambia a la ligera.** Son las invariantes de las que
   cuelga todo lo demás; romper una obliga a revisar medio sistema. Si de verdad
   hay que cambiar una, se anota en §11 con el motivo.
3. **§11 (Bitácora) registra decisiones, no actividad.** "Añadí el orquestador"
   no aporta nada —eso está en git—. "Elegí un bucle propio en vez de APScheduler,
   porque X" sí.

---

## 1. Qué es esto y para qué sirve

Un sistema que opera acciones US **sin dinero real**, 24/7, donde varios agentes
LLM compiten entre sí: cada uno con su propia estrategia y su propio portfolio.

El objetivo **no es ganar dinero** (es simulado). Es construir un **banco de
pruebas** que permita responder empíricamente:

- ¿Qué estrategia funciona mejor, y se distingue del ruido?
- ¿Cuántos tokens cuesta cada dólar de P&L?
- ¿Un agente LLM local bate a un simple buy & hold del S&P 500?
- ¿Compensa la latencia de un modelo local frente a pagar una API frontera?

Por eso **la instrumentación es tan producto como el trading**: cada decisión
registra tokens, latencia, velocidad de generación, reintentos, coste sombra y
coste energético.

### Hardware

MacBook Pro **M3 Pro, 36 GB** unificados, 12 núcleos. Techo real: modelos hasta
~32B cuantizados a 4 bits; un 70B no cabe. El ancho de banda de memoria es
moderado y la generación está limitada por ancho de banda, así que la elección
del modelo prioriza fiabilidad/latencia sobre tamaño.

**Tensión conocida:** es un portátil y el requisito es 24/7. El sistema tolera
interrupciones limpiamente (estado persistente, reanudación), pero continuidad
real acabará pidiendo una máquina siempre encendida.

---

## 2. Decisiones cerradas

| Decisión | Elección |
|---|---|
| Mercado | Solo acciones US |
| Universo | S&P 500 completo (+ SPY como benchmark) |
| Arquitectura de agentes | Varios compitiendo, portfolio independiente cada uno |
| Cadencia de decisión | Cada 15 min en horario de mercado |
| Modelo | LLM local, **el mismo para todos** (la estrategia es la única variable) |
| Servidor de inferencia | `llama-server` de llama.cpp (API compatible OpenAI + GBNF) |
| Familia de modelo | Qwen3 (el mejor en llamada a herramientas entre los abiertos) |
| Stack | Python/FastAPI + Next.js |
| Datos de mercado | Finnhub (tier gratuito) |
| Ejecución | Ledger interno propio con fills simulados |
| Base de datos | PostgreSQL + TimescaleDB |
| Hosting | Docker Compose local, portable a cloud |

### La máquina de fases (el corazón del "24/7")

El mercado US abre 32 h/semana. Un sistema que solo actúe en esa ventana estaría
ocioso el 80% del tiempo. Cada fase da a los agentes un trabajo distinto:

| Fase | Horario ET | Actividad | `DecisionKind` |
|---|---|---|---|
| `PRE_MARKET` | 04:00–09:30 | Digerir noticias overnight, gaps, watchlist y plan del día | `PLAN` |
| `OPEN` | 09:30–16:00 | Ciclos de decisión de trading cada 15 min | `TRADE` |
| `AFTER_HOURS` | 16:00–20:00 | Revisión post-cierre, atribución de P&L, diario | `REVIEW` |
| `CLOSED` | Noche, findes, festivos | Research profundo, reflexión sobre la estrategia | `RESEARCH` |

Solo `TRADE` puede mover el ledger. Las otras tres producen texto e
instrumentación, que es precisamente lo que se quiere medir de forma continua.

---

## 3. Arquitectura

```
   ┌─────────────┐            ┌─────────────┐           ┌─────────────┐
   │ AGENTE      │            │ AGENTE      │    ...    │ BASELINES   │
   │ Momentum    │            │ Value       │           │ SPY / Quant │
   └──────┬──────┘            └──────┬──────┘           └──────┬──────┘
          │  herramientas             │                        │ sin LLM
          │  get_bars, screen_stocks, │                        │
          │  get_news, get_quote…     │                        │
          ▼                           ▼                        ▼
   ┌────────────────────────────────────────────────────────────────┐
   │              MOTOR DE EJECUCIÓN (ledger interno)               │
   │   órdenes → simulación de fill con quote real + slippage       │
   │   posiciones · cash · P&L por agente                           │
   └───────────────────────────────┬────────────────────────────────┘
                                   ▼
   ┌────────────────────────────────────────────────────────────────┐
   │        PostgreSQL + TimescaleDB   (series temporales)          │
   └───────────────────────────────┬────────────────────────────────┘
                                   ▼
   ┌────────────────────────────────────────────────────────────────┐
   │   MOTOR DE MÉTRICAS  →  API FastAPI  →  Dashboard Next.js      │
   └────────────────────────────────────────────────────────────────┘
```

### Mapa de módulos

```
backend/app/
├── core/          config, logging estructurado, bus de eventos (SSE)
├── db/            modelos SQLAlchemy, engine/sesiones, upsert, migraciones Alembic
├── market/        proveedores de datos, rate limiter, calendario+fases, repository,
│                  indicadores, screener, universo
├── engine/        ledger (contabilidad), risk (rails deterministas), execution (fills)
├── llm/           cliente OpenAI-compatible, GBNF, schemas, cola secuencial, pricing
├── agents/        base/contexto, tools, runtime LLM instrumentado, estrategias,
│                  baselines, registry (roster + dispatch de runtime)
├── orchestrator/  resources, jobs, ingest, cycle, scheduler (máquina de fases)
├── metrics/       financial (puro), rollup (diario), queries (lectura)
├── api/           deps + routes/{agents,trading,market,system,stream}
└── scripts/       seed del roster, utilidades de línea de comandos

frontend/          Next.js 15 + Tailwind 4 + Recharts (solo modo oscuro; ver §4.9)
├── app/           resumen, agente, operaciones, decisiones, economía, sistema
├── components/    EquityChart, Leaderboard, StatTile, Nav, LiveStatus
└── lib/           cliente tipado de la API, hooks (polling + SSE), tokens de tema
```

### Por qué un ledger interno

Seis portfolios necesitan contabilidad propia, y ningún proveedor de paper
trading deja abrir seis cuentas cómodamente (Alpaca tope en tres, y solo desde su
panel web). El sistema mantiene su propio ledger y simula los fills contra quotes
reales más slippage y comisión.

Deja de ser una concesión y pasa a ser ventaja:

- Añadir o quitar un agente es una fila en la base de datos
- Control total sobre atribución y métricas
- El **mismo** motor sirve para backtesting reproduciendo datos históricos, sin
  escribir un segundo motor que inevitablemente divergiría
- Determinista y auditable: cada fill guarda el quote que lo originó
- Independiza el sistema del proveedor de datos, que pasa a ser sustituible

### Capa de datos de mercado

Finnhub gratuito impone dos límites que condicionan el diseño: **60 peticiones
por minuto** y **~50 símbolos** en WebSocket. Con 500 valores, la ingesta se
organiza en dos niveles que encajan con la máquina de fases:

- **Barras diarias de las 500**, una vez al día tras el cierre. A 60 req/min son
  ~9 minutos, irrelevantes en `AFTER_HOURS`. De aquí salen indicadores y screening.
- **Tiempo real por WebSocket solo del conjunto activo** — posiciones abiertas de
  los seis portfolios + mejores candidatos del screening, rotando. Por debajo de 50.

Los datos ingeridos se persisten y **los agentes leen siempre de la base de datos,
nunca del proveedor**. Eso desacopla las decisiones de la red, hace los ciclos
reproducibles y permite replicar un día entero en modo replay.

### Capa de inferencia local

**Modelo escalonado por fase.** Los 36 GB son suficientes pero no infinitos, y el
portátil es además la máquina de trabajo:

- En `OPEN`, cuando probablemente estés usando el equipo: modelo ~14B a 4 bits
  (~11 GB), rápido y deja memoria libre.
- En `CLOSED` (madrugada, findes): un MoE mayor para research profundo. Los MoE
  activan solo una fracción de sus parámetros por token: calidad de modelo grande
  a coste de memoria de modelo mediano.

**Salida estructurada forzada — la pieza clave.** Los modelos locales fallan sobre
todo produciendo JSON válido de forma consistente en bucles de varios pasos, y un
fallo de parseo a las 3 de la mañana es una decisión perdida sin que nadie se
entere. La solución no es pedir JSON y confiar, sino **restringir la generación
con una gramática GBNF**: el muestreador solo puede emitir tokens que continúen
una derivación válida del esquema. Con la gramática activa, una salida malformada
es *imposible*, no improbable.

Honestidad sobre lo que esto resuelve: la gramática garantiza el **formato**, no
la **calidad** de la decisión. Hay evidencia de que constreñir a un esquema
estricto puede degradar algo el razonamiento sobre qué herramienta llamar. Por eso
en el runtime el bucle de herramientas va **sin** gramática y solo la respuesta
final va constreñida.

**Ejecución secuencial.** Todos los agentes comparten un único modelo en memoria,
así que sus decisiones se encolan (`InferenceQueue`, concurrencia 1). Con cadencia
de 15 minutos sobra margen, y se evita que varias copias del modelo peleen por la
memoria unificada. La profundidad de la cola es una métrica de primera clase.

### Expectativa realista de calidad

Con Qwen3 + gramáticas, los fallos de formato desaparecen y lo que queda es una
**brecha de razonamiento**, no de fiabilidad. En días rutinarios y con el trabajo
pesado hecho en Python, el modelo local rinde bien. La diferencia frente a un
modelo frontera se notará en los casos difíciles: regímenes nuevos, noticias
ambiguas, dimensionado poco habitual. Esto refuerza el principio de §4.1: cuanto
más estrecha sea la tarea de juicio, menos importa esa brecha.

---

## 4. Principios de diseño (invariantes)

Estas son las reglas de las que cuelga el resto del sistema. Cualquier cambio
futuro debe respetarlas o justificar explícitamente por qué no.

### 4.1 Determinista primero, LLM al final

La tentación con agentes LLM es darles el mercado entero y pedirles que
"analicen". Es mala idea: un modelo local razona menos que uno frontera, cada
token cuesta segundos de latencia, y el razonamiento libre es casi imposible de
depurar cuando una estrategia empieza a perder dinero.

- **Python calcula**: indicadores técnicos, filtrado del S&P 500, orden de
  candidatos, reglas de riesgo, tamaño de posición, validación de que la orden es
  legal contra el efectivo disponible.
- **El LLM decide**: sobre un puñado de candidatos ya preprocesados y con las
  señales ya calculadas, cuáles elegir, con qué convicción y por qué.

El agente no recibe "aquí está el mercado" sino *"estos son los 15 mejores
candidatos según tu screener, con sus señales calculadas; tu estrategia es esta;
elige y justifica"*.

Beneficio que importa: cuando una estrategia falla, puedes separar **fallo de
cálculo** de **fallo de juicio**.

### 4.2 El modelo propone, Python dispone

Ninguna orden llega al ledger sin pasar por `engine/risk.py`. Los límites
(posición máxima, exposición bruta, notional mínimo, buffer de caja, órdenes por
ciclo) se aplican **después** del LLM y son sobreescribibles por agente
(`agents.config`) —de hecho el Buy & Hold necesita un tope del 100% para poder
seguir al índice—.

### 4.3 Los agentes nunca hablan con el proveedor de datos

Todo pasa por `market/repository.py`. Consecuencias: ciclos reproducibles,
decisiones desacopladas de la latencia de red, y modo replay posible.

### 4.4 Todo lectura acepta `as_of`

Cada consulta de precios admite un instante simulado. Es lo que permite que un
replay de un día pasado sea reproducible bit a bit. Si añades una lectura nueva,
propaga `as_of`.

### 4.5 Dinero en punto fijo, nunca float

`Decimal` en Python, `Numeric(20,6)` en la base de datos. La deriva de redondeo
en un ledger es el tipo de bug que corrompe silenciosamente todas las métricas.
Los `float` solo aparecen en ratios y métricas derivadas.

### 4.6 La instrumentación es producto

Cada decisión —también las de los baselines, con ceros— escribe una fila en
`decisions` con tokens, latencia, tok/s, tool calls, reintentos, coste sombra y
coste energético. Eso es lo que hace comparable el coste por dólar de P&L en todo
el roster.

### 4.7 Los baselines son test de cordura permanente

**Buy & Hold SPY tiene que replicar el índice.** Si su curva diverge del SPY, el
bug está en el motor o en las métricas, no en el mercado. Es la alarma más barata
y más fiable del sistema; no la desactives "temporalmente".

### 4.8 Un dashboard lento no puede frenar el trading

El bus de eventos (`core/events.py`) descarta eventos antiguos de los
suscriptores lentos en vez de bloquear al publicador.

### 4.9 El color nunca lleva la identidad solo

La paleta de los seis agentes está **validada** contra deficiencia de visión
cromática sobre la superficie oscura del dashboard: los cuatro tonos LLM son el
único subconjunto de la rampa de referencia que pasa con todos los pares en
pantalla a la vez, y los dos baselines van en gris porque son referencia, no
competidores. Aun así el color nunca va solo: leyenda siempre visible, nombre
del agente junto a cada chip y etiquetas directas.

Consecuencia práctica: **cambiar el `color` de un agente obliga a revalidar la
paleta.** Los hexes viven en `app/agents/strategies.py` y `registry.py`, no en
el frontend, porque son datos del roster.

### 4.10 El dashboard no hace aritmética

El frontend formatea; no calcula. Cada número viene de `metrics/queries.py`,
que a su vez lee `metrics_daily` en vez de recalcular. Dos implementaciones de
«Sharpe» acabarían discrepando, y la que se ve en pantalla sería la que nadie
ha testeado.

---

## 5. Estado actual

**Resumen:** Fases 1, 2 y 3 completas y verificadas en seco. Queda la Fase 0
(validar el modelo local) y la verificación en vivo con el mercado abierto.
Nada commiteado todavía.

### 5.1 Hecho

| Módulo | Ficheros | Estado |
|---|---|---|
| `core/` | `config.py`, `logging.py`, `events.py` | ✅ Settings, logging estructurado, bus SSE con suscripción por cola |
| `db/` | `base.py`, `models.py`, `upsert.py`, `migrations/0001` | ✅ 14 tablas, hypertables Timescale, upsert troceado por límite de parámetros |
| `market/` | `provider.py`, `rate_limiter.py`, `calendar.py`, `repository.py`, `indicators.py`, `screener.py`, `universe.py` | ✅ Finnhub + proveedor sintético, limitador, calendario NYSE con fases, 8 presets |
| `engine/` | `ledger.py`, `risk.py`, `execution.py` | ✅ Contabilidad, rails de riesgo, fills con slippage/comisión/participación |
| `llm/` | `client.py`, `grammar.py`, `schemas.py`, `queue.py`, `pricing.py` | ✅ Cliente OpenAI-compatible, compilador JSON Schema→GBNF, cola secuencial, coste sombra |
| `agents/` | `base.py`, `tools.py`, `llm_agent.py`, `strategies.py`, `baselines.py`, `registry.py` | ✅ 4 estrategias + 2 baselines, 7 herramientas, runtime instrumentado, roster y dispatch |
| `orchestrator/` | `resources.py`, `jobs.py`, `ingest.py`, `cycle.py`, `scheduler.py` | ✅ Máquina de fases 24/7, guardia de recursos, ingesta en dos niveles, ciclo por agente |
| `metrics/` | `financial.py`, `rollup.py`, `queries.py` | ✅ Todas las métricas del §9, rollup diario, consultas de lectura |
| `api/` | `deps.py`, `routes/{agents,trading,market,system,stream}.py` | ✅ 24 endpoints + SSE |
| raíz backend | `main.py`, `scripts/seed.py` | ✅ Composición, ciclo de vida, seed idempotente |
| empaquetado | `Dockerfile`, `docker-compose.yml`, `.env.example`, `.gitignore` | ✅ Timescale + backend + frontend |
| `frontend/` | Next.js 15 + Tailwind 4 + Recharts | ✅ Resumen, agente, operaciones, decisiones, economía, sistema |
| `tests/` | 7 ficheros | ✅ **150 tests en verde en ~2,4 s**, ruff limpio |

**Cobertura de tests por área:** ledger (22), gramática GBNF (23), riesgo (26),
ejecución (11), métricas (25), orquestador (13), runtime LLM (15), API (15).

**Roster:** `momentum`, `value`, `contrarian`, `sentiment` (LLM) +
`buy_and_hold_spy`, `quant_sma` (baselines).

### 5.2 Pendiente

| Pieza | Por qué importa | Fase |
|---|---|---|
| ⬜ **Validar el modelo local** | El riesgo principal del proyecto, y sigue sin medir. Ver §6 Fase 0. | 0 |
| ⬜ **Feed en vivo por WebSocket** | Hoy los quotes se sondean por REST cada 60 s. Funciona, pero 45 símbolos por minuto consumen casi todo el presupuesto de 55 req/min y compiten con la ingesta diaria. El WebSocket de Finnhub lo resolvería sin gastar peticiones. | 1 |
| ⬜ **Sesión real en vivo** | Verificación nivel 3: nada se ha ejecutado aún contra el mercado abierto. | — |
| ⬜ **Histórico en el tier gratuito** | `/stock/candle` es premium, así que el histórico se acumula día a día y los indicadores de ventana larga (SMA200, rango 52 semanas) tardan meses en poblarse. Documentado en `app/orchestrator/ingest.py`. | — |

### 5.3 Verificado en seco

- Un **día completo de mercado replicado** con reloj simulado, sin red y sin
  servidor de inferencia, produce decisiones, órdenes, snapshots de equity,
  transiciones de fase y filas de métricas (`tests/test_orchestrator.py`).
- El **runtime LLM** se ejercita de punta a punta contra un cliente falso
  (`tests/fake_llm.py`): bucle de herramientas, respuesta constreñida,
  reintentos por salida inválida e instrumentación completa.
- **Buy & Hold SPY replica al índice** dentro del coste de entrada
  (`tests/test_metrics.py`), que es la alarma permanente del §4.7.

## 6. Roadmap

### Fase 0 — Validar el modelo local ⬜ (no hecha, y es deuda)

Montar `llama-server` y comprobar con un banco de pruebas que el modelo elegido
produce decisiones válidas de forma fiable: JSON conforme al esquema,
herramientas bien invocadas, criterio razonable sobre candidatos preprocesados.

Iba primero en el plan original **deliberadamente**: si el modelo local no da la
talla, todo lo demás se diseña distinto. Se ha construido por delante de esta
validación, así que el riesgo sigue abierto. La salida si sale mal es el esquema
híbrido (local para ciclos rutinarios, API para research profundo), y la
abstracción de proveedor en `llm/client.py` ya lo deja preparado.

### Fase 1 — Núcleo operativo ✅

Al terminar, el sistema **opera y persiste decisiones solo**.

1. `orchestrator/resources.py` — `model_for_phase()`, memoria libre vía `psutil`,
   veto de inferencia si no hay RAM (`min_free_memory_gb`).
2. `orchestrator/ingest.py` — barras diarias de las 500 tras el cierre,
   recálculo de señales para el screener, quotes del conjunto activo, noticias.
   Cada job escribe una fila en `job_runs`.
3. `orchestrator/cycle.py` — ejecuta un ciclo: resuelve el roster activo, monta
   `AgentContext` por agente, los pasa por la cola, hace snapshot de equity.
4. `orchestrator/scheduler.py` — bucle 24/7 sobre `MarketCalendar`: detecta
   transiciones de fase, dispara ciclos cada `decision_interval_minutes` en
   `OPEN`, un `PLAN`/`REVIEW`/`RESEARCH` por fase, y soporta `ReplayClock`.
5. `scripts/seed.py` — crea los 6 agentes y sus portfolios de forma idempotente.

**Criterio de aceptación:** un replay de un día pasado con el proveedor sintético
produce decisiones, órdenes, fills y snapshots de equity coherentes, sin red y
sin LLM real.

### Fase 2 — Métricas y API ✅

1. `metrics/` — retorno diario/acumulado, Sharpe, Sortino, Calmar, max drawdown
   y duración, VaR, win rate, profit factor, expectancy, turnover, exposición,
   concentración (HHI), alpha/beta contra SPY; y las de agente (tokens, latencia
   p50/p95/p99, tok/s, reintentos, coste sombra, segundos por dólar de P&L).
2. `api/` — leaderboard, curvas de equity, detalle de agente, decisiones,
   operaciones, economía de agentes, salud del sistema, y SSE en vivo.
3. `main.py` — arranque, ciclo de vida, montaje del orquestador.
4. Docker Compose con TimescaleDB.

**Criterio de aceptación:** `GET /api/leaderboard` devuelve las 6 filas con
métricas coherentes tras un replay, y el SSE emite eventos al vuelo.

### Fase 3 — Dashboard ✅

Next.js + Tailwind: leaderboard, curvas superpuestas sobre SPY, detalle por
agente con su razonamiento, registro de operaciones filtrable, economía de
agentes y panel de salud.

### Verificación (los tres niveles del plan original)

1. **Tests unitarios del motor** — es donde un bug corrompe silenciosamente todas
   las métricas. Casos deterministas, números escritos a mano. Hecho para el
   ledger; **falta para `risk.py` y `execution.py`**.
2. **Ciclo en seco contra datos históricos** — orquestador en modo replay sobre
   un día pasado. Valida de una sentada fases, herramientas, persistencia y
   métricas, sin esperar a que abra el mercado.
3. **Sesión real en vivo** — con el mercado abierto: llegan quotes, las decisiones
   se persisten con tokens y coste, el dashboard refleja los cambios.

Y de forma permanente: **la curva del Buy & Hold SPY tiene que coincidir con el
índice** (§4.7).

---

## 7. Entorno de desarrollo (cómo retomar en frío)

### Restricciones de esta máquina

- El único intérprete instalado es `/usr/bin/python3` (**3.9.6**), y el proyecto
  pide **>=3.11**.
- **No hay Docker**, ni pyenv, ni conda, ni Homebrew Python, ni `uv` global.
- No hay venv en el repo, y no debe haberlo.

### Montar el entorno de tests

```bash
cd /tmp                       # o el scratchpad de la sesión
python3 -m venv bootstrap
./bootstrap/bin/pip install uv
./bootstrap/bin/uv venv --python 3.12 venv312
VIRTUAL_ENV=$PWD/venv312 ./bootstrap/bin/uv pip install \
  numpy "pandas>=2.2" pandas-market-calendars "pydantic>=2.9" pydantic-settings \
  "sqlalchemy[asyncio]>=2.0.36" aiosqlite greenlet alembic httpx structlog orjson \
  psutil fastapi "uvicorn[standard]" sse-starlette tenacity "apscheduler<4" \
  websockets pytest pytest-asyncio freezegun ruff
```

### Ejecutar los tests

```bash
cd backend
PYTHONPATH=. /ruta/al/venv312/bin/python -m pytest -q
```

Estado esperado: **45 passed**.

### Frontend

Node 22+ (aquí hay 24). El build se verifica sin backend levantado:

```bash
cd frontend
npm install
npm run build          # comprueba tipos y compila
npm run dev            # http://localhost:3000
```

Apunta al backend con `NEXT_PUBLIC_API_URL` (por defecto
`http://localhost:8000/api`). Next inlina esa variable en el bundle de cliente,
así que en Docker se pasa como build arg, no en runtime.

### Servicios externos

- **PostgreSQL + TimescaleDB** — todavía no hay `docker-compose.yml`. Los tests
  no lo necesitan (usan SQLite vía `aiosqlite`); el sistema en marcha sí.
- **`llama-server`** — no hace falta para tests ni para un replay sintético.
  `settings.market_data_offline=true` fuerza el proveedor sintético.

---

## 8. Modelo de datos

14 tablas en `app/db/models.py`. Las marcadas *(hypertable)* usan TimescaleDB;
sobre Postgres pelado el sistema funciona igual, solo pierde chunking y
compresión.

| Tabla | Contenido |
|---|---|
| `agents` | slug, nombre, tipo (LLM/BASELINE), runtime, prompt de estrategia, modelo, config, color, activo |
| `portfolios` | agent_id, cash, capital inicial, P&L realizado, comisiones pagadas |
| `positions` | symbol, cantidad, precio medio de entrada, P&L realizado |
| `orders` | symbol, lado, cantidad, tipo, estado, decisión que la originó, motivo de rechazo |
| `fills` | precio, cantidad, slippage, comisión, precio de referencia y timestamp del quote |
| `decisions` | fase, tipo, estado, modelo, tokens in/out, ms de inferencia, espera en cola, tok/s, tool calls, reintentos, coste sombra, coste energético, razonamiento, salida estructurada, respuesta cruda |
| `market_bars` *(hypertable)* | OHLCV diario del universo |
| `market_quotes` *(hypertable)* | quotes en vivo (comprimida a partir de 7 días) |
| `news_items` | noticias por símbolo |
| `universe_members` | constituyentes del S&P 500 + caché de señales para el screener |
| `equity_snapshots` *(hypertable)* | equity, cash, valor de posiciones, exposición por agente |
| `metrics_daily` | todas las métricas diarias por agente |
| `system_events` | transiciones de fase, errores, salud |
| `job_runs` | cada job programado deja traza; alimenta la métrica de éxito de jobs |

La migración `0001` construye el esquema desde los metadatos de SQLAlchemy, así
que la revisión inicial no puede divergir de `models.py`. A partir de ahí,
migraciones normales. (Contrapartida: `alembic upgrade --sql` offline no funciona
para esa revisión.)

---

## 9. Catálogo de métricas

**Financieras** — curva de equity, P&L realizado y no realizado, retorno diario y
acumulado, Sharpe, Sortino, Calmar, max drawdown y su duración, win rate, profit
factor, expectancy, exposición bruta y neta, turnover, concentración, alpha y
beta contra SPY, VaR.

**Por operación** — entrada, salida, holding period, P&L, slippage, comisión,
agente responsable y el razonamiento que la motivó.

**De los agentes** — tokens in/out por decisión y acumulados, tiempo de
inferencia, tok/s, latencia p50/p95/p99, decisiones por hora, distribución de
acciones, tool calls por decisión, reintentos por salida inválida, **coste
sombra** (lo que habría costado en la API frontera) y **tiempo de cómputo por
dólar de P&L**, el equivalente local a si el agente se paga a sí mismo.

**De mercado** — precios del universo, volatilidad, rendimiento por sector.

**Del sistema** — uptime, éxito de jobs, lag de datos, tamaño de la base de
datos, consumo del rate limit del proveedor, y salud de la inferencia local:
memoria del modelo, profundidad de la cola y si el modelo sigue residente.

---

## 10. Riesgos conocidos

Tres de estos son limitaciones reales del enfoque, no cosas que se arreglen
escribiendo mejor código.

**El modelo local puede no dar la talla.** Es el riesgo principal y por eso la
Fase 0 existía. Se ha construido por delante de esa validación, así que sigue
abierto. Salida: esquema híbrido, ya preparado por la abstracción de proveedor.

**Portátil contra 24/7.** No se resuelve con software. El sistema tolera
interrupciones limpiamente, pero la continuidad real pide una máquina siempre
encendida.

**Datos fundamentales limitados.** El tier gratuito es fuerte en precios y flojo
en fundamentales, lo que afecta sobre todo al agente Value. Se mitiga con proxies
derivados del precio, pero ese agente juega con menos información y eso sesga la
comparación en su contra. Tenlo presente al leer el leaderboard.

**Significancia estadística.** El más fácil de olvidar y el más importante para
interpretar bien los resultados: con seis portfolios y unos pocos meses de datos,
las diferencias de rentabilidad son **en su mayoría ruido, no habilidad**. El
sistema sirve para observar comportamiento, generar hipótesis y comparar coste
contra resultado — no para concluir que una estrategia es mejor que otra. Por eso
los baselines importan tanto: son la referencia que permite distinguir señal de
azar.

---

## 11. Bitácora de decisiones

Solo decisiones con un porqué. La actividad ya está en git.

| Fecha | Decisión | Motivo |
|---|---|---|
| 2026-09-27 | Ledger interno propio en vez de un proveedor de paper trading | Ningún proveedor deja abrir 6 cuentas cómodamente; además habilita backtesting con el mismo motor |
| 2026-09-27 | Gramática GBNF solo en la respuesta final, no en el bucle de herramientas | Una gramática fijada al esquema de respuesta haría imposible emitir una tool call |
| 2026-09-27 | Límites de riesgo sobreescribibles por agente (`agents.config`) | El tope global del 20% por posición rompería el Buy & Hold, que necesita el 100% para seguir al índice |
| 2026-09-27 | Acciones fraccionadas activadas por defecto | Sin ellas el Buy & Hold arrastra cash residual y su retorno deja de replicar el índice, que es justo lo que valida el sistema |
| 2026-09-27 | Enums como VARCHAR + CHECK (`native_enum=False`) | Añadir un valor pasa a ser una migración normal en vez de un `ALTER TYPE` |
| 2026-09-27 | La revisión Alembic 0001 se construye desde los metadatos | Garantiza que el esquema inicial no pueda divergir de `models.py` |
| 2026-09-27 | Proveedor sintético como ciudadano de primera | Permite ejercitar el sistema entero sin API key y sin gastar presupuesto de peticiones |
| 2026-09-28 | Este documento | El plan vivía solo en el transcript de una sesión; una pausa lo habría perdido |
| 2026-09-28 | El riesgo dimensiona contra el precio de fill esperado, no el de referencia | Sin ello cualquier orden que gaste el 100% del efectivo falla en el ledger por los 5 bps de slippage — y el Buy & Hold es justo el agente que siempre lo hace. Rompía el test de cordura del §4.7 |
| 2026-09-28 | `PRAGMA foreign_keys=ON` al abrir SQLite | SQLite ignora `ON DELETE CASCADE` por defecto, así que retirar un agente dejaba huérfanos su cartera, decisiones y órdenes — y una ejecución arrastraba datos de la anterior |
| 2026-09-28 | El upsert trocea por límite de parámetros | SQLite y Postgres topan el número de variables por sentencia; un backfill del universo lo supera con holgura |
| 2026-09-28 | El bus expone `listen()` (cola) además de `subscribe()` (generador) | Cancelar el `__anext__()` de un generador asíncrono lo rompe para siempre: el primer keepalive del SSE mataba el stream cada 15 segundos |
| 2026-09-28 | El paseo sintético se ancla a un epoch fijo | Con un paseo relativo a la ventana pedida, el quote y la barra del mismo día discrepaban, y una ejecución offline mostraba un salto de equity que parecía un bug del motor y no lo era |
| 2026-09-28 | El rollup ensancha la ventana hasta el inicio contiguo | Si faltaban días previos en `metrics_daily`, Sharpe y drawdown describían la ventana en vez de la vida del agente. Sin error visible: un drawdown de -4% en una cartera que perdía el 25% |
| 2026-09-28 | Dashboard solo en modo oscuro | El color de cada agente vive como un único hex en la base de datos; un modo claro significaría enviar una paleta sin validar contra superficie clara. Un modo validado es mejor que dos donde uno es a ojo |
| 2026-09-28 | Reglas `UP*` de ruff ignoradas en vez de aplicadas | Son 240 anotaciones en todo el repo sin ganancia de comportamiento, y `UP042` (`StrEnum`) cambiaría cómo se serializan los enums en la base de datos: eso es una pregunta de esquema, no de estilo |
| 2026-09-28 | Cliente LLM falso en `tests/fake_llm.py` | El runtime LLM era el módulo más grande sin cobertura. Un doble determinista prueba el bucle de herramientas, los reintentos por salida inválida y la instrumentación sin necesitar servidor de inferencia |
