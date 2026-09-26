# Traspaso v4 — Massi / Petra Inmuebles (WhatsApp MVP)

Pégale este archivo a Claude Code y dile: "Lee esto y continuemos donde quedamos."
Reemplaza a los traspasos anteriores (v1, v2, v3). Actualizado: 26 sep 2026.

## Ubicación

- Código: GitHub `fabricio754/petra-inmuebles` (privado). Rama `main` = lo que corre en Render.
- Render: `https://petra-inmuebles.onrender.com`, servicio `srv-daraa5ad0e5s73dvng6g`, plan Free.
  Despliega solo cada push a `main`.
- Proyecto SEPARADO de "Petra Secondaries". No mezclar (ni cuentas de Google, ni proyectos de Cloud).

## Producto

Massi = hub de WhatsApp con verticales en un solo bot y un solo número:
Inmobiliario ("Petra", sub-marca intencional), Crédito (dummy) y Pago de servicios (dummy).
Piloto: conjunto Arrayanes (el QR manda el texto `ARRAYANES`).

## Estado actual (todo probado en vivo el 26 sep 2026)

### Hito 1 — Bot en vivo: HECHO
### Hito 2 — Datos permanentes en Google Sheets: HECHO
- Sheet "Massi - Datos" en la cuenta PERSONAL de Gmail del usuario:
  `https://docs.google.com/spreadsheets/d/1VUeKNtD5TzmiHZPZ4GVZ0sJtt_e8cJDxBHVQJ3wd6zI`
- Pestañas que crea el bot solo: `Inventario` (sembrada con `data/inventario.seed.json`),
  `Sesiones`, `Contactos` (se crea con el primer lead).
- Se puede editar a mano (precios, disponibilidad); el bot lo ve en máx. 30 s.
  Entiende precios tipo "2.500.000".
- Google Cloud: proyecto `massi` (ID `massi-509819`), cuenta personal. API de Sheets habilitada.
  Cuenta de servicio `massi-bot@massi-509819.iam.gserviceaccount.com` (Editor de la Sheet).
- Render: Secret File `google-credentials.json` (llave JSON de la cuenta de servicio)
  y variable `GOOGLE_SHEET_ID`. Sin `GOOGLE_SHEET_ID` el bot usa los JSON de `data/` (modo local).
- Código: `app/state.py` (firmas iguales, `bot.py` no cambió). Librería `gspread`.
- Verificado: edición manual se refleja y las sesiones sobreviven un reinicio de Render.

### Hito 3 — Número de producción: HECHO
- Número +57 320 2813268, nombre visible "Petra Inmuebles", estado Conectado.
  - ID del número: `1325652213964548` (en Render: `META_PHONE_NUMBER_ID`)
  - Cuenta de WhatsApp Business (WABA): `1505990838003656`
  - Portafolio (Business): `922831663892602` — verificado.
  - Número de prueba viejo (NO borrado): ID `1327118233822523`, WABA `1021827157582493`.
- Registrado en la API con PIN de 6 dígitos (lo tiene el usuario; nunca en el chat).
- App "Petra Inmuebles" suscrita a la WABA nueva (webhook OK).
- Token permanente de usuario del sistema `petra-inmueblesbot` en `META_ACCESS_TOKEN`
  (el anterior se revocó porque se vio en una captura).
- La app de Meta sigue en "Modo: En desarrollo" y aun así recibe/envía mensajes reales.
- ERROR 139000 RESUELTO: el número de producción desbloqueó el WhatsApp Flow.

### WhatsApp Flow "Publicar mi inmueble"
- Flow nuevo en la WABA de producción: ID `1098781332903923` (en Render: `META_FLOW_PUBLICAR_ID`).
  Estado: BORRADOR (muestra "This flow is only for testing"). NO publicar sin aprobación.
- Flow viejo en la WABA de prueba: `1448752417166220` (ya no se usa).
- JSON respaldado en `flows/publicar_inmueble.json`. Una sola pantalla (FORM) que completa
  directo: con dos pantallas, el validador de Meta exige `number` y WhatsApp manda texto.
- Probado de punta a punta: formulario → autorización del propietario → "Tu inmueble quedó publicado".

## Arreglos de código de esta sesión (todos en `main`)
- `server.py` / `whatsapp.py`: soporte BSUID. Usuarios con nombre de usuario de WhatsApp
  llegan con `from_user_id` (ej. `CO.1107...`) en vez de `from`; se responde con `recipient`.
- `bot.py`: `_numero` acepta int/float (68.5 no se vuelve 685).
- `state.py`: Google Sheets (ver Hito 2).

## Pendiente, en orden
1. Hito 4 (SOLO con aprobación explícita del usuario):
   a. Borrar el formulario conversacional viejo (bloque `=== FALLBACK ===` en `app/bot.py`).
   b. Publicar el Flow (salir de Borrador). Meta exige requisitos de calidad.
2. Seguridad:
   - Render API key vieja: revocar y crear nueva, guardándola desde la Terminal del usuario
     (nunca en el chat). Sigue sin confirmarse.
   - GitHub PAT pegado en una sesión anterior: confirmar que se revocó.
   - Llave JSON de Google: el usuario la tiene en Descargas; moverla a un lugar privado.
3. Limpieza: borrar la Sheet "Massi - Datos" creada por error en la cuenta de Petra Secondaries.
4. Futuro: "Modo: En desarrollo" de la app de Meta — revisar si hace falta pasarla a Activo
   (hoy funciona así). No cambiar sin revisar juntos.

## Forma de trabajar del usuario
- Explicaciones breves, en español simple, pasos literales, una acción a la vez en paneles
  externos (salvo que pida "el paso a paso").
- Nunca pedir ni aceptar tokens, contraseñas, PIN o llaves en el chat. Pedir capturas con
  valores tapados y el "ojito" cerrado.
- Verificar con evidencia (logs de Render, pruebas reales) antes de afirmar.
- No hacer acciones irreversibles ni push a `main` sin autorización explícita.
- No mezclar con Petra Secondaries.
