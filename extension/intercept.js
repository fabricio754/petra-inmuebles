// Corre en la página del portal. Cuando la persona toca el botón de WhatsApp
// o Llamar (después de llenar el formulario del portal), en vez de abrir su
// WhatsApp personal toma el número y se lo pasa a content.js.
(() => {
  const ES_CONTACTO = /(wa\.me\/|api\.whatsapp\.com|web\.whatsapp\.com|whatsapp:\/\/|^tel:)/i;
  const activo = () => document.documentElement.dataset.massiActivo === "1";

  const capturar = (url) => {
    if (!url || !ES_CONTACTO.test(String(url)) || !activo()) return false;
    window.postMessage({ massi: "contacto", url: String(url) }, "*");
    return true;
  };

  const abrirOriginal = window.open;
  window.open = function (url, ...resto) {
    if (capturar(url)) return null;
    return abrirOriginal.call(this, url, ...resto);
  };

  document.addEventListener("click", (e) => {
    const a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (a && capturar(a.href)) {
      e.preventDefault();
      e.stopImmediatePropagation();
    }
  }, true);
})();
