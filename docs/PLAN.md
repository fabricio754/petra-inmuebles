# Plan Massi Crédito

Aprobado por el usuario el 2 oct 2026. Reemplaza el menú de Arrayanes por el flujo de crédito
con Sureti. Hitos 1–4 (bot en vivo, Google Sheets, número de producción, Flow) ya están hechos:
ver `docs/TRASPASO.md`.

Reglas: se trabaja en la rama `claude/busy-albattani-etg12j`; nada entra a `main` sin
autorización del usuario. Nunca se piden contraseñas, tokens ni llaves por el chat.

---

## Próximos pasos (orden estricto)

| # | Acción | Quién | Estado |
|---|--------|-------|--------|
| 1 | Apify: click **Build** en console.apify.com/actors/c5Dx4kjZENfbeKYi1/source | Usuario | ⏳ pendiente |
| 2 | Apify: test E2E (metrocuadrado, max 5, timeout 300 s) — verificar 200 en Render | Claude | ⏳ esperando build |
| 3 | Apify: configurar schedule cada 2 h, 4 portales, max 300 | Claude + Usuario | ⏳ esperando build |
| 4 | Render: agregar `ANTHROPIC_API_KEY` en env vars | Usuario | ⏳ pendiente |
| 5 | Render: agregar `SURETI_EMAIL` y `SURETI_PASSWORD` en env vars | Usuario | ⏳ pendiente |
| 6 | Sureti: verificar selectores CSS en agentes.sureti.co con F12 | Usuario | ⏳ pendiente |
| 7 | Meta: confirmar que `massi_apertura_a` esté aprobada en WhatsApp Manager | Usuario | ⏳ pendiente |
| 8 | Render: activar `ENVIO_AUTOMATICO=true` (solo después de #7) | Usuario | ⏳ pendiente |
| 9 | GitHub: hacer el repo privado nuevamente | Usuario | ⏳ pendiente |
| 10 | GitHub: revocar PAT viejo (Settings → Developer settings → Tokens) | Usuario | ⏳ pendiente |
| 11 | Google: borrar Sheet de Petra Secondaries Drive + vaciar papelera | Usuario | ⏳ pendiente |
| 12 | Piloto: 500 contactos Bogotá — medir respuesta, calificación, aprobaciones | Ambos | ⏳ post-config |

---

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

### Decisión de arquitectura
Los portales solo muestran el teléfono tras un formulario (+ reCAPTCHA en Metrocuadrado).
Solución: actor Apify en `actor/` que scrapea páginas de particular directo, extrae teléfonos
y hace POST a `/scraper/ingest`. El endpoint filtra duplicados, aplica reglas de negocio y
despacha la plantilla de apertura.

### Código — HECHO
- `actor/src/main.py`: scraper Playwright para Metrocuadrado, FincaRaíz, Ciencuadras,
  PropDirecto. Lee input via `apify-client`, desencripta secrets con AES-256-GCM si aplica.
- `actor/requirements.txt`: `apify-client`, `cryptography`, `httpx`.
- `actor/.actor/input_schema.json`: sin `isSecret` — token llega en claro.
- `app/server.py` → `POST /scraper/ingest`: recibe payload, llama `scraper._guardar()`.
- `app/captacion.py`: normaliza teléfono, precio, tipo, ciudad; filtra duplicados y no_contactar.
- `app/envios.py`: envía plantilla respetando Ley 2300 (L–V 7–19, sáb 8–15) y límite diario.
- `app/scraper.py → _guardar()`: persiste en `contactos`, llama `envios.enviar_pendientes()`.

### Pendiente (acciones manuales)
1. **Build**: ir a console.apify.com/actors/c5Dx4kjZENfbeKYi1/source → click **Build**.
   El auto-build desde GitHub está roto (todos los builds son `origin: WEB`). Último build
   exitoso: `0.0.10` (commit `b962cb2`, 5 oct 2026 00:52 UTC). Código actual: commit `a7e04dd`.
2. **Test E2E**: Claude dispara `portals=["metrocuadrado"]`, `max_per_portal=5`, `timeout=300s`
   y verifica en los logs de Render que llega un POST 200.
3. **Schedule**: cada 2 h, 4 portales, `max_per_portal=300`. Configurar en Apify Console
   (no hay MCP de builds/schedules disponible).
4. **Plantilla**: variable `META_PLANTILLA_APERTURA=massi_apertura_a` ya debe estar en Render.
   `ENVIO_AUTOMATICO=true` solo tras confirmar aprobación de Meta.

### Observación — Extensión Chrome (alternativa descartada por ahora)
La extensión en `extension/` sigue siendo una alternativa si Apify es bloqueado por los portales.
No requiere desarrollo adicional; solo instalarla y operarla manualmente.

## Hito 8 — Documentos

### Código — HECHO
- `app/docs_auto.py → obtener_chip()`: consulta ArcGIS de Catastro Bogotá (IDECA), sin API key.
  Solo funciona para Bogotá; devuelve `None` para otras ciudades.
  Se llama automáticamente al confirmar dirección en el Flow de datos (`bot.py:195`).
- `app/media.py → download_and_save()`: descarga media de Meta API, guarda en disco,
  registra en tabla `documentos`.
- `app/parser.py → parsear_predial()`: extrae avalúo, dirección y matrícula del recibo de predial.
  Estrategia: pdfplumber → regex → Claude vision (fallback si PDF escaneado o imagen).
- `app/bot.py`: recibe PDF/imagen, llama a `parser.parsear_predial()`, actualiza DB, responde
  con los datos extraídos (`bot.py:423–441`).

### Pendiente
1. **`ANTHROPIC_API_KEY`** en Render: necesaria para el fallback de visión en `parser.py`.
   Sin ella, los PDFs escaneados y las fotos del predial no se parsean.
2. **Certificado de tradición (CTL)**: el bot ya avisa al usuario con matrícula y enlace oficial
   (SNR). El usuario lo compra (COP 23.000, PSE) y lo reenvía. La recepción está implementada
   como documento genérico en `bot.py:463`. No se requiere código adicional.
3. **Prueba end-to-end**: enviar un PDF de predial real por WhatsApp al bot y verificar que
   devuelva avalúo y matrícula correctos.

## Hito 9 — Sureti

### Código — HECHO
- `app/sureti.py`: `SuretiSession` con Playwright sincrónico.
  - `registrar_lead(data, docs)`: llena el formulario `/nuevo-lead` (nombre, cédula, email,
    teléfono, ciudad, dirección, matrícula, valor solicitado, tipo de persona) y extrae el ID.
  - `consultar_estado(sureti_lead_id)`: navega a `/leads/{id}` y extrae estado, monto aprobado
    o razón de rechazo.
  - Modo DRY-RUN automático si no hay credenciales.
- `app/scheduler.py → registrar_nuevos()`: registra en Sureti los leads `NUEVO` sin
  `sureti_lead_id`. Se ejecuta cada 2 h.
- `app/scheduler.py → revisar_leads()`: consulta estado de leads `REGISTRADO` o `EN_ESTUDIO`.
  Si hay cambio notifica al cliente (aprobado, rechazado, desembolsado) y registra comisión.

### Pendiente
1. **`SURETI_EMAIL` y `SURETI_PASSWORD`** en Render: sin estas variables el módulo opera en
   DRY-RUN y no registra nada real.
2. **Verificar selectores CSS** (acción manual, ≈15 min):
   - Abrir agentes.sureti.co con F12 → Network + Elements.
   - Ir al formulario de nuevo lead y confirmar que los selectores en `sureti.py`
     (`input[name='name']`, `input[name='cedula']`, `select` para matrícula, etc.) coincidan.
   - Si alguno difiere, actualizar en `sureti.py` y hacer push.
3. **Prueba en staging**: con DRY-RUN desactivado, registrar 1 lead real y confirmar que
   aparece en el portal de Sureti.

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

Checklist previo al piloto (en orden):
  1. Build Apify + test E2E (Hito 7).
  2. `ANTHROPIC_API_KEY` en Render (Hito 8).
  3. `SURETI_EMAIL` + `SURETI_PASSWORD` en Render + verificar selectores (Hito 9).
  4. Confirmar aprobación de `massi_apertura_a` en WhatsApp Manager → Meta Business Suite.
  5. Activar `ENVIO_AUTOMATICO=true` en Render (solo si plantilla aprobada).
  6. Schedule Apify activo (cada 2 h, 4 portales).
  7. Seguridad completa (Hito 12).
  8. Disparar manualmente el primer run con `max_per_portal=50` y monitorear 24 h.

## Hito 12 — Seguridad
- HECHO (2 oct 2026): Render API keys viejas revocadas; no queda ninguna.
- HECHO (3 oct 2026): `.gitignore` actualizado — bloquea `google-credentials.json`,
  `*credentials*.json`, `*service-account*.json`, `.env.local`, etc.
- HECHO (3 oct 2026): `render.yaml` — blueprint de deploy reproducible con todas las
  variables de entorno documentadas.

Pendiente (acciones manuales, 10 min):
  1. **GitHub PAT viejo**: github.com → Settings → Developer settings →
     Personal access tokens → revocar el token que tenía acceso a este repo.
  2. **Repo privado**: GitHub Settings del repo → Change visibility → Private.
     (Fue hecho público temporalmente para conectar Apify; el auto-build no funcionó,
     pero el repo ya no necesita ser público.)
  3. **Google credentials**: ya apunta a `/etc/secrets/google-credentials.json`
     (Render Secret File). Verificar que NO esté en otro directorio, la Sheet ni el email.
  4. **Sheet de Petra Secondaries**: Google Drive de esa cuenta → encontrar la Sheet →
     borrarla → vaciar papelera.

---

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
