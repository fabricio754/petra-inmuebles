# WhatsApp Flow — Multi-select de inmuebles (visitante QR)

Instrucciones para crear en Meta Business Manager el Flow que usa el
visitante para elegir varios inmuebles de una vez (ver mockup del PR
#65 "camino A' — Flow nativo").

El backend ya está listo detrás del feature flag `PETRA_FLOW_INMUEBLES_ID`.
Mientras ese env no esté seteado, el visitante sigue viendo el carrito
nativo (camino B). Al setear la env, automáticamente arranca el Flow.

---

## 1. Crear el Flow en Meta Business Manager

1. Entrá a <https://business.facebook.com/wa/manage/flows/>
2. Elegí la WABA del piloto Petra.
3. **Create flow** → **Blank**.
4. Nombre: `petra_inmuebles_multiselect` (o el que prefieras — Meta no
   lo expone al usuario).
5. Categoría: **OTHER**.

En el editor que abre:

6. Click en el ícono `{}` (JSON editor) arriba a la derecha.
7. Pegá el JSON de la sección 2 abajo. Guardá.
8. **Preview** → chequear que la pantalla "SELECT_PROPERTIES" rendea
   bien con items dummy.
9. **Publish** (o dejalo en Draft para probar con `PETRA_FLOW_MODE=draft`).
10. Copiá el **Flow ID** que Meta asigna (string numérico largo).

## 2. JSON del Flow

Pantalla única `SELECT_PROPERTIES` con un `CheckboxGroup` que se
construye dinámicamente desde `${data.items}` (los inmuebles del
edificio en que está el visitante). Submit devuelve `selected_ids`
que el backend procesa en `_procesar_flow_submit`.

Flow schema versión `6.3` (compatible con `flow_message_version` 3):

```json
{
  "version": "6.3",
  "data_api_version": "3.0",
  "routing_model": {
    "SELECT_PROPERTIES": []
  },
  "screens": [
    {
      "id": "SELECT_PROPERTIES",
      "title": "Elegí los inmuebles",
      "data": {
        "edificio": {
          "type": "string",
          "__example__": "Conjunto Monaco"
        },
        "operacion_label": {
          "type": "string",
          "__example__": "Arriendos"
        },
        "items": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "id":       { "type": "string" },
              "title":    { "type": "string" },
              "subtitle": { "type": "string" }
            }
          },
          "__example__": [
            { "id": "12",  "title": "#12 — Casa",  "subtitle": "3 hab · 2 baños · 95 m² · $2.2M" },
            { "id": "24",  "title": "#24 — Casa",  "subtitle": "4 hab · 3 baños · 140 m² · $3.1M" },
            { "id": "305", "title": "#305 — Apto", "subtitle": "3 hab · 2 baños · 88 m² · $2.6M" }
          ]
        }
      },
      "terminal": true,
      "layout": {
        "type": "SingleColumnLayout",
        "children": [
          {
            "type": "TextSubheading",
            "text": "${data.operacion_label} — ${data.edificio}"
          },
          {
            "type": "TextBody",
            "text": "Marcá los inmuebles que te interesen."
          },
          {
            "type": "Form",
            "name": "form",
            "children": [
              {
                "type": "CheckboxGroup",
                "name": "selected_ids",
                "label": "Inmuebles disponibles",
                "required": true,
                "data-source": "${data.items}"
              },
              {
                "type": "Footer",
                "label": "Confirmar selección",
                "on-click-action": {
                  "name": "complete",
                  "payload": {
                    "selected_ids": "${form.selected_ids}"
                  }
                }
              }
            ]
          }
        ]
      }
    }
  ]
}
```

Notas sobre el JSON:

- `CheckboxGroup` con `data-source: "${data.items}"` toma cada objeto
  del array y usa `id` como valor interno y `title` como label. Si tu
  versión del editor pide un formato diferente (`list-items` con
  `id/title/description` explícitos), ajustá — la clave es que el
  action `complete` devuelva `selected_ids` como lista de los `id`
  seleccionados.
- `terminal: true` + `on-click-action: complete` cierra el Flow y
  dispara el `nfm_reply` al webhook con el payload.
- El límite duro de items es ~30 por pantalla. El backend ya trunca
  (`_FLOW_MAX_ITEMS = 30` en `app/petra_visitante/router.py`) y avisa
  en el body si hay más.

## 3. Setear en Render

Una vez publicado el Flow, en Render → petra-inmuebles → Environment:

| Variable | Valor | Notas |
|---|---|---|
| `PETRA_FLOW_INMUEBLES_ID` | `<flow_id>` que te dio Meta | obligatorio para activar el camino A' |
| `PETRA_FLOW_MODE` | `published` (default) o `draft` | `draft` solo mientras el Flow está sin publicar |

Al setear la variable, Render redeploya automáticamente y el próximo
"Tomar en arriendo" / "Comprar" dentro de un edificio abre el Flow
nativo en vez del carrito.

**Rollback instantáneo:** borrar `PETRA_FLOW_INMUEBLES_ID` → vuelve
al carrito, sin redeploy manual.

## 4. Qué pasa cuando el visitante confirma

Lo detalla el código en `app/petra_visitante/router.py`
(`_procesar_flow_submit`):

1. Backend recibe `nfm_reply` con el JSON `{selected_ids: ["12","305"]}`.
2. Persiste **una** fila en `petra_interesados_qr` con el array completo.
3. Persiste **una fila por inmueble** en `visitante_contactos`.
4. Manda **un mensaje por inmueble** con header `🔑 #12 — Casa · 3hab · $2.2M`
   y `👉 Hablar sobre este inmueble: https://wa.me/<prop>?text=...` ya
   pre-cargado con el saludo.
5. Limpia el estado y vuelve al menú del edificio.

**Según el mockup del user, NO hay mensaje resumen entre el Flow y los
N mensajes individuales** — el Flow confirma por sí solo con su propia
pantalla de "selección enviada".

## 5. Troubleshooting

- **El botón "Ver inmuebles" aparece deshabilitado o gris en WhatsApp:**
  el Flow está en draft y la env `PETRA_FLOW_MODE` sigue en `published`.
  Setear `PETRA_FLOW_MODE=draft` o publicar el Flow en Meta.
- **El Flow abre pero muestra "Something went wrong":** el schema del
  JSON cambió entre versiones de Meta. Chequear la versión soportada
  más reciente en <https://developers.facebook.com/docs/whatsapp/flows/reference/components>
  y actualizar `version` en el JSON.
- **El `nfm_reply` llega pero sin `selected_ids`:** el `CheckboxGroup`
  está usando otro nombre de key. Chequear que `name="selected_ids"`
  coincida con lo que `_flow_response_extraer_ids` espera (soporta
  `selected_ids`, `selected_items`, `ids`, `items` como fallback).
- **El Flow devuelve items pero el backend dice "No seleccionaste
  ningún inmueble":** el visitante abrió el Flow y lo cerró sin marcar
  nada. Comportamiento correcto.

## 6. Decidido fuera del alcance de este PR

- Multi-screen (filtros, confirmación extra, etc.): mantener en una
  pantalla única el máximo tiempo posible — reduce fricción.
- Flow con `data_channel_uri` (data_exchange endpoint, requiere RSA
  encryption keys): no es necesario porque los items caben en el
  `flow_action_payload` del mensaje inicial. Si en el futuro el
  edificio tiene >30 inmuebles de la misma operación con frecuencia,
  evaluar paginación via `data_channel_uri`.
