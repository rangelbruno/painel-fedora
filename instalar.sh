#!/usr/bin/env bash
# Instala o Painel do PC para o usuário atual (sem sudo).
#   ./instalar.sh            instala (ou atualiza) atalho do menu, ícone e serviço
#   ./instalar.sh --remover  desfaz a instalação (não apaga esta pasta)
set -euo pipefail

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
UNITS="$HOME/.config/systemd/user"
DESKTOP="$APPS/painel-pc.desktop"
ICON="$ICONS/painel-pc.svg"
SERVICE="$UNITS/painel-pc.service"

tem_systemd() { systemctl --user show-environment >/dev/null 2>&1; }

if [[ "${1:-}" == "--remover" ]]; then
  if tem_systemd; then
    systemctl --user disable --now painel-pc.service 2>/dev/null || true
  fi
  rm -f "$DESKTOP" "$ICON" "$SERVICE"
  tem_systemd && systemctl --user daemon-reload || true
  update-desktop-database "$APPS" >/dev/null 2>&1 || true
  echo "Painel do PC removido. A pasta $DIR continua aqui; apague à mão se quiser."
  exit 0
fi

# 1. Requisitos
if ! command -v python3 >/dev/null; then
  echo "Erro: python3 não encontrado. No Fedora: sudo dnf install python3" >&2
  exit 1
fi
if ! command -v curl >/dev/null; then
  echo "Erro: curl não encontrado. No Fedora: sudo dnf install curl" >&2
  exit 1
fi
chmod +x "$DIR/abrir-painel.sh" "$DIR/server.py"

# 2. Ícone
mkdir -p "$ICONS"
cp "$DIR/icon.svg" "$ICON"

# 3. Atalho no menu de aplicativos
mkdir -p "$APPS"
cat >"$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Painel do PC
Comment=Disco, memória, CPU, GPU e temperaturas do computador
Exec="$DIR/abrir-painel.sh"
Icon=painel-pc
Terminal=false
Categories=System;Monitor;
Keywords=disco;memória;cpu;gpu;temperatura;monitor;espaço;
StartupWMClass=chrome-127.0.0.1__-Default
EOF
update-desktop-database "$APPS" >/dev/null 2>&1 || true

# 4. Serviço do systemd: o servidor sobe junto com o login
if tem_systemd; then
  mkdir -p "$UNITS"
  cat >"$SERVICE" <<EOF
[Unit]
Description=Painel do PC (disco, memória, CPU, GPU)
After=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/env python3 "$DIR/server.py" 8765
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable painel-pc.service >/dev/null 2>&1
  systemctl --user restart painel-pc.service
  echo "Serviço ativo: o painel sobe sozinho a cada login."
else
  echo "Aviso: systemd de usuário indisponível. O atalho sobe o servidor quando for aberto."
fi

echo "Pronto. Procure \"Painel do PC\" no menu de aplicativos."
echo "Ou abra no navegador: http://127.0.0.1:8765/"
echo "Se mover esta pasta de lugar, rode ./instalar.sh de novo."
