// Lee los datos del anuncio que está en pantalla y los manda a Massi
// (a través de background.js) cuando intercept.js captura un contacto.

function telefonoDe(url) {
  const u = decodeURIComponent(url);
  const m = u.match(/(?:wa\.me\/|phone=|tel:)\+?(\d[\d\s-]{6,})/i);
  return m ? m[1].replace(/\D/g, "") : "";
}

function jsonLd() {
  const datos = [];
  document.querySelectorAll('script[type="application/ld+json"]').forEach((s) => {
    try {
      const j = JSON.parse(s.textContent);
      (Array.isArray(j) ? j : [j]).forEach((x) => datos.push(x, ...(x["@graph"] || [])));
    } catch (_) {}
  });
  return datos;
}

function buscar(obj, clave) {
  if (!obj || typeof obj !== "object") return undefined;
  if (clave in obj) return obj[clave];
  for (const v of Object.values(obj)) {
    const r = buscar(v, clave);
    if (r !== undefined) return r;
  }
  return undefined;
}

function datosDelAnuncio() {
  const texto = document.body ? document.body.innerText : "";
  const ld = jsonLd();
  const desdeLd = (clave) => {
    for (const d of ld) { const v = buscar(d, clave); if (v !== undefined && v !== "") return v; }
    return "";
  };
  const meta = (p) => (document.querySelector(`meta[property="${p}"], meta[name="${p}"]`) || {}).content || "";

  // Precio: primero el dato estructurado; si no, el primer "$ 260.000.000" grande del texto.
  let precio = desdeLd("price");
  if (!precio) {
    const m = [...texto.matchAll(/\$\s?([\d.]{9,})/g)].map((x) => Number(x[1].replace(/\./g, "")));
    precio = m.find((n) => n >= 30000000) || "";
  }
  const estrato = (texto.match(/Estrato\s*:?\s*(\d)/i) || [])[1] || "";
  const titulo = (document.querySelector("h1") || {}).innerText || document.title;
  const migas = [...document.querySelectorAll('nav a, [class*="breadcrumb" i] a')].map((a) => a.innerText).join(" ");

  return {
    portal: location.hostname.includes("fincaraiz") ? "Finca Raíz" : "Metrocuadrado",
    url: location.href.split("#")[0],
    titulo,
    precio,
    estrato,
    ciudad: [desdeLd("addressLocality"), desdeLd("addressRegion"), migas].join(" "),
    barrio: desdeLd("streetAddress") || "",
    tipo: titulo,
    foto: meta("og:image"),
  };
}

function aviso(texto, color) {
  const d = document.createElement("div");
  d.textContent = texto;
  d.style.cssText = `position:fixed;z-index:2147483647;right:16px;bottom:16px;max-width:360px;
    padding:12px 16px;border-radius:10px;font:14px/1.4 system-ui,sans-serif;color:#fff;
    background:${color};box-shadow:0 4px 16px rgba(0,0,0,.25)`;
  document.documentElement.appendChild(d);
  setTimeout(() => d.remove(), 6000);
}

// Avisa a intercept.js si la extensión está activa.
chrome.storage.local.get({ activo: true }, ({ activo }) => {
  document.documentElement.dataset.massiActivo = activo ? "1" : "0";
});
chrome.storage.onChanged.addListener((c) => {
  if (c.activo) document.documentElement.dataset.massiActivo = c.activo.newValue ? "1" : "0";
});

window.addEventListener("message", (e) => {
  if (e.source !== window || !e.data || e.data.massi !== "contacto") return;
  const anuncio = { ...datosDelAnuncio(), telefono: telefonoDe(e.data.url) };
  aviso("Enviando a Massi…", "#57606a");
  chrome.runtime.sendMessage({ tipo: "captura", anuncio }, (r) => {
    if (!r) return aviso("No se pudo conectar con Massi.", "#cf222e");
    const colores = { nuevo: "#1a7f37", duplicado: "#9a6700", descartado: "#57606a" };
    const titulos = { nuevo: "✓ Enviado a Massi", duplicado: "Ya estaba en la lista", descartado: "Descartado",
      invalido: "Sin teléfono válido", no_autorizado: "Clave incorrecta (revisa la extensión)" };
    aviso(`${titulos[r.resultado] || r.resultado}: ${r.detalle || ""}`, colores[r.resultado] || "#cf222e");
  });
});
