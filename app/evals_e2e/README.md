# Evals end-to-end · Petra

**Qué resuelven:** verificar que una conversación completa (user ↔ bot, N turnos)
produzca las respuestas esperadas. A diferencia de `app/evals/` (que testea
cada tool-call por separado), estos tests cubren el flow entero.

## Correr

```bash
python -m app.evals_e2e                       # corre todas las goldens
python -m app.evals_e2e 01_inscripcion.json   # corre una
python -m app.evals_e2e --list                # lista disponibles
python -m app.evals_e2e --baseline            # genera baseline.json (snapshot)
```

Exit code 0 si pasan todas, 1 si alguna falla.

## Formato de una golden (JSON)

```jsonc
{
  "name": "inscripcion_happy_path",
  "description": "Agente nueva se inscribe de principio a fin sin problemas.",
  "telefono": "+99999000001",       // teléfono único de prueba
  "meta_inicial": {},                // meta previa (vacío = agente nueva)
  "turnos": [
    {
      "user": "Hola",
      "bot_contiene": ["Bienvenida a Petra", "nombre completo"]
    },
    {
      "user": "Juana Pérez",
      "bot_contiene": ["Perfecto", "cédula"]
    },
    // ...
    {
      "boton": "ONBAG_CONFIRM",      // simula pulsar un botón
      "bot_contiene": ["Felicitaciones", "Agente Petra"]
    }
  ]
}
```

### Campos

**Turno (envía al bot):**
- `user` — texto que manda la agente
- `boton` — simula pulsar un botón interactivo (id del botón, ej. `ONBAG_CONFIRM`)

**Turno (verifica la respuesta):**
- `bot_contiene` — lista de strings que DEBEN aparecer en los mensajes que el bot envió en ese turno
- `bot_no_contiene` — strings que NO deben aparecer
- `bot_regex` — regex que debe matchear
- `estado_esperado` — estado del agente después del turno (ej. `inscrito`, `kit_pendiente`)
- `tool_llamada` — nombre de tool que debe haberse llamado (si aplica)
- `avanzar_tiempo_horas` — simula paso del tiempo (para probar nudges, retornos)

**Setup global:**
- `telefono` — número único (por convención `+99999NNNNNN`)
- `meta_inicial` — estado del agente antes del test (ej. ya inscrito, ya autorizado)

## Side effects

El runner **mockea `app.petra_wa.send_*`** para capturar lo que el bot "mandaría"
sin tocar WhatsApp real. La DB es la real (por eso usamos teléfonos de prueba
`+99999*` que se borran al final del test).
