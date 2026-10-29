# WhatsApp Flow — Publicar Inmueble

Flow nativo para que un visitante publique su inmueble a través de un
formulario de una sola pantalla, en vez del chat-by-chat tradicional
(operación → tipo → identificador → área → precio → confirmar).

Es la variante "formulario único" del flujo "Publicar inmueble" del
menú del edificio.

Trabaja en conjunto con el backend:

- `app/petra_visitante/router.py` → `_mostrar_flow_publicar_inmueble`
  (envía el Flow después de que el visitante eligió "Arriendo" o
  "Venta" en el chat).
- `app/petra_visitante/router.py` → `_procesar_flow_publicar_submit`
  (recibe el `nfm_reply` del Flow e inserta en `inmuebles_visitante`).

Mientras el env `PETRA_FLOW_PUBLICAR_ID` esté vacío, el camino sigue
siendo el chat-by-chat de siempre (ver `publicar_operacion`,
`publicar_tipo`, `publicar_identificador`, `publicar_area`,
`publicar_precio`, `publicar_autorizacion` en el router).

---

## 1. Crear el Flow en Meta Business Manager

1. Entrá a WhatsApp Manager → **Account tools → Flows**.
2. **Create flow** (arriba derecha).
3. Completá:
   - **Name**: `Petra Publicar Inmueble`
   - **Categories**: `OTHER`
   - **Template**: pestaña `Sin punto de conexión` + opción
     `Predeterminado`.
4. **Create**.
5. En el editor del Flow, **Ctrl+A → Delete** del JSON por default y
   pegá el JSON de abajo.
6. **Guardar**. Debe decir **"Errores de JSON de Flow: 0"**.
7. **Publicar** (botón arriba derecha). Si Meta te pide verificación de
   negocio o quality rating, resolvé primero y volvé.
8. Copiá el **flow_id** de la URL del editor (los 16 dígitos entre
   `/flows/` y `/edit`):
   ```
   https://business.facebook.com/wa/manage/flows/XXXXXXXXXXXXXXXX/edit
                                                 └── flow_id ──┘
   ```
9. En Render → servicio `petra-inmuebles` → **Environment** →
   agregá/editá:
   - `PETRA_FLOW_PUBLICAR_ID=<flow_id>`
10. Redeploy automático. Listo — probá publicando un inmueble vía
    `like` → 📋 Publicar inmueble → Arriendo (o Venta).

Para desactivar (rollback instantáneo al chat-by-chat): borrá la env
`PETRA_FLOW_PUBLICAR_ID` del servicio.

---

## 2. JSON del Flow

```json
{
  "version": "7.3",
  "screens": [
    {
      "id": "WELCOME_SCREEN",
      "title": "Publicar inmueble",
      "terminal": true,
      "success": true,
      "data": {},
      "layout": {
        "type": "SingleColumnLayout",
        "children": [
          { "type": "TextHeading", "text": "📋 Tu inmueble" },
          { "type": "TextBody",    "text": "Completá los datos. Una agente Petra te contactará para tomar fotos y mostrarlo." },
          {
            "type": "Form",
            "name": "form",
            "children": [
              {
                "type": "TextInput",
                "name": "direccion",
                "label": "Dirección",
                "helper-text": "Ej: Cl. 140 # 15-20, Torre 2 Apto 502",
                "required": true,
                "max-length": 200
              },
              {
                "type": "Dropdown",
                "name": "tipo",
                "label": "Tipo",
                "required": true,
                "data-source": [
                  { "id": "apartamento", "title": "Apartamento" },
                  { "id": "casa",        "title": "Casa" },
                  { "id": "local",       "title": "Local" },
                  { "id": "oficina",     "title": "Oficina" },
                  { "id": "parqueadero", "title": "Parqueadero" }
                ]
              },
              {
                "type": "TextInput",
                "name": "precio",
                "label": "Precio (en pesos)",
                "helper-text": "Ej: 2500000 para arriendo · 650000000 para venta",
                "input-type": "number",
                "required": true,
                "max-length": 15
              },
              {
                "type": "Dropdown",
                "name": "habitaciones",
                "label": "Habitaciones",
                "required": true,
                "data-source": [
                  { "id": "0", "title": "Estudio (sin alcoba separada)" },
                  { "id": "1", "title": "1" },
                  { "id": "2", "title": "2" },
                  { "id": "3", "title": "3" },
                  { "id": "4", "title": "4" },
                  { "id": "5", "title": "5 o más" }
                ]
              },
              {
                "type": "Dropdown",
                "name": "banos",
                "label": "Baños",
                "required": true,
                "data-source": [
                  { "id": "1", "title": "1" },
                  { "id": "2", "title": "2" },
                  { "id": "3", "title": "3" },
                  { "id": "4", "title": "4" },
                  { "id": "5", "title": "5 o más" }
                ]
              },
              {
                "type": "Footer",
                "label": "Continuar",
                "on-click-action": {
                  "name": "complete",
                  "payload": {
                    "direccion":    "${form.direccion}",
                    "tipo":         "${form.tipo}",
                    "precio":       "${form.precio}",
                    "habitaciones": "${form.habitaciones}",
                    "banos":        "${form.banos}"
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

---

## 3. Flujo end-to-end

1. Visitante toca **📋 Publicar inmueble** en el menú del edificio.
2. Bot: *"¿Es para arriendo o para venta?"* (chat, 2 botones).
3. Visitante toca **Arriendo** (o **Venta**).
4. **Si `PETRA_FLOW_PUBLICAR_ID` está seteado**:
   - Bot envía mensaje de texto introductorio ("¡Hola! Te ayudo a
     publicarlo…").
   - Bot envía el Flow con botón CTA **"Completar datos"**.
   - Visitante toca el CTA → se abre el formulario nativo.
   - Visitante completa dirección, tipo, precio, habitaciones, baños →
     **Continuar**.
   - El Flow envía `nfm_reply` → backend inserta en
     `inmuebles_visitante` con `activo=TRUE`, `gestionado_por_petra=TRUE`,
     `fuente='petra'`, `propietario_telefono=<telefono del visitante>`.
   - Bot: *"✅ ¡Listo! Registramos tu inmueble. Te contacta una agente
     Petra en las próximas horas para coordinar las fotos."*
5. **Si `PETRA_FLOW_PUBLICAR_ID` está vacío**: cae al chat-by-chat de
   siempre.

---

## 4. Rollback instantáneo

Borrá la env `PETRA_FLOW_PUBLICAR_ID` del servicio. El redeploy
automático de Render reactiva el chat-by-chat sin tocar código.

## 5. Troubleshooting

- **"Flow is not in a draft state, but the mode is set to 'draft'"** →
  `PETRA_FLOW_MODE=published` en Render (ya seteado para el Flow
  anterior, se comparte entre ambos).
- **"Specified screen X is not allowed as first screen"** → el JSON
  debe tener el screen `id: "WELCOME_SCREEN"` como primero.
- **Dropdown no muestra opciones** → chequear que `data-source` sea un
  array de `{id, title}` directo (no `${data.X}` — las variables top
  level no se interpolan bien en algunos clientes).
- **"Parameter display_text requires a length of at most, 20"** → el
  CTA del envío ("Completar datos") tiene que caber en 20 chars.
