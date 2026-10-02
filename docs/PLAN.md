# Plan Massi Crédito

Aprobado por el usuario el 2 oct 2026. Reemplaza el menú de Arrayanes por el flujo de crédito
con Sureti. Hitos 1–4 (bot en vivo, Google Sheets, número de producción, Flow) ya están hechos:
ver `docs/TRASPASO.md`.

Reglas: se trabaja en la rama `claude/relaxed-wozniak-l1yxva`; nada entra a `main` sin
autorización del usuario. Nunca se piden contraseñas, tokens ni llaves por el chat.

## Hito 5 — Infraestructura (≈ USD 13/mes) — HECHO (2 oct 2026), falta la copia diaria a la Sheet
- Usuario: Render plan Starter (USD 7) + Postgres Basic-256mb (USD 6), guiado.
- Claude: tablas `contactos`, `sesiones`, `pipeline`, `documentos`, `remarketing`;
  `state.py` a Postgres sin cambiar sus funciones; migrar datos de la Sheet;
  exportación diaria a la Sheet para consulta.
- Listo: el bot responde sin la espera de 50 s y los datos viven en Postgres.
- Estado: Render plan 0.5c-512mb, Postgres `massi-db` (0.1c-256mb, PG 18, Virginia),
  `DATABASE_URL` en Render, Start Command
  `gunicorn -w 1 --threads 4 --timeout 120 -b 0.0.0.0:$PORT app.server:app`.
  Importó de la Sheet: 6 inmuebles, 2 sesiones, 5 contactos. Pendiente: copia diaria a la Sheet.
- MCP de Render: conector oficial de claude.ai (OAuth) conectado. Probar en sesión nueva con
  list_workspaces / list_logs sobre `srv-daraa5ad0e5s73dvng6g`.

## Hito 6 — Bot de crédito (reemplaza Arrayanes)
- Autorización de tratamiento de datos (Ley 1581) como primer paso.
- 4 preguntas de descarte: hipoteca/embargo, patrimonio de familia con menores,
  propietario mayor de 75, ponerse al día con predial/servicios/administración
  (marca `requiere_paz_salvo`).
- 4 datos: nombre, cédula, correo, confirmar dirección.
- Opción "NO" para no recibir más mensajes, en cualquier momento.
- Listo: un lead calificado queda en `pipeline` con todos sus datos.

## Hito 7 — Captación
- `app/scraper.py`: Finca Raíz Bogotá primero (luego Metrocuadrado y otras ciudades).
  Pre-filtro: ciudad cubierta, estrato (1 descartado; 2–3 con PH), tipo aceptado,
  no contactado antes. Deduplicar por teléfono. Guarda fotos (fachada).
- 3 plantillas A/B de primer mensaje (estimado 20–30 % del precio publicado) para
  aprobación de Meta. Identifican a Massi y ofrecen salir con "NO".
- Envío solo L–V 7:00–19:00 y sáb 8:00–15:00 (Ley 2300), volumen bajo al inicio,
  vigilando la calidad del número en WhatsApp Manager.
- Costo: ≈ USD 0,013 por mensaje de marketing (Colombia).
- Listo: 50 mensajes de prueba enviados; las respuestas entran al flujo del Hito 6.

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

## Hito 10 — Seguimiento y comisión
- Remarketing paz y salvos (días 15 y 30) y un solo recordatorio a quien no respondió,
  respetando Ley 2300.
- Comisión: 3,5 % ($15M–$99M), 3 % ($100M–$399M), 2,5 % ($400M+), en 4 cuotas
  (desembolso + 3 primeros pagos). Aviso al usuario al desembolso.
- Listo: una comisión de prueba registrada.

## Hito 11 — Piloto (500 contactos Bogotá)
- Medir respuesta, calificación, envíos a Sureti, aprobaciones y calidad del número.
- Decidir: más volumen, más ciudades/Metrocuadrado o un segundo número.

## Hito 12 — Seguridad (al final, por decisión del usuario)
- HECHO (2 oct 2026): Render API keys viejas revocadas; no queda ninguna (el MCP de Render usa el
  conector oficial con OAuth). Falta: confirmar que el GitHub PAT viejo está revocado.
- Mover la llave JSON de Google a un lugar privado.
- Borrar la Sheet creada por error en la cuenta de Petra Secondaries.

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
