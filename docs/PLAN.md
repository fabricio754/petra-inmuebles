# Plan Massi Crédito

Bot de WhatsApp que capta propietarios con inmuebles publicados y los lleva a
Sureti para crédito con garantía hipotecaria. Reemplaza el flujo anterior de
Arrayanes.

Reglas: se trabaja en la rama `claude/sharp-einstein-weou7g`; nada entra a
`main` sin autorización del usuario. Nunca se piden contraseñas, tokens ni
llaves por el chat.

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

## Flujo end-to-end

### 0. Identidad de marca en WhatsApp (prerequisito del primer contacto)

Los mensajes de Massi tienen que llegar con la ficha de negocio verificada
—como la de "Viajes Éxito Ofertas"— para que el propietario abra el chat con
confianza y las tasas de respuesta sean sanas. Esto es configuración de la
cuenta en Meta, no código:

- **Cuenta oficial de WhatsApp Business** (API) ligada al número de producción.
- **Verificación de Meta Business** (check azul): completar Meta Business
  Verification en Business Manager (documentos de la empresa).
- **Perfil del negocio** con logo cuadrado de Massi, nombre comercial
  ("Massi Crédito" o el acordado), categoría, descripción, correo, sitio web
  y dirección.
- **Páginas vinculadas** de Facebook e Instagram del mismo negocio para que
  el seguidor count aparezca en la ficha (como los `208K` / `101K` del
  ejemplo). Vincular desde Meta Business Suite → Settings → Business assets.
- **Opt-in de marketing**: la primera plantilla llega con la tarjeta
  "You are getting offers and announcements from this business" y los
  botones **Stop** / **Profile**. Para habilitarlo, aceptar los términos de
  Marketing Messages en WhatsApp Manager y usar categoría MARKETING en la
  plantilla (`massi_apertura_a` ya está en Marketing).
- **Tier y calidad**: empezar en Tier 250/día; subir de tier solo si la
  calidad del número se mantiene en verde en WhatsApp Manager → Phone numbers.
- **Botón "Stop"**: Meta ya lo pone solo en mensajes de marketing; cuando el
  usuario lo toca, Meta deja de entregar marketing a ese número por 90 días
  y manda un webhook que el bot debe tratar como `set_no_contactar`
  (equivalente a STOP/BAJA/PARA/SALIR).

Listo cuando: un número de prueba recibe la primera plantilla y ve la ficha
con check azul, logo, seguidores de FB e IG, botón Stop y botón Profile.

### 1. Captación (los 4 canales convergen en la tabla `contactos`)

1. **Actor Apify** (`actor/`, cloud, cada 2 h) — scrapea Finca Raíz y
   Metrocuadrado con Playwright y hace POST a `/scraper/ingest` con header
   `X-Ingest-Token`. Actor `c5Dx4kjZENfbeKYi1`, build `0.0.12` SUCCEEDED.
2. **Extensión Chrome "Enviar a Massi"** (`extension/`, manual) — la persona
   navega el portal, llena el formulario del anuncio y la extensión captura lo
   que el portal ya muestra (teléfono revelado por el usuario). Entra por
   `/captacion` y usa `app/captacion.py:procesar()`.
3. **Scraper local** (`app/scraper.py`, flag `SCRAPER_ENABLED=true`) —
   Playwright headless + 2captcha para PropDirecto, Metrocuadrado, Finca Raíz,
   Ciencuadras. Respaldo si Apify no cubre.
4. **Google Sheet** (`.github/workflows/import-sheets.yml`, disparo manual) —
   lee filas (Tipo, Ciudad, Operación, Zona/Conjunto, Teléfono, Torre, Piso,
   Metros, Parqueadero) del Sheet
   `1NDG1YAW1f9eD1Jp2lP0yQ57zNcI8PJkfRWihYC9_2Wk`, normaliza el teléfono
   colombiano y hace POST a `/scraper/ingest` como `portal=manual_sheet`.
   Autenticación por Service Account; fallback a API key.

**Filtros compartidos (en `app/captacion.py`):**
- Ciudad cubierta por Sureti: Bogotá, Medellín, Barranquilla, Cartagena,
  Santa Marta, Cúcuta, Chía.
- Zonas excluidas de Bogotá: San Cristóbal, Ciudad Bolívar.
- Tipos aceptados: apartamento, casa, local, oficina, bodega, lote.
- Estrato ≠ 1, precio ≥ $50M, monto resultante entre $20M–$800M.
- Dedup por teléfono y respeto a `no_contactar`.

### 2. Primer contacto (plantilla Meta)

- Plantilla `massi_apertura_a` (Marketing, 3 variables: tipo, portal, monto
  "hasta" en millones) con botones "Quiero saber más" / "No me interesa".
- Monto "hasta": 40 % del precio publicado si es residencial, 30 % si es
  comercial.
- Horario Ley 2300: L–V 7:00–19:00 y sáb 8:00–15:00, con límite diario
  escalonado.
- Envío desde el número oficial (API de Meta).

### 3. Autorización de datos (Ley 1581)

Botones Acepto / No acepto, menciona a Sureti y enlaza `/privacidad` (política
servida por el bot; responsable y contacto por variables `POLITICA_RESPONSABLE`
y `POLITICA_CONTACTO`). Respuesta ambigua → se repite la pregunta.
"No acepto" / STOP / BAJA / PARA / SALIR → `set_no_contactar` (upsert en
`contactos`).

### 4. Flow 1 — `Credito requisitos` (ID `1752916979273486`)

Ubicación del inmueble (dirección, apto, barrio, ciudad) + 4 preguntas de
descarte:
- Hipoteca o embargo.
- Patrimonio de familia con menores.
- Propietario mayor de 75.
- Puede ponerse al día con predial / servicios / administración.

Resultado: `descartado_<motivo>`, `pausado_paz_salvo` (remarketing a 30 días) o
sigue con `requiere_paz_salvo` marcado.

### 5. Flow 2 — `Credito datos` (ID `4394652567511716`)

Nombre, cédula, correo, confirmar dirección. Lead queda en `pipeline`.

Ambos Flows están **publicados**: no se pueden editar; para cambiarlos se crea
un Flow nuevo y se cambia el ID en Render (`META_FLOW_REQUISITOS_ID`,
`META_FLOW_DATOS_ID`, `META_FLOWS_MODE=published`). WhatsApp para Mac no abre
Flows; probar en celular.

### 6. Documentos

- `app/docs_auto.py`: consulta CHIP automático por dirección con ArcGIS de
  Catastro Bogotá (sin pedir predial al usuario).
- Recepción de fotos/PDF por WhatsApp (media de Meta → disco → tabla
  `documentos`).
- `app/parser.py`: predial con pdfplumber; imágenes con la API de Claude
  (requiere `ANTHROPIC_API_KEY` en Render — **pendiente**).
- Certificado de tradición (CTL): el bot avisa al usuario con matrícula y
  enlace oficial; el usuario lo compra (COP 23.000, PSE) solo para leads
  calificados y lo reenvía al bot.

### 7. Sureti

- `app/sureti.py`: registra el lead + sube documentos, consulta estado cada
  2 h (`app/scheduler.py`), sube documentos de fase 2.
- Credenciales en variables de entorno de Render (`SURETI_EMAIL`,
  `SURETI_PASSWORD`) — **pendientes**.
- Si el acceso falla dos veces: aviso al usuario por WhatsApp.
- Antes del piloto: abrir `agentes.sureti.co` con F12 y confirmar que los
  selectores de `_fill`, `_select_or_fill`, `_check_radio` sigan vigentes.

### 8. Remarketing

- No respondedores: días 3, 7, 15 (`app/remarketing.py`).
- Pausados paz y salvo: días 15, 30 (`_procesar_paz_salvos`).

### 9. Desembolso y comisión

- `scheduler.revisar_leads()` detecta estado DESEMBOLSADO en Sureti.
- Notifica al cliente por WhatsApp y registra la comisión:
  - 3,5 % para $15M–$99M.
  - 3 % para $100M–$399M.
  - 2,5 % para $400M+.
- Funciones: `db.calcular_comision()`, `db.registrar_desembolso()`,
  `db.marcar_comision_cobrada()`.
- Cobro real en 4 cuotas: manual, post-piloto.

## Infraestructura

- Render plan Starter (0.5c-512mb) + Postgres `massi-db` (0.1c-256mb, PG 18,
  Virginia). ≈ USD 13/mes.
- Start command:
  `gunicorn -w 1 --threads 4 --timeout 120 -b 0.0.0.0:$PORT app.server:app`.
- Build command (incluye Playwright):
  `pip install -r requirements.txt && playwright install chromium`.
- Disco persistente montado en `/var/data/media`.
- Tablas: `contactos`, `sesiones`, `pipeline`, `documentos`, `remarketing`.
- Export diario a Sheet a las 6am (pestañas Pipeline + Captacion) via
  `app/export.py`.
- MCP de Render disponible (`mcp__Render__*`) para servicios, logs y env vars.
  No tiene `list_env_vars`: para leer secrets usar el dashboard.
- `render.yaml` es el blueprint reproducible con todas las variables de
  entorno documentadas (las sensibles marcadas `sync: false`).
- Secret File en Render: `google-credentials.json` en Settings → Secret Files.

**Variables de entorno críticas pendientes de setear en Render:**
`SURETI_EMAIL`, `SURETI_PASSWORD`, `ANTHROPIC_API_KEY`, `CAPTURA_TOKEN`,
`INGEST_TOKEN`, `TWOCAPTCHA_API_KEY` (si se activa scraper local).

**Despliegue del actor Apify:**
- Opción A: repo público temporal → source "Git repository" →
  `fabricio754/petra-inmuebles`, directorio `actor`.
- Opción B: pegar los 5 archivos de `actor/` en el Web IDE del actor.
- Al construir: configurar `ingest_url` + `ingest_token` y schedule cada 2 h
  con `isExclusive: true`.
- Bug 401 ya resuelto: `"isSecret": true` en `actor/.actor/input_schema.json`.

**Activación final:** confirmar aprobación de la plantilla `massi_apertura_a`
en WhatsApp Manager y poner `ENVIO_AUTOMATICO=true`.

## Piloto (500 contactos Bogotá)

Objetivo: medir respuesta, calificación, envíos a Sureti, aprobaciones y
calidad del número. Decisión posterior: más volumen, más ciudades/Metrocuadrado
o un segundo número.

## Seguridad

- Render API keys viejas revocadas; no queda ninguna.
- `.gitignore` bloquea `google-credentials.json`, `*credentials*.json`,
  `*service-account*.json`, `.env.local`.
- `render.yaml` documenta todas las vars de entorno.

**Pendientes (acciones manuales, 5 min):**
1. Revocar GitHub PAT viejo (github.com → Settings → Developer settings →
   Personal access tokens).
2. Verificar que la llave JSON de Google viva solo en
   `/etc/secrets/google-credentials.json` (Render Secret File) y no en Sheets,
   email ni otro directorio accesible.
3. Borrar la Sheet antigua en el Drive de Petra Secondaries (vaciar papelera).

## En pausa hasta tener permiso o concepto

- Automatizar Supernotariado (CAPTCHA): solo con permiso escrito. Antes,
  preguntar a Sureti si ellos obtienen el CTL.
- Envío multi-número (hasta 7): solo si el piloto muestra buena calidad; nunca
  para evadir límites de Meta.
- Consultar predial con la cédula de un tercero: solo con la autorización de
  tratamiento de datos aceptada por el propietario.

## Costos mensuales estimados del piloto

| Concepto | Costo |
|---|---|
| Render Starter + Postgres | USD 13 |
| Mensajes de Meta (500) | ≈ USD 7 |
| Certificados de tradición (~140 leads) | ≈ COP 3,2 M |
| API de Claude (parser) | pocos USD |
| Actor Apify (runs cada 2 h) | ≈ USD 0,25–0,50 por run de 5 contactos |
