# Plan Massi Crédito

Aprobado por el usuario el 2 oct 2026. Reemplaza el menú de Arrayanes por el flujo de crédito
con Sureti. Hitos 1–4 (bot en vivo, Google Sheets, número de producción, Flow) ya están hechos:
ver `docs/TRASPASO.md`.

Reglas: se trabaja en la rama `claude/sharp-einstein-weou7g`; nada entra a `main` sin
autorización del usuario. Nunca se piden contraseñas, tokens ni llaves por el chat.

**Estado al 5 oct 2026** — Hito 7 EN PRUEBA. Siguiente acción: E2E test del actor Apify.
Ver sección de Hito 7 para instrucciones exactas.

## Entorno de Claude al iniciar la conversación

- **Todas las APIs y MCPs están conectados** y listos para usar: GitHub, Render,
  Apify, Google Drive, Gmail, Google Sheets (vía Service Account), Claude Docs,
  Attio, Apollo.io, Gamma, entre otros. No hace falta pedir credenciales ni
  tokens al usuario: ya están cargados en el entorno (Render, GitHub secrets,
  variables de sesión).
- **Al iniciar cada conversación, Claude debe leer la documentación completa
  del tech stack** antes de proponer cambios o ejecutar acciones. Esto incluye:
  - `docs/PLAN.md` (este archivo) y `docs/TRASPASO.md`.
  - `README.md`, `render.yaml`, `requirements.txt`, `.env.example`.
  - El código bajo `app/` (bot, server, parser, scheduler, captacion,
    scraper, docs_auto, sureti, remarketing, envios, state, db, export,
    schema.sql).
  - `actor/` (código del actor Apify) y `extension/LEEME.md` (extensión Chrome).
  - `flows/` (JSON de los WhatsApp Flows publicados).
  - `.github/workflows/` (jobs de GitHub Actions, incl. `import-sheets.yml`).
  Objetivo: evitar reinventar lo que ya existe y mantener coherencia entre
  canales de captación, pipeline y automatizaciones.

## Hito 5 — Infraestructura (≈ USD 13/mes) — HECHO (3 oct 2026)
- Usuario: Render plan Starter (USD 7) + Postgres Basic-256mb (USD 6), guiado.
- Claude: tablas `contactos`, `sesiones`, `pipeline`, `documentos`, `remarketing`;
  `state.py` a Postgres sin cambiar sus funciones; migrar datos de la Sheet;
  exportación diaria a la Sheet para consulta.
- Listo: el bot responde sin la espera de 50 s y los datos viven en Postgres.
- Estado: Render plan 0.5c-512mb, Postgres `massi-db` (0.1c-256mb, PG 18, Virginia),
  `DATABASE_URL` en Render, Start Command
  `gunicorn -w 1 --threads 4 --timeout 120 -b 0.0.0.0:$PORT app.server:app`.
  Importó de la Sheet: 6 inmuebles, 2 sesiones, 5 contactos.
  Exportación diaria 6am → Sheet (pestañas Pipeline + Captacion): `app/export.py`, job en scheduler.
- MCP de Render: FUNCIONA (5 oct 2026). Se usa `mcp__Render__*` para consultar servicios,
  logs y env vars. No tiene `list_env_vars` — para leer secrets usar Render dashboard.

## Hito 6 — Bot de crédito (reemplaza Arrayanes) — HECHO (2 oct 2026, probado en vivo)
- Autorización de tratamiento de datos (Ley 1581) como primer paso.
- 4 preguntas de descarte: hipoteca/embargo, patrimonio de familia con menores,
  propietario mayor de 75, ponerse al día con predial/servicios/administración
  (marca `requiere_paz_salvo`).
- 4 datos: nombre, cédula, correo, confirmar dirección.
- Opción "NO" para no recibir más mensajes, en cualquier momento (+ STOP/BAJA/PARA/SALIR).
- Listo: un lead calificado queda en `pipeline` con todos sus datos.
- Preguntas en 2 WhatsApp Flows (pedido del usuario): `Credito requisitos` (ID 1752916979273486)
  y `Credito datos` (ID 4394652567511716), JSON en `flows/`. Render: `META_FLOW_REQUISITOS_ID`,
  `META_FLOW_DATOS_ID`, `META_FLOWS_MODE=published`. Ambos PUBLICADOS (3 oct 2026): no se
  pueden editar; para cambiarlos se crea un Flow nuevo y se cambia el ID en Render.
  Paso 1 = ubicación del inmueble (dirección, apto, barrio, ciudad) + 4 requisitos;
  Paso 2 = propietario (nombre, cédula, correo).
  WhatsApp para Mac no abre Flows; probar en el celular.
- El bot es SOLO de crédito
  (Arrayanes, catálogo, Publicar/Flow, pagos y crédito dummy eliminados, decisión del usuario).
  Autorización con botones Acepto/No acepto que menciona a Sureti y enlaza
  `/privacidad` (política servida por el bot; responsable y contacto por variables
  `POLITICA_RESPONSABLE` y `POLITICA_CONTACTO`). Respuesta ambigua → se repite la pregunta.
  Paz y salvo: No → `pausado_paz_salvo` (remarketing a 30 días); Sí → sigue con
  `requiere_paz_salvo`. Descartes quedan en pipeline como `descartado_<motivo>`.
  `set_no_contactar` hace upsert en `contactos` (teléfono hasta 150 caracteres por BSUID).

## Hito 7 — Captación multicanal — EN PRUEBA (5 oct 2026)

### Canales de captación (los 4 convergen en la tabla `contactos`)

1. **Actor Apify** (`actor/`, cloud, cada 2 h) — scrapea Finca Raíz y Metrocuadrado
   con Playwright y hace POST a `/scraper/ingest`. Es el canal del Hito 7 en prueba.
2. **Extensión Chrome "Enviar a Massi"** (`extension/`, manual) — la persona navega
   el portal, llena el formulario del anuncio y la extensión captura lo que el
   portal ya muestra (teléfono revelado por el usuario). Entra por `/captacion` y
   usa `app/captacion.py:procesar()`.
3. **Scraper local** (`app/scraper.py`, flag `SCRAPER_ENABLED=true`) — Playwright
   headless + 2captcha para PropDirecto, Metrocuadrado, Finca Raíz, Ciencuadras.
   Pensado como respaldo si Apify no cubre.
4. **Google Sheet** (`.github/workflows/import-sheets.yml`, disparo manual) — lee
   filas (Tipo, Ciudad, Operación, Zona/Conjunto, Teléfono, Torre, Piso, Metros,
   Parqueadero) de un Sheet (default `1NDG1YAW1f9eD1Jp2lP0yQ57zNcI8PJkfRWihYC9_2Wk`),
   normaliza el teléfono colombiano y hace POST a `/scraper/ingest` como
   `portal=manual_sheet`. Autenticación por Service Account; fallback a API key.

### Filtros compartidos
- Ciudad cubierta por Sureti (Bogotá, Medellín, Barranquilla, Cartagena,
  Santa Marta, Cúcuta, Chía).
- Zonas excluidas de Bogotá: San Cristóbal, Ciudad Bolívar.
- Tipos aceptados: apartamento, casa, local, oficina, bodega, lote.
- Estrato ≠ 1, precio ≥ $50M, monto resultante entre $20M–$800M.
- Dedup por teléfono y respeto a `no_contactar`.

### Envío a Meta
- Plantilla `massi_apertura_a` (Marketing, 3 variables: tipo, portal, monto "hasta"
  en millones) con botones "Quiero saber más" / "No me interesa". En revisión de Meta.
- Horario Ley 2300: L–V 7:00–19:00 y sáb 8:00–15:00, con límite diario escalonado.
- Monto "hasta": 40 % del precio publicado si es residencial (apartamento, casa),
  30 % si es comercial (local, oficina, bodega, lote).

### Costos
- Actor Apify: ≈ USD 0,25–0,50 por run de 5 contactos de prueba.
- Mensaje marketing: ≈ USD 0,013 por mensaje (Colombia).

### Estado técnico
- Build `0.0.12` SUCCEEDED en Apify (commit `3f6548c` en `main`).
- Bug 401 resuelto: faltaba `"isSecret": true` en `actor/.actor/input_schema.json`.
  Sin ese flag Apify no inyecta `APIFY_ACTOR_INPUT_PRIVATE_KEY` y la desencriptación falla.
- `INGEST_TOKEN` ya está seteado en Render (Environment → `INGEST_TOKEN`).
- `APIFY_TOKEN` disponible en el environment de Claude Code.

### ⚠️ Próxima acción: E2E test

**Prerequisito:** agregar `INGEST_TOKEN` al environment de Claude Code:
  → Menú del entorno cloud (título de la sesión) → Edit → agregar variable `INGEST_TOKEN`
  → Abrir sesión nueva para que quede disponible como `$INGEST_TOKEN`

**Correr el actor** (tool `mcp__Apify__petra-secondaries--my-actor`, `waitSecs=0`):
```
portals: ["fincaraiz"]   # metrocuadrado puede bloquear IPs de Apify residential
max_per_portal: 5
ingest_url: https://petra-inmuebles.onrender.com
ingest_token: <leer de $INGEST_TOKEN>
```

**Verificar éxito:** en Render logs debe aparecer:
  `[Ingest] X/Y contactos guardados.`  (NO `no_autorizado`)

Una vez confirmado el 200, activar `SCRAPER_ENABLED=true` en Render y configurar
schedule en Apify (cada 2 h, `isExclusive: true`).

## Hito 8 — Documentos — CÓDIGO LISTO, PENDIENTE ANTHROPIC_API_KEY
- `app/docs_auto.py`: consulta CHIP por dirección con ArcGIS de Catastro Bogotá
  (sin pedir predial al usuario). Ya en `main`.
- Recepción de fotos/PDF por WhatsApp (media de Meta → disco → tabla `documentos`).
- `app/parser.py`: predial con pdfplumber; imágenes con la API de Claude
  (requiere `ANTHROPIC_API_KEY` en Render).
- Certificado de tradición (CTL): el bot avisa al usuario con matrícula y enlace oficial;
  el usuario lo compra (COP 23.000, PSE) solo para leads calificados y lo reenvía al bot.
- **Pendiente:** agregar `ANTHROPIC_API_KEY` en Render → Environment y activar el flujo.
- Listo cuando: un lead real recibe CHIP automático sin pedirle nada.

## Hito 9 — Sureti (automatización autorizada por Sureti)
- `app/sureti.py`: registrar lead + subir documentos, consultar estado cada 2 h
  (`app/scheduler.py`), subir documentos de fase 2.
- Credenciales en variables de entorno de Render (`SURETI_EMAIL`, `SURETI_PASSWORD`).
- Si el acceso falla dos veces: aviso al usuario por WhatsApp.
- Listo: 1 lead registrado y su estado actualizado sin intervención.

## Hito 10 — Seguimiento y comisión — HECHO (3 oct 2026)
- Remarketing no-respondedores (días 3, 7, 15) en `app/remarketing.py`.
- Remarketing paz y salvos (días 15, 30) en `app/remarketing.py` (`_procesar_paz_salvos`).
- Comisión: 3,5 % ($15M–$99M), 3 % ($100M–$399M), 2,5 % ($400M+). `db.calcular_comision()`,
  `db.registrar_desembolso()`, `db.marcar_comision_cobrada()`.
- `scheduler.revisar_leads()` detecta DESEMBOLSADO y notifica al cliente + registra comisión.
- Pendiente: cobro real en 4 cuotas (manual, post-piloto).

## Hito 11 — Piloto (500 contactos Bogotá)
- Medir respuesta, calificación, envíos a Sureti, aprobaciones y calidad del número.
- Decidir: más volumen, más ciudades/Metrocuadrado o un segundo número.

Pasos de configuración previos al piloto:
  1. En Render Dashboard: usar `render.yaml` como Blueprint o configurar manualmente.
     Build command: `pip install -r requirements.txt && playwright install chromium`
     Start command: `gunicorn -w 1 --threads 4 --timeout 120 -b 0.0.0.0:$PORT app.server:app`
     Disco persistente: montar en `/var/data/media`.
  2. Variables de entorno: completar todas las marcadas con `sync: false` en `render.yaml`.
     En especial: `SURETI_EMAIL`, `SURETI_PASSWORD`, `ANTHROPIC_API_KEY`, `CAPTURA_TOKEN`,
     `INGEST_TOKEN` (token que usará el actor Apify para POST /scraper/ingest).
  3. Secret File: subir `google-credentials.json` en Settings → Secret Files de Render.
  4. ⚠️ Verificar selectores de Sureti: abrir agentes.sureti.co con F12 abierto,
     ir al formulario de registro de lead y confirmar que los selectores en
     `app/sureti.py` (`_fill`, `_select_or_fill`, `_check_radio`) coincidan.
  5. Plantilla Meta `massi_apertura_a`: confirmar aprobación en WhatsApp Manager.
     Activar `ENVIO_AUTOMATICO=true` solo una vez aprobada.
  6. Actor Apify: el código está en `actor/` (rama actual). Para desplegarlo:
     Opción A (recomendada): hacer el repo público temporalmente en GitHub Settings,
     cambiar el source del actor (apify.com/actors/c5Dx4kjZENfbeKYi1/source) a
     "Git repository" → `fabricio754/petra-inmuebles`, directorio `actor`.
     Opción B: ir al Web IDE del actor y pegar los 5 archivos de `actor/` manualmente.
     Una vez construido: configurar input `ingest_url` + `ingest_token` y activar
     un schedule cada 2 h.
  7. Activar scraper local (opcional, solo si Apify no cubre): `SCRAPER_ENABLED=true`
     + `TWOCAPTCHA_API_KEY`.

## Hito 12 — Seguridad
- HECHO (2 oct 2026): Render API keys viejas revocadas; no queda ninguna.
- HECHO (3 oct 2026): `.gitignore` actualizado — bloquea `google-credentials.json`,
  `*credentials*.json`, `*service-account*.json`, `.env.local`, etc.
- HECHO (3 oct 2026): `render.yaml` — blueprint de deploy reproducible con todas las
  variables de entorno documentadas.

Pendiente (acciones manuales, 5 min):
  1. GitHub PAT viejo → github.com → Settings → Developer settings →
     Personal access tokens → revocar el token que tenía acceso a este repo.
  2. Llave JSON de Google → ya apunta a `/etc/secrets/google-credentials.json`
     (Render Secret File). Verificar que NO esté en otro directorio accesible
     ni en la Sheet ni en el email. Si está en otro lugar, bórralo de ahí.
  3. Sheet en Petra Secondaries → ir a Google Drive de esa cuenta, encontrar la
     Sheet y borrarla (vaciar también la papelera).

## En pausa hasta tener permiso o concepto
- Automatizar Supernotariado (CAPTCHA): solo con permiso escrito. Antes, preguntar a
  Sureti si ellos obtienen el CTL.
- Envío multi-número (hasta 7): solo si el piloto muestra buena calidad; nunca para
  evadir límites de Meta.
- Consultar predial con la cédula de un tercero: solo con la autorización del Hito 6.

## Costos mensuales estimados del piloto
| Concepto | Costo |
|---|---|
| Render Starter + Postgres | USD 13 |
| Mensajes de Meta (500) | ≈ USD 7 |
| Certificados de tradición (~140 leads) | ≈ COP 3,2 M |
| API de Claude (parser) | pocos USD |
