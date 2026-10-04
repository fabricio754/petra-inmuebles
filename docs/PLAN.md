# Plan Massi Crédito

Aprobado por el usuario el 2 oct 2026. Reemplaza el menú de Arrayanes por el flujo de crédito
con Sureti. Hitos 1–4 (bot en vivo, Google Sheets, número de producción, Flow) ya están hechos:
ver `docs/TRASPASO.md`.

Reglas: se trabaja en la rama `claude/busy-albattani-etg12j`; nada entra a `main` sin
autorización del usuario. Nunca se piden contraseñas, tokens ni llaves por el chat.

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
- MCP de Render: EN PAUSA. El conector oficial queda "Connected" pero toda llamada devuelve
  `unauthorized` (probado en 3 sesiones). Se sigue con capturas; reintentar más adelante.

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

## Hito 7 — Captación (rediseñado 3 oct 2026)
Los portales (Metrocuadrado, Finca Raíz) solo muestran el teléfono tras un formulario
(+ reCAPTCHA en Metrocuadrado): no se automatiza ese formulario ni un WhatsApp personal.
Solución acordada:
- Extensión de Chrome "Enviar a Massi": una persona navega el portal (formulario lleno una
  vez) y con un clic por anuncio la extensión toma teléfono (enlace wa.me), precio, ciudad,
  estrato, tipo, URL y foto, y los manda al bot (endpoint con clave secreta).
- El bot filtra (ciudad, estrato, tipo, duplicados, no_contactar), guarda en `contactos`
  y envía la plantilla desde el número oficial (API) solo L–V 7:00–19:00 y sáb 8:00–15:00
  (Ley 2300), con límite diario que sube poco a poco.
- Plantilla `massi_apertura_a` (Marketing, 3 variables: tipo, portal, monto "hasta" en
  millones) con botones "Quiero saber más" / "No me interesa". En revisión de Meta.
- Monto "hasta": 40 % del precio publicado si es residencial (apartamento, casa),
  30 % si es comercial (local, oficina, bodega, lote).
- Costo: ≈ USD 0,013 por mensaje de marketing (Colombia).

## Hito 8 — Documentos
- CHIP por dirección con ArcGIS de Catastro Bogotá (servicio público).
- Recepción de fotos/PDF por WhatsApp (media de Meta → disco → tabla `documentos`).
- `app/parser.py`: predial con pdfplumber; imágenes con la API de Claude
  (requiere `ANTHROPIC_API_KEY`).
- Certificado de tradición (CTL): el bot avisa al usuario con matrícula y enlace oficial;
  el usuario lo compra (COP 23.000, PSE) solo para leads calificados y lo reenvía al bot.
- Listo: un lead real con CHIP, CTL, predial y fachada.

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
