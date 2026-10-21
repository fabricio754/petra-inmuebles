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

-- Campos extra para calificación completa de Sureti (chat + voz).
ALTER TABLE pilot_leads ADD COLUMN IF NOT EXISTS direccion TEXT;
ALTER TABLE pilot_leads ADD COLUMN IF NOT EXISTS apto TEXT;
ALTER TABLE pilot_leads ADD COLUMN IF NOT EXISTS barrio TEXT;
ALTER TABLE pilot_leads ADD COLUMN IF NOT EXISTS estrato INTEGER;
ALTER TABLE pilot_leads ADD COLUMN IF NOT EXISTS es_ph BOOLEAN;
ALTER TABLE pilot_leads ADD COLUMN IF NOT EXISTS hipoteca BOOLEAN;
ALTER TABLE pilot_leads ADD COLUMN IF NOT EXISTS patrimonio_familia TEXT;
ALTER TABLE pilot_leads ADD COLUMN IF NOT EXISTS propietario_mayor_75 BOOLEAN;
ALTER TABLE pilot_leads ADD COLUMN IF NOT EXISTS paz_salvo BOOLEAN;
ALTER TABLE pilot_leads ADD COLUMN IF NOT EXISTS tipo_persona TEXT;
ALTER TABLE pilot_leads ADD COLUMN IF NOT EXISTS cedula_nit TEXT;
ALTER TABLE pilot_leads ADD COLUMN IF NOT EXISTS edad INTEGER;
ALTER TABLE pilot_leads ADD COLUMN IF NOT EXISTS email TEXT;

-- === PETRA plataforma inmobiliaria — Sprint 1 (oct 2026) =====================
-- Modelo multi-rol: contacto único con N roles acumulativos (interesado,
-- anunciante, lead crédito, agente Petra, cliente servicios activos).
-- Edificios + inventario de inmuebles por conjunto, conectados al QR en
-- portería. Prefijo petra_* para aislar del esquema productivo heredado.

CREATE TABLE IF NOT EXISTS petra_edificios (
  id SERIAL PRIMARY KEY,
  nombre TEXT NOT NULL,
  ciudad TEXT NOT NULL,
  direccion TEXT,
  barrio TEXT,
  estrato INTEGER,
  qr_slug TEXT UNIQUE,
  agente_petra_contacto_id INTEGER,
  activo BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_edificios_ciudad_idx
  ON petra_edificios (ciudad, activo);
CREATE INDEX IF NOT EXISTS petra_edificios_qr_idx
  ON petra_edificios (qr_slug) WHERE qr_slug IS NOT NULL;

CREATE TABLE IF NOT EXISTS petra_contactos (
  id SERIAL PRIMARY KEY,
  telefono TEXT UNIQUE NOT NULL,
  nombre TEXT,
  email TEXT,
  opt_out BOOLEAN DEFAULT FALSE,
  opt_out_motivo TEXT,
  opt_out_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS petra_contacto_roles (
  id SERIAL PRIMARY KEY,
  contacto_id INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
  rol TEXT NOT NULL,
  estado TEXT NOT NULL DEFAULT 'activo',
  edificio_id INTEGER REFERENCES petra_edificios(id) ON DELETE SET NULL,
  metadata JSONB DEFAULT '{}'::jsonb,
  estado_desde TIMESTAMPTZ DEFAULT NOW(),
  proximo_contacto_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (contacto_id, rol, edificio_id)
);
CREATE INDEX IF NOT EXISTS petra_contacto_roles_contacto_idx
  ON petra_contacto_roles (contacto_id, rol);
CREATE INDEX IF NOT EXISTS petra_contacto_roles_proximo_idx
  ON petra_contacto_roles (proximo_contacto_at)
  WHERE proximo_contacto_at IS NOT NULL AND estado = 'activo';

CREATE TABLE IF NOT EXISTS petra_inmuebles (
  id SERIAL PRIMARY KEY,
  edificio_id INTEGER NOT NULL REFERENCES petra_edificios(id) ON DELETE CASCADE,
  contacto_id INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
  tipo TEXT NOT NULL,
  modalidad TEXT NOT NULL,
  torre TEXT,
  piso TEXT,
  area_m2 NUMERIC,
  parqueadero BOOLEAN,
  precio_millones NUMERIC,
  precio_arriendo_mensual_millones NUMERIC,
  nombre_anunciante TEXT,
  telefono_contacto TEXT,
  estado TEXT NOT NULL DEFAULT 'borrador',
  publicado_at TIMESTAMPTZ,
  cerrado_at TIMESTAMPTZ,
  cerrado_motivo TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_inmuebles_edificio_estado_idx
  ON petra_inmuebles (edificio_id, estado);
CREATE INDEX IF NOT EXISTS petra_inmuebles_contacto_idx
  ON petra_inmuebles (contacto_id, estado);
CREATE INDEX IF NOT EXISTS petra_inmuebles_modalidad_idx
  ON petra_inmuebles (modalidad, estado) WHERE estado = 'activo';

CREATE TABLE IF NOT EXISTS petra_conversacion_estado (
  id SERIAL PRIMARY KEY,
  contacto_id INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
  flow_name TEXT NOT NULL,
  paso_actual TEXT NOT NULL,
  datos_parciales JSONB DEFAULT '{}'::jsonb,
  contexto JSONB DEFAULT '{}'::jsonb,
  activa BOOLEAN DEFAULT TRUE,
  ultimo_mensaje_at TIMESTAMPTZ DEFAULT NOW(),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (contacto_id, flow_name, activa)
);
CREATE INDEX IF NOT EXISTS petra_conversacion_estado_activa_idx
  ON petra_conversacion_estado (contacto_id, activa)
  WHERE activa = TRUE;

-- Sprint 3: búsquedas (criterios de interesado) + cola de salida + remarketing.

CREATE TABLE IF NOT EXISTS petra_busquedas (
  id SERIAL PRIMARY KEY,
  contacto_id INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
  edificio_id INTEGER REFERENCES petra_edificios(id) ON DELETE SET NULL,
  modalidad TEXT NOT NULL,
  tipo TEXT,
  precio_min_millones NUMERIC,
  precio_max_millones NUMERIC,
  area_min_m2 NUMERIC,
  area_max_m2 NUMERIC,
  parqueadero BOOLEAN,
  activa BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_busquedas_edif_modal_idx
  ON petra_busquedas (edificio_id, modalidad, activa) WHERE activa = TRUE;
CREATE INDEX IF NOT EXISTS petra_busquedas_contacto_idx
  ON petra_busquedas (contacto_id, activa);

CREATE TABLE IF NOT EXISTS petra_outbound_queue (
  id SERIAL PRIMARY KEY,
  contacto_id INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
  telefono TEXT NOT NULL,
  tipo TEXT NOT NULL,
  payload JSONB NOT NULL,
  origen TEXT,
  estado TEXT NOT NULL DEFAULT 'pendiente',
  intentos INTEGER DEFAULT 0,
  error TEXT,
  enviado_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_outbound_queue_pendiente_idx
  ON petra_outbound_queue (estado, created_at) WHERE estado = 'pendiente';
CREATE INDEX IF NOT EXISTS petra_outbound_queue_contacto_idx
  ON petra_outbound_queue (contacto_id, created_at DESC);

CREATE TABLE IF NOT EXISTS petra_remarketing_eventos (
  id SERIAL PRIMARY KEY,
  contacto_id INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
  regla TEXT NOT NULL,
  entidad_tipo TEXT,
  entidad_id INTEGER,
  disparado_at TIMESTAMPTZ DEFAULT NOW(),
  resultado TEXT,
  skipped_motivo TEXT
);
CREATE INDEX IF NOT EXISTS petra_remarketing_eventos_regla_idx
  ON petra_remarketing_eventos (regla, disparado_at DESC);
CREATE INDEX IF NOT EXISTS petra_remarketing_eventos_contacto_idx
  ON petra_remarketing_eventos (contacto_id, regla, disparado_at DESC);

-- Log de requests al endpoint Custom LLM. Útil para observabilidad y
-- debugging del agente de ElevenLabs; también permite reconstruir
-- conversaciones si el histórico se pierde del lado del cliente.
CREATE TABLE IF NOT EXISTS petra_llm_logs (
  id SERIAL PRIMARY KEY,
  telefono VARCHAR(20),
  rama TEXT,                           -- 'router' | 'claude' | 'fallback'
  texto_usuario TEXT,
  texto_asistente TEXT,
  mensajes_in JSONB,
  stream BOOLEAN,
  latencia_ms INTEGER,
  error TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_llm_logs_telefono_idx
  ON petra_llm_logs (telefono, created_at DESC);
CREATE INDEX IF NOT EXISTS petra_llm_logs_created_idx
  ON petra_llm_logs (created_at DESC);

-- =============================================================================
-- Sprint 6 — Schema completo de la plataforma Petra
-- Agrega las entidades transaccionales que faltaban: oportunidades, contratos,
-- visitas, casos de soporte, comisiones, consentimientos, auditoría.
-- Separa "publicar en inventario" de "contratar servicio con Petra".
-- =============================================================================

-- Pipeline comercial. Una oportunidad representa una posible transacción:
-- venta/arriendo de un inmueble, un crédito, un servicio de admin de arriendo.
-- Vive aunque el inmueble pase a vendido o la persona desaparezca.
CREATE TABLE IF NOT EXISTS petra_oportunidades (
  id SERIAL PRIMARY KEY,
  contacto_id INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
  inmueble_id INTEGER REFERENCES petra_inmuebles(id) ON DELETE SET NULL,
  tipo TEXT NOT NULL,
      -- venta | arriendo | credito | admin_arriendo | intermediacion
  estado TEXT NOT NULL DEFAULT 'lead',
      -- lead | contactado | visita_agendada | visita_completa | oferta
      -- | negociando | cerrado_ganado | cerrado_perdido | pausado
  origen TEXT,
      -- qr | template | anuncio | referido | cross_sell | panel
  origen_metadata JSONB,
  valor_cop BIGINT,
  responsable_contacto_id INTEGER REFERENCES petra_contactos(id) ON DELETE SET NULL,
  prob_cierre SMALLINT DEFAULT 0,
  cerrado_motivo TEXT,
  cerrado_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_oportunidades_contacto_idx
  ON petra_oportunidades (contacto_id, estado);
CREATE INDEX IF NOT EXISTS petra_oportunidades_inmueble_idx
  ON petra_oportunidades (inmueble_id) WHERE inmueble_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS petra_oportunidades_estado_idx
  ON petra_oportunidades (estado, updated_at DESC);
CREATE INDEX IF NOT EXISTS petra_oportunidades_tipo_idx
  ON petra_oportunidades (tipo, estado);

-- Contrato de servicio con Petra. Separado de la publicación en inventario:
-- un propietario puede publicar sin contratar nada; para que Petra opere su
-- venta/arriendo, firma uno de estos.
CREATE TABLE IF NOT EXISTS petra_contratos (
  id SERIAL PRIMARY KEY,
  oportunidad_id INTEGER REFERENCES petra_oportunidades(id) ON DELETE SET NULL,
  contacto_id INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
  tipo TEXT NOT NULL,
      -- intermediacion_venta | intermediacion_arriendo | admin_arriendo | credito
  estado TEXT NOT NULL DEFAULT 'borrador',
      -- borrador | enviado_firma | firmado | activo | cancelado | vencido
  documento_url TEXT,
  firma_proveedor TEXT,
      -- auco | manual | otro
  firma_proveedor_tx_id TEXT,
  firma_tipo TEXT,
      -- electronica_simple | electronica_avanzada | digital
  firmado_at TIMESTAMPTZ,
  inicio DATE,
  fin DATE,
  condiciones JSONB DEFAULT '{}',
      -- % comisión, canon, fee fijo, descuentos, cláusulas especiales
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_contratos_contacto_idx
  ON petra_contratos (contacto_id, estado);
CREATE INDEX IF NOT EXISTS petra_contratos_oportunidad_idx
  ON petra_contratos (oportunidad_id) WHERE oportunidad_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS petra_contratos_firma_tx_idx
  ON petra_contratos (firma_proveedor_tx_id) WHERE firma_proveedor_tx_id IS NOT NULL;

-- Visitas (comerciales y de entrega/recepción de inmueble).
CREATE TABLE IF NOT EXISTS petra_visitas (
  id SERIAL PRIMARY KEY,
  inmueble_id INTEGER REFERENCES petra_inmuebles(id) ON DELETE SET NULL,
  oportunidad_id INTEGER REFERENCES petra_oportunidades(id) ON DELETE SET NULL,
  agente_id INTEGER REFERENCES petra_contactos(id) ON DELETE SET NULL,
  interesado_id INTEGER REFERENCES petra_contactos(id) ON DELETE SET NULL,
  tipo TEXT NOT NULL,
      -- comercial | entrega | recepcion | inspeccion
  programada_at TIMESTAMPTZ,
  estado TEXT NOT NULL DEFAULT 'programada',
      -- programada | confirmada | realizada | cancelada | no_show
  checklist JSONB DEFAULT '{}',
  evidencias JSONB DEFAULT '[]',
      -- lista de {tipo, url, categoria, descripcion, fecha}
  acta_url TEXT,
  acta_firma_tx_id TEXT,
  observaciones TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_visitas_agente_idx
  ON petra_visitas (agente_id, programada_at DESC);
CREATE INDEX IF NOT EXISTS petra_visitas_inmueble_idx
  ON petra_visitas (inmueble_id, programada_at DESC);
CREATE INDEX IF NOT EXISTS petra_visitas_estado_idx
  ON petra_visitas (estado, programada_at);

-- Casos de soporte / atención al cliente.
CREATE TABLE IF NOT EXISTS petra_casos_soporte (
  id SERIAL PRIMARY KEY,
  contacto_id INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
  contrato_id INTEGER REFERENCES petra_contratos(id) ON DELETE SET NULL,
  categoria TEXT NOT NULL,
      -- cobro | mantenimiento | reclamo | consulta | cancelacion | otros
  prioridad TEXT NOT NULL DEFAULT 'media',
      -- baja | media | alta | critica
  estado TEXT NOT NULL DEFAULT 'abierto',
      -- abierto | en_gestion | pendiente_cliente | escalado | resuelto | cerrado
  responsable_humano TEXT,
  descripcion TEXT,
  sla_primera_respuesta_at TIMESTAMPTZ,
  sla_resolucion_at TIMESTAMPTZ,
  primera_respuesta_at TIMESTAMPTZ,
  resuelto_at TIMESTAMPTZ,
  resolucion_nota TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_casos_contacto_idx
  ON petra_casos_soporte (contacto_id, estado);
CREATE INDEX IF NOT EXISTS petra_casos_estado_idx
  ON petra_casos_soporte (estado, prioridad, created_at);
CREATE INDEX IF NOT EXISTS petra_casos_sla_idx
  ON petra_casos_soporte (sla_primera_respuesta_at)
  WHERE primera_respuesta_at IS NULL;

-- Comisiones a liquidar por los agentes Petra.
CREATE TABLE IF NOT EXISTS petra_comisiones (
  id SERIAL PRIMARY KEY,
  agente_id INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
  evento_tipo TEXT NOT NULL,
      -- edificio_activado | inmueble_publicado | visita_realizada
      -- | cierre_venta | cierre_arriendo | cierre_credito | cierre_admin
  evento_entidad TEXT,
  evento_entidad_id INTEGER,
      -- (polimórfica: edificio_id | inmueble_id | visita_id | contrato_id ...)
  monto_cop BIGINT NOT NULL DEFAULT 0,
  moneda TEXT DEFAULT 'COP',
  estado TEXT NOT NULL DEFAULT 'causada',
      -- causada | aprobada | pagada | cancelada | reversada
  base_calculo JSONB,
      -- {regla, porcentaje, valor_base, override_motivo}
  aprobada_at TIMESTAMPTZ,
  pagada_at TIMESTAMPTZ,
  aprobada_por TEXT,
  nota TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_comisiones_agente_idx
  ON petra_comisiones (agente_id, estado, created_at DESC);
CREATE INDEX IF NOT EXISTS petra_comisiones_evento_idx
  ON petra_comisiones (evento_entidad, evento_entidad_id);
-- Evita contar dos veces el mismo evento para el mismo agente.
CREATE UNIQUE INDEX IF NOT EXISTS petra_comisiones_unica_idx
  ON petra_comisiones (agente_id, evento_tipo, evento_entidad, evento_entidad_id)
  WHERE evento_entidad_id IS NOT NULL;

-- Consentimientos del contacto. Un registro por finalidad; cuando se revoca,
-- se guarda la fecha pero no se borra (para auditoría).
CREATE TABLE IF NOT EXISTS petra_consentimientos (
  id SERIAL PRIMARY KEY,
  contacto_id INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
  finalidad TEXT NOT NULL,
      -- tratamiento_datos | comunicaciones_comerciales | publicidad
      -- | grabacion_voz | cesion_terceros | geolocalizacion
  estado TEXT NOT NULL DEFAULT 'otorgado',
      -- otorgado | revocado
  canal TEXT,
      -- whatsapp | voz | chat | email | panel | formulario
  evidencia JSONB,
      -- {texto_mostrado, texto_respuesta, template_id, wa_msg_id, ip, ua}
  version_politica TEXT,
  otorgado_at TIMESTAMPTZ DEFAULT NOW(),
  revocado_at TIMESTAMPTZ,
  revocado_motivo TEXT
);
CREATE INDEX IF NOT EXISTS petra_consentimientos_contacto_idx
  ON petra_consentimientos (contacto_id, finalidad, estado);
-- Un contacto solo puede tener un consentimiento OTORGADO por finalidad a la vez
-- (los revocados se dejan en historia).
CREATE UNIQUE INDEX IF NOT EXISTS petra_consentimientos_activo_idx
  ON petra_consentimientos (contacto_id, finalidad)
  WHERE estado = 'otorgado';

-- Auditoría genérica de cambios y acciones. No es un reemplazo de logs
-- técnicos: es la bitácora de "qué pasó en el dominio". Se usa para
-- reconstruir la vida de un contacto/inmueble/oportunidad.
CREATE TABLE IF NOT EXISTS petra_auditoria_eventos (
  id BIGSERIAL PRIMARY KEY,
  actor_tipo TEXT,
      -- contacto | agente_petra | sistema | humano_panel | integracion_externa
  actor_id INTEGER,
  actor_label TEXT,
  accion TEXT NOT NULL,
      -- publicar_inmueble | cambiar_estado | enviar_template | otorgar_consentimiento
      -- | crear_oportunidad | firmar_contrato | ...
  entidad_tipo TEXT NOT NULL,
      -- contacto | inmueble | oportunidad | contrato | visita | caso | comision
  entidad_id INTEGER,
  antes JSONB,
  despues JSONB,
  metadata JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_auditoria_entidad_idx
  ON petra_auditoria_eventos (entidad_tipo, entidad_id, created_at DESC);
CREATE INDEX IF NOT EXISTS petra_auditoria_actor_idx
  ON petra_auditoria_eventos (actor_tipo, actor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS petra_auditoria_accion_idx
  ON petra_auditoria_eventos (accion, created_at DESC);

-- Ampliar estados ricos del inmueble sin romper lo existente. Se mantiene
-- la columna existente 'estado' en petra_inmuebles; agregamos campos
-- auxiliares para el ciclo de vida más detallado.
ALTER TABLE petra_inmuebles ADD COLUMN IF NOT EXISTS reservado_hasta TIMESTAMPTZ;
ALTER TABLE petra_inmuebles ADD COLUMN IF NOT EXISTS vence_at TIMESTAMPTZ;
ALTER TABLE petra_inmuebles ADD COLUMN IF NOT EXISTS validado_en TIMESTAMPTZ;
ALTER TABLE petra_inmuebles ADD COLUMN IF NOT EXISTS validado_por TEXT;
ALTER TABLE petra_inmuebles ADD COLUMN IF NOT EXISTS motivo_pausa TEXT;
-- Hash de deduplicación: (edificio, torre, piso, tipo, modalidad) normalizados.
ALTER TABLE petra_inmuebles ADD COLUMN IF NOT EXISTS dedup_hash TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS petra_inmuebles_dedup_idx
  ON petra_inmuebles (dedup_hash)
  WHERE dedup_hash IS NOT NULL AND estado IN ('borrador','activo','reservado','pausado');

-- =============================================================================
-- Sprint 13 — Flujo 13: recuperación de errores de integración externa
-- Cola de reintentos para fallos con Meta / Sureti / Auco / ElevenLabs.
-- =============================================================================
CREATE TABLE IF NOT EXISTS petra_integracion_errores (
  id BIGSERIAL PRIMARY KEY,
  proveedor TEXT NOT NULL,
      -- meta_whatsapp | sureti | auco | elevenlabs | otro
  operacion TEXT NOT NULL,
      -- enviar_template | enviar_mensaje | crear_solicitud | firmar_doc | ...
  entidad_tipo TEXT,
  entidad_id INTEGER,
  payload JSONB,
  error_mensaje TEXT,
  error_codigo TEXT,
  intentos INTEGER NOT NULL DEFAULT 0,
  max_intentos INTEGER NOT NULL DEFAULT 5,
  estado TEXT NOT NULL DEFAULT 'pendiente_retry',
      -- pendiente_retry | reintentando | resuelto | dead_letter | manual
  proximo_intento_at TIMESTAMPTZ DEFAULT NOW(),
  resuelto_at TIMESTAMPTZ,
  nota_humano TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_integ_err_pendiente_idx
  ON petra_integracion_errores (proximo_intento_at)
  WHERE estado = 'pendiente_retry';
CREATE INDEX IF NOT EXISTS petra_integ_err_proveedor_idx
  ON petra_integracion_errores (proveedor, estado, created_at DESC);
CREATE INDEX IF NOT EXISTS petra_integ_err_dead_letter_idx
  ON petra_integracion_errores (created_at DESC) WHERE estado = 'dead_letter';

-- Hits del link público de reclutamiento (/petra/agente-link). Registra UTM
-- + user-agent + IP para rastrear campañas sin ensuciar el mensaje de WA.
CREATE TABLE IF NOT EXISTS petra_agente_link_hits (
  id BIGSERIAL PRIMARY KEY,
  utm_source TEXT,
  utm_campaign TEXT,
  utm_medium TEXT,
  user_agent TEXT,
  ip TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_agente_link_hits_created_idx
  ON petra_agente_link_hits (created_at DESC);
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

-- =============================================================================
-- Contenidos del piloto Petra: videos de capacitación, PDFs, scripts,
-- plantillas de texto. Fabricio los sube desde el panel; Massi los usa.
-- =============================================================================
CREATE TABLE IF NOT EXISTS petra_contenidos (
  id            BIGSERIAL PRIMARY KEY,
  tipo          TEXT NOT NULL,
    -- video_capacitacion | documento_pdf | script_mensaje | kit_autorizacion |
    -- plantilla_whatsapp | otro
  orden         INTEGER NOT NULL DEFAULT 0,
  titulo        TEXT NOT NULL,
  url           TEXT,
    -- URL externa (YouTube, Drive, S3, etc.)
  texto         TEXT,
    -- Contenido inline cuando NO hay URL (scripts, plantillas, FAQ)
  duracion_seg  INTEGER,
  descripcion   TEXT,
  activo        BOOLEAN NOT NULL DEFAULT TRUE,
  metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_contenidos_tipo_idx
  ON petra_contenidos (tipo, orden ASC) WHERE activo = TRUE;

-- =============================================================================
-- FASE A — Agente Petra operativo
-- =============================================================================

-- A1. Progreso de capacitación del agente contra petra_contenidos
CREATE TABLE IF NOT EXISTS petra_capacitacion_progreso (
  id              BIGSERIAL PRIMARY KEY,
  contacto_id     INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
  contenido_id    INTEGER NOT NULL REFERENCES petra_contenidos(id) ON DELETE CASCADE,
  enviado_at      TIMESTAMPTZ,
  visto_at        TIMESTAMPTZ,
  comentario      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (contacto_id, contenido_id)
);
CREATE INDEX IF NOT EXISTS petra_cap_prog_agente_idx
  ON petra_capacitacion_progreso (contacto_id, visto_at NULLS FIRST);

-- A2. Autorizaciones del administrador del conjunto para instalar el QR
CREATE TABLE IF NOT EXISTS petra_autorizaciones_admin (
  id              BIGSERIAL PRIMARY KEY,
  agente_id       INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
  conjunto_nombre TEXT NOT NULL,
  edificio_id     INTEGER REFERENCES petra_edificios(id) ON DELETE SET NULL,
  estado          TEXT NOT NULL DEFAULT 'pendiente',
      -- pendiente | kit_enviado | en_gestion | aprobada | rechazada | descartada
  kit_enviado_at  TIMESTAMPTZ,
  aprobada_at     TIMESTAMPTZ,
  nota            TEXT,
  metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_autoriz_estado_idx
  ON petra_autorizaciones_admin (estado, updated_at DESC);
CREATE INDEX IF NOT EXISTS petra_autoriz_agente_idx
  ON petra_autorizaciones_admin (agente_id);

-- A4. Anuncios existentes detectados por el agente para sobrepublicar con QR
CREATE TABLE IF NOT EXISTS petra_anuncios_detectados (
  id              BIGSERIAL PRIMARY KEY,
  agente_id       INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
  edificio_id     INTEGER REFERENCES petra_edificios(id) ON DELETE SET NULL,
  url_anuncio     TEXT,
  foto_url        TEXT,
  portal          TEXT,
      -- fincaraiz | metrocuadrado | mercadolibre | olx | otro
  estado          TEXT NOT NULL DEFAULT 'pendiente',
      -- pendiente | qr_cargado | descartado
  nota            TEXT,
  cargado_at      TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_anuncios_estado_idx
  ON petra_anuncios_detectados (estado, created_at DESC);

-- =============================================================================
-- FASE B — Captura de inventario (mandatos de venta/arriendo/administración)
-- =============================================================================
CREATE TABLE IF NOT EXISTS petra_mandatos (
  id              BIGSERIAL PRIMARY KEY,
  contacto_id     INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
      -- propietario
  inmueble_id     INTEGER REFERENCES petra_inmuebles(id) ON DELETE SET NULL,
  tipo            TEXT NOT NULL,
      -- venta | arriendo | administracion
  comision_pct    NUMERIC(5,2),
      -- comisión acordada (p.ej. 3.00 = 3%)
  canon_acordado  BIGINT,
      -- COP para arriendo/administracion
  exclusivo       BOOLEAN NOT NULL DEFAULT FALSE,
  plazo_meses     INTEGER,
  estado          TEXT NOT NULL DEFAULT 'propuesto',
      -- propuesto | firmado | activo | finalizado | cancelado
  auco_doc_id     TEXT,
      -- id del documento en Auco cuando se mande a firma
  firmado_at      TIMESTAMPTZ,
  nota            TEXT,
  metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_mandatos_estado_idx
  ON petra_mandatos (estado, tipo, updated_at DESC);
CREATE INDEX IF NOT EXISTS petra_mandatos_contacto_idx
  ON petra_mandatos (contacto_id);

CREATE TABLE IF NOT EXISTS petra_administraciones_arriendo (
  id              BIGSERIAL PRIMARY KEY,
  mandato_id      INTEGER NOT NULL REFERENCES petra_mandatos(id) ON DELETE CASCADE,
  propietario_id  INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
  inmueble_id     INTEGER REFERENCES petra_inmuebles(id) ON DELETE SET NULL,
  inquilino_tel   TEXT,
  inquilino_nombre TEXT,
  canon           BIGINT NOT NULL,
  dia_corte       SMALLINT NOT NULL DEFAULT 5,
      -- día del mes en que vence el canon
  cuenta_bancaria TEXT,
      -- cuenta del propietario donde se transfiere
  estado          TEXT NOT NULL DEFAULT 'activa',
      -- activa | pausada | terminada
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_admin_arr_estado_idx
  ON petra_administraciones_arriendo (estado, updated_at DESC);

-- =============================================================================
-- FASE C — Interesados y cierre (ofertas, slots de visita, postulaciones)
-- =============================================================================
CREATE TABLE IF NOT EXISTS petra_slots_visita (
  id              BIGSERIAL PRIMARY KEY,
  oportunidad_id  INTEGER NOT NULL REFERENCES petra_oportunidades(id) ON DELETE CASCADE,
  slots           JSONB NOT NULL,
      -- array de ISO8601: ["2026-10-20T15:00:00-05:00", ...]
  elegido_at      TIMESTAMPTZ,
  elegido_idx     SMALLINT,
  visita_id       INTEGER REFERENCES petra_visitas(id) ON DELETE SET NULL,
  estado          TEXT NOT NULL DEFAULT 'propuesto',
      -- propuesto | confirmado | cancelado | expirado
  propuesto_por   TEXT,
      -- panel | massi | agente
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_slots_oport_idx
  ON petra_slots_visita (oportunidad_id, created_at DESC);

CREATE TABLE IF NOT EXISTS petra_ofertas (
  id              BIGSERIAL PRIMARY KEY,
  oportunidad_id  INTEGER NOT NULL REFERENCES petra_oportunidades(id) ON DELETE CASCADE,
  interesado_id   INTEGER REFERENCES petra_contactos(id) ON DELETE SET NULL,
  monto           BIGINT NOT NULL,
  condiciones     TEXT,
  estado          TEXT NOT NULL DEFAULT 'presentada',
      -- presentada | aceptada | rechazada | contraofertada | retirada
  respuesta_propietario TEXT,
  respondida_at   TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_ofertas_oport_idx
  ON petra_ofertas (oportunidad_id, created_at DESC);

CREATE TABLE IF NOT EXISTS petra_postulaciones_arriendo (
  id              BIGSERIAL PRIMARY KEY,
  oportunidad_id  INTEGER REFERENCES petra_oportunidades(id) ON DELETE SET NULL,
  inmueble_id     INTEGER REFERENCES petra_inmuebles(id) ON DELETE SET NULL,
  interesado_id   INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
  codeudor_nombre TEXT,
  codeudor_tel    TEXT,
  ingresos_decl   BIGINT,
  docs_entregados JSONB NOT NULL DEFAULT '[]'::jsonb,
  estado          TEXT NOT NULL DEFAULT 'recibida',
      -- recibida | en_estudio | aprobada | rechazada | contrato_firmado
  nota            TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_postul_estado_idx
  ON petra_postulaciones_arriendo (estado, created_at DESC);

-- =============================================================================
-- FASE D — Pagos de arriendo (administración mensual)
-- =============================================================================
CREATE TABLE IF NOT EXISTS petra_pagos_arriendo (
  id              BIGSERIAL PRIMARY KEY,
  administracion_id INTEGER NOT NULL REFERENCES petra_administraciones_arriendo(id) ON DELETE CASCADE,
  mes_anio        TEXT NOT NULL,
      -- 'YYYY-MM'
  monto_esperado  BIGINT NOT NULL,
  monto_pagado    BIGINT,
  estado          TEXT NOT NULL DEFAULT 'pendiente',
      -- pendiente | pagado | parcial | atrasado | condonado
  pagado_at       TIMESTAMPTZ,
  comprobante_url TEXT,
  nota            TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (administracion_id, mes_anio)
);
CREATE INDEX IF NOT EXISTS petra_pagos_arr_estado_idx
  ON petra_pagos_arriendo (estado, mes_anio DESC);

-- Tokens de dashboard pública (propietario / agente ven sus datos read-only)
CREATE TABLE IF NOT EXISTS petra_dashboard_tokens (
  token           TEXT PRIMARY KEY,
  contacto_id     INTEGER NOT NULL REFERENCES petra_contactos(id) ON DELETE CASCADE,
  rol_vista       TEXT NOT NULL,
      -- agente | propietario
  activo          BOOLEAN NOT NULL DEFAULT TRUE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ultimo_acceso_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS petra_dash_tokens_contacto_idx
  ON petra_dashboard_tokens (contacto_id, rol_vista) WHERE activo = TRUE;

-- Multi-agent: intent activo del contacto (a qué sub-agente estamos
-- ruteando los mensajes recientes). Expira por inactividad en lectura.
CREATE TABLE IF NOT EXISTS petra_intent_activo (
  telefono    TEXT PRIMARY KEY,
  agente      TEXT NOT NULL,
  razon       TEXT,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_intent_activo_updated_idx
  ON petra_intent_activo (updated_at DESC);

-- Sesión conversacional del contacto. Una sesión agrupa los mensajes
-- consecutivos (<5 min entre uno y otro). Si pasan 5+ min sin actividad,
-- el cron la cierra con un mensaje "Vamos a cerrar esta conversación..."
-- Al próximo mensaje tras el cierre, se abre una sesión nueva (fresh).
-- Historial que Claude ve = mensajes DE la sesión actual (NO basado en
-- ventana temporal arbitraria).
CREATE TABLE IF NOT EXISTS petra_sesion_chat (
  id             BIGSERIAL PRIMARY KEY,
  telefono       TEXT NOT NULL,
  abierta_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ultimo_msg_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  cerrada_at     TIMESTAMPTZ,
  razon_cierre   TEXT
);
CREATE INDEX IF NOT EXISTS petra_sesion_chat_abierta_idx
  ON petra_sesion_chat (telefono, cerrada_at)
  WHERE cerrada_at IS NULL;
CREATE INDEX IF NOT EXISTS petra_sesion_chat_inactividad_idx
  ON petra_sesion_chat (ultimo_msg_at)
  WHERE cerrada_at IS NULL;

-- Dedup de webhooks entrantes: evita que Petra responda 2 veces el mismo
-- wa_msg_id si Meta reenvía o si hay un retry.
CREATE TABLE IF NOT EXISTS petra_mensajes_vistos (
  wa_msg_id  TEXT PRIMARY KEY,
  telefono   TEXT,
  visto_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_mensajes_vistos_visto_idx
  ON petra_mensajes_vistos (visto_at);

-- =============================================================================
-- FASE A — Textos editables desde panel
-- =============================================================================
-- Diccionario de textos del bot editable sin tocar código.
-- Clave = dot-notation estable (ej. "autorizacion.msg_bienvenida").
-- Contenido = plantilla con placeholders {var} que el helper `t()` llena.
-- Si una clave no existe en DB, el código cae a su hardcode fallback.
CREATE TABLE IF NOT EXISTS petra_textos (
  clave         TEXT PRIMARY KEY,
  contenido     TEXT NOT NULL,
  descripcion   TEXT,
    -- Nota para humanos: qué es este texto, cuándo se envía
  variables     TEXT[] NOT NULL DEFAULT '{}'::text[],
    -- Lista de placeholders disponibles (ej. {'nombre','conjunto'})
  categoria     TEXT NOT NULL DEFAULT 'otro',
    -- Agrupador en el panel (inscripcion | capacitacion | autorizacion | ...)
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_by    TEXT
);
CREATE INDEX IF NOT EXISTS petra_textos_categoria_idx
  ON petra_textos (categoria, clave);

-- =============================================================================
-- FASE E — Inventario: columnas extra en petra_inmuebles
-- =============================================================================
-- Agregamos campos faltantes para la captura por WhatsApp.
ALTER TABLE petra_inmuebles
  ADD COLUMN IF NOT EXISTS habitaciones INTEGER,
  ADD COLUMN IF NOT EXISTS banos INTEGER,
  ADD COLUMN IF NOT EXISTS fotos JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS notas TEXT,
  ADD COLUMN IF NOT EXISTS capturado_por_agente_id INTEGER
    REFERENCES petra_contactos(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS petra_inmuebles_capturado_por_idx
  ON petra_inmuebles (capturado_por_agente_id, created_at DESC);

-- =============================================================================
-- FASE F — Nudges: reglas de remarketing automático
-- =============================================================================
-- Cada regla dispara un mensaje cuando una agente lleva N horas
-- "atascada" en un estado sin avanzar. El cron horario las evalúa.
-- El tracking de qué nudge ya se envió a cada contacto vive en
-- petra_contacto_roles.metadata.nudges_enviados.
CREATE TABLE IF NOT EXISTS petra_nudges (
  clave               TEXT PRIMARY KEY,
    -- Identificador estable (ej. "capacitacion_pausada_12h")
  descripcion         TEXT,
  estado_origen       TEXT NOT NULL,
    -- Estado del agente que dispara el nudge
  horas_sin_avanzar   INTEGER NOT NULL DEFAULT 12,
  texto_clave         TEXT NOT NULL,
    -- Clave de petra_textos con el mensaje a enviar
  activo              BOOLEAN NOT NULL DEFAULT TRUE,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_nudges_estado_idx
  ON petra_nudges (estado_origen) WHERE activo = TRUE;

-- =============================================================================
-- Media adjunto por estación — permite al humano agregar imágenes/videos
-- extra que el bot envía UNA sola vez cuando el agente entra a ese estado.
-- =============================================================================
CREATE TABLE IF NOT EXISTS petra_media_estacion (
  id            SERIAL PRIMARY KEY,
  estado_key    TEXT NOT NULL,
    -- clave del preset en simulador.PRESETS (ej. "autorizacion_pendiente")
  orden         INTEGER NOT NULL DEFAULT 0,
  titulo        TEXT,
  url           TEXT NOT NULL,
    -- URL absoluta o ruta relativa /petra/media/xxx.png
  activo        BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_media_estacion_idx
  ON petra_media_estacion (estado_key, orden) WHERE activo = TRUE;

-- =============================================================================
-- Botones personalizables por estación (editable desde el panel).
-- tipo='custom':   botón NUEVO que agrega el admin; cuando el agente lo pulsa
--                  el bot responde con `texto_respuesta` y se queda en el
--                  mismo estado. button_id en WhatsApp = CUSTOM_<id>.
-- tipo='override': regla sobre un botón DEFAULT (ONBAG_*). Si activo=FALSE
--                  el botón default no se envía. (Opcional: titulo overridea
--                  el label; hoy ese override ya existe vía petra_textos.)
-- =============================================================================
CREATE TABLE IF NOT EXISTS petra_botones_estacion (
  id              SERIAL PRIMARY KEY,
  estado_key      TEXT NOT NULL,
  tipo            TEXT NOT NULL DEFAULT 'custom',
  boton_id        TEXT NOT NULL,
    -- 'custom'   → se autogenera CUSTOM_<id>
    -- 'override' → id del botón default (ej. ONBAG_YA_TENGO_AUTORIZACION)
  titulo          TEXT,
  texto_respuesta TEXT,
  orden           INTEGER NOT NULL DEFAULT 0,
  activo          BOOLEAN NOT NULL DEFAULT TRUE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_botones_estacion_idx
  ON petra_botones_estacion (estado_key);
CREATE UNIQUE INDEX IF NOT EXISTS petra_botones_estacion_override_uq
  ON petra_botones_estacion (estado_key, boton_id)
  WHERE tipo = 'override';

-- Cola de mensajes pendientes de procesar por tool-calling LLM.
-- Cuando el LLM falla (API down, timeout, etc), encolamos el mensaje acá
-- y mandamos a la agente un "disculpame, se me enredó, te escribo en 5 min".
-- Un cron reprocesa cada 2 min hasta que el LLM responde (o 10 intentos máx).
CREATE TABLE IF NOT EXISTS petra_mensajes_pendientes_llm (
  id            SERIAL PRIMARY KEY,
  telefono      TEXT NOT NULL,
  texto         TEXT NOT NULL,
  cid           INTEGER,
  fase          TEXT,         -- 'inscripcion' | 'autorizacion' | ...
  estado        TEXT NOT NULL DEFAULT 'pendiente', -- pendiente | resuelto | abandonado
  intentos      INTEGER NOT NULL DEFAULT 0,
  ultimo_error  TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resuelto_at   TIMESTAMPTZ,
  siguiente_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_mensajes_pendientes_llm_idx
  ON petra_mensajes_pendientes_llm (estado, siguiente_at)
  WHERE estado = 'pendiente';
CREATE INDEX IF NOT EXISTS petra_mensajes_pendientes_llm_tel_idx
  ON petra_mensajes_pendientes_llm (telefono, created_at DESC);

-- Soft-delete y adjuntos por MENSAJE (no por estación)
-- Cada entrada de petra_textos puede ocultarse (activo=FALSE) — el bot la
-- saltea. Y la media/botones puede atarse a una clave específica para que
-- el bot los mande junto a ese mensaje puntual (en lugar de "por estación").
ALTER TABLE petra_textos
  ADD COLUMN IF NOT EXISTS activo BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE petra_media_estacion
  ADD COLUMN IF NOT EXISTS clave_texto TEXT;
ALTER TABLE petra_botones_estacion
  ADD COLUMN IF NOT EXISTS clave_texto TEXT;
CREATE INDEX IF NOT EXISTS petra_media_estacion_clave_idx
  ON petra_media_estacion (clave_texto) WHERE clave_texto IS NOT NULL;
CREATE INDEX IF NOT EXISTS petra_botones_estacion_clave_idx
  ON petra_botones_estacion (clave_texto) WHERE clave_texto IS NOT NULL;

-- Mensajes extra: textos que el bot envía ADICIONALMENTE después de un
-- mensaje "padre". Los agrega el admin desde el panel sin tocar código.
-- parent_clave apunta a una clave de petra_textos. orden define el orden
-- de envío relativo entre varios extras del mismo padre.
CREATE TABLE IF NOT EXISTS petra_mensajes_extra (
  id            SERIAL PRIMARY KEY,
  parent_clave  TEXT NOT NULL,
  orden         INTEGER NOT NULL DEFAULT 0,
  contenido     TEXT NOT NULL,
  activo        BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS petra_mensajes_extra_parent_idx
  ON petra_mensajes_extra (parent_clave, orden) WHERE activo = TRUE;

-- Fix de URLs rotas: _guardar_archivo_media guardaba '/petra/media/X' pero
-- el blueprint sirve en '/panel/petra/media/X'. Idempotente: solo toca filas
-- que empiezan con el prefix viejo.
UPDATE petra_media_estacion
   SET url = '/panel' || url
 WHERE url LIKE '/petra/media/%';

-- Re-clasificar "Como usar el QR" subido como 'otro' → 'video_como_usar_qr'
-- (el tipo real que busca el cron de kit enviado y el endpoint del panel).
-- Idempotente: solo toca filas con el título exacto que vinieron en 'otro'.
UPDATE petra_contenidos
   SET tipo = 'video_como_usar_qr'
 WHERE titulo = 'Como usar el QR'
   AND tipo = 'otro';

-- Adjuntar Vid 2.0 (TikTok) al mensaje "📦 ¡Tu kit ya está en camino!"
-- Idempotente: solo inserta si no existe ya un adjunto con esa URL para
-- esa clave. estado_key='kit_pendiente' (la agente está saliendo de ese
-- estado cuando se dispara el mensaje).
INSERT INTO petra_media_estacion (estado_key, clave_texto, orden, titulo, url, activo)
SELECT 'kit_pendiente',
       'activacion.msg_kit_enviado',
       1,
       'Vid 2.0',
       (SELECT url FROM petra_contenidos WHERE titulo='Vid 2.0' AND tipo='otro' ORDER BY id DESC LIMIT 1),
       TRUE
 WHERE EXISTS (SELECT 1 FROM petra_contenidos WHERE titulo='Vid 2.0' AND tipo='otro')
   AND NOT EXISTS (
     SELECT 1 FROM petra_media_estacion
      WHERE clave_texto='activacion.msg_kit_enviado'
        AND titulo='Vid 2.0'
   );

-- Adjuntos al mensaje "¡Dirección confirmada!" — 3 contenidos que Fabricio
-- cargó en el panel para que el bot los mande justo después del texto:
--   1) Vid 2.0 (TikTok)
--   2) Tu QR único (api.qrserver.com)
--   3) Como usar el QR (link externo)
-- Idempotente: cada INSERT verifica que no exista ya con ese título.

-- 1) Vid 2.0
INSERT INTO petra_media_estacion (estado_key, clave_texto, orden, titulo, url, activo)
SELECT 'kit_pendiente', 'activacion.msg_direccion_completa', 1, 'Vid 2.0',
       (SELECT url FROM petra_contenidos WHERE titulo='Vid 2.0' AND tipo='otro' ORDER BY id DESC LIMIT 1),
       TRUE
 WHERE EXISTS (SELECT 1 FROM petra_contenidos WHERE titulo='Vid 2.0' AND tipo='otro')
   AND NOT EXISTS (SELECT 1 FROM petra_media_estacion
                    WHERE clave_texto='activacion.msg_direccion_completa' AND titulo='Vid 2.0');

-- 2) Tu QR único
INSERT INTO petra_media_estacion (estado_key, clave_texto, orden, titulo, url, activo)
SELECT 'kit_pendiente', 'activacion.msg_direccion_completa', 2, 'Tu QR único',
       (SELECT url FROM petra_contenidos WHERE titulo='Tu QR unico' ORDER BY id DESC LIMIT 1),
       TRUE
 WHERE EXISTS (SELECT 1 FROM petra_contenidos WHERE titulo='Tu QR unico')
   AND NOT EXISTS (SELECT 1 FROM petra_media_estacion
                    WHERE clave_texto='activacion.msg_direccion_completa' AND titulo='Tu QR único');

-- 3) Como usar el QR
INSERT INTO petra_media_estacion (estado_key, clave_texto, orden, titulo, url, activo)
SELECT 'kit_pendiente', 'activacion.msg_direccion_completa', 3, 'Como usar el QR',
       (SELECT url FROM petra_contenidos WHERE titulo='Como usar el QR' ORDER BY id DESC LIMIT 1),
       TRUE
 WHERE EXISTS (SELECT 1 FROM petra_contenidos WHERE titulo='Como usar el QR')
   AND NOT EXISTS (SELECT 1 FROM petra_media_estacion
                    WHERE clave_texto='activacion.msg_direccion_completa' AND titulo='Como usar el QR');

-- Mejora idempotente del mensaje activacion.msg_direccion_completa:
-- reemplaza la versión corta vieja por la nueva más cálida con bullets.
-- Solo corre si el contenido actual coincide EXACTAMENTE con el viejo.
UPDATE petra_textos
   SET contenido = E'🎉 *¡Dirección confirmada!* 📍\n\n'
                || E'Ya pedimos tu kit de activación. En *2-3 días hábiles* lo recibís:\n\n'
                || E'📦 Tu QR personalizado para pegar en el edificio\n'
                || E'💜 Un regalito Petra para arrancar\n\n'
                || E'Te aviso acá apenas salga en camino 🚚\n\n'
                || E'Mientras tanto, podés ir contándoles a los vecinos que vos vas '
                || E'a ser la Agente Petra del conjunto 🏢✨',
       updated_at = NOW()
 WHERE clave = 'activacion.msg_direccion_completa'
   AND contenido LIKE E'¡Listo! Tengo tu dirección 📍%';

-- Fix idempotente del párrafo duplicado "Mañana a las 8am..." en
-- capacitacion.msg_dia_completo. Deja solo la segunda versión (la que
-- menciona el botón). Solo corre si el contenido actual trae las dos.
UPDATE petra_textos
   SET contenido = REPLACE(
         contenido,
         E'Mañana a las *8am* te mando los próximos 2 videos, para que no pierdas el ritmo 👋\r\n\r\n',
         ''),
       updated_at = NOW()
 WHERE clave = 'capacitacion.msg_dia_completo'
   AND contenido LIKE E'%Mañana a las *8am* te mando los próximos 2 videos, para que no pierdas el ritmo 👋\r\n\r\nMañana%';

-- Botones custom con ACCIÓN: por defecto 'enviar_texto' (envía texto_respuesta).
--   'disparar_boton' + payload=<ONBAG_X>: el flow procesa como si pulsaran
--   el botón default X (ej. ONBAG_LISTO avanza al próximo día de capacitación).
ALTER TABLE petra_botones_estacion
  ADD COLUMN IF NOT EXISTS accion TEXT NOT NULL DEFAULT 'enviar_texto';
ALTER TABLE petra_botones_estacion
  ADD COLUMN IF NOT EXISTS payload TEXT;

-- Observabilidad del tool-calling.
-- Cada turno del LLM que resulta en una llamada a tool deja una fila acá.
-- Permite ver qué está haciendo el bot sin leer logs línea por línea:
--   - qué tools fallan, qué prompts confunden al modelo
--   - cuánto tarda cada turno end-to-end
--   - una agente puntual se trabó → qué pasó en sus últimos turnos
CREATE TABLE IF NOT EXISTS petra_tool_calls (
  id             BIGSERIAL PRIMARY KEY,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  telefono       TEXT NOT NULL,
  cid            INTEGER,
  fase           TEXT NOT NULL,          -- inscripcion | autorizacion | kit | capacitacion | activa
  mensaje_user   TEXT,                   -- lo que mandó la agente (truncado)
  tool_nombre    TEXT NOT NULL,          -- marcar_tengo_autorizacion, etc.
  tool_args      JSONB,                  -- lo que el LLM pasó a la tool
  tool_resultado JSONB,                  -- lo que el handler devolvió
  ok             BOOLEAN NOT NULL,       -- false si el handler devolvió error
  latencia_ms    INTEGER,                -- cuánto tardó el turno LLM+tool
  modelo         TEXT                    -- claude-haiku-4-5 u otro
);
CREATE INDEX IF NOT EXISTS petra_tool_calls_tel_idx
  ON petra_tool_calls (telefono, created_at DESC);
CREATE INDEX IF NOT EXISTS petra_tool_calls_fase_idx
  ON petra_tool_calls (fase, created_at DESC);
CREATE INDEX IF NOT EXISTS petra_tool_calls_tool_idx
  ON petra_tool_calls (tool_nombre, created_at DESC);

-- Nudges fase autorización: cadencia 24h → 48h → 7d (último escala a humano
-- vía el handler de nudges.py). Idempotente (ON CONFLICT no sobrescribe).
INSERT INTO petra_textos (clave, contenido, activo)
VALUES (
  'nudges.autorizacion_pendiente_24h',
  E'¡Hola {nombre}! 👋\n\n¿Cómo vas con la gestión de la autorización del admin?\n\nSi ya hablaste y te dijo que sí, escribime *"ya me autorizó"* y seguimos.\n\nSi todavía no, dale cuando puedas — cualquier duda sobre qué decirle, acá estoy 💜',
  TRUE
)
ON CONFLICT (clave) DO NOTHING;

UPDATE petra_textos
   SET contenido = E'¡Hola {nombre}! 💜\n\nYa pasaron 2 días desde que arrancaste con la autorización. ¿Cómo va?\n\n✅ Si el admin ya te dijo que sí → escribime *"ya me autorizó"*\n⏳ Si todavía no pudiste hablar → dale esta semana\n❓ Si no sabés cómo abordarlo → escribime *"mandame el material"* y te paso el pack otra vez\n\nAcordate: Petra es *gratis* para el edificio y vos sumás ingresos sin dejar de hacer lo tuyo 🚀',
       updated_at = NOW()
 WHERE clave = 'nudges.autorizacion_pendiente_48h';

INSERT INTO petra_textos (clave, contenido, activo)
VALUES (
  'nudges.autorizacion_pendiente_7d',
  E'¡Hola {nombre}! 💜\n\nLlevamos una semana con la gestión del admin de tu edificio. Ya pedí que alguien del equipo te contacte directo para ver cómo te podemos ayudar a destrabarlo.\n\nTe escribimos pronto 🙌',
  TRUE
)
ON CONFLICT (clave) DO NOTHING;

-- Autorización 7d + 14d: autónomos (bot resuelve, sin escalar a humano).
UPDATE petra_textos
   SET contenido = E'¡Hola {nombre}! 💜\n\nYa va una semana con la gestión del admin. Entiendo que a veces cuesta — te paso argumentos concretos según lo que te esté diciendo:\n\n🟢 Si duda si *vale la pena* → Petra ya está activo en edificios como el tuyo y en 30 días levantan de 2 a 5 operaciones. Mandame "quiero caso" y te paso un ejemplo real.\n\n💰 Si pregunta *cuánto cuesta* → nada. Petra es gratis para el edificio. Vos ganás comisión, el edificio no paga un peso.\n\n🔒 Si le preocupa *seguridad o datos* → el QR solo deja un formulario público para interesados, no guarda nada del edificio.\n\n📅 Si *necesita tiempo* o lo tiene que llevar a asamblea → escribime "pausar 15 días" y te vuelvo a escribir en 2 semanas, sin molestar antes.\n\n¿Cuál es el tema? Decime en una línea y te armo la respuesta.',
       updated_at = NOW()
 WHERE clave = 'nudges.autorizacion_pendiente_7d';

INSERT INTO petra_textos (clave, contenido, activo)
VALUES (
  'nudges.autorizacion_pendiente_7d',
  E'¡Hola {nombre}! 💜\n\nYa va una semana con la gestión del admin. Te paso argumentos según lo que te esté diciendo:\n\n🟢 Si duda si *vale la pena* → mandame "quiero caso" y te paso un ejemplo real.\n💰 Si pregunta *cuánto cuesta* → Petra es gratis para el edificio.\n🔒 Si le preocupa *seguridad* → el QR solo capta interesados, no toca datos del edificio.\n📅 Si *necesita tiempo* → escribime "pausar 15 días" y te vuelvo a escribir en 2 semanas.\n\n¿Cuál es el tema? Decime en una línea y te armo la respuesta.',
  TRUE
)
ON CONFLICT (clave) DO NOTHING;

INSERT INTO petra_textos (clave, contenido, activo)
VALUES (
  'nudges.autorizacion_pendiente_14d',
  E'¡Hola {nombre}! 👋\n\nNo te quiero seguir molestando. Dejamos la autorización en pausa — cuando el admin te dé el ok (o quieras intentar con otro edificio), escribime *"ya me autorizó"* o *"quiero cambiar de edificio"* y seguimos.\n\nAcá te espero cuando sea el momento 💜',
  TRUE
)
ON CONFLICT (clave) DO NOTHING;

-- =============================================================================
-- SEED FAQs del Agente Petra (knowledge base del bot)
-- Fuente: docs/petra_agente_faq.md
-- Idempotente: no duplica por titulo (WHERE NOT EXISTS).
-- Para editar/desactivar una FAQ, hacelo desde /panel/petra tab FAQ.
-- =============================================================================
DO $faq_seed$
DECLARE
  faqs TEXT[][] := ARRAY[
    -- 1. Qué es Petra
    ['¿Qué es Petra?', E'Petra es una plataforma inmobiliaria que te conecta con interesados reales que buscan comprar o arrendar en *tu edificio*. Vos sos la cara visible: cuando alguien escanea el QR del lobby, llega a tu WhatsApp.'],
    ['¿Cómo gano dinero como agente?', E'Cobrás *comisión* por cada operación que se cierra con un lead que viniste trabajando: 1% del valor de venta o un % del arriendo (según el plan de tu edificio). El pago llega a la cuenta que nos pases.'],
    ['¿Cuánto me va a tomar al día?', E'Lo que vos quieras. Mínimo: responder las citas que te lleguen por WhatsApp (3-5 min cada una). Si querés más ingresos, podés cargar inmuebles del edificio proactivamente.'],
    ['¿Me tengo que mudar o cambiar de trabajo?', E'No. Petra está pensada para que sumes ingresos *sin dejar lo que estás haciendo*. Todo pasa por WhatsApp, en tus tiempos.'],
    ['¿Puedo ser agente de varios edificios?', E'Por ahora *no*. Un edificio = una agente. Es parte de lo que hace confiable el modelo para los admins.'],
    ['¿Puedo dejar de ser agente cuando quiera?', E'Sí. Escribime *"ya no quiero ser agente"* y damos de baja tu perfil sin letra chica. Si cambiás de idea, podés volver después.'],
    ['¿Petra me cobra algo?', E'*No*. Para la agente es *100% gratis*. Vos ganás comisión, no pagás nada.'],
    ['¿Petra le cobra al edificio?', E'No. Petra es *gratis para el edificio*. El admin solo da el ok para pegar un QR en el lobby. No hay cuotas ni letra chica.'],
    ['¿Qué hace Petra con mi información?', E'Solo la usamos para operar la plataforma (procesar leads, calcular comisiones, enviarte citas). No la vendemos ni la compartimos. Está protegida bajo la Ley 1581 de Colombia.'],
    ['¿Necesito descargar una app?', E'No. Todo pasa por WhatsApp, no hay app para descargar. Si tenés WhatsApp, podés ser Agente Petra.'],

    -- 2. Inscripción
    ['¿Qué datos necesitan para inscribirme?', E'Solo 5: nombre completo, cédula, ciudad, barrio, y nombre del conjunto donde vivís. Nada más — ni comprobante de ingresos ni papeles.'],
    ['¿Para qué me piden la cédula?', E'Para registrarte como Agente Petra y poder pagarte comisiones en regla. No la compartimos con el admin ni con nadie fuera del equipo.'],
    ['¿Puedo cambiar los datos después?', E'Sí. Escribime *"quiero corregir mi barrio"* (o el dato que sea) y lo cambiamos al toque.'],
    ['Me equivoqué de edificio al inscribirme', E'Decime *"quiero cambiar de edificio"* y te dejo arrancar de nuevo con el conjunto correcto.'],
    ['¿Cuánto tarda la inscripción?', E'Menos de 2 minutos. 5 datos y listo.'],
    ['¿Dónde ven mis datos?', E'Solo el equipo de Petra y vos. No se publican, no los ven otros agentes ni el admin del edificio.'],

    -- 3. Capacitación
    ['¿Cuántos videos son?', E'Son *14 videos cortos*, 2 por día durante 7 días.'],
    ['¿Cuánto duran los videos?', E'Entre 2 y 5 minutos cada uno. En total menos de 1 hora en toda la semana.'],
    ['¿Puedo ver todos los videos de una?', E'Si querés avanzar más rápido, escribime *"mandame el siguiente día"* después de ver los 2 de hoy y te mando los próximos sin esperar al cron de las 8am.'],
    ['¿Y si no entiendo algo del video?', E'Preguntame por acá, en texto. Si no puedo resolverlo, te lo paso a alguien del equipo.'],
    ['No me llegaron los videos de hoy', E'Escribime *"reenviar videos"* y te los mando de nuevo.'],
    ['¿Puedo volver a ver un video pasado?', E'Sí. Decime *"quiero ver el video del día X"* y te lo re-envío.'],
    ['¿Qué pasa si me salto un día de capacitación?', E'Nada grave. Cuando quieras seguir, escribime *"listo"* y te mando el próximo día pendiente.'],
    ['¿Hay examen después de la capacitación?', E'No hay examen. Son para que arranques sabiendo lo mínimo. Después aprendés más haciendo.'],
    ['¿Cuándo termino la capacitación?', E'En 7 días si ves 2 por día. Después pasás a la fase de pedirle autorización al admin.'],

    -- 4. Autorización del admin
    ['¿Qué le digo al admin para que me autorice?', E'Decile 3 cosas:\n1. Petra es *gratis para el edificio* (no cobra ni mantenimiento ni cuota).\n2. Solo necesita permiso para *pegar un QR* chico en el lobby/portería.\n3. Que vos vas a atender a los interesados, el edificio no se preocupa de nada.'],
    ['¿Y si el admin dice que no?', E'Decime *"el admin dijo que no"* y vemos: a veces solo le falta más info, otras veces es tema de que lo tenga que pasar a asamblea. Si no hay manera, podés cambiar de edificio.'],
    ['El admin pregunta cuánto cuesta', E'*Nada*. Petra es gratis para el edificio. Vos ganás comisión, el edificio no paga un peso.'],
    ['El admin dice que lo tiene que pasar a asamblea', E'Perfecto, es normal. Escribime *"pausar 15 días"* y te espero sin molestar. Cuando se defina, me avisás.'],
    ['Al admin le preocupa la seguridad / los datos', E'El QR solo abre un formulario público (nombre + teléfono + qué busca). *No toca datos del edificio*, no accede a la portería ni a información de residentes. Es como pegar un cartel con un número.'],
    ['¿Puedo activar el QR sin autorización del admin?', E'No. Necesitamos el ok del admin porque el QR va pegado en una zona común del edificio. Sin eso no te podemos enviar el kit.'],
    ['Soy el admin del edificio', E'¡Mejor! Escribime *"soy el admin del edificio"* y agilizamos el proceso.'],
    ['No tengo contacto directo con el admin', E'Decime y vemos: podemos mandarte un mensaje tipo para que lo reenvíes al grupo del edificio o al portero.'],
    ['¿Petra puede hablar con el admin directamente?', E'Prefiero que lo hagas vos porque conocés al admin y es tu edificio. Si el tema se traba, podemos mandar algo escrito que vos le reenvíes.'],
    ['¿Qué pasa si el admin se cambia?', E'Si el nuevo admin pide revalidar, me avisás y le pasamos el material de nuevo.'],
    ['¿Me pueden mostrar casos de otros edificios?', E'Sí. Escribime *"quiero caso"* y te paso un ejemplo real.'],

    -- 5. Kit QR
    ['¿Qué incluye el kit?', E'Un *QR físico* para pegar en el lobby del edificio + *un regalito* 💜 de bienvenida.'],
    ['¿Cuánto tarda en llegar el kit?', E'*2 a 3 días hábiles* desde que me pasás la dirección.'],
    ['¿A qué dirección mandan el kit?', E'A la que me pases cuando el admin te autorice. Puede ser tu apto, una casa o el edificio directo.'],
    ['¿Dónde pego el QR?', E'Donde más lo vea la gente que pasa: *portería, lobby o ascensor*. A la altura de los ojos, bien visible.'],
    ['¿Me cobran por el kit o el envío?', E'No. Todo *gratis*. Nosotros lo mandamos.'],
    ['El kit no me llegó aún', E'Si pasaron más de 4 días hábiles, decime *"no llegó el kit"* y lo rastreo.'],
    ['Rompí el QR, ¿qué hago?', E'Decime *"necesito otro QR"* y te mandamos uno nuevo sin costo. Mientras tanto podés usar el QR digital que ya tenés por el link.'],
    ['¿Y si me mudo del edificio?', E'Avisame. Si te mudás a otro conjunto, lo migramos. Si dejás el barrio, podemos ponerte en pausa hasta que decidas.'],
    ['¿Puedo imprimir el QR mientras me llega el físico?', E'Sí. El link del QR digital te lo mandé por acá — podés imprimirlo y pegarlo mientras llega el físico.'],
    ['¿Cómo sé que el QR funciona?', E'Después de pegarlo, mandame una *foto del QR instalado* y lo verificamos. Apenas confirmo que anda, te activo oficialmente.'],

    -- 6. Comisiones y pagos
    ['¿Cuánto gano por una venta?', E'*1% del valor de venta*. En un apto de 500 millones son 5 millones de comisión para vos.'],
    ['¿Cuánto gano por un arriendo?', E'Un *% del canon mensual*. Varía según el edificio y el tipo de inmueble.'],
    ['¿Cuándo me pagan?', E'Apenas se concreta la operación y el propietario recibe la plata. Normalmente *dentro de los 5 días hábiles*.'],
    ['¿Cómo me pagan?', E'Por *transferencia bancaria* o Nequi/Daviplata/Bancolombia a la cuenta que vos nos pases. Vos elegís.'],
    ['¿Puedo ver mi plata acumulada?', E'Sí. Pronto vas a tener un panel propio donde ver todo. Mientras, decime *"cuánto tengo"* y te paso el detalle.'],
    ['¿Me cobran impuestos?', E'Si tus ingresos como agente superan el tope del régimen simplificado, sí hay retención. Te podemos ayudar a entender cuándo y cuánto.'],
    ['¿Hay un monto mínimo para retirar?', E'No. Lo que generes se te paga al cierre de la operación, sin mínimos.'],
    ['Un lead cerró conmigo pero por fuera de Petra', E'Si el lead vino por tu QR y lo podemos probar (están los chats), la comisión es tuya igual. Avisanos y lo gestionamos.'],
    ['¿Qué pasa si una venta se cae?', E'No se cobra comisión porque no hubo operación. Si vuelve a reactivarse, sí.'],

    -- 7. Inmuebles
    ['¿Qué inmuebles puedo cargar?', E'Los de *tu edificio* que estén en *venta o arriendo*. Necesitás el ok del propietario.'],
    ['¿Necesito permiso del propietario para publicarlo?', E'Sí. Es el propietario el que decide qué publicás y a qué precio.'],
    ['¿Puedo cargar inmuebles de otros edificios?', E'No. Cada agente opera *solo su edificio*. Mantiene el modelo limpio.'],
    ['¿Qué fotos pongo de un inmueble?', E'Fotos del *inmueble como está* — sin filtros ni montajes. Ideal: 5-8 fotos (sala, cuartos, baño, cocina, vista si hay).'],
    ['¿Puedo cambiar el precio de un inmueble después?', E'Sí. Decime *"quiero cambiar el precio del inmueble X"* y lo actualizo.'],
    ['¿Cuántos inmuebles puedo cargar?', E'Los que tengas. No hay límite.'],
    ['¿Qué datos pide Petra del inmueble?', E'10 campos: modalidad (venta/arriendo), torre, apto, área m², habitaciones, baños, parqueadero, precio, nombre y teléfono del propietario.'],
    ['El propietario pide que no se vea su número', E'Lo manejamos nosotros. Vos sos la intermediaria — los interesados te escriben a vos primero.'],
    ['¿Puedo despublicar un inmueble?', E'Sí. Decime *"quitar inmueble X"* y queda fuera del catálogo.'],

    -- 8. Citas / visitas
    ['¿Cómo me llegan las citas?', E'Te llega un mensaje por acá con los datos del interesado y la hora propuesta. Vos aceptás, rechazás o proponés otra hora.'],
    ['No puedo esa hora de la cita, ¿qué hago?', E'Pulsá *"Proponer otra hora"* y mandame día/hora alternativo (ej. *jueves 3pm*).'],
    ['El interesado nunca llegó a la cita', E'Decime *"no vino el interesado de la cita X"* y lo marcamos como no-show.'],
    ['¿Tengo que acompañar al interesado durante la visita?', E'Sí. La idea es que lo recibas, le muestres el inmueble y respondas preguntas básicas. Si pregunta algo técnico que no sepás, podés decir "te lo confirmo".'],
    ['¿Y si tengo que cancelar la visita?', E'Avisame apenas puedas y le reagendamos al interesado. Un cancelado no es grave; una serie de cancelados sí.'],
    ['¿Cuánto me demora una visita?', E'Entre 20 y 40 minutos: recibirlo, mostrarle, responder dudas y despedirse.'],
    ['¿Petra me paga por hacer la visita?', E'No por la visita en sí. Cobrás solo si la operación se cierra (venta o arriendo).'],
    ['¿Puedo bloquear horarios en que no quiero citas?', E'Sí. Decime *"no disponible el sábado"* o lo que sea y bloqueo esas horas.'],

    -- 9. Legal / privacidad
    ['¿Firmo contrato?', E'Vas a firmar un *acuerdo de agente* digital (click en WhatsApp cuenta). Sin ir a notaría. Define que sos Agente Petra autorizada y cómo se calculan tus comisiones.'],
    ['¿Qué pasa con mis datos personales?', E'Están protegidos por la *Ley 1581 de 2012* (habeas data Colombia). Solo los usa Petra, no se venden ni se comparten. Podés pedir que los borremos cuando dejes de ser agente.'],
    ['¿Petra se queda con mi inmueble si lo cargo?', E'No. El inmueble sigue siendo del propietario. Nosotros solo lo publicamos y gestionamos los leads.'],
    ['¿Puedo competir con otras agentes?', E'No competís directamente. Cada edificio tiene UNA agente. Si otro conjunto tiene su agente, ustedes trabajan en zonas distintas.'],
    ['¿Puedo trabajar con una inmobiliaria tradicional a la vez?', E'Mientras no haya conflicto directo con tu edificio, sí. Avisanos si hay algún tema.'],
    ['Soy menor de edad, ¿puedo ser agente?', E'Necesitás ser mayor de 18 años para firmar el acuerdo y recibir pagos.'],
    ['Soy extranjero/a, ¿puedo ser agente?', E'Si tenés cédula de extranjería y cuenta bancaria en Colombia, sí.'],
    ['¿Qué pasa con el QR si doy de baja mi cuenta?', E'Lo retiramos del circuito. Si alguien escanea el QR después, le decimos que no está activa esa agente.'],

    -- 10. Técnico / soporte
    ['No me llegan mensajes de Petra', E'Chequeá que no me tengas bloqueado y que mi número esté guardado. Si sigue, escribime desde otro medio y lo revisamos.'],
    ['Me equivoqué de teléfono al registrarme', E'Decime *"quiero cambiar mi teléfono"* y vemos cómo migrar tu cuenta sin perder progreso.'],
    ['Quiero hablar con un humano', E'Escribime *"necesito hablar con alguien del equipo"* y alguien te contacta en el día.'],
    ['¿Puedo pausar la cuenta un tiempo?', E'Sí. Decime *"pausar X días"* y no te molesto hasta esa fecha. Después retomo donde quedamos.'],
    ['Cambié de celular / perdí el teléfono', E'Si cambiaste de celular pero mantenés el mismo número, no hay drama — tus mensajes siguen llegando. Si perdiste el número, escribime desde el nuevo y recuperamos tu cuenta.'],
    ['¿Puedo tener Petra en 2 celulares?', E'Como WhatsApp Business permite 1 número = 1 sesión, por ahora es 1 celular por agente.'],
    ['El QR no funciona cuando lo escaneo yo', E'Normal — algunos celulares no abren tu propio QR si estás logueada. Pedile a alguien de tu casa que lo pruebe.'],
    ['¿Hay horario de atención?', E'El bot está 24/7. Si pedís humano, respondemos en horario laboral (lunes a viernes 9-18, Colombia).'],

    -- 11. Día a día
    ['¿Qué pasa después de que el QR esté instalado?', E'Empezás a *recibir leads*. Cuando alguien escanea el QR del lobby, su mensaje te llega directo acá.'],
    ['¿Cómo convierto un lead en una operación?', E'Lo saludás, le preguntás qué busca, le mostrás un inmueble del edificio, coordinás una visita, y si le gusta, lo cerrás con el propietario.'],
    ['¿Qué pasa si un lead no responde?', E'Normal. De cada 10 leads, cerrás 1-2. El resto es parte del proceso.'],
    ['¿Cuántos leads voy a recibir?', E'Depende del edificio. En promedio: *3 a 8 leads por mes* en edificios activos.'],
    ['No me siento cómoda vendiendo', E'No necesitás ser comercial pro. Sé vos, honesta y clara. Es más efectivo que forzar un libreto de venta.'],
    ['¿Petra me sugiere qué decir al interesado?', E'Sí, en casos complejos. Para las respuestas comunes, armás tu estilo y el bot te lo recuerda.'],
    ['El interesado necesita crédito hipotecario', E'Petra tiene aliados de crédito. Decime *"el interesado necesita crédito"* y te paso el contacto.'],
    ['¿Hay programas para agentes top?', E'Sí. Si cerrás varias operaciones y querés escalar, podemos conversar: hay programas para agentes top (más edificios, más herramientas, pagos más rápidos).']
  ];
  pair TEXT[];
BEGIN
  FOREACH pair SLICE 1 IN ARRAY faqs LOOP
    INSERT INTO petra_contenidos (tipo, titulo, texto, activo)
    SELECT 'faq', pair[1], pair[2], TRUE
    WHERE NOT EXISTS (
      SELECT 1 FROM petra_contenidos
      WHERE tipo='faq' AND titulo = pair[1]
    );
  END LOOP;
END $faq_seed$;

-- Dejar solo el texto principal de "¡Dirección confirmada!" — quitar los
-- 3 links adjuntos (Vid 2.0, Tu QR único, Como usar el QR). Idempotente:
-- si ya están desactivos, no toca nada. Reversible: toggleable desde
-- /panel/petra tab Flujo.
-- Nota: petra_media_estacion NO tiene columna updated_at — solo activo.
UPDATE petra_media_estacion SET activo = FALSE
 WHERE clave_texto = 'activacion.msg_direccion_completa'
   AND titulo IN ('Vid 2.0', 'Tu QR único', 'Como usar el QR')
   AND activo = TRUE;

-- Mejorar textos de autorización: más cálidos, con contexto, pero no largos.
UPDATE petra_textos
   SET contenido = E'¡Buenísimo! 🚀 Entramos en la última etapa antes de activarte.\n\nAhora toca hablar con el *admin de tu edificio* para que te dé el OK como Agente Petra. Te armé un combo con todo lo que vas a necesitar: un video corto, piezas gráficas y respuestas a lo que te puede preguntar.\n\nPulsá *📎 Ver material* y lo tenés al toque 💜'
 WHERE clave = 'autorizacion.cta_botones';

UPDATE petra_textos
   SET contenido = E'Perfecto, ya tenés todo lo que necesitás 🙌\n\nAhora el próximo paso es *hablar con el admin*. Tomate tu tiempo — no hay apuro.\n\nCuando tengas novedades, elegí abajo:\n\n✅ *Ya tengo autorización* → si el admin te dijo que sí, seguimos con el envío de tu kit.\n❓ *Tengo una duda* → si no sabés cómo abordarlo o te surgió algo, contame y lo vemos juntas 💜'
 WHERE clave = 'autorizacion.msg_post_material';

-- Autorización: ajustar texto post-material tras quitar el botón "Ya tengo
-- autorización". Ahora la agente escribe "ya me autorizó" (lo detecta el
-- LLM via lenguaje natural con `marcar_tengo_autorizacion`).
UPDATE petra_textos
   SET contenido = E'Perfecto, ya tenés todo lo que necesitás 🙌\n\nAhora el próximo paso es *hablar con el admin*. Tomate tu tiempo — no hay apuro.\n\n✅ Cuando el admin te dé el OK, escribime acá *"ya me autorizó"* (o algo parecido) y seguimos con el envío de tu kit 📦\n\n❓ Si te surge alguna duda, pulsá el botón de abajo y la vemos juntas 💜'
 WHERE clave = 'autorizacion.msg_post_material';
