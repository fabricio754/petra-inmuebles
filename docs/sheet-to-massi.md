# Sincronizar Google Sheet → Massi en tiempo real

Guia paso a paso para instalar el Apps Script [`sheet-to-massi.gs`](./sheet-to-massi.gs)
en el Google Sheet **"Base anuncios de campo"**
(`1QzewQHKpzvBFyuUj33GJo6uPxKwOH3QvNLpk-w1lgwo`).

Cuando este configurado, cada fila nueva que agregues al Sheet se
envia automaticamente al endpoint `POST /scraper/ingest` de Massi,
en segundos. El script evita duplicados marcando cada fila enviada
con la fecha en una columna de control (`enviado_massi`).

---

## 1. Abrir el editor de Apps Script

1. Abri el Google Sheet:
   https://docs.google.com/spreadsheets/d/1QzewQHKpzvBFyuUj33GJo6uPxKwOH3QvNLpk-w1lgwo/edit
2. En el menu superior: **Extensiones → Apps Script**.
3. Se abre el editor en una pestaña nueva. Vas a ver un archivo
   `Code.gs` con una funcion vacia.

## 2. Pegar el codigo

1. Borra todo el contenido de `Code.gs`.
2. Copia el contenido completo de [`docs/sheet-to-massi.gs`](./sheet-to-massi.gs)
   y pegalo en `Code.gs`.
3. (Opcional) Cambia el nombre del archivo a `sheet-to-massi.gs`
   desde el panel izquierdo (icono de tres puntos → **Rename**).
4. Guarda con **Ctrl+S** (o **Cmd+S** en Mac). Al guardar por
   primera vez te va a pedir ponerle un nombre al proyecto; usa
   `Sheet → Massi`.

## 3. Guardar el token `INGEST_TOKEN` en Script Properties

El script lee el token desde las **Script Properties** del
proyecto (nunca va en el codigo).

1. En el editor de Apps Script, click en el icono de engranaje
   **Project Settings** (menu izquierdo).
2. Baja hasta la seccion **Script Properties**.
3. Click en **Add script property**.
4. Property: `INGEST_TOKEN`
5. Value: pega el token real de Massi. Para obtenerlo:
   - Entra a Render → el servicio `petra-inmuebles` → **Environment**.
   - Copia el valor de la variable `INGEST_TOKEN`.
6. Click en **Save script properties**.

> Si cambias el token en Render, hay que actualizar tambien esta
> property.

## 4. Autorizar el script

1. Volve al archivo `Code.gs` (menu izquierdo → **Editor**).
2. En el selector de funciones (arriba, al lado de **Debug**),
   elegi `instalarTrigger`.
3. Click en **Run**.
4. Google va a pedirte autorizacion:
   - **Review permissions** → elegi tu cuenta.
   - Como el proyecto no esta verificado, va a aparecer un
     mensaje de advertencia "Google hasn't verified this app".
     Click en **Advanced** → **Go to Sheet → Massi (unsafe)**.
   - Permisos que te va a pedir:
     - Ver y manejar hojas de calculo de Google Drive
       (`SpreadsheetApp`).
     - Conectarse a servicios externos (`UrlFetchApp`) — es el
       permiso para hacer el POST al endpoint.
     - Mostrar y correr contenido de terceros (menu de UI y
       script properties).
   - **Allow**.
5. Si todo salio bien ves un popup "Listo. Trigger
   onChangeMassi instalado."

## 5. Verificar que el trigger quedo instalado

1. En el editor de Apps Script, menu izquierdo → icono de reloj
   **Triggers**.
2. Debe aparecer una fila con:
   - Function: `onChangeMassi`
   - Event source: **From spreadsheet**
   - Event type: **On change**
3. Si no aparece, correr otra vez `instalarTrigger` desde el
   editor.

## 6. Probar con una fila nueva

1. Volve al Sheet (pestaña `Base anuncios de campo`).
2. Agrega una fila al final con un telefono valido en la columna
   `tel` o `Telefono` (ej. `3101234567` o `573101234567`), y los
   demas campos que quieras (`tipo`, `ciudad`, `barrio`, etc).
3. En pocos segundos el script debe:
   - Agregar la columna `enviado_massi` (si no existia).
   - Escribir la fecha/hora actual en esa columna para la fila
     recien agregada.
4. Para confirmar en Massi: entra al panel y buscar el telefono
   que acabas de agregar. Deberia aparecer con `portal =
   manual_sheet`.

## 7. Depurar si algo no se envia

### Ver los logs

1. Editor de Apps Script → menu izquierdo → **Executions**.
2. Vas a ver una lista con cada corrida (manual y automatica).
   Click en una fila para ver el log completo.
3. Lineas tipicas que vas a ver:
   - `[Massi] Fila 123 OK (HTTP 200): {"saved":1,"total":1}` → OK.
   - `[Massi] Fila 123 ERROR HTTP 401: ...` → `INGEST_TOKEN`
     invalido o no esta en Script Properties.
   - `[Massi] Fila 123 ERROR HTTP 500: ...` → el endpoint
     devolvio error. Mirar logs de Render.
   - `[Massi] Fila 123: telefono invalido ...` → el telefono no
     cumple el formato (10 digitos empezando en 3 o 12 digitos
     empezando en 573).
   - `ERROR: falta INGEST_TOKEN en Script Properties.` → paso 3
     no completado.

### Forzar un reenvio manual

- Menu del Sheet: **Massi → Enviar filas nuevas ahora**. Esto
  procesa todas las filas sin fecha en `enviado_massi`.

### Reenviar una fila ya enviada

- Borra la celda de `enviado_massi` para esa fila.
- Menu **Massi → Enviar filas nuevas ahora**.

### Reenviar TODAS las filas (reset completo)

- Menu **Massi → Resetear marcas (reenvia todo)** → confirmar.
- Despues: **Massi → Enviar filas nuevas ahora**.

### Marcar historial viejo como "ya enviado" (no reenviar)

Util cuando instalas el script sobre un Sheet con filas viejas
que ya estan en Massi y no queres duplicarlas:

- Menu **Massi → Marcar todas como enviadas**.
- A partir de ahi solo se envian las filas nuevas.

### El trigger no dispara automaticamente

- Verifica que esta instalado (paso 5).
- `onChange` dispara cuando se agregan/editan filas. Pegar un
  bloque de celdas tambien dispara. Si no dispara, correr
  manualmente desde el menu "Massi → Enviar filas nuevas ahora"
  para confirmar que el envio funciona, y despues reinstalar el
  trigger con **Massi → Instalar trigger automatico**.

---

## Notas tecnicas

- **Headers soportados** (case/accent-insensitive, substring):
  `tel`, `tipo`, `ciudad`, `barrio`, `direccion`, `nombre`,
  `precio` (opcional), `foto` (opcional), `anunciante`
  (opcional), `url` (opcional), `arrend` (operacion
  venta/arriendo, opcional), `estrato` (opcional).
- **Precio por defecto**: si la fila no tiene `precio`, se manda
  `300000000` para que `_guardar()` no filtre el registro
  (requiere precio >= 50M). Si en el futuro agregas la columna
  `precio` en el Sheet, se usa ese valor.
- **Portal**: todas las filas se marcan con `portal =
  manual_sheet` para distinguirlas de las que vienen del scraper
  Apify.
- **URL de origen**: si la fila no trae `url`, el script genera
  `sheet://<SHEET_ID>/fila/<N>` como identificador.
- **Reintentos**: si un POST falla (HTTP 4xx/5xx o error de red)
  la fila NO se marca como enviada. En la proxima corrida del
  trigger (o al correr manualmente) se reintenta.
- **1 POST por fila**: por simplicidad el script envia 1 request
  por fila nueva. Con el volumen esperado (manual, pocas filas
  por dia) es suficiente. El workflow `import-sheets.yml` sigue
  siendo util para cargas masivas iniciales (manda en chunks de 50).
