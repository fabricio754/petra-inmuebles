-- Esquema de Massi en PostgreSQL. Se ejecuta al arrancar el bot
-- (app/db.py): todo es CREATE ... IF NOT EXISTS, así que es seguro
-- correrlo cada vez.

-- Estado de cada conversación de WhatsApp. "datos" guarda el dict de
-- sesión que usa bot.py (conjunto, flow, flow_step, flow_data, ...).
CREATE TABLE IF NOT EXISTS sesiones (
  id SERIAL PRIMARY KEY,
  telefono VARCHAR(100) UNIQUE NOT NULL,
  estado VARCHAR(100),
  datos JSONB NOT NULL DEFAULT '{}',
  ultima_actividad TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Clasificador de texto libre (ver BOT_PATTERNS en app/bot.py): cuántas veces
-- un contacto nos escribió texto libre fuera de un flujo activo sin que
-- matcheara ningún patrón conocido. A la 2ª vez el bot lo deriva a un asesor.
-- El valor "canónico" se guarda dentro de `datos` JSONB (campo
-- veces_mensaje_libre) junto con el resto de la sesión; esta columna queda
-- disponible para consultas SQL directas si en el futuro hace falta.
ALTER TABLE sesiones ADD COLUMN IF NOT EXISTS veces_mensaje_libre INT DEFAULT 0;

-- Inventario de inmuebles del menú de Arrayanes. Temporal: se deja de
-- usar cuando el flujo de crédito reemplace ese menú (Hito 6).
CREATE TABLE IF NOT EXISTS inventario (
  id VARCHAR(100) PRIMARY KEY,
  datos JSONB NOT NULL,
  creado TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Registro de interacciones que hoy guarda save_lead (interesados,
-- crédito, pagos). Columnas fijas para filtrar + "datos" con todo.
CREATE TABLE IF NOT EXISTS leads (
  id SERIAL PRIMARY KEY,
  fecha TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  telefono VARCHAR(100),
  operacion VARCHAR(50),
  datos JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS leads_telefono_idx ON leads (telefono);

-- === Tablas del flujo de crédito (Hitos 6 a 10, docs/PLAN.md) ===========

CREATE TABLE IF NOT EXISTS contactos (
  id SERIAL PRIMARY KEY,
  telefono VARCHAR(20) UNIQUE,
  nombre VARCHAR(200),
  direccion TEXT,
  ciudad VARCHAR(100),
  tipo_inmueble VARCHAR(50),
  precio_publicado BIGINT,
  estrato SMALLINT,
  requiere_ph BOOLEAN DEFAULT FALSE,
  url_listing TEXT,
  portal VARCHAR(50),
  fecha_publicacion DATE,
  fecha_scraping TIMESTAMPTZ DEFAULT NOW(),
  contactado BOOLEAN DEFAULT FALSE,
  fecha_contacto TIMESTAMPTZ,
  resultado_contacto VARCHAR(50),
  -- Pidió no recibir más mensajes ("NO"): nunca se le vuelve a escribir.
  no_contactar BOOLEAN DEFAULT FALSE
);

-- Los usuarios con nombre de usuario de WhatsApp llegan con un ID tipo
-- "CO.1107512575300980" (hasta 128 caracteres), no con teléfono.
ALTER TABLE contactos ALTER COLUMN telefono TYPE VARCHAR(150);
-- Captación (Hito 7): foto del anuncio y monto "hasta" que se le ofreció.
ALTER TABLE contactos ADD COLUMN IF NOT EXISTS foto_url TEXT;
ALTER TABLE contactos ADD COLUMN IF NOT EXISTS monto_hasta_millones INTEGER;
ALTER TABLE contactos ADD COLUMN IF NOT EXISTS barrio TEXT;

-- Lead caliente: el contacto pidió explícitamente que lo llamen / hablar con
-- un humano. Se setea cuando `filtros.es_pedido_llamada()` matchea un IN de
-- texto. Idempotente (ver `db.marcar_requiere_humano`): la marca ocurre una
-- sola vez por contacto, para que la alerta externa no se dispare dos veces.
ALTER TABLE contactos
    ADD COLUMN IF NOT EXISTS requiere_humano boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS requiere_humano_motivo text,
    ADD COLUMN IF NOT EXISTS requiere_humano_at timestamptz;

-- Resultado de cierre de conversación (acción humana post-contacto).
-- `resultado_contacto` ya existe arriba como VARCHAR(50); los otros tres
-- campos registran cuándo, quién y una nota libre asociada al cierre.
ALTER TABLE contactos
    ADD COLUMN IF NOT EXISTS resultado_contacto text,
    ADD COLUMN IF NOT EXISTS resultado_contacto_at timestamptz,
    ADD COLUMN IF NOT EXISTS resultado_contacto_por text,
    ADD COLUMN IF NOT EXISTS resultado_contacto_nota text;

-- Audit de acciones humanas sobre un contacto desde el panel.
-- Cada acción del asesor (cerrar conversación, mandar al flow, marcar como
-- broker, etc.) deja una row inmutable aquí: quién, qué, cuándo y con qué
-- nota. Permite reconstruir el historial de intervenciones humanas.
CREATE TABLE IF NOT EXISTS panel_acciones (
    id bigserial PRIMARY KEY,
    telefono varchar(150) NOT NULL,
    accion text NOT NULL,
    nota text,
    actor text,
    fecha timestamptz NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_panel_acciones_telefono_fecha
    ON panel_acciones(telefono, fecha DESC);

CREATE TABLE IF NOT EXISTS documentos (
  id SERIAL PRIMARY KEY,
  telefono VARCHAR(100),
  tipo VARCHAR(50),
  media_id VARCHAR(200),
  url_storage TEXT,
  obtenido_automaticamente BOOLEAN DEFAULT FALSE,
  fecha_recepcion TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS documentos_telefono_idx ON documentos (telefono);

CREATE TABLE IF NOT EXISTS pipeline (
  id SERIAL PRIMARY KEY,
  telefono VARCHAR(100),
  nombre VARCHAR(200),
  cedula VARCHAR(20),
  edad SMALLINT,
  email VARCHAR(200),
  direccion_inmueble TEXT,
  ciudad VARCHAR(100),
  tipo_inmueble VARCHAR(50),
  estrato SMALLINT,
  es_ph BOOLEAN,
  chip VARCHAR(50),
  matricula_oficina VARCHAR(20),
  matricula_numero VARCHAR(50),
  avaluo_catastral BIGINT,
  avaluo_comercial BIGINT,
  valor_solicitado BIGINT,
  objetivo_prestamo TEXT,
  requiere_paz_salvo BOOLEAN DEFAULT FALSE,
  autorizacion_datos_en TIMESTAMPTZ,
  sureti_lead_id VARCHAR(100),
  estado VARCHAR(50),
  monto_aprobado BIGINT,
  razon_no_aprobado TEXT,
  fecha_ingreso TIMESTAMPTZ DEFAULT NOW(),
  fecha_aprobacion TIMESTAMPTZ,
  fecha_desembolso TIMESTAMPTZ,
  comision BIGINT,
  comision_cobrada BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS pipeline_telefono_idx ON pipeline (telefono);

ALTER TABLE pipeline ADD COLUMN IF NOT EXISTS requires_human BOOLEAN DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS remarketing (
  id SERIAL PRIMARY KEY,
  telefono VARCHAR(100),
  tipo VARCHAR(50),
  mensaje_enviado TEXT,
  fecha_envio TIMESTAMPTZ,
  respondio BOOLEAN DEFAULT FALSE
);

-- Marcas internas (ej. si ya se importaron los datos de la Sheet).
CREATE TABLE IF NOT EXISTS meta (
  clave VARCHAR(100) PRIMARY KEY,
  valor TEXT,
  actualizado TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Bitácora de mensajes del bot (entrada y salida), para auditoría y panel.
-- "direccion": 'in' (del contacto hacia el bot) o 'out' (del bot hacia el contacto).
-- "tipo": text, button_reply, list_reply, flow_reply, template, interactive, media, ...
-- "resumen": texto plano legible por una persona (lo que llegó o se envió).
-- "payload": JSONB con el objeto completo original (opcional, para debug).
CREATE TABLE IF NOT EXISTS mensajes (
  id BIGSERIAL PRIMARY KEY,
  telefono VARCHAR(150) NOT NULL,
  direccion VARCHAR(4) NOT NULL,
  tipo VARCHAR(40),
  resumen TEXT,
  payload JSONB,
  fecha TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS mensajes_telefono_fecha_idx
  ON mensajes (telefono, fecha DESC);

-- Trackeo del wa_msg_id en cada OUT para correlacionar con statuses de
-- WhatsApp (sent/delivered/read/failed). Meta devuelve ese id en la
-- respuesta de la Cloud API al enviar, y lo repite en los webhooks de
-- status. Lo guardamos para poder actualizar el estado del OUT cuando
-- llega cada webhook.
ALTER TABLE mensajes
    ADD COLUMN IF NOT EXISTS wa_msg_id text,
    ADD COLUMN IF NOT EXISTS estado_entrega text,
    ADD COLUMN IF NOT EXISTS estado_entrega_at timestamptz,
    ADD COLUMN IF NOT EXISTS estado_entrega_error text;

CREATE INDEX IF NOT EXISTS idx_mensajes_wa_msg_id
    ON mensajes(wa_msg_id) WHERE wa_msg_id IS NOT NULL;

-- === Nuevas features (iter 1 — oct-2026) =====================================
-- Pausar bot per lead: cuando el asesor "toma" la conversación de un lead,
-- el bot deja de responderle. NULL = bot activo. Un TIMESTAMP futuro = bot
-- pausado hasta ese momento. Un TIMESTAMP pasado = la pausa expiró y el bot
-- vuelve a responder.
-- Para pausa indefinida se usa el sentinel '2999-01-01'.
ALTER TABLE contactos
    ADD COLUMN IF NOT EXISTS bot_pausado_hasta TIMESTAMPTZ;

-- Marcar leído/no leído del lead en el panel. NULL (default) o
-- visto_at < última_actividad del contacto → no leído. visto_at >= última
-- actividad → leído. La última actividad se obtiene de
-- sesiones.ultima_actividad por telefono (o del último mensaje).
ALTER TABLE contactos
    ADD COLUMN IF NOT EXISTS visto_at TIMESTAMPTZ;

-- Notas internas sobre el lead que escribe el asesor. Nunca se envían al
-- cliente — son solo para contexto del equipo.
CREATE TABLE IF NOT EXISTS notas_contacto (
  id SERIAL PRIMARY KEY,
  contacto_id INTEGER NOT NULL REFERENCES contactos(id) ON DELETE CASCADE,
  texto TEXT NOT NULL,
  autor VARCHAR(100) DEFAULT 'asesor',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS notas_contacto_idx
  ON notas_contacto (contacto_id, created_at DESC);

-- Eventos del timeline del lead. Mezcla acciones humanas, estados del bot
-- y señales externas (bucket cambió, nota agregada, bot pausado, no_contactar
-- marcado, Sureti enviado, formulario enviado, llamada recibida, template de
-- remarketing, requiere_humano marcado). El panel combina estos eventos con
-- los mensajes (tabla `mensajes`) para armar la línea de tiempo unificada.
--
-- `detalle` es un JSONB con metadatos del evento (ej.
-- {from: 'nuevo', to: 'contactado'} para bucket_cambio).
CREATE TABLE IF NOT EXISTS eventos_contacto (
  id SERIAL PRIMARY KEY,
  contacto_id INTEGER NOT NULL REFERENCES contactos(id) ON DELETE CASCADE,
  tipo VARCHAR(50) NOT NULL,
  detalle JSONB NOT NULL DEFAULT '{}',
  autor VARCHAR(100) DEFAULT 'sistema',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS eventos_contacto_idx
  ON eventos_contacto (contacto_id, created_at DESC);

-- === Nuevas features (iter 3 — oct-2026) =====================================
-- Quick replies: biblioteca de respuestas pre-escritas que el asesor inserta
-- al responder manualmente a un lead. El texto puede contener variables entre
-- llaves dobles ({{nombre}}, {{ciudad}}, ...) que se interpolan desde los
-- datos del contacto antes de insertarlas en el textarea.
CREATE TABLE IF NOT EXISTS quick_replies (
  id SERIAL PRIMARY KEY,
  shortcut VARCHAR(50) NOT NULL UNIQUE,
  titulo VARCHAR(200) NOT NULL,
  texto TEXT NOT NULL,
  autor VARCHAR(100) DEFAULT 'asesor',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS quick_replies_shortcut_idx
  ON quick_replies (shortcut);

-- Cola persistente de webhooks de WhatsApp. El handler HTTP solo
-- INSERTa aquí y responde 200 OK; un worker (app/webhook_worker.py)
-- drena las filas pendientes. Si el proceso muere a mitad de camino,
-- lo pendiente queda en la DB y se reprocesa al reiniciar — 0 pérdidas.
CREATE TABLE IF NOT EXISTS webhook_queue (
  id BIGSERIAL PRIMARY KEY,
  payload JSONB NOT NULL,
  recibido_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  procesado_at TIMESTAMPTZ,
  intentos INT NOT NULL DEFAULT 0,
  ultimo_error TEXT,
  estado VARCHAR(20) NOT NULL DEFAULT 'pending'
    CHECK (estado IN ('pending', 'procesando', 'ok', 'error'))
);
CREATE INDEX IF NOT EXISTS webhook_queue_pendientes_idx
  ON webhook_queue (recibido_at) WHERE estado = 'pending';

-- === Piloto ElevenLabs (Massi) ============================================
-- Esquema aislado para el agente de voz/texto en +57 300 1278631. NO toca
-- las tablas productivas (contactos, mensajes, recordatorios). Lo consumen
-- los endpoints de `app/tools.py` via HTTP tools de ElevenLabs.

CREATE TABLE IF NOT EXISTS pilot_leads (
  id SERIAL PRIMARY KEY,
  telefono TEXT UNIQUE NOT NULL,
  nombre TEXT,
  ciudad TEXT,
  tipo_inmueble TEXT,
  avaluo NUMERIC,
  monto_solicitado NUMERIC,
  destino_credito TEXT,
  requiere_humano BOOLEAN DEFAULT FALSE,
  requiere_humano_motivo TEXT,
  requiere_humano_at TIMESTAMPTZ,
  enviado_sureti_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pilot_notas (
  id SERIAL PRIMARY KEY,
  telefono TEXT NOT NULL REFERENCES pilot_leads(telefono) ON DELETE CASCADE,
  autor TEXT DEFAULT 'massi',
  texto TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS pilot_notas_telefono_idx
  ON pilot_notas (telefono);

CREATE TABLE IF NOT EXISTS pilot_recordatorios (
  id SERIAL PRIMARY KEY,
  telefono TEXT NOT NULL REFERENCES pilot_leads(telefono) ON DELETE CASCADE,
  disparar_at TIMESTAMPTZ NOT NULL,
  texto TEXT NOT NULL,
  disparado BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS pilot_recordatorios_disparar_idx
  ON pilot_recordatorios (disparar_at, disparado);

-- === Nuevas features (iter 5 — oct-2026) ====================================
-- Next step / próxima acción por lead
ALTER TABLE contactos ADD COLUMN IF NOT EXISTS next_step_texto TEXT;
ALTER TABLE contactos ADD COLUMN IF NOT EXISTS next_step_fecha DATE;
ALTER TABLE contactos ADD COLUMN IF NOT EXISTS next_step_at TIMESTAMPTZ;

-- Reminders / recordatorios programados
CREATE TABLE IF NOT EXISTS reminders (
  id SERIAL PRIMARY KEY,
  contacto_id INTEGER NOT NULL REFERENCES contactos(id) ON DELETE CASCADE,
  texto TEXT NOT NULL,
  disparar_at TIMESTAMPTZ NOT NULL,
  estado VARCHAR(20) NOT NULL DEFAULT 'pendiente',
  disparado_at TIMESTAMPTZ,
  autor VARCHAR(100) DEFAULT 'asesor',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS reminders_disparar_idx ON reminders (estado, disparar_at);
CREATE INDEX IF NOT EXISTS reminders_contacto_idx ON reminders (contacto_id, estado);

-- Scheduled messages / mensajes programados a leads individuales
CREATE TABLE IF NOT EXISTS mensajes_programados (
  id SERIAL PRIMARY KEY,
  contacto_id INTEGER NOT NULL REFERENCES contactos(id) ON DELETE CASCADE,
  template_name VARCHAR(100) NOT NULL,
  template_params JSONB NOT NULL DEFAULT '{}',
  enviar_at TIMESTAMPTZ NOT NULL,
  estado VARCHAR(20) NOT NULL DEFAULT 'pendiente',
  enviado_at TIMESTAMPTZ,
  error TEXT,
  autor VARCHAR(100) DEFAULT 'asesor',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS mensajes_programados_envio_idx
  ON mensajes_programados (estado, enviar_at);

-- Broadcasts filtrados (envíos masivos auditables)
CREATE TABLE IF NOT EXISTS broadcasts (
  id SERIAL PRIMARY KEY,
  nombre VARCHAR(200),
  filtros JSONB NOT NULL DEFAULT '{}',
  template_name VARCHAR(100) NOT NULL,
  template_params JSONB NOT NULL DEFAULT '{}',
  total_destinatarios INT NOT NULL DEFAULT 0,
  total_enviados INT NOT NULL DEFAULT 0,
  total_fallidos INT NOT NULL DEFAULT 0,
  estado VARCHAR(20) NOT NULL DEFAULT 'pendiente',
  errores JSONB,
  autor VARCHAR(100) DEFAULT 'asesor',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS broadcasts_estado_idx ON broadcasts (estado, created_at DESC);

-- Vistas guardadas / filtros custom
CREATE TABLE IF NOT EXISTS vistas_guardadas (
  id SERIAL PRIMARY KEY,
  nombre VARCHAR(100) NOT NULL,
  ruta VARCHAR(50) NOT NULL,
  query_params JSONB NOT NULL DEFAULT '{}',
  autor VARCHAR(100) DEFAULT 'asesor',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS vistas_ruta_idx ON vistas_guardadas (ruta, created_at DESC);
