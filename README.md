# Petra Inmuebles — Prototipo (Conjunto Arrayanes)

Prototipo funcional de punta a punta: QR → WhatsApp → menú → catálogo
de arriendo → contactar. Sin WATI, sin Make, sin Meta Commerce
Catalog — esas piezas quedan para cuando el negocio esté verificado.
Esto usa la WhatsApp Cloud API de Meta directamente, con un webhook
propio (la única parte de código de este prototipo).

## Qué ya funciona (probado, sin tu intervención)

- Lógica completa de conversación: detectar Arrayanes desde el texto
  precargado del QR, menú de 4 opciones, catálogo de arriendo con los
  2 apartamentos, ficha de detalle, botón Contactar, y registro del
  lead en `data/leads.json`.
- Servidor webhook real (`app/server.py`) probado con peticiones HTTP
  idénticas a las que envía Meta.
- Túnel público para exponer el webhook a internet sin comprar
  hosting (`scripts/start.sh`), probado y funcionando.
- Generador del QR final (`scripts/generate_qr.py`), pendiente solo
  del número de WhatsApp que te da Meta.

Puedes verlo funcionar ahora mismo, sin ninguna cuenta, así:

```bash
cd /Users/afabriciozabala/petra-inmuebles
python3 scripts/simulate.py
```

Eso imprime en consola la conversación completa (modo DRY-RUN: no
manda nada real, solo demuestra que la lógica es correcta).

## Qué falta para que sea real en tu WhatsApp

Todo lo que sigue son cuentas y clics tuyos — yo no puedo crearlas.

### Fase 1 — Crear la app de Meta y el número de prueba (gratis, ~10 min)

1. Entra a **developers.facebook.com** → inicia sesión con tu Facebook.
2. **Mis apps → Crear app → tipo "Otro" → "Empresa"**. Nómbrala `Petra Inmuebles`.
3. En el panel de la app, agrega el producto **WhatsApp**.
4. Meta te asigna automáticamente:
   - Un **número de prueba** (test number) — ya activo, sin SIM ni verificación de negocio.
   - Un **Token de acceso temporal** (dura 24h, luego se renueva con un clic).
   - Un **Phone Number ID**.
5. En la misma pantalla, sección **"To" (destinatarios de prueba)**: agrega **tu propio número de celular** con WhatsApp. Meta te manda un código por WhatsApp para confirmarlo.

Cuando tengas esos 3 datos (número de prueba, token, phone number id), me los pasas y sigo yo.

### Fase 2 — Conectar tus credenciales

Dime el token, el Phone Number ID y el número de prueba. Yo:

```bash
cp .env.example .env
# relleno META_ACCESS_TOKEN, META_PHONE_NUMBER_ID, META_TEST_NUMBER
```

### Fase 3 — Levantar el webhook y exponerlo

Yo ejecuto:

```bash
./scripts/start.sh
```

Esto imprime una URL pública tipo `https://algo.trycloudflare.com/webhook`.

### Fase 4 — Registrar el webhook en Meta

En el panel de tu app → **WhatsApp → Configuration**:

1. **Callback URL**: pega la URL que te di (`https://algo.trycloudflare.com/webhook`).
2. **Verify token**: `petra-verify-token` (o el que hayas puesto en `.env`).
3. Clic en **Verify and save**. Si el servidor está corriendo, se pone en verde solo.
4. Abajo, en **Webhook fields**, activa la casilla **messages**.

### Fase 5 — Generar e imprimir el QR

Yo ejecuto:

```bash
python3 scripts/generate_qr.py
```

Genera `qr-arrayanes.png` en la carpeta del proyecto, apuntando a
`wa.me/<tu número de prueba>?text=ARRAYANES`. Lo imprimes o lo abres
en el celular para escanearlo con otro celular (o simplemente tocas
el link desde el mismo teléfono).

## Cómo probarlo

1. Escanea (o toca) el QR de Arrayanes.
2. Se abre WhatsApp con el mensaje "ARRAYANES" precargado hacia el
   número de prueba de Meta. Envíalo.
3. Debe llegar el menú de 4 opciones.
4. Toca **🏠 Tomar en arriendo**.
5. Debe llegar el catálogo con Apto 101 y Apto 204.
6. Toca un apartamento → llega la ficha con el botón **Contactar**.
7. Toca **Contactar** → llega la confirmación, y el lead queda
   guardado en `data/leads.json`.

Nota importante: el número de prueba de Meta **solo puede escribirle
a los números que agregaste como destinatarios de prueba** (máx. 5).
Suficiente para el piloto y la demo; para abrir a todos los
residentes de Arrayanes hace falta la verificación de negocio — ese
es un paso posterior, no de este prototipo.

## Qué NO tiene este prototipo (a propósito)

- No usa el catálogo de Meta Commerce Manager todavía — el "catálogo"
  que ves es una lista interactiva nativa de WhatsApp. Se ve igual de
  bien y no depende de aprobación de Meta. Migrar a Commerce Manager
  es un cambio interno, no cambia lo que ve el usuario.
- No usa WATI — es Cloud API directa. Se puede reemplazar por WATI
  más adelante sin tocar los datos.
- Comprar, Publicar y Crédito responden "Estamos preparando esta
  opción", como pediste.
- Sin pagos, sin contratos, sin scoring, sin múltiples conjuntos.
- El inventario vive en `data/inventario.json` (no en Google Sheets
  todavía) — es el archivo más fácil de editar a mano mientras
  probamos. Migrar a Sheets es un cambio de una función, no de
  arquitectura.
- Los leads se guardan en `data/leads.json`, no en un CRM.

## Estructura

```
petra-inmuebles/
  app/
    server.py      → webhook Flask (recibe mensajes de Meta)
    bot.py          → lógica de conversación (el "cerebro")
    whatsapp.py     → arma y envía los mensajes a la Cloud API
    state.py        → guarda sesión, inventario y leads en JSON
  data/
    inventario.json → los apartamentos de Arrayanes
    sessions.json   → qué conjunto detectó cada teléfono (se crea solo)
    leads.json      → contactos generados (se crea solo)
  scripts/
    simulate.py     → prueba toda la conversación sin WhatsApp real
    start.sh         → levanta servidor + túnel público
    generate_qr.py   → genera el PNG del QR de Arrayanes
  bin/cloudflared    → binario del túnel (sin cuenta, sin costo)
```
