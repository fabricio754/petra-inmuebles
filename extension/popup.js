const activo = document.getElementById("activo");
const clave = document.getElementById("clave");
chrome.storage.local.get({ activo: true, clave: "" }, (v) => {
  activo.checked = v.activo;
  clave.value = v.clave;
});
activo.addEventListener("change", () => chrome.storage.local.set({ activo: activo.checked }));
document.getElementById("guardar").addEventListener("click", () => {
  chrome.storage.local.set({ clave: clave.value.trim(), activo: activo.checked }, () => {
    document.getElementById("estado").textContent = "Guardado ✓";
  });
});
