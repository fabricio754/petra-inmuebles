#!/bin/bash
# Levanta el webhook de Petra y lo expone en internet con un túnel
# temporal de Cloudflare (sin necesidad de crear cuenta ni pagar).
# La URL pública cambia cada vez que se reinicia esto — normal en un
# prototipo. Para producción real se reemplaza por hosting fijo.
set -e
cd "$(dirname "$0")/.."

echo "Iniciando servidor Petra en el puerto ${PORT:-3000}..."
python3 -m app.server > /tmp/petra-server.log 2>&1 &
SERVER_PID=$!
echo "  servidor PID: $SERVER_PID (logs: /tmp/petra-server.log)"

sleep 2

echo "Abriendo túnel público (Cloudflare)..."
./bin/cloudflared tunnel --url "http://localhost:${PORT:-3000}" > /tmp/petra-tunnel.log 2>&1 &
TUNNEL_PID=$!
echo "  túnel PID: $TUNNEL_PID (logs: /tmp/petra-tunnel.log)"

sleep 6
URL=$(grep -o "https://[a-zA-Z0-9.-]*\.trycloudflare\.com" /tmp/petra-tunnel.log | head -1)

echo ""
echo "=========================================================="
echo "URL PÚBLICA DEL WEBHOOK (pégala en Meta -> WhatsApp -> Configuration):"
echo "  $URL/webhook"
echo ""
echo "Verify token: ${META_VERIFY_TOKEN:-petra-verify-token}"
echo "=========================================================="
echo ""
echo "Para detener: kill $SERVER_PID $TUNNEL_PID"
