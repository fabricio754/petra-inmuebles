// Corre en la página del portal. Cuando la persona toca el botón de WhatsApp
// o Llamar (después de llenar el formulario del portal), en vez de abrir su
// WhatsApp personal toma el número y se lo pasa a content.js.
(() => {
  const ES_CONTACTO = /(wa\.me\/|api\.whatsapp\.com|web\.whatsapp\.com|whatsapp:\/\/|^tel:)/i;
  const activo = () => document.documentElement.dataset.massiActivo === "1";
  let ultimo = { url: "", t: 0 };

  const capturar = (url) => {
    url = url ? String(url) : "";
    if (!url || !ES_CONTACTO.test(url) || !activo()) return false;
    const ahora = Date.now();
    if (url === ultimo.url && ahora - ultimo.t < 3000) return true; // mismo clic, varias vías
    ultimo = { url, t: ahora };
    window.postMessage({ massi: "contacto", url }, "*");
    return true;
  };

  // 1) window.open(url)
  const abrirOriginal = window.open;
  window.open = function (url, ...resto) {
    if (capturar(url)) return null;
    return abrirOriginal.call(this, url, ...resto);
  };

  // 2) Clic en un enlace de la página.
  document.addEventListener("click", (e) => {
    const a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (a && capturar(a.href)) {
      e.preventDefault();
      e.stopImmediatePropagation();
    }
  }, true);

  // 3) Enlace creado por código y "clickeado" sin estar en la página.
  const clickOriginal = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function () {
    if (capturar(this.href)) return;
    return clickOriginal.call(this);
  };

  // 4) La página manda la pestaña a WhatsApp (location.href = ..., redirecciones).
  if (window.navigation) {
    navigation.addEventListener("navigate", (e) => {
      if (e.cancelable && capturar(e.destination && e.destination.url)) e.preventDefault();
    });
  }
})();
