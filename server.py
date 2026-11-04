#!/usr/bin/env python3
"""Painel local do PC: disco, memória, CPU e tamanhos de pastas.

Só biblioteca padrão. Lê /proc e usa `du`/`find` para tamanhos.
Uso: python3 server.py [porta]   -> http://127.0.0.1:8765
"""
import ctypes
import fcntl
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE = Path(__file__).resolve().parent
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
HOME = str(Path.home())

# Sistemas de arquivos que não interessam no painel
IGNORED_FS = {
    "tmpfs", "devtmpfs", "proc", "sysfs", "cgroup", "cgroup2", "overlay",
    "composefs", "efivarfs", "securityfs", "debugfs", "tracefs", "pstore",
    "bpf", "configfs", "fusectl", "mqueue", "hugetlbfs", "autofs", "devpts",
    "binfmt_misc", "nsfs", "ramfs", "squashfs", "fuse.portal", "erofs",
}

_cpu_lock = threading.Lock()
_cpu_prev = None
_scan_cache = {}  # path -> (timestamp, result)
SCAN_TTL = 300


def read_cpu_percent():
    """Percentual de CPU desde a última chamada (delta de /proc/stat)."""
    global _cpu_prev
    with open("/proc/stat") as f:
        parts = [int(x) for x in f.readline().split()[1:]]
    idle = parts[3] + parts[4]  # idle + iowait
    total = sum(parts[:8])
    with _cpu_lock:
        prev, _cpu_prev = _cpu_prev, (idle, total)
    if not prev or total == prev[1]:
        return 0.0
    return round(100 * (1 - (idle - prev[0]) / (total - prev[1])), 1)


CPU_SENSORS = ("k10temp", "zenpower", "coretemp", "cpu_thermal")


def read_cpu_temp():
    """Temperatura da CPU via hwmon (Tctl no AMD, Package no Intel)."""
    base = Path("/sys/class/hwmon")
    for hw in sorted(base.glob("hwmon*")):
        try:
            name = (hw / "name").read_text().strip()
        except OSError:
            continue
        if name not in CPU_SENSORS:
            continue
        inputs = sorted(hw.glob("temp*_input"))
        chosen = inputs[0] if inputs else None
        for inp in inputs:
            label_file = hw / inp.name.replace("_input", "_label")
            label = label_file.read_text().strip() if label_file.exists() else ""
            if label in ("Tctl", "Tdie", "Package id 0"):
                chosen = inp
                break
        if chosen:
            try:
                return {"value": int(chosen.read_text()) / 1000, "sensor": name}
            except (OSError, ValueError):
                pass
    return None


class _NvmlUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _NvmlMem(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong),
                ("used", ctypes.c_ulonglong)]


class Nvidia:
    """Leitura da GPU NVIDIA via NVML (libnvidia-ml), sem precisar do nvidia-smi."""

    def __init__(self):
        self.lib = None
        self.handles = []
        try:
            lib = ctypes.CDLL("libnvidia-ml.so.1")
            if lib.nvmlInit_v2() != 0:
                return
            count = ctypes.c_uint()
            lib.nvmlDeviceGetCount_v2(ctypes.byref(count))
            for i in range(count.value):
                h = ctypes.c_void_p()
                if lib.nvmlDeviceGetHandleByIndex_v2(i, ctypes.byref(h)) == 0:
                    self.handles.append(h)
            self.lib = lib
        except OSError:
            pass

    def _uint(self, fn, h, *args):
        v = ctypes.c_uint()
        ok = getattr(self.lib, fn)(h, *args, ctypes.byref(v)) == 0
        return v.value if ok else None

    def read(self):
        if not self.lib:
            return []
        gpus = []
        for h in self.handles:
            name = ctypes.create_string_buffer(96)
            self.lib.nvmlDeviceGetName(h, name, 96)
            util, mem = _NvmlUtil(), _NvmlMem()
            util_ok = self.lib.nvmlDeviceGetUtilizationRates(h, ctypes.byref(util)) == 0
            mem_ok = self.lib.nvmlDeviceGetMemoryInfo(h, ctypes.byref(mem)) == 0
            power = self._uint("nvmlDeviceGetPowerUsage", h)
            limit = self._uint("nvmlDeviceGetEnforcedPowerLimit", h)
            gpus.append({
                "name": name.value.decode(errors="replace"),
                "util": util.gpu if util_ok else None,
                "vram_total": mem.total if mem_ok else None,
                "vram_used": mem.used if mem_ok else None,
                "temp": self._uint("nvmlDeviceGetTemperature", h, 0),
                "power": power / 1000 if power is not None else None,
                "power_limit": limit / 1000 if limit is not None else None,
                "fan": self._uint("nvmlDeviceGetFanSpeed", h),
            })
        return gpus


NVIDIA = Nvidia()


PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
DESKTOP_DIRS = [
    os.path.join(HOME, ".local/share/applications"),
    os.path.join(HOME, ".local/share/flatpak/exports/share/applications"),
    "/var/lib/flatpak/exports/share/applications",
    "/usr/local/share/applications",
    "/usr/share/applications",
]
# Nomes amigáveis para peças do sistema que não têm atalho no menu
SYSTEM_NAMES = {
    "org.gnome.Shell": ("GNOME Shell", "A interface da área de trabalho (barra, janelas, menus)."),
    "gnome-shell": ("GNOME Shell", "A interface da área de trabalho (barra, janelas, menus)."),
    "gnome-software": ("GNOME Software", "Loja de apps; fica aberta em segundo plano para checar atualizações."),
    "docker": ("Contêineres Docker", "Serviços rodando dentro do Docker (bancos de dados, servidores de projetos)."),
    "tracker-miner-fs-3": ("Indexador de arquivos", "Monta o índice da busca do GNOME."),
    "localsearch-3": ("Indexador de arquivos", "Monta o índice da busca do GNOME."),
    "evolution-data-server": ("Agenda/contatos do GNOME", "Serviço de calendário e contatos."),
    "pipewire": ("Áudio (PipeWire)", "Som do sistema."),
    "wireplumber": ("Áudio (WirePlumber)", "Som do sistema."),
    "Xwayland": ("Xwayland", "Compatibilidade para apps antigos (X11)."),
    "gdm": ("Tela de login", "Gerenciador de login do GNOME."),
    "NetworkManager": ("Rede", "Wi-Fi e cabo de rede."),
    "systemd-journald": ("Registro do sistema", "Guarda os logs do sistema."),
    "ollama": ("Ollama", "Servidor de modelos de IA local."),
    "containerd": ("Docker (motor)", "O motor que mantém os contêineres Docker rodando."),
    "org.freedesktop.IBus.session.GNOME": ("Teclado (IBus)", "Métodos de entrada e acentuação do teclado."),
    "claude": ("Claude Code", "Este assistente, rodando no terminal."),
}
SYSTEM_IDS = {"org.freedesktop.IBus.session.GNOME", "org.gnome.Shell", "gnome-shell", "Xwayland", "pipewire", "wireplumber", "gdm",
              "NetworkManager", "systemd-journald", "tracker-miner-fs-3", "localsearch-3",
              "evolution-data-server", "gnome-software"}
_desktop_cache = {}


def desktop_name(app_id):
    """Nome do app como aparece no menu (lido do arquivo .desktop)."""
    if app_id in _desktop_cache:
        return _desktop_cache[app_id]
    name = None
    for d in DESKTOP_DIRS:
        f = os.path.join(d, app_id + ".desktop")
        if not os.path.exists(f):
            continue
        try:
            in_entry = False
            plain = local = None
            for line in open(f, encoding="utf-8", errors="replace"):
                line = line.strip()
                if line.startswith("["):
                    in_entry = line == "[Desktop Entry]"
                elif in_entry and line.startswith("Name[pt_BR]="):
                    local = line.split("=", 1)[1]
                elif in_entry and line.startswith("Name="):
                    plain = line.split("=", 1)[1]
            name = local or plain
        except OSError:
            pass
        if name:
            break
    _desktop_cache[app_id] = name
    return name


def _group_of(pid, comm):
    """(chave, tipo) do programa a que o processo pertence, pelo cgroup do systemd."""
    try:
        if "ms-playwright" in os.readlink(f"/proc/{pid}/exe"):
            return "playwright-browser", "terminal"
    except OSError:
        pass
    try:
        cg = open(f"/proc/{pid}/cgroup").read().strip().splitlines()[-1].rsplit("/", 1)[-1]
    except OSError:
        cg = ""
    m = (re.match(r"app-flatpak-(.+?)-\d+\.scope$", cg)
         or re.match(r"app-(?:gnome-|kde-)?(.+?)(?:@\w+)?-\d+\.scope$", cg)
         or re.match(r"app-(?:gnome-|kde-)?(.+?)(?:@\w*)?\.service$", cg)
         or re.match(r"dbus-:[\d.]+-(.+?)(?:@\d+)?\.service$", cg))
    if m and not m.group(1).startswith(("ptyxis-spawn", "vte-spawn")):
        return m.group(1), "app"
    if cg.startswith("docker-"):
        return "docker", "docker"
    m = re.match(r"(.+?)(?:@\w*)?\.service$", cg)
    if m and m.group(1) not in ("user", "session"):
        return m.group(1), "service"
    # Iniciado pelo terminal ou sessão: agrupa pelo nome do executável
    return comm, "terminal"


def read_processes(limit=15):
    groups = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = entry.name
        try:
            comm = open(f"/proc/{pid}/comm").read().strip()
            with open(f"/proc/{pid}/statm") as f:
                rss = int(f.read().split()[1]) * PAGE_SIZE
        except (OSError, ValueError, IndexError):
            continue
        if rss == 0:
            continue  # thread do kernel
        mem = rss
        try:
            with open(f"/proc/{pid}/smaps_rollup") as f:
                for line in f:
                    if line.startswith("Pss:"):
                        mem = int(line.split()[1]) * 1024
                        break
        except OSError:
            pass  # processo de outro usuário: fica com o RSS
        key, kind = _group_of(pid, comm)
        g = groups.setdefault(key, {"key": key, "kind": kind, "mem": 0, "count": 0, "comms": {}})
        g["mem"] += mem
        g["count"] += 1
        g["comms"][comm] = g["comms"].get(comm, 0) + mem

    out = []
    for g in groups.values():
        key = g["key"]
        friendly, note = SYSTEM_NAMES.get(key, (None, None))
        if key == "playwright-browser":
            friendly, note = "Navegador do Playwright", "Aberto por ferramentas de teste (ex.: o Claude testando telas)."
        name = friendly or desktop_name(key) or key
        kind = "system" if key in SYSTEM_IDS else "docker" if key in ("docker", "containerd") else g["kind"]
        if not note:
            note = {
                "app": "Aplicativo aberto por você.",
                "service": "Serviço em segundo plano.",
                "docker": "Contêiner Docker.",
                "terminal": "Iniciado pelo terminal.",
                "system": "Parte do sistema.",
            }[kind]
        out.append({"name": name, "id": key, "kind": kind, "mem": g["mem"],
                    "count": g["count"], "note": note, "browser": key in BROWSERS,
                    "main": max(g["comms"], key=g["comms"].get)})
    out.sort(key=lambda g: -g["mem"])
    rest = out[limit:]
    return {"top": out[:limit],
            "others": {"count": len(rest), "mem": sum(g["mem"] for g in rest)}}


# Navegadores Chromium: chave do grupo (cgroup) -> pasta de configuração
BROWSERS = {
    "com.google.Chrome": ".var/app/com.google.Chrome/config/google-chrome",
    "google-chrome": ".config/google-chrome",
    "google\\x2dchrome": ".config/google-chrome",
    "org.chromium.Chromium": ".var/app/org.chromium.Chromium/config/chromium",
    "chromium-browser": ".config/chromium",
    "chromium": ".config/chromium",
    "com.brave.Browser": ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser",
    "brave-browser": ".config/BraveSoftware/Brave-Browser",
    "com.microsoft.Edge": ".var/app/com.microsoft.Edge/config/microsoft-edge",
    "microsoft-edge": ".config/microsoft-edge",
}
UTILITY_NAMES = {
    "network": ("Rede", "Baixa as páginas e arquivos."),
    "audio": ("Áudio", "Som das abas (vídeos, chamadas)."),
    "video_capture": ("Câmera", "Alguma aba usou câmera/microfone; fica ativo até ela fechar."),
    "storage": ("Armazenamento", "Cookies e dados salvos dos sites."),
    "data_decoder": ("Decodificador", "Lê imagens e dados recebidos."),
    "on_device_model": ("IA local", "Modelo de IA embutido do navegador."),
}
BOOT_TIME = next(int(l.split()[1]) for l in open("/proc/stat") if l.startswith("btime"))
CLK_TCK = os.sysconf("SC_CLK_TCK")
_session_cache = {}  # arquivo -> (mtime, abas)
_cpu_ticks = {}  # pid -> (ticks de CPU, instante da leitura)


def _proc_stat(pid):
    """(início em epoch, ppid, ticks de CPU usados) do processo."""
    with open(f"/proc/{pid}/stat") as f:
        fields = f.read().rsplit(")", 1)[1].split()
    return BOOT_TIME + int(fields[19]) / CLK_TCK, int(fields[1]), int(fields[11]) + int(fields[12])


def _cpu_since_last(pid, ticks, now):
    """% de um núcleo usado desde a leitura anterior (None na primeira vez)."""
    prev = _cpu_ticks.get(pid)
    _cpu_ticks[pid] = (ticks, now)
    if not prev or now - prev[1] < 0.5:
        return None
    return round(100 * (ticks - prev[0]) / CLK_TCK / (now - prev[1]), 1)


def _browser_role(cmd, parent_cmd):
    """Papel do processo dentro do navegador, pela linha de comando."""
    t = next((a[7:] for a in cmd if a.startswith("--type=")), None)
    if t is None:
        exe = os.path.basename(cmd[0]) if cmd else ""
        return ("core", "Janela principal") if "crashpad" not in exe else ("core", "Relatório de falhas")
    if t == "renderer":
        return ("ext", "Extensão") if "--extension-process" in cmd else ("tab", "Aba")
    if t == "zygote":
        # No Flatpak as abas nascem como cópias do zygote e herdam a linha de comando dele
        if any(a == "--type=zygote" for a in parent_cmd):
            return "tab", "Aba ou extensão"
        return "core", "Zygote (molde das abas)"
    if t in ("gpu-process", "broker"):
        return "gpu", "Placa de vídeo"
    if t == "utility":
        sub = next((a.split("=", 1)[1] for a in cmd if a.startswith("--utility-sub-type=")), "")
        key = sub.split(".")[0]
        name = UTILITY_NAMES.get(key, (key or "Utilitário", ""))[0]
        return "service", name
    return "service", t


def _snss_tabs(path):
    """Abas abertas num arquivo de sessão do Chromium (formato SNSS, log de comandos)."""
    data = open(path, "rb").read()
    if data[:4] != b"SNSS":
        return []
    navs, sel, tabwin, pinned, last = {}, {}, {}, {}, {}
    closed, win_closed = set(), set()
    pos = 8
    while pos + 2 <= len(data):
        size, = struct.unpack_from("<H", data, pos)
        pos += 2
        if size == 0 or pos + size > len(data):
            break
        cid, p = data[pos], data[pos + 1:pos + size]
        pos += size
        try:
            if cid == 6:  # UpdateTabNavigation (pickle: tab, índice, url, título)
                tid, idx, n = struct.unpack_from("<iii", p, 4)
                url = p[16:16 + n].decode("utf-8", "replace")
                o = 16 + ((n + 3) & ~3)
                n, = struct.unpack_from("<i", p, o)
                title = p[o + 4:o + 4 + 2 * n].decode("utf-16-le", "replace")
                navs.setdefault(tid, {})[idx] = (url, title)
            elif cid == 7:  # SetSelectedNavigationIndex
                tid, idx = struct.unpack_from("<ii", p)
                sel[tid] = idx
            elif cid == 0:  # SetTabWindow
                w, tid = struct.unpack_from("<ii", p)
                tabwin[tid] = w
            elif cid == 12:  # SetPinnedState
                tid, = struct.unpack_from("<i", p)
                pinned[tid] = bool(p[4])
            elif cid == 16:  # TabClosed
                closed.add(struct.unpack_from("<i", p)[0])
            elif cid == 17:  # WindowClosed
                win_closed.add(struct.unpack_from("<i", p)[0])
            elif cid == 21:  # LastActiveTime (µs desde 1601)
                tid, = struct.unpack_from("<i", p)
                last[tid] = struct.unpack_from("<q", p, 8)[0] / 1e6 - 11644473600
        except struct.error:
            continue
    tabs = []
    for tid, hist in navs.items():
        if tid in closed or tabwin.get(tid) in win_closed:
            continue
        url, title = hist.get(sel.get(tid), hist[max(hist)])
        tabs.append({"url": url, "title": title, "window": tabwin.get(tid),
                     "pinned": pinned.get(tid, False), "last_active": last.get(tid)})
    return tabs


def _open_tabs(config, since):
    """Abas dos perfis com sessão gravada depois que o navegador abriu."""
    base = Path(HOME, config)
    names = {}
    try:
        cache = json.loads((base / "Local State").read_text())["profile"]["info_cache"]
        names = {k: v.get("name", k) for k, v in cache.items()}
    except (OSError, ValueError, KeyError):
        pass
    tabs = []
    for prof in sorted(base.glob("*/Sessions")):
        for kind in ("Session_", "Apps_"):
            files = sorted(prof.glob(kind + "*"), key=lambda f: f.stat().st_mtime)
            if not files or files[-1].stat().st_mtime < since - 60:
                continue
            f = files[-1]
            mtime = f.stat().st_mtime
            cached = _session_cache.get(str(f))
            if not cached or cached[0] != mtime:
                try:
                    cached = (mtime, _snss_tabs(f))
                except OSError:
                    continue
                _session_cache[str(f)] = cached
            pname = names.get(prof.parent.name, prof.parent.name)
            tabs += [{**t, "profile": pname, "app": kind == "Apps_"} for t in cached[1]]
    return tabs


def read_browser(key):
    """Raio-X de um navegador Chromium: memória por papel do processo e abas abertas."""
    procs = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = entry.name
        try:
            comm = open(f"/proc/{pid}/comm").read().strip()
            if _group_of(pid, comm)[0] != key:
                continue
            # o Chrome reescreve o título do processo juntando os argumentos com espaço
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace").replace("\0", " ").split()
            mem = 0
            with open(f"/proc/{pid}/smaps_rollup") as f:
                for line in f:
                    if line.startswith("Pss:"):
                        mem = int(line.split()[1]) * 1024
                        break
            start, ppid, ticks = _proc_stat(pid)
        except (OSError, ValueError, IndexError):
            continue
        if mem:
            procs.append({"pid": int(pid), "ppid": ppid, "cmd": cmd, "mem": mem, "start": start,
                          "cpu": _cpu_since_last(int(pid), ticks, time.monotonic())})
    by_pid = {p["pid"]: p for p in procs}
    now = time.time()
    roles = {}
    tabs_procs = []
    for p in procs:
        parent = by_pid.get(p["ppid"], {}).get("cmd", [])
        role, label = _browser_role(p["cmd"], parent)
        r = roles.setdefault(role, {"mem": 0, "count": 0, "items": {}})
        r["mem"] += p["mem"]
        r["count"] += 1
        r["items"][label] = r["items"].get(label, 0) + p["mem"]
        if role in ("tab", "ext"):
            tabs_procs.append({"pid": p["pid"], "mem": p["mem"], "age": now - p["start"], "cpu": p["cpu"]})
    tabs_procs.sort(key=lambda t: -t["mem"])
    alive = set(by_pid)
    for pid in [k for k in _cpu_ticks if k not in alive]:
        del _cpu_ticks[pid]
    browser_start = min((p["start"] for p in procs), default=now)
    config = BROWSERS.get(key)
    tabs = _open_tabs(config, browser_start) if config else []
    return {
        "id": key,
        "name": desktop_name(key) or key,
        "total": sum(p["mem"] for p in procs),
        "count": len(procs),
        "uptime": now - browser_start,
        "flatpak": key.count(".") >= 2,
        "roles": {k: {**v, "items": sorted(v["items"].items(), key=lambda i: -i[1])}
                  for k, v in roles.items()},
        "tab_processes": tabs_procs,
        "cpu": sum(p["cpu"] or 0 for p in procs),
        "tabs": tabs,
    }


def _ipv4(ifname):
    """IP local da interface (ioctl SIOCGIFADDR), ou None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            packed = fcntl.ioctl(sk.fileno(), 0x8915, struct.pack("256s", ifname[:15].encode()))
            return socket.inet_ntoa(packed[20:24])
    except OSError:
        return None


def read_network():
    """Interfaces físicas (cabo/Wi-Fi) com contadores de bytes; ignora as virtuais do Docker."""
    nets = []
    signal = {}
    try:
        for line in open("/proc/net/wireless").read().splitlines()[2:]:
            name, rest = line.split(":", 1)
            signal[name.strip()] = float(rest.split()[2].rstrip("."))  # dBm
    except (OSError, ValueError, IndexError):
        pass
    base = Path("/sys/class/net")
    for iface in sorted(base.iterdir()):
        if not (iface / "device").exists():
            continue  # virtual (lo, docker0, veth, br-…)
        def rd(p, default=None):
            try:
                return (iface / p).read_text().strip()
            except OSError:
                return default
        speed = rd("speed")
        try:
            speed = int(speed)
            speed = speed if speed > 0 else None
        except (TypeError, ValueError):
            speed = None
        wifi = (iface / "wireless").exists() or (iface / "phy80211").exists()
        nets.append({
            "name": iface.name,
            "type": "wifi" if wifi else "ethernet",
            "state": rd("operstate", "unknown"),
            "speed": speed,  # Mbps do link
            "ip": _ipv4(iface.name),
            "mac": rd("address"),
            "rx": int(rd("statistics/rx_bytes", "0")),
            "tx": int(rd("statistics/tx_bytes", "0")),
            "signal_dbm": signal.get(iface.name),
        })
    return {"t": time.monotonic(), "interfaces": nets}


def read_diskio():
    """Contadores de leitura/escrita dos discos físicos inteiros (sem partições, zram, loop)."""
    disks = []
    try:
        lines = open("/proc/diskstats").read().splitlines()
    except OSError:
        return {"t": time.monotonic(), "disks": []}
    for line in lines:
        f = line.split()
        if len(f) < 13:
            continue
        name = f[2]
        if not os.path.exists(f"/sys/block/{name}/device"):
            continue  # partição ou dispositivo virtual
        try:
            rot = Path(f"/sys/block/{name}/queue/rotational").read_text().strip() == "1"
        except OSError:
            rot = None
        try:
            model = Path(f"/sys/block/{name}/device/model").read_text().strip() or None
        except OSError:
            model = None
        disks.append({
            "name": name,
            "model": model,
            "rotational": rot,
            "transport": "nvme" if name.startswith("nvme") else None,
            "read": int(f[5]) * 512,     # setores lidos -> bytes
            "write": int(f[9]) * 512,    # setores escritos -> bytes
            "io_ms": int(f[12]),         # tempo ocupado com E/S
        })
    return {"t": time.monotonic(), "disks": disks}


def read_meminfo():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, val = line.split(":", 1)
            info[key] = int(val.split()[0]) * 1024
    total = info["MemTotal"]
    available = info["MemAvailable"]
    cache = info.get("Cached", 0) + info.get("Buffers", 0) + info.get("SReclaimable", 0)
    return {
        "total": total,
        "used": total - available,
        "cache": cache,
        "available": available,
        "free": info["MemFree"],
        "swap_total": info.get("SwapTotal", 0),
        "swap_used": info.get("SwapTotal", 0) - info.get("SwapFree", 0),
    }


def read_system():
    with open("/proc/uptime") as f:
        uptime = float(f.read().split()[0])
    cpu_model = ""
    with open("/proc/cpuinfo") as f:
        for line in f:
            if line.startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip()
                break
    os_name = ""
    try:
        for line in open("/etc/os-release"):
            if line.startswith("PRETTY_NAME="):
                os_name = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return {
        "hostname": socket.gethostname(),
        "os": os_name,
        "kernel": os.uname().release,
        "cpu_model": cpu_model,
        "cpu_count": os.cpu_count(),
        "uptime": uptime,
        "loadavg": os.getloadavg(),
        "cpu_percent": read_cpu_percent(),
        "memory": read_meminfo(),
        "cpu_temp": read_cpu_temp(),
        "gpus": NVIDIA.read(),
        "net": read_network(),
        "diskio": read_diskio(),
    }


def block_info(dev):
    """Tipo físico do disco a partir do /sys: nvme/sata/usb, HD ou SSD, modelo, nº da partição."""
    name = os.path.basename(os.path.realpath(dev))
    sys_path = os.path.realpath(f"/sys/class/block/{name}")
    info = {"partition": None, "transport": None, "rotational": None, "model": None, "disk": name}
    try:
        info["partition"] = int(Path(sys_path, "partition").read_text())
        parent = os.path.dirname(sys_path)
    except (OSError, ValueError):
        parent = sys_path
    info["disk"] = os.path.basename(parent)
    if info["disk"].startswith("nvme"):
        info["transport"] = "nvme"
    elif "/usb" in parent:
        info["transport"] = "usb"
    elif info["disk"].startswith(("sd", "hd")):
        info["transport"] = "sata"
    try:
        info["rotational"] = Path(parent, "queue/rotational").read_text().strip() == "1"
    except OSError:
        pass
    try:
        info["model"] = Path(parent, "device/model").read_text().strip() or None
    except OSError:
        pass
    return info


def read_disks():
    """Um item por dispositivo físico (dedup por device), com o ponto de montagem mais curto."""
    seen = {}
    with open("/proc/mounts") as f:
        for line in f:
            dev, mnt, fstype = line.split()[:3]
            mnt = mnt.replace("\\040", " ")
            if fstype in IGNORED_FS or not dev.startswith("/dev/"):
                continue
            try:
                st = os.statvfs(mnt)
            except OSError:
                continue
            total = st.f_blocks * st.f_frsize
            if total == 0:
                continue
            free = st.f_bavail * st.f_frsize
            used = (st.f_blocks - st.f_bfree) * st.f_frsize
            entry = {"device": dev, "mount": mnt, "fstype": fstype,
                     "total": total, "used": used, "free": free, **block_info(dev)}
            # Prefere o ponto de montagem mais significativo (/var/home > /etc)
            cur = seen.get(dev)
            if cur is None or _mount_rank(mnt) < _mount_rank(cur["mount"]):
                seen[dev] = entry
    return sorted(seen.values(), key=lambda d: -d["total"])


def _mount_rank(mnt):
    if mnt == HOME or HOME.startswith(mnt + "/") and mnt.endswith("home"):
        return 0
    if mnt in ("/", "/var/home", "/home"):
        return 1
    return 2 + len(mnt)


CACHE_DIR = os.path.realpath(os.path.join(HOME, ".cache"))
PERSONAL_DIRS = {"Documentos", "Documents", "Downloads", "Imagens", "Pictures", "Vídeos",
                 "Videos", "Músicas", "Music", "Área de trabalho", "Desktop", "Público", "Modelos"}
DB_EXT = (".fdb", ".gdb", ".db", ".sqlite", ".sqlite3", ".mdb", ".accdb", ".dbf")


# Caches que o painel nunca apaga (uso diário do Bruno)
PROTECTED_CACHES = {
    "ms-playwright-mcp": "Navegador do Playwright MCP — protegido porque você usa todo dia.",
}

CACHE_NOTES = {
    "uv": "Pacotes Python do uv. Limpa com o próprio uv (uv cache clean); projetos já criados continuam funcionando.",
    "pip": "Downloads do pip. Nenhum efeito além de baixar de novo ao instalar algo.",
    "huggingface": "Modelos de IA baixados. Se algum programa usar de novo, ele baixa outra vez.",
    "whisper": "Modelos de transcrição do Whisper. Baixa de novo se usar.",
    "ms-playwright": "Navegadores de automação do Playwright. Baixam de novo no próximo uso.",
    "ms-playwright-mcp": "Navegador usado pelo Playwright MCP (o Claude usa para testar telas). Baixa de novo no próximo uso.",
    "ms-playwright-go": "Navegadores do Playwright (Go). Baixam de novo no próximo uso.",
    "puppeteer": "Navegador do Puppeteer. Baixa de novo no próximo uso.",
    "google-chrome-for-testing": "Chrome de testes. Baixa de novo no próximo uso.",
    "codex-runtimes": "Runtime do Codex CLI. Baixa de novo ao abrir o Codex.",
    "mozilla": "Cache do Firefox. Feche o Firefox antes de limpar.",
    "google-chrome": "Cache do Chrome. Feche o Chrome antes de limpar.",
    "tracker3": "Índice de busca do GNOME. Ele reindexa sozinho.",
    "pnpm": "Pacotes do pnpm. Baixam de novo ao instalar.",
    "typescript": "Tipos baixados pelo editor. Recriado automaticamente.",
    "electron": "Downloads do Electron. Baixa de novo se precisar.",
    "nvidia": "Cache de shaders da NVIDIA. Recriado automaticamente (jogos podem engasgar na 1ª vez).",
    "mesa_shader_cache": "Cache de shaders. Recriado automaticamente.",
    "thumbnails": "Miniaturas de imagens. Recriadas ao abrir as pastas.",
    "gnome-software": "Cache da loja de apps. Recriado automaticamente.",
    "libdnf5": "Cache de pacotes do sistema. Recriado na próxima atualização.",
    "codebase-memory-mcp": "Índices de código do codebase-memory. Precisam ser reindexados depois.",
}


def classify(path):
    """Diz se um arquivo pode ser limpo sem afetar o sistema, e o que exatamente apagar.

    Só itens dentro de ~/.cache são considerados seguros. O "target" é a unidade a
    remover (ex.: a pasta inteira de um modelo do HuggingFace, não um blob solto).
    """
    real = os.path.realpath(path)
    try:
        rel = Path(real).relative_to(HOME)
    except ValueError:
        return {"safe": False, "label": "Arquivo do sistema",
                "note": "Fica fora da sua pasta pessoal. Não apague por aqui."}
    parts = rel.parts
    if not parts:
        return {"safe": False, "label": "Pasta pessoal", "note": ""}

    if real.startswith(CACHE_DIR + os.sep):
        sub = Path(real).relative_to(CACHE_DIR).parts
        if sub[0] in PROTECTED_CACHES:
            return {"safe": False, "protected": True, "label": "Protegido",
                    "note": PROTECTED_CACHES[sub[0]]}
        target, label = real, f"Cache ({sub[0]})"
        note = "Arquivo temporário guardado por um programa. Pode apagar; o programa recria se precisar."
        if len(sub) == 1 and os.path.isdir(real):
            # subpasta inteira da .cache: esvazia o conteúdo
            return {"safe": True, "label": f"Cache ({sub[0]})", "target": real,
                    "note": CACHE_NOTES.get(sub[0], "Cache de um programa. Ele recria o que precisar.")}
        if len(sub) >= 3 and sub[0] == "huggingface" and sub[1] == "hub" and sub[2].startswith("models--"):
            target = os.path.join(CACHE_DIR, *sub[:3])
            name = sub[2][len("models--"):].replace("--", "/")
            label = "Modelo de IA baixado (cache)"
            note = (f"Modelo {name}. Apagar não afeta o sistema; se algum programa usar esse "
                    "modelo de novo, ele é baixado outra vez. Remove a pasta inteira do modelo.")
        elif sub[0] == "whisper":
            label = "Modelo do Whisper (cache)"
            note = "Modelo de transcrição de áudio. Se usar o Whisper de novo, ele é baixado outra vez."
        elif sub[0] == "uv":
            label = "Cache do uv (Python)"
            note = "Pacote guardado pelo uv para instalar mais rápido. Ambientes já criados continuam funcionando."
            if len(sub) >= 3 and sub[1] == "archive-v0":
                target = os.path.join(CACHE_DIR, *sub[:3])
                note += " Remove a pasta inteira desse pacote no cache."
        elif sub[0] == "pip":
            label = "Cache do pip (Python)"
            note = "Download guardado pelo pip. Pode apagar sem problema."
        return {"safe": True, "label": label, "note": note, "target": target}

    if parts[0] == ".local" and "site-packages" in parts:
        i = parts.index("site-packages")
        pkg = parts[i + 1] if len(parts) > i + 1 else "?"
        return {"safe": False, "label": "Biblioteca Python instalada",
                "note": f"Faz parte do pacote “{pkg}”. Apagar quebra os programas que usam. "
                        f"Se não usa mais: pip uninstall {pkg}"}
    if parts[:2] == (".local", "share") and len(parts) > 2:
        return {"safe": False, "label": "Arquivo de aplicativo",
                "note": f"Faz parte do app “{parts[2]}”. Apagar por aqui pode quebrar o app — "
                        "se não usa mais, desinstale-o."}
    if parts[0] == ".var" and len(parts) > 2:
        return {"safe": False, "label": "Dados de app Flatpak",
                "note": f"Pertence ao app {parts[2]}. Limpe pelo próprio app ou desinstale-o."}
    if parts[0] in PERSONAL_DIRS or not parts[0].startswith("."):
        if real.lower().endswith(DB_EXT):
            return {"safe": False, "label": "Banco de dados de programa",
                    "note": "Parece guardar os dados de um programa (cadastros, histórico…). "
                            "Provavelmente importante — não apague."}
        return {"safe": False, "label": "Arquivo pessoal",
                "note": "Não afeta o sistema, mas pode ser importante. Revise você mesmo antes de apagar."}
    return {"safe": False, "label": "Configuração/programa",
            "note": "Usado por algum programa. Não apague sem saber de onde vem."}


def freeable(target):
    """Espaço que realmente volta ao apagar (ignora hardlinks compartilhados)."""
    total = freed = 0
    seen = set()
    entries = []
    if os.path.isdir(target) and not os.path.islink(target):
        for root, _dirs, files in os.walk(target):
            entries += [os.path.join(root, f) for f in files]
    else:
        entries = [target]
    for f in entries:
        try:
            st = os.lstat(f)
        except OSError:
            continue
        if (st.st_dev, st.st_ino) in seen or os.path.islink(f):
            continue
        seen.add((st.st_dev, st.st_ino))
        size = st.st_blocks * 512
        total += size
        if st.st_nlink == 1:
            freed += size
    return total, freed


def _rmtree(path):
    """rmtree que também apaga pastas somente leitura (o dotslash cria assim)."""
    def fix(func, p, _exc):
        os.chmod(os.path.dirname(p), 0o700)  # libera a pasta-mãe e tenta de novo
        if func is os.rmdir or func is os.unlink:
            func(p)
        else:
            raise _exc
    shutil.rmtree(path, onexc=fix)


def delete_target(target):
    """Apaga um item de cache, revalidando tudo no servidor."""
    real = os.path.realpath(target)
    info = classify(real)
    if not info["safe"] or os.path.realpath(info["target"]) != real:
        raise PermissionError("Esse item não está na lista de limpeza segura.")
    if not real.startswith(CACHE_DIR + os.sep):
        raise PermissionError("Só é permitido apagar dentro de ~/.cache.")
    _total, freed = freeable(real)
    top_level = os.path.dirname(real) == CACHE_DIR and os.path.isdir(real)
    uv = shutil.which("uv") or os.path.join(HOME, ".local/bin/uv")
    if top_level and os.path.basename(real) == "uv" and os.access(uv, os.X_OK):
        # o próprio uv sabe limpar sem atrapalhar ambientes em uso
        subprocess.run([uv, "cache", "clean"], capture_output=True, timeout=600, check=True)
    elif top_level:
        # esvazia a subpasta da .cache, mantendo a pasta em si
        errors = 0
        with os.scandir(real) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        _rmtree(e.path)
                    else:
                        os.unlink(e.path)
                except OSError:
                    errors += 1  # arquivo em uso/sem permissão: segue com o resto
        freed = max(freed - freeable(real)[1], 0)
        _scan_cache.clear()
        return freed, errors
    elif os.path.isdir(real) and not os.path.islink(real):
        _rmtree(real)
    else:
        os.unlink(real)
    _scan_cache.clear()
    return freed, 0


def scan_dir(path):
    """Tamanho de cada filho direto (du) + maiores arquivos da subárvore (find)."""
    now = time.time()
    cached = _scan_cache.get(path)
    if cached and now - cached[0] < SCAN_TTL:
        return cached[1]

    children = []
    du = subprocess.run(
        ["du", "-x", "-d", "1", "-B1", path],
        capture_output=True, text=True, timeout=180,
    )
    total = 0
    for line in du.stdout.splitlines():
        size, p = line.split("\t", 1)
        if p == path:
            total = int(size)
            continue
        children.append({"name": os.path.basename(p), "path": p,
                         "size": int(size), "type": "dir"})
    # Arquivos soltos na pasta também contam
    loose = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.is_file(follow_symlinks=False):
                    loose += e.stat(follow_symlinks=False).st_blocks * 512
    except OSError:
        pass
    if loose:
        children.append({"name": "(arquivos nesta pasta)", "path": None,
                         "size": loose, "type": "files"})
    children.sort(key=lambda c: -c["size"])
    in_cache = os.path.realpath(path) == CACHE_DIR
    for c in children:
        if c["type"] != "dir":
            continue
        if in_cache:
            c.update(classify(c["path"]))
            c["freeable"] = freeable(c["path"])[1] if c["safe"] else 0
        elif os.path.realpath(c["path"]) == CACHE_DIR:
            c["cache_root"] = True  # atalho "limpar" na linha .cache da home

    find = subprocess.run(
        ["find", path, "-xdev", "-type", "f", "-size", "+20M",
         "-printf", "%s\t%T@\t%p\n"],
        capture_output=True, text=True, timeout=180,
    )
    files = []
    for line in find.stdout.splitlines():
        try:
            size, mtime, p = line.split("\t", 2)
            files.append({"path": p, "size": int(size), "mtime": float(mtime)})
        except ValueError:
            continue
    files.sort(key=lambda f: -f["size"])
    files = files[:25]
    targets = {}
    for f in files:
        f.update(classify(f["path"]))
        if f["safe"]:
            t = f["target"]
            if t not in targets:
                targets[t] = freeable(t)
            f["target_size"], f["freeable"] = targets[t]
            f["target_is_dir"] = os.path.isdir(t)

    result = {"path": path, "parent": str(Path(path).parent) if path != "/" else None,
              "total": total, "children": children, "largest_files": files,
              "scanned_at": now, "partial": du.returncode != 0}
    _scan_cache[path] = (now, result)
    return result


FLATPAK_ID = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+){2,}$")
_apps_cache = None  # (timestamp, resultado)
APPS_TTL = 60


def _si_bytes(text):
    """'525,6 MB' (formato do flatpak, base 1000) -> bytes."""
    m = re.match(r"([\d.,]+)\s*([kMGT]?B)", text.strip().replace("\u00a0", " "))
    if not m:
        return 0
    mult = {"B": 1, "kB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}[m.group(2)]
    return int(float(m.group(1).replace(",", ".")) * mult)


def _du_many(paths):
    """Tamanho real de cada caminho, numa só chamada do du."""
    paths = [p for p in paths if os.path.lexists(p)]
    if not paths:
        return {}
    out = subprocess.run(["du", "-s", "-B1", "--", *paths], capture_output=True, text=True, timeout=180)
    sizes = {}
    for line in out.stdout.splitlines():
        size, p = line.split("\t", 1)
        sizes[p] = int(size)
    return sizes


def flatpak_leftovers(app_id):
    """Arquivos que um app Flatpak deixa na pasta pessoal (dados, atalhos, ajustes)."""
    if not FLATPAK_ID.match(app_id):
        return []
    found = [os.path.join(HOME, ".var/app", app_id),
             os.path.join(HOME, ".local/share/flatpak/overrides", app_id),
             os.path.join(HOME, ".config/autostart", app_id + ".desktop")]
    apps_dir = Path(HOME, ".local/share/applications")
    found += [str(p) for p in apps_dir.glob(app_id + ".desktop*")]
    found += [str(p) for p in Path(HOME, ".local/share/icons").glob(f"*/*/apps/{app_id}.*")]
    found += [str(p) for p in Path(HOME, ".local/share/icons").glob(f"*/*/apps/{app_id}-*")]
    return [p for p in found if os.path.lexists(p)]


def read_apps(refresh=False):
    """Apps Flatpak instalados + restos de apps que já foram removidos."""
    global _apps_cache
    if not refresh and _apps_cache and time.time() - _apps_cache[0] < APPS_TTL:
        return _apps_cache[1]
    env = {**os.environ, "LANGUAGE": "pt_BR:pt"}  # nomes e descrições em português
    cols = "application,name,description,version,origin,installation,size"
    out = subprocess.run(["flatpak", "list", "--app", "--columns=" + cols],
                         capture_output=True, text=True, timeout=60, env=env, check=True)
    ps = subprocess.run(["flatpak", "ps", "--columns=application"],
                        capture_output=True, text=True, timeout=30, env=env)
    running = set(ps.stdout.split())
    apps = []
    for line in out.stdout.splitlines():
        f = line.split("\t")
        if len(f) < 7:
            continue
        apps.append({"id": f[0], "name": f[1], "description": f[2], "version": f[3],
                     "origin": f[4], "installation": f[5], "size": _si_bytes(f[6]),
                     "running": f[0] in running, "leftovers": flatpak_leftovers(f[0]), "kind": "flatpak"})
    installed = {a["id"] for a in apps}
    layered, protected = read_layered()
    apps += layered

    # Restos: pastas e ajustes de apps que não estão mais instalados
    orphans = {}
    for base in (".var/app", ".local/share/flatpak/overrides"):
        try:
            names = os.listdir(os.path.join(HOME, base))
        except OSError:
            continue
        for name in names:
            if FLATPAK_ID.match(name) and name not in installed:
                orphans[name] = flatpak_leftovers(name)

    sizes = _du_many([p for a in apps for p in a["leftovers"]] + [p for ps_ in orphans.values() for p in ps_])
    for a in apps:
        a["data"] = sum(sizes.get(p, 0) for p in a["leftovers"])
    orphan_list = [{"id": k, "paths": v, "size": sum(sizes.get(p, 0) for p in v)} for k, v in orphans.items()]
    apps.sort(key=lambda a: -(a["size"] + a["data"]))
    orphan_list.sort(key=lambda o: -o["size"])
    result = {"apps": apps, "orphans": orphan_list, "protected": protected}
    _apps_cache = (time.time(), result)
    return result


# Pacotes do rpm-ostree que fazem o PC funcionar: nunca aparecem para remover
LAYERED_PROTECTED = ("akmod-", "kmod-", "kernel", "xorg-x11-drv-", "nvidia", "rpmfusion-",
                     "docker", "containerd", "zsh", "git", "nodejs", "podman")
# Onde cada app guarda os dados (além do que a busca por nome encontra)
LAYERED_DATA = {
    "code": [".config/Code", ".vscode"],
    "antigravity": [".config/Antigravity", ".antigravity", ".cache/antigravity"],
    "chatgpt": [".config/ChatGPT", ".cache/ChatGPT"],
    "screamingfrogseospider": [".ScreamingFrogSEOSpider"],
}
DATA_BASES = [".config", ".local/share", ".cache"]


def _desktop_fields(path):
    """Name/Comment/Exec/StartupWMClass da seção principal de um .desktop."""
    out, in_entry = {}, False
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line.startswith("["):
                in_entry = line == "[Desktop Entry]"
            elif in_entry and "=" in line:
                k, v = line.split("=", 1)
                if k in ("Name", "Name[pt_BR]", "Comment", "Comment[pt_BR]", "Exec", "StartupWMClass", "Icon"):
                    out.setdefault(k, v)
    except OSError:
        pass
    return out


def layered_leftovers(name, desktop):
    """Pastas de dados do app na pasta pessoal: lista conhecida + pastas com o mesmo nome do app."""
    found = [os.path.join(HOME, p) for p in LAYERED_DATA.get(name, [])]
    exe = os.path.basename((desktop.get("Exec") or "").split(" ")[0])
    keys = {k.lower().replace(" ", "") for k in (name, desktop.get("Name"), desktop.get("StartupWMClass"), exe) if k}
    for base in DATA_BASES:
        try:
            entries = os.listdir(os.path.join(HOME, base))
        except OSError:
            continue
        found += [os.path.join(HOME, base, e) for e in entries if e.lower().replace(" ", "") in keys]
    found += [os.path.join(HOME, e) for e in os.listdir(HOME)
              if e.startswith(".") and e[1:].lower().replace(" ", "") in keys]
    found.append(os.path.join(HOME, ".config/autostart", f"{name}.desktop"))
    uniq = []
    for p in found:
        if os.path.lexists(p) and p not in uniq:
            uniq.append(p)
    return uniq


def _rpm_name(local):
    """'chatgpt-26.908.40834-1.x86_64' -> 'chatgpt'."""
    return re.sub(r"-[^-]+-[^-]+\.[^.]+$", "", local)


def read_layered():
    """Apps instalados com rpm-ostree (pacotes em camada que têm atalho no menu).

    Devolve (apps, quantos pacotes ficaram de fora por serem peças do sistema).
    """
    try:
        st = json.loads(subprocess.run(["rpm-ostree", "status", "--json"], capture_output=True,
                                       text=True, timeout=60, check=True).stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return [], 0
    deps = st.get("deployments", [])
    booted = next((d for d in deps if d.get("booted")), None)
    if not booted:
        return [], 0
    staged = next((d for d in deps if d.get("staged")), None)
    reqs = lambda d: set(d.get("requested-packages", [])) | {_rpm_name(x) for x in d.get("requested-local-packages", [])}
    names = sorted(reqs(booted))
    staged_names = reqs(staged) if staged else None
    q = subprocess.run(["rpm", "-q", "--qf", "[%{=NAME}\t%{FILENAMES}\n]", *names],
                       capture_output=True, text=True, timeout=60)
    files = {}
    for line in q.stdout.splitlines():
        n, _, f = line.partition("\t")
        files.setdefault(n, []).append(f)
    # executáveis em uso agora, para saber se o app está aberto
    exes = set()
    for entry in os.scandir("/proc"):
        if entry.name.isdigit():
            try:
                exes.add(os.readlink(f"/proc/{entry.name}/exe"))
            except OSError:
                pass
    apps, protected = [], 0
    for n in names:
        desks = [f for f in files.get(n, []) if f.startswith("/usr/share/applications/") and f.endswith(".desktop")]
        if not desks or n.endswith("-devel") or n.startswith(LAYERED_PROTECTED):
            protected += 1
            continue
        main = next((d for d in desks if "url" not in d.lower()), desks[0])
        d = _desktop_fields(main)
        info = subprocess.run(["rpm", "-q", "--qf", "%{SIZE}\t%{VERSION}", n],
                              capture_output=True, text=True, timeout=30).stdout.split("\t")
        own = set(files.get(n, []))
        apps.append({
            "id": n, "kind": "rpm", "origin": "rpm-ostree", "installation": "system",
            "name": d.get("Name[pt_BR]") or d.get("Name") or n,
            "description": d.get("Comment[pt_BR]") or d.get("Comment") or "",
            "version": info[1] if len(info) > 1 else "",
            "size": int(info[0]) if info[0].isdigit() else 0,
            "running": bool(exes & own),
            "pending": staged_names is not None and n not in staged_names,
            "leftovers": layered_leftovers(n, d),
        })
    return apps, protected


def uninstall_layered(name, paths):
    """Tira o pacote da camada do rpm-ostree (some ao reiniciar) e apaga os dados escolhidos."""
    global _apps_cache
    app = next((a for a in read_apps(refresh=True)["apps"] if a["id"] == name and a["kind"] == "rpm"), None)
    if not app:
        raise PermissionError("Esse programa não está na lista de remoção.")
    if app["running"]:
        raise PermissionError(f"{app['name']} está aberto. Feche o app e tente de novo.")
    bad = [p for p in paths if p not in app["leftovers"]]
    if bad:
        raise PermissionError("Pasta fora da lista de dados do app: " + bad[0])
    avisos = []
    if not app["pending"]:
        r = subprocess.run(["rpm-ostree", "uninstall", name], capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip()
            raise RuntimeError(msg.splitlines()[-1] if msg else "O rpm-ostree não conseguiu remover.")
    before = _free_bytes()
    avisos += _remove_paths(paths)
    _apps_cache = None
    _scan_cache.clear()
    return {"freed": max(_free_bytes() - before, 0), "avisos": avisos, "reboot": True}


def _free_bytes():
    """Espaço livre somando os discos onde ficam apps Flatpak e a pasta pessoal."""
    seen, total = set(), 0
    for p in (HOME, "/var/lib/flatpak"):
        try:
            st = os.statvfs(p)
            dev = os.stat(p).st_dev
        except OSError:
            continue
        if dev not in seen:
            seen.add(dev)
            total += st.f_bavail * st.f_frsize
    return total


def _remove_paths(paths):
    """Apaga arquivos/pastas dentro da pasta pessoal. Devolve a lista de erros."""
    errors = []
    for p in paths:
        real = os.path.realpath(p) if not os.path.islink(p) else p
        if not real.startswith(HOME + os.sep) or real == os.path.join(HOME, ".var/app"):
            errors.append(f"{p}: fora da pasta pessoal")
            continue
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                _rmtree(p)
            elif os.path.lexists(p):
                os.unlink(p)
        except OSError as e:
            errors.append(f"{p}: {e.strerror}")
    return errors


def uninstall_app(app_id):
    """Remove um app Flatpak por completo: programa, dados, ajustes, atalhos e runtimes órfãos."""
    global _apps_cache
    app = next((a for a in read_apps(refresh=True)["apps"] if a["id"] == app_id and a["kind"] == "flatpak"), None)
    if not app:
        raise PermissionError("Esse app não está instalado (ou não é Flatpak).")
    if app["running"]:
        raise PermissionError(f"{app['name']} está aberto. Feche o app e tente de novo.")
    scope = "--system" if app["installation"] == "system" else "--user"
    before = _free_bytes()
    avisos = []
    run = lambda *a: subprocess.run(["flatpak", *a], capture_output=True, text=True, timeout=600)
    # 1. o programa e os dados dele (~/.var/app/ID)
    r = run("uninstall", scope, "-y", "--noninteractive", "--delete-data", app_id)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        raise RuntimeError(msg.splitlines()[-1] if msg else "O flatpak não conseguiu desinstalar.")
    # 2. ajustes de permissão (sistema e usuário) e permissões dadas pelo portal
    if app["installation"] == "system" and os.path.exists(f"/var/lib/flatpak/overrides/{app_id}"):
        r = run("override", "--system", "--reset", app_id)
        if r.returncode != 0:
            avisos.append(f"Ajuste de sistema ficou em /var/lib/flatpak/overrides/{app_id} (precisa de senha para apagar).")
    run("permission-reset", app_id)
    # 3. o que sobrou na pasta pessoal (dados, atalhos, ícones)
    avisos += _remove_paths(flatpak_leftovers(app_id))
    # 4. bibliotecas (runtimes) que nenhum app usa mais
    r = run("uninstall", scope, "-y", "--noninteractive", "--unused")
    runtimes = len(re.findall(r"^\s*\d+\.\s", r.stdout, re.M))
    _apps_cache = None
    _scan_cache.clear()
    return {"freed": max(_free_bytes() - before, 0), "runtimes": runtimes, "avisos": avisos}


def delete_orphan(app_id):
    """Apaga os restos de um app que já não está instalado."""
    global _apps_cache
    orphan = next((o for o in read_apps(refresh=True)["orphans"] if o["id"] == app_id), None)
    if not orphan:
        raise PermissionError("Não há restos desse app (ou ele ainda está instalado).")
    before = _free_bytes()
    avisos = _remove_paths(orphan["paths"])
    subprocess.run(["flatpak", "permission-reset", app_id], capture_output=True, timeout=30)
    _apps_cache = None
    _scan_cache.clear()
    return {"freed": max(_free_bytes() - before, 0), "avisos": avisos}


# ---- Aliviar CPU ----------------------------------------------------------
# Põe os processos pesados num grupo (scope do systemd) com teto de CPU.
# Não mata nada: eles só andam mais devagar. "Liberar" tira o teto.
RELIEF_UNIT = "painel-alivio.scope"
RELIEF_SHARE = 0.5          # teto: metade do que o grupo está usando (mínimo 1 thread)
RELIEF_MIN_CPU = 25         # % de um núcleo para entrar no grupo
# Pais que criam novos trabalhadores (ex.: um teste que abre 14 workers).
RUNNERS = {"node", "node-22", "npm", "npx", "pnpm", "yarn", "bun", "deno", "python", "python3",
           "make", "cargo", "go", "java", "php", "composer", "vite", "esbuild"}
_relief_prev_profile = None
PROFILES = ("org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles",
            "org.freedesktop.UPower.PowerProfiles")


def _busctl(*args, user=True):
    cmd = ["busctl"] + (["--user"] if user else []) + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "busctl falhou")
    return r.stdout.strip()


def _systemd(method, sig, *args):
    return _busctl("call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                   "org.freedesktop.systemd1.Manager", method, sig, *[str(a) for a in args])


def _power_profile(new=None):
    """Lê (ou troca) o perfil de energia do sistema. None se não existir."""
    try:
        if new:
            _busctl("set-property", *PROFILES, "ActiveProfile", "s", new, user=False)
        return _busctl("get-property", *PROFILES, "ActiveProfile", user=False).split('"')[1]
    except (RuntimeError, IndexError, OSError, subprocess.TimeoutExpired):
        return None


def _relief_cgroup():
    base = Path(f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service")
    return next(base.glob(f"**/{RELIEF_UNIT}"), None)


def _cgroup_leaf(pid):
    try:
        return open(f"/proc/{pid}/cgroup").read().strip().rsplit("/", 1)[-1]
    except OSError:
        return ""


def _movable(pid):
    """Só mexe em processos do usuário em scopes de app comuns.

    Flatpak fica de fora (o portal identifica o app pelo cgroup) e serviços
    também (o systemd perderia o controle deles).
    """
    leaf = _cgroup_leaf(pid)
    if leaf == RELIEF_UNIT:
        return True
    return bool(re.match(r"app-.+\.scope$", leaf)) and not leaf.startswith("app-flatpak-")


def _own_procs():
    """pid -> (comm, ppid, ticks) dos processos do usuário."""
    uid, out = os.getuid(), {}
    for e in os.scandir("/proc"):
        if not e.name.isdigit():
            continue
        try:
            if e.stat().st_uid != uid:
                continue
            comm = open(f"/proc/{e.name}/comm").read().strip()
            _, ppid, ticks = _proc_stat(e.name)
            out[int(e.name)] = (comm, ppid, ticks)
        except (OSError, ValueError, IndexError):
            continue
    return out


def _cmd_short(pid):
    try:
        args = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        args = [a.decode(errors="replace") for a in args if a]
        if len(args) == 1:  # Chrome reescreve a linha de comando numa string só
            args = args[0].split()
        script = next((a for a in args[1:] if not a.startswith("-")), "")
        return os.path.basename(args[0]) + (" " + script if script else "")
    except OSError:
        return ""


def cpu_relief_status():
    cg = _relief_cgroup()
    procs, threads = [], None
    if cg:
        try:
            procs = [int(p) for p in (cg / "cgroup.procs").read_text().split()]
            quota = (cg / "cpu.max").read_text().split()[0]
            threads = None if quota == "max" else round(int(quota) / 100000, 1)
        except OSError:
            pass
    names = {}
    for p in procs:
        n = _cmd_short(p)
        if n:
            names[n] = names.get(n, 0) + 1
    active = bool(procs) and threads is not None
    return {"active": active, "threads": threads, "count": len(procs),
            "names": sorted(names.items(), key=lambda kv: -kv[1])[:5],
            "profile": _power_profile()}


def _relief_pick():
    """(PIDs que vão para o grupo com teto, threads que eles usam agora). Mede 1 s."""
    me = os.getpid()
    first, t0 = _own_procs(), time.time()
    time.sleep(1)
    now, dt = _own_procs(), time.time() - t0
    use = {pid: (ticks - first[pid][2]) / CLK_TCK / dt
           for pid, (_c, _pp, ticks) in now.items() if pid in first}
    hot = {pid for pid, u in use.items()
           if pid != me and 100 * u >= RELIEF_MIN_CPU
           and _group_of(pid, now[pid][0])[0] not in SYSTEM_IDS and _movable(pid)}
    if not hot:
        return [], 0

    children = {}
    for pid, (_c, ppid, _t) in now.items():
        children.setdefault(ppid, []).append(pid)
    chosen, stack = set(), list(hot)
    while stack:  # o processo pesado e todos os filhos dele
        pid = stack.pop()
        if pid in chosen or pid == me:
            continue
        chosen.add(pid)
        stack.extend(children.get(pid, []))
    for pid in list(hot):  # sobe para o "pai" que cria trabalhadores novos
        pp = now[pid][1]
        while pp in now and pp != me and now[pp][0] in RUNNERS and _movable(pp):
            chosen.add(pp)
            pp = now[pp][1]
    pids = sorted(p for p in chosen if _movable(p))
    return pids, sum(use.get(p, 0) for p in pids)


def cpu_relief_on():
    global _relief_prev_profile
    pids, usage = _relief_pick()
    if not pids:
        return {"moved": 0, "msg": "Nenhum processo seu está pesando agora (≥ 25% de um núcleo)."}

    # Ex.: 14 workers usando 14 threads -> teto 7; 2 abas usando 1,3 -> teto 1
    quota = int(max(1.0, RELIEF_SHARE * usage) * 1_000_000)
    rest, moved = list(pids), 0
    if not _relief_cgroup():
        # Cria o grupo com o primeiro processo que ainda existir.
        # Delegate=yes: sem isso o systemd não deixa anexar outros depois.
        while rest:
            try:
                _systemd("StartTransientUnit", "ssa(sv)a(sa(sv))", RELIEF_UNIT, "fail", 3,
                         "PIDs", "au", 1, rest.pop(0), "Delegate", "b", "true",
                         "CPUQuotaPerSecUSec", "t", quota, 0)
                moved = 1
                break
            except RuntimeError:
                continue  # sumiu entre a medição e agora
        for _ in range(20):
            if _relief_cgroup():
                break
            time.sleep(0.05)
    for pid in rest:  # um por vez: um worker que terminou não derruba o resto
        try:
            _systemd("AttachProcessesToUnit", "ssau", RELIEF_UNIT, "", 1, pid)
            moved += 1
        except RuntimeError:
            pass
    if not _relief_cgroup():
        raise RuntimeError("os processos terminaram antes de entrar no grupo")
    _systemd("SetUnitProperties", "sba(sv)", RELIEF_UNIT, "true", 1, "CPUQuotaPerSecUSec", "t", quota)

    cur = _power_profile()
    if cur and cur != "power-saver":
        _relief_prev_profile = cur
        _power_profile("power-saver")
    return {"moved": moved, **cpu_relief_status()}


def cpu_relief_off():
    global _relief_prev_profile
    if _relief_cgroup():
        _systemd("SetUnitProperties", "sba(sv)", RELIEF_UNIT, "true", 1,
                 "CPUQuotaPerSecUSec", "t", 2**64 - 1)
    if _power_profile() == "power-saver":
        _power_profile(_relief_prev_profile or "balanced")
    _relief_prev_profile = None
    return cpu_relief_status()


def _label(labels, key):
    m = re.search(r"(?:^|,)" + re.escape(key) + r"=([^,]*)", labels or "")
    return m.group(1) if m else ""


def read_docker():
    """Todos os contêineres (ligados e parados), via `docker ps -a`."""
    if not shutil.which("docker"):
        return {"error": "Docker não está instalado"}
    out = subprocess.run(["docker", "ps", "-a", "--size", "--format", "{{json .}}"],
                         capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        return {"error": (out.stderr.strip() or "docker ps falhou")[:300]}
    items = []
    for line in out.stdout.splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        published, internal = [], []
        for p in filter(None, (s.strip() for s in (c.get("Ports") or "").split(","))):
            if "->" in p:
                host, inner = p.split("->", 1)
                hport = host.rsplit(":", 1)[-1]
                local = host.startswith("127.0.0.1") or host.startswith("[::1]")
                entry = {"host": hport, "inner": inner, "local": local}
                if entry not in published:
                    published.append(entry)
            elif p not in internal:
                internal.append(p)
        m = re.match(r"(.+?)\s*\(virtual (.+)\)", c.get("Size") or "")
        m_exit = re.match(r"Exited \((-?\d+)\)", c.get("Status") or "")
        labels = c.get("Labels") or ""
        items.append({
            "id": c.get("ID"), "name": c.get("Names"), "image": c.get("Image"),
            "state": c.get("State"), "status": c.get("Status"),
            "health": c.get("HealthStatus") if c.get("HealthStatus") not in (None, "", "none") else "",
            "exit": int(m_exit.group(1)) if m_exit else None,
            "created": c.get("CreatedAt"), "running_for": c.get("RunningFor"),
            "published": published, "internal": internal,
            "size": _si_bytes(m.group(1)) if m else 0,
            "virtual": _si_bytes(m.group(2)) if m else 0,
            "project": _label(labels, "com.docker.compose.project"),
            "workdir": _label(labels, "com.docker.compose.project.working_dir"),
        })
    with _dk_stats_lock:
        stats = dict(_dk_stats)
    for it in items:
        s = stats.get(it["id"]) if it["state"] == "running" else None
        it["cpu"] = s["cpu"] if s else None
        it["mem"] = s["mem"] if s else None
    return {"containers": items, "stats_at": _dk_stats_at}


# Uso de CPU/memória: `docker stats` leva ~2 s, então roda numa thread e o painel lê o cache.
_dk_stats = {}
_dk_stats_at = 0
_dk_stats_lock = threading.Lock()
DK_STATS_EVERY = 10


def _bin_bytes(text):
    """'9.992MiB' (formato do docker stats, base 1024) -> bytes."""
    m = re.match(r"([\d.]+)\s*([KMGT]?i?B)", text.strip())
    if not m:
        return 0
    mult = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
            "kB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}.get(m.group(2), 1)
    return int(float(m.group(1)) * mult)


def _dk_stats_loop():
    global _dk_stats, _dk_stats_at
    while True:
        try:
            out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                                 capture_output=True, text=True, timeout=30)
            fresh = {}
            for line in out.stdout.splitlines():
                try:
                    s = json.loads(line)
                    fresh[s["ID"]] = {
                        "cpu": float(s.get("CPUPerc", "0").rstrip("%") or 0),
                        "mem": _bin_bytes(s.get("MemUsage", "0B").split("/")[0]),
                    }
                except (ValueError, KeyError):
                    continue
            if out.returncode == 0:
                with _dk_stats_lock:
                    _dk_stats, _dk_stats_at = fresh, time.time()
        except Exception:  # noqa: BLE001 - docker fora do ar: tenta de novo depois
            pass
        time.sleep(DK_STATS_EVERY)


DK_ACTIONS = {"start", "stop", "restart"}


def docker_action(cid, action):
    """Liga, para ou reinicia um contêiner. Só aceita IDs que o `docker ps -a` conhece."""
    if action not in DK_ACTIONS:
        raise PermissionError("Ação não permitida")
    if not re.fullmatch(r"[0-9a-f]{12,64}", cid or ""):
        raise PermissionError("ID de contêiner inválido")
    known = subprocess.run(["docker", "ps", "-aq", "--no-trunc"], capture_output=True, text=True, timeout=10)
    if not any(full.startswith(cid) for full in known.stdout.split()):
        raise FileNotFoundError(cid)
    out = subprocess.run(["docker", action, cid], capture_output=True, text=True, timeout=90)
    if out.returncode != 0:
        raise RuntimeError((out.stderr.strip() or f"docker {action} falhou")[:300])
    return {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _local_only(self):
        # Bloqueia pedidos vindos de outros sites (CSRF / DNS rebinding)
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost") and self.headers.get("X-Painel") == "1"

    def do_POST(self):
        url = urlparse(self.path)
        if url.path not in ("/api/delete", "/api/uninstall", "/api/orphan", "/api/uninstall-rpm", "/api/cpu-relief",
                            "/api/docker-action"):
            return self._json({"error": "not found"}, 404)
        if not self._local_only():
            return self._json({"error": "Pedido recusado"}, 403)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            if url.path == "/api/cpu-relief":
                on = data.get("on", True)
                return self._json({"ok": True, **(cpu_relief_on() if on else cpu_relief_off())})
            if url.path == "/api/docker-action":
                return self._json({"ok": True, **docker_action(str(data.get("id", "")), str(data.get("action", "")))})
            if url.path == "/api/uninstall":
                return self._json({"ok": True, **uninstall_app(str(data.get("id", "")))})
            if url.path == "/api/uninstall-rpm":
                paths = [str(p) for p in data.get("paths", [])]
                return self._json({"ok": True, **uninstall_layered(str(data.get("id", "")), paths)})
            if url.path == "/api/orphan":
                return self._json({"ok": True, **delete_orphan(str(data.get("id", "")))})
            freed, falhas = delete_target(str(data.get("target", "")))
            res = {"ok": True, "freed": freed}
            if falhas:
                res["aviso"] = f"{falhas} item(ns) não puderam ser apagados (em uso ou sem permissão)"
            self._json(res)
        except PermissionError as e:
            self._json({"error": str(e)}, 403)
        except FileNotFoundError:
            _scan_cache.clear()
            self._json({"error": "O item já não existe."}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 500)

    def do_GET(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        try:
            if url.path in ("/favicon.ico", "/icon.svg"):
                body = (BASE / "icon.svg").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/":
                body = (BASE / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/system":
                self._json(read_system())
            elif url.path == "/api/cpu-relief":
                self._json(cpu_relief_status())
            elif url.path == "/api/processes":
                self._json(read_processes())
            elif url.path == "/api/browser":
                key = qs.get("id", [""])[0]
                if key not in BROWSERS:
                    return self._json({"error": "Navegador não suportado"}, 404)
                self._json(read_browser(key))
            elif url.path == "/api/apps":
                self._json(read_apps(refresh=bool(qs.get("refresh"))))
            elif url.path == "/api/docker":
                self._json(read_docker())
            elif url.path == "/api/disks":
                self._json(read_disks())
            elif url.path == "/api/files":
                path = os.path.realpath(qs.get("path", [HOME])[0])
                if not os.path.isdir(path):
                    return self._json({"error": "Pasta não encontrada"}, 404)
                if qs.get("refresh"):
                    _scan_cache.pop(path, None)
                self._json(scan_dir(path))
            else:
                self._json({"error": "not found"}, 404)
        except subprocess.TimeoutExpired:
            self._json({"error": "A varredura demorou demais"}, 504)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 500)


if __name__ == "__main__":
    read_cpu_percent()  # primeira amostra
    if shutil.which("docker"):
        threading.Thread(target=_dk_stats_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Painel rodando em http://127.0.0.1:{PORT}  (Ctrl+C para sair)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
