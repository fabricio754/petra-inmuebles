# Extensión "Enviar a Massi"

Con la extensión activa, al tocar **WhatsApp** o **Llamar** en un anuncio de
Metrocuadrado o Finca Raíz (después de llenar el formulario del portal), no se
abre tu WhatsApp: el contacto y los datos del anuncio se envían a Massi, que
filtra y le escribe desde el número oficial.

## Instalar (Chrome)
1. Descomprime `enviar-a-massi.zip` en una carpeta.
2. Abre `chrome://extensions` y activa **Modo de desarrollador** (arriba a la derecha).
3. **Cargar descomprimida** → elige la carpeta.
4. Toca el ícono de la extensión → escribe la **clave de captura** (la misma de
   `CAPTURA_TOKEN` en Render) → **Guardar**.

La extensión no se salta formularios ni reCAPTCHA: solo lee lo que el portal ya
muestra después de que una persona llenó su formulario.
