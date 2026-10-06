-- Backfill del lead caliente del piloto 6-oct (573508463133). Pidió "Me
-- pueden llamar?" 3 veces antes de que existiera `es_pedido_llamada`, así
-- que la columna quedó en FALSE. Lo marcamos a mano para que aparezca en los
-- reportes de leads que requieren atención humana.
--
-- Correr una sola vez, post-merge de este PR y post-deploy (schema.sql
-- se ejecuta al arrancar, así que las columnas ya existen).
--
-- Ejecutar via `mcp__Render__query_render_postgres`:
--   UPDATE contactos SET requiere_humano=true,
--     requiere_humano_motivo='backfill 6-oct: pidió llamada 3 veces',
--     requiere_humano_at=NOW()
--   WHERE telefono='573508463133';

UPDATE contactos SET requiere_humano = TRUE,
    requiere_humano_motivo = 'backfill 6-oct: pidió llamada 3 veces',
    requiere_humano_at = NOW()
WHERE telefono = '573508463133';
