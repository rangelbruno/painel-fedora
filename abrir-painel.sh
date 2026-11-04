#!/usr/bin/env bash
# Abre o Painel do PC: sobe o servidor se ele não estiver rodando e abre a janela.
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PORT=8765
URL="http://127.0.0.1:$PORT/"

if ! curl -s -o /dev/null "$URL"; then
  # Prefere o serviço do systemd (sobe junto com o PC); sem ele, roda direto
  if ! systemctl --user start painel-pc.service 2>/dev/null; then
    nohup python3 "$DIR/server.py" "$PORT" >"$DIR/servidor.log" 2>&1 &
  fi
  for _ in $(seq 1 50); do
    curl -s -o /dev/null "$URL" && break
    sleep 0.1
  done
fi

# Janela própria (modo app) se tiver Chrome/Chromium; senão, o navegador padrão
if flatpak info com.google.Chrome >/dev/null 2>&1; then
  exec flatpak run com.google.Chrome --app="$URL"
elif command -v google-chrome >/dev/null; then
  exec google-chrome --app="$URL"
elif command -v chromium-browser >/dev/null; then
  exec chromium-browser --app="$URL"
else
  exec xdg-open "$URL"
fi
