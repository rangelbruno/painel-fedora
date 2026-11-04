# Painel do PC

Painel local para ver e liberar recursos do computador: disco, memória, CPU, GPU, temperaturas, apps Flatpak, contêineres Docker e abas do Chrome.

Tudo roda no seu próprio PC. O servidor só aceita conexões de `127.0.0.1`. Nada é enviado para a internet.

## Requisitos

- Fedora (testado no Fedora Silverblue).
- `python3` e `curl`. Os dois já vêm no Fedora.
- Opcional: Google Chrome ou Chromium, para abrir o painel numa janela própria.

Não precisa instalar pacotes Python. O servidor usa só a biblioteca padrão.

## Instalar

1. Baixe o projeto:

   ```bash
   git clone https://github.com/rangelbruno/painel-fedora.git ~/painel-fedora
   cd ~/painel-fedora
   ```

2. Rode o instalador (não precisa de `sudo`):

   ```bash
   ./instalar.sh
   ```

3. Abra **Painel do PC** no menu de aplicativos.

O instalador cria três coisas, todas na sua pasta pessoal:

| O quê | Onde |
|---|---|
| Atalho do menu | `~/.local/share/applications/painel-pc.desktop` |
| Ícone | `~/.local/share/icons/hicolor/scalable/apps/painel-pc.svg` |
| Serviço que sobe o painel no login | `~/.config/systemd/user/painel-pc.service` |

Se mover a pasta do projeto de lugar, rode `./instalar.sh` de novo.

## Usar sem instalar

```bash
python3 server.py
```

Depois abra <http://127.0.0.1:8765/> no navegador.

## Extensão do Chrome (opcional)

A extensão deixa o painel listar as abas e adormecer as que estão paradas. Sem ela, o resto do painel funciona normalmente.

1. Abra `chrome://extensions`.
2. Ligue o **Modo do desenvolvedor** (canto superior direito).
3. Clique em **Carregar sem compactação**.
4. Escolha a pasta `extensao` dentro deste projeto.
5. Recarregue a página do painel (F5).

## O que muda em cada sistema

| Recurso | Silverblue / Kinoite | Fedora Workstation (dnf) |
|---|---|---|
| Disco, memória, CPU, temperaturas | sim | sim |
| Apps Flatpak | sim | sim |
| Pacotes instalados com `rpm-ostree` | sim | não (o comando não existe) |
| GPU NVIDIA | com driver NVIDIA | com driver NVIDIA |
| Docker | se o Docker estiver instalado | se o Docker estiver instalado |

## Atualizar

```bash
cd ~/painel-fedora
git pull
./instalar.sh
```

## Desinstalar

```bash
./instalar.sh --remover
```

Isso remove o atalho, o ícone e o serviço. A pasta do projeto continua lá; apague à mão se quiser.

## Problemas comuns

- **O painel não abre.** Veja se o serviço está rodando: `systemctl --user status painel-pc`.
- **A porta 8765 já está em uso.** Outro programa usa essa porta. Rode `python3 server.py 8766` e abra `http://127.0.0.1:8766/`. A extensão do Chrome só funciona na porta 8765.
- **Ver o log do servidor:** `journalctl --user -u painel-pc -f`.
