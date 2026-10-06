/**
 * sheet-to-massi.gs
 * ------------------------------------------------------------
 * Google Apps Script que sincroniza el Sheet "Base anuncios de
 * campo" con el endpoint POST /scraper/ingest de Massi.
 *
 * Cuando se agrega una fila nueva (o se corre manualmente desde
 * el menu "Massi"), el script:
 *   1. Lee las filas pendientes (sin fecha en la columna
 *      "enviado_massi").
 *   2. Normaliza telefono y campos del contacto.
 *   3. Envia cada fila al endpoint via UrlFetchApp.fetch con el
 *      header X-Ingest-Token.
 *   4. Si el POST responde 2xx, escribe la fecha de envio en la
 *      columna "enviado_massi" para no reenviarla.
 *
 * El token NO esta en el codigo: se guarda en Script Properties
 * con el nombre INGEST_TOKEN. Ver docs/sheet-to-massi.md.
 * ------------------------------------------------------------
 */

// ───────────────────── Configuracion ─────────────────────────

/** URL del endpoint de ingesta. */
var INGEST_URL = 'https://petra-inmuebles.onrender.com/scraper/ingest';

/** Nombre de la hoja a procesar. */
var SHEET_NAME = 'Base anuncios de campo';

/** Columna con la marca de envio. Si no existe se crea al final. */
var COL_ENVIADO = 'enviado_massi';

/** Portal con el que se identifican las filas creadas desde el sheet. */
var PORTAL = 'manual_sheet';

/**
 * Precio placeholder cuando la fila no trae precio. El guardado
 * en Massi requiere precio >= 50M para no filtrar el contacto.
 */
var PRECIO_DEFAULT = '300000000';

// ────────────────────────── Menu ─────────────────────────────

/**
 * Se dispara automaticamente al abrir el Sheet. Agrega el menu
 * "Massi" con las acciones manuales.
 */
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Massi')
    .addItem('Enviar filas nuevas ahora', 'procesarPendientes')
    .addItem('Marcar todas como enviadas', 'marcarTodasEnviadas')
    .addItem('Resetear marcas (reenvia todo)', 'resetearMarcas')
    .addSeparator()
    .addItem('Instalar trigger automatico', 'instalarTrigger')
    .addToUi();
}

// ──────────────────── Trigger instalable ─────────────────────

/**
 * Instala un trigger onChange a nivel de spreadsheet. Hay que
 * correrlo UNA sola vez. onChange corre como el owner del
 * trigger, por eso tiene permisos para UrlFetchApp.fetch (los
 * triggers onEdit simples no los tienen).
 */
function instalarTrigger() {
  var ss = SpreadsheetApp.getActive();

  // Borrar triggers existentes de este proyecto para evitar
  // duplicados si se corre varias veces.
  var existentes = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existentes.length; i++) {
    if (existentes[i].getHandlerFunction() === 'onChangeMassi') {
      ScriptApp.deleteTrigger(existentes[i]);
    }
  }

  ScriptApp.newTrigger('onChangeMassi')
    .forSpreadsheet(ss)
    .onChange()
    .create();

  SpreadsheetApp.getUi().alert(
    'Listo. Trigger "onChangeMassi" instalado.\n\n' +
    'Cada vez que agregues una fila a "' + SHEET_NAME + '", ' +
    'se enviara automaticamente al endpoint de Massi.'
  );
}

/**
 * Handler del trigger onChange. Solo procesa cambios tipo
 * INSERT_ROW/EDIT y se limita a la hoja objetivo.
 */
function onChangeMassi(e) {
  try {
    // e.changeType puede ser EDIT, INSERT_ROW, REMOVE_ROW, etc.
    // Procesamos en los casos en que es probable que haya datos
    // nuevos. Si el cambio no tiene datos nuevos, procesarPendientes
    // simplemente no encuentra nada que mandar y retorna.
    if (e && e.changeType && e.changeType !== 'EDIT' &&
        e.changeType !== 'INSERT_ROW' &&
        e.changeType !== 'OTHER' &&
        e.changeType !== 'FORMAT') {
      // Ignoramos removes y cambios estructurales que no suman filas.
      return;
    }
  } catch (err) {
    // Si no viene event (ejecucion manual), seguimos de largo.
  }
  procesarPendientes();
}

// ────────────────── Logica principal ─────────────────────────

/**
 * Procesa todas las filas que no tengan fecha en la columna
 * "enviado_massi". Hace un POST por fila. Si falla la escribe
 * en el log y la deja sin marcar para reintentar en la proxima.
 */
function procesarPendientes() {
  var token = PropertiesService.getScriptProperties().getProperty('INGEST_TOKEN');
  if (!token) {
    var msg = 'ERROR: falta INGEST_TOKEN en Script Properties. ' +
              'Ve a Project Settings → Script Properties.';
    Logger.log(msg);
    try { SpreadsheetApp.getUi().alert(msg); } catch (e) {}
    return;
  }

  var sheet = SpreadsheetApp.getActive().getSheetByName(SHEET_NAME);
  if (!sheet) {
    Logger.log('ERROR: no se encontro la hoja "' + SHEET_NAME + '".');
    return;
  }

  var lastRow = sheet.getLastRow();
  var lastCol = sheet.getLastColumn();
  if (lastRow < 2) {
    Logger.log('[Massi] Sin filas de datos.');
    return;
  }

  var data = sheet.getRange(1, 1, lastRow, lastCol).getValues();
  var headers = data[0];

  // Mapear columnas por keyword (case/accent insensitive).
  var idx = {
    tel:        findCol_(headers, 'tel'),
    tipo:       findCol_(headers, 'tipo'),
    ciudad:     findCol_(headers, 'ciudad'),
    barrio:     findCol_(headers, 'barrio'),
    direccion:  findCol_(headers, 'direccion'),
    nombre:     findCol_(headers, 'nombre'),
    precio:     findCol_(headers, 'precio'),
    foto:       findCol_(headers, 'foto'),
    anunciante: findCol_(headers, 'anunciante'),
    url:        findCol_(headers, 'url'),
    arrend:     findCol_(headers, 'arrend'),
    estrato:    findCol_(headers, 'estrato')
  };

  if (idx.tel === -1) {
    Logger.log('ERROR: no hay columna de telefono (keyword "tel") en los headers: ' + headers.join(', '));
    return;
  }

  // Asegurar la columna "enviado_massi" (crearla al final si no existe).
  var idxEnviado = findColExacto_(headers, COL_ENVIADO);
  if (idxEnviado === -1) {
    idxEnviado = lastCol; // se insertara justo despues del ultimo
    sheet.getRange(1, idxEnviado + 1).setValue(COL_ENVIADO);
    headers.push(COL_ENVIADO);
    lastCol += 1;
    // Releer data con la columna nueva para que data[i].length coincida.
    data = sheet.getRange(1, 1, lastRow, lastCol).getValues();
  }

  var now = new Date();
  var okCount = 0;
  var errCount = 0;
  var skipTel = 0;
  var skipYa  = 0;

  for (var r = 1; r < data.length; r++) {
    var row = data[r];

    // Si ya tiene fecha en "enviado_massi" la saltamos.
    var marca = row[idxEnviado];
    if (marca !== '' && marca !== null && marca !== undefined) {
      skipYa += 1;
      continue;
    }

    var telRaw = row[idx.tel];
    var telefono = normalizarTel_(telRaw);
    if (!telefono) {
      skipTel += 1;
      if (telRaw) {
        Logger.log('[Massi] Fila ' + (r + 1) + ': telefono invalido ' +
                   JSON.stringify(telRaw) + ' — omitida.');
      }
      continue;
    }

    var tipoRaw    = getCell_(row, idx.tipo);
    var ciudadRaw  = getCell_(row, idx.ciudad);
    var barrio     = getCell_(row, idx.barrio);
    var direccion  = getCell_(row, idx.direccion);
    var nombre     = getCell_(row, idx.nombre);
    var precioRaw  = getCell_(row, idx.precio) || PRECIO_DEFAULT;
    var foto       = getCell_(row, idx.foto);
    var anunciante = getCell_(row, idx.anunciante);
    var url        = getCell_(row, idx.url);
    var operacion  = getCell_(row, idx.arrend);
    var estratoRaw = getCell_(row, idx.estrato);

    // El endpoint acepta anunciante como string o dict. Si no viene
    // nombre de anunciante pero si viene "operacion" (venta/arriendo)
    // lo mandamos como objeto para no perder el dato.
    var anuncianteFinal = anunciante;
    if (!anuncianteFinal && operacion) {
      anuncianteFinal = { operacion: operacion };
    }

    var payload = {
      portal:      PORTAL,
      telefono:    telefono,
      tipo_raw:    tipoRaw,
      ciudad_raw:  ciudadRaw,
      direccion:   direccion,
      barrio:      barrio,
      nombre:      nombre,
      estrato_raw: estratoRaw,
      foto:        foto,
      url:         url || ('sheet://' + SpreadsheetApp.getActive().getId() + '/fila/' + (r + 1)),
      anunciante:  anuncianteFinal,
      precio_raw:  precioRaw
    };

    var ok = enviarPayload_(payload, token, r + 1);
    if (ok) {
      // Escribimos la fecha en la columna de marca. Lo hacemos
      // 1 a 1 para que, si el script falla a mitad, lo ya
      // enviado quede marcado.
      sheet.getRange(r + 1, idxEnviado + 1).setValue(now);
      SpreadsheetApp.flush();
      okCount += 1;
    } else {
      errCount += 1;
    }
  }

  Logger.log('[Massi] Enviados OK: ' + okCount +
             ' | Errores: ' + errCount +
             ' | Sin tel valido: ' + skipTel +
             ' | Ya enviados: ' + skipYa);
}

/**
 * Marca todas las filas con datos (telefono presente) como ya
 * enviadas, sin hacer POST. Util cuando se instala el script
 * sobre un sheet con historial que NO se quiere reenviar.
 */
function marcarTodasEnviadas() {
  var sheet = SpreadsheetApp.getActive().getSheetByName(SHEET_NAME);
  if (!sheet) return;
  var lastRow = sheet.getLastRow();
  var lastCol = sheet.getLastColumn();
  if (lastRow < 2) return;

  var data = sheet.getRange(1, 1, lastRow, lastCol).getValues();
  var headers = data[0];
  var idxTel = findCol_(headers, 'tel');
  var idxEnviado = findColExacto_(headers, COL_ENVIADO);
  if (idxEnviado === -1) {
    idxEnviado = lastCol;
    sheet.getRange(1, idxEnviado + 1).setValue(COL_ENVIADO);
    lastCol += 1;
  }

  var now = new Date();
  var n = 0;
  for (var r = 1; r < data.length; r++) {
    if (idxTel !== -1 && data[r][idxTel]) {
      sheet.getRange(r + 1, idxEnviado + 1).setValue(now);
      n += 1;
    }
  }
  SpreadsheetApp.getUi().alert('Marcadas ' + n + ' filas como enviadas.');
}

/**
 * Borra la columna "enviado_massi" entera (deja el header). La
 * proxima corrida reenviara todas las filas al endpoint.
 */
function resetearMarcas() {
  var ui = SpreadsheetApp.getUi();
  var resp = ui.alert(
    'Resetear marcas',
    'Esto borra todas las fechas de "enviado_massi" y la proxima corrida reenviara TODAS las filas al endpoint. Continuar?',
    ui.ButtonSet.YES_NO
  );
  if (resp !== ui.Button.YES) return;

  var sheet = SpreadsheetApp.getActive().getSheetByName(SHEET_NAME);
  if (!sheet) return;
  var lastRow = sheet.getLastRow();
  var lastCol = sheet.getLastColumn();
  if (lastRow < 2) return;

  var headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
  var idxEnviado = findColExacto_(headers, COL_ENVIADO);
  if (idxEnviado === -1) {
    ui.alert('No existe la columna "' + COL_ENVIADO + '". Nada que resetear.');
    return;
  }

  sheet.getRange(2, idxEnviado + 1, lastRow - 1, 1).clearContent();
  ui.alert('Marcas borradas. La proxima corrida reenviara todas las filas.');
}

// ────────────────────── Helpers ──────────────────────────────

/**
 * Envia un payload al endpoint. Retorna true si HTTP 2xx.
 * Loguea el detalle del error en el Execution log.
 */
function enviarPayload_(payload, token, filaNumero) {
  var opts = {
    method: 'post',
    contentType: 'application/json',
    headers: { 'X-Ingest-Token': token },
    payload: JSON.stringify([payload]),  // el endpoint acepta array
    muteHttpExceptions: true
  };

  try {
    var resp = UrlFetchApp.fetch(INGEST_URL, opts);
    var code = resp.getResponseCode();
    var body = resp.getContentText();

    if (code >= 200 && code < 300) {
      Logger.log('[Massi] Fila ' + filaNumero + ' OK (HTTP ' + code + '): ' + body.substring(0, 200));
      return true;
    } else {
      Logger.log('[Massi] Fila ' + filaNumero + ' ERROR HTTP ' + code + ': ' + body.substring(0, 500));
      return false;
    }
  } catch (err) {
    Logger.log('[Massi] Fila ' + filaNumero + ' excepcion de red: ' + err);
    return false;
  }
}

/**
 * Normaliza un telefono colombiano al formato 573XXXXXXXXX.
 * - 10 digitos empezando en 3 → se prefija 57.
 * - Si queda 12 digitos empezando en 573, lo retorna.
 * - En caso contrario retorna null.
 */
function normalizarTel_(raw) {
  if (raw === null || raw === undefined || raw === '') return null;
  var d = String(raw).replace(/\D/g, '');
  if (d.length === 10 && d.charAt(0) === '3') d = '57' + d;
  if (d.length !== 12 || d.substring(0, 3) !== '573') return null;
  return d;
}

/**
 * Normaliza un header del sheet: sin acentos, lowercase, trim.
 * Usa Unicode property escapes (soportados en V8 que es el
 * runtime default de Apps Script).
 */
function normalizarHeader_(h) {
  if (h === null || h === undefined) return '';
  var s = String(h);
  try {
    s = s.normalize('NFD').replace(/\p{Diacritic}/gu, '');
  } catch (e) {
    // Fallback para runtimes antiguos (Rhino). Debera migrarse
    // a V8 si cae aqui, pero no rompemos la ejecucion.
    s = s.replace(/[̀-ͯ]/g, '');
  }
  return s.toLowerCase().trim();
}

/**
 * Busca un header por substring. Retorna el indice (0-based) o -1.
 * Es tolerante a acentos y mayusculas.
 */
function findCol_(headers, keyword) {
  var kw = normalizarHeader_(keyword);
  for (var i = 0; i < headers.length; i++) {
    var h = normalizarHeader_(headers[i]);
    if (!h) continue;
    if (h.indexOf(kw) !== -1 || kw.indexOf(h) !== -1) return i;
  }
  return -1;
}

/**
 * Busca un header por match exacto (normalizado). Util para la
 * columna de control "enviado_massi" que no queremos confundir
 * con substrings arbitrarios.
 */
function findColExacto_(headers, keyword) {
  var kw = normalizarHeader_(keyword);
  for (var i = 0; i < headers.length; i++) {
    if (normalizarHeader_(headers[i]) === kw) return i;
  }
  return -1;
}

/**
 * Lee la celda con proteccion de rango. Retorna '' si idx es -1
 * o si la fila no tiene ese indice.
 */
function getCell_(row, idx) {
  if (idx === -1 || idx === undefined || idx === null) return '';
  if (idx >= row.length) return '';
  var v = row[idx];
  if (v === null || v === undefined) return '';
  return String(v).trim();
}
