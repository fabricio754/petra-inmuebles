// Envía el anuncio al bot. Va aquí (y no en content.js) para no depender de
// los permisos de la página del portal.
const SERVIDOR = "https://petra-inmuebles.onrender.com/captura";

chrome.runtime.onMessage.addListener((msg, _sender, responder) => {
  if (msg.tipo !== "captura") return;
  chrome.storage.local.get({ clave: "" }, async ({ clave }) => {
    try {
      const r = await fetch(SERVIDOR, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Massi-Token": clave },
        body: JSON.stringify(msg.anuncio),
      });
      responder(await r.json());
    } catch (e) {
      responder({ resultado: "error", detalle: String(e) });
    }
  });
  return true; // respuesta asíncrona
});
