# Paso 0 — Identidad de marca en WhatsApp (checklist operativo)

Objetivo: que la primera plantilla que recibe un propietario abra una ficha
como la de "Viajes Éxito Ofertas": check azul, logo, redes vinculadas con
seguidores, y los botones **Stop** / **Profile**.

Es configuración en Meta Business Manager + WhatsApp Manager. No depende de
código del repo (salvo el webhook de Stop, que ya quedó en `app/server.py`).

## Hecho en código (commit de este paso)

- `app/server.py` → el webhook ya detecta el campo `user_preferences` con
  `category=marketing_messages` y `value=stop`, y llama a
  `state.set_no_contactar(wa_id, True)`. Equivalente a STOP/BAJA/PARA/SALIR
  por texto.

## Acciones manuales (Fabricio)

### 1. Perfil del negocio
- Business Manager → WhatsApp Manager → Phone numbers → el número de Massi
  → Profile.
- Completar: **Nombre comercial** (p. ej. "Massi Crédito"), **logo**
  (cuadrado, 640×640 mínimo, fondo plano), **categoría**, **descripción**
  corta, **correo**, **sitio web** (`https://petra-inmuebles.onrender.com`
  o el dominio final), **dirección**.

### 2. Verificación de Meta Business (check azul)
- Business Manager → Security Center → Business verification.
- Subir: cámara de comercio reciente, RUT, comprobante de dirección.
- Tiempo estimado: 3–7 días hábiles.
- Una vez verificado, pedir en el mismo panel el **green/blue checkmark**
  (Official Business Account): se gestiona desde el soporte de WhatsApp,
  requiere notoriedad de marca o volumen demostrable.

### 3. Vincular Facebook e Instagram
- Business Suite → Settings → Business assets → Add → agregar la Página
  de Facebook y la cuenta de Instagram del negocio.
- Confirmar que las dos están **ligadas al mismo Business Manager** del
  número de WhatsApp.
- El contador de seguidores (`208K` / `101K` en el ejemplo) aparece
  automático una vez vinculadas.

### 4. Marketing Messages opt-in (tarjeta con Stop / Profile)
- WhatsApp Manager → Account tools → Templates.
- La plantilla `massi_apertura_a` ya está en categoría **MARKETING**: el
  cliente verá la tarjeta "You are getting offers and announcements from
  this business" con **Stop** y **Profile** la primera vez que llegue un
  mensaje.
- Aceptar los términos de Marketing Messages en WhatsApp Manager → Settings
  si todavía no están aceptados.
- No hay que agregar botones Stop a mano: Meta los pone al lado de la
  plantilla la primera vez.

### 5. Tier y calidad
- Arrancar en Tier 250/día (default).
- Monitorear calidad en WhatsApp Manager → Phone numbers (verde / amarillo
  / rojo). Si cae a amarillo: bajar ritmo de envíos.
- Subir de tier solo con calidad verde sostenida.

## Prueba de aceptación

Enviar la plantilla `massi_apertura_a` a un número de prueba:
- Llega con logo y nombre comercial.
- Ficha del negocio muestra check azul.
- Ficha muestra el conteo de seguidores de FB e IG.
- Botones **Stop** y **Profile** visibles bajo la ficha.
- Tocar **Stop** → en Render logs aparece
  `Opt-out de marketing recibido de <wa_id> (botón Stop).`
- Una consulta a `contactos` muestra `no_contactar = TRUE` para ese
  teléfono.
