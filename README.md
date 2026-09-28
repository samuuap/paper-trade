# paper-trade

Un banco de pruebas donde **varios agentes LLM compiten operando acciones US con
dinero simulado**, 24/7, cada uno con su estrategia y su propio portfolio.

El objetivo no es ganar dinero —es simulado— sino poder responder con datos:

- ¿Qué estrategia funciona mejor, y se distingue del ruido?
- ¿Cuántos tokens cuesta cada dólar de P&L?
- ¿Un modelo local bate a un simple buy & hold del S&P 500?
- ¿Compensa la latencia de correr en local frente a pagar una API frontera?

Por eso **la instrumentación es tan producto como el trading**: cada decisión
registra tokens, latencia, velocidad de generación, reintentos, coste sombra
(lo que habría costado en una API frontera) y coste energético.

> 📘 **El diseño completo, el estado del desarrollo y el trabajo pendiente están
> en [PLAN.md](PLAN.md).** Si vas a tocar el código, empieza por ahí.

---

## Cómo funciona, en un párrafo

Un orquestador con **máquina de fases** (pre-market, sesión, after-hours,
cerrado) da a los agentes un trabajo distinto según la hora: planificar el día,
operar cada 15 minutos, revisar el cierre o investigar de madrugada. Python
calcula los indicadores, filtra el S&P 500 y valida el riesgo; **el LLM solo
elige** entre un puñado de candidatos ya preprocesados, y su respuesta va
constreñida por una gramática GBNF que hace imposible una salida malformada.
Las órdenes se ejecutan contra un ledger propio con fills simulados a partir de
quotes reales más slippage y comisión. Dos baselines sin LLM —Buy & Hold SPY y
una regla de medias móviles— hacen que el leaderboard signifique algo.

---

## Arranque rápido

### Con Docker (recomendado)

```bash
cp .env.example .env          # añade tu FINNHUB_API_KEY, o deja MARKET_DATA_OFFLINE=true
docker compose up --build
```

- Dashboard: <http://localhost:3000>
- API y documentación: <http://localhost:8000/docs>

El contenedor del backend aplica las migraciones y siembra el roster al
arrancar, así que la primera ejecución deja el sistema listo.

**El servidor de inferencia no va en Docker.** llama.cpp necesita la GPU Metal
y un contenedor Linux en macOS no la alcanza; correrlo dentro caería a CPU y
sería inservible. Levántalo en el host:

```bash
llama-server -m ~/models/qwen3-14b-instruct-q4_k_m.gguf \
  --host 0.0.0.0 --port 8080 --ctx-size 8192
```

El backend lo busca en `host.docker.internal:8080` (configurable con
`LLM_BASE_URL`).

### Sin Docker

Necesitas Python **3.11+** y un PostgreSQL con TimescaleDB.

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

alembic upgrade head            # esquema + hypertables
python -m app.scripts.seed      # 6 agentes, 6 portfolios, universo S&P 500
uvicorn app.main:app --reload
```

### Sin API key y sin base de datos de producción

`MARKET_DATA_OFFLINE=true` sustituye Finnhub por un proveedor sintético
determinista: mismos precios para el mismo símbolo y día. Sirve para ejercitar
el sistema entero sin red y sin gastar presupuesto de peticiones.

---

## Estructura

```
backend/app/
├── core/          configuración, logging estructurado, bus de eventos (SSE)
├── db/            modelos SQLAlchemy, sesiones, migraciones Alembic
├── market/        proveedores, rate limiter, calendario y fases, indicadores,
│                  screener, repositorio de precios
├── engine/        ledger, reglas de riesgo, motor de ejecución con fills
├── llm/           cliente OpenAI-compatible, GBNF, schemas, cola, coste
├── agents/        contexto, herramientas, runtime instrumentado, estrategias,
│                  baselines, roster
├── metrics/       matemática financiera, rollup diario, consultas para la API
├── orchestrator/  máquina de fases, scheduler, ingesta, guardia de recursos
├── api/           rutas FastAPI + SSE
└── scripts/       seed y utilidades
frontend/          dashboard Next.js
```

---

## Tests

```bash
cd backend
pytest -q
```

La suite corre contra SQLite y un mercado sintético: no necesita red, ni
Postgres, ni servidor de inferencia. Incluye un **replay completo de un día de
mercado** que ejercita la máquina de fases, la ingesta, la ejecución y las
métricas de una sentada.

Un test merece mención aparte: **la curva de Buy & Hold SPY tiene que seguir al
índice**. Si diverge, el bug está en el motor o en las métricas, no en el
mercado. Es la alarma más fiable del sistema.

---

## API

Todo bajo `/api` (ver `/docs` para el detalle):

| Endpoint | Qué devuelve |
|---|---|
| `GET /leaderboard` | Ranking por retorno, con Sharpe, drawdown y coste |
| `GET /equity` | Curvas de equity de todos los agentes + benchmark rebasado |
| `GET /agents/{slug}` | Portfolio, posiciones abiertas e histórico diario |
| `GET /decisions` | Historial de decisiones con razonamiento e instrumentación |
| `GET /trades` | Registro de operaciones con su justificación y costes |
| `GET /orders/rejected` | Órdenes que el riesgo rechazó, y por qué |
| `GET /economics` | Tokens, latencias p50/p95/p99, coste sombra, s/$ de P&L |
| `GET /health` | Datos, proveedor, inferencia, jobs, cola |
| `GET /stream` | Eventos en vivo por SSE |

---

## Cómo leer los resultados

Con seis portfolios y unos pocos meses de datos, **las diferencias de
rentabilidad son en su mayoría ruido, no habilidad**. El sistema sirve para
observar comportamiento, generar hipótesis y comparar coste contra resultado —
no para concluir que una estrategia es mejor que otra.

Por eso los baselines importan tanto: son la referencia que permite distinguir
señal de azar. Los demás riesgos conocidos están en
[PLAN.md §10](PLAN.md#10-riesgos-conocidos).
