import os
import asyncio
import json
import base64
import socket
import subprocess
import tarfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import decky

PLUGIN_DIR = Path(decky.DECKY_PLUGIN_DIR)
DATA_DIR = Path(decky.DECKY_PLUGIN_RUNTIME_DIR)  # /home/deck/homebrew/data/<plugin>
BIN_PATH = PLUGIN_DIR / "bin" / "sing-box"  # кладём бинарник сюда при билде
CONFIG_PATH = DATA_DIR / "config.json"
PROFILES_PATH = DATA_DIR / "profiles.json"
SUBSCRIPTIONS_META_PATH = DATA_DIR / "subscriptions.json"  # {source: {url, last_updated}}
LOG_PATH = DATA_DIR / "sing-box.log"
STATE_PATH = DATA_DIR / "state.json"  # хранит имя активного профиля между перезапусками

# версия sing-box, зафиксирована — при желании обновить, поменять тут
SINGBOX_VERSION = "1.13.13"
SINGBOX_URL = (
    f"https://github.com/SagerNet/sing-box/releases/download/"
    f"v{SINGBOX_VERSION}/sing-box-{SINGBOX_VERSION}-linux-amd64.tar.gz"
)

DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------- СОСТОЯНИЕ СКАЧИВАНИЯ БИНАРНИКА (общее для всех методов) ----------
download_state = {
    "downloading": False,
    "progress": 0,      # 0-100
    "done": BIN_PATH.exists(),
    "error": None,
}


def _build_ssl_context():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def download_singbox():
    """Качает и распаковывает sing-box в BIN_PATH. Работает в отдельном потоке,
    прогресс пишет в download_state, чтобы фронт мог его опрашивать."""
    if BIN_PATH.exists():
        # уже есть какой-то бинарник (свой, ручной, или наш старый) — проверяем что рабочий,
        # но НЕ удаляем сами, если он не наш — мало ли юзер сам туда что-то положил
        try:
            check = subprocess.run([str(BIN_PATH), "version"], capture_output=True, timeout=10)
            if check.returncode != 0:
                decky.logger.error("download_singbox: существующий бинарник не отвечает на 'version', но оставляем как есть")
        except Exception as e:
            decky.logger.error(f"download_singbox: не удалось проверить существующий бинарник: {e}")
        download_state["done"] = True
        return

    download_state["downloading"] = True
    download_state["progress"] = 0
    download_state["error"] = None

    archive_path = DATA_DIR / "sing-box.tar.gz"
    last_error = None
    attempts = 3

    for attempt in range(1, attempts + 1):
        try:
            decky.logger.info(f"скачиваем sing-box {SINGBOX_VERSION} с {SINGBOX_URL} (попытка {attempt}/{attempts})")

            req = urllib.request.Request(SINGBOX_URL, headers={"User-Agent": "Mozilla/5.0"})
            try:
                ctx = _build_ssl_context()
                resp = urllib.request.urlopen(req, timeout=30, context=ctx)
            except Exception as e:
                # проверенный сертификат не прошёл (частая история на SteamOS/за DPI) —
                # откатываемся на незащищённый контекст, как и в fetch_and_parse_subscription
                decky.logger.error(f"download_singbox: проверка SSL не прошла ({e}), пробуем без верификации")
                import ssl
                ctx_unverified = ssl._create_unverified_context()
                resp = urllib.request.urlopen(req, timeout=30, context=ctx_unverified)

            with resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(archive_path, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            download_state["progress"] = int(downloaded / total * 90)  # 90% — сама загрузка

            decky.logger.info("sing-box скачан, распаковываем")
            BIN_PATH.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive_path, "r:gz") as tar:
                member = next((m for m in tar.getmembers() if m.name.endswith("/sing-box") or m.name == "sing-box"), None)
                if not member:
                    raise RuntimeError("в архиве не найден бинарник sing-box")
                member.name = "sing-box"  # кладём плоско, без вложенной папки
                tar.extract(member, path=BIN_PATH.parent)

            os.chmod(BIN_PATH, 0o755)
            archive_path.unlink(missing_ok=True)

            # проверяем, что бинарник реально рабочий и не урезанная сборка —
            # полноценный sing-box всегда весит от ~30 МБ, меньше — подозрительно
            min_size_bytes = 25 * 1024 * 1024
            actual_size = BIN_PATH.stat().st_size
            if actual_size < min_size_bytes:
                BIN_PATH.unlink(missing_ok=True)
                raise RuntimeError(
                    f"бинарник подозрительно маленький ({actual_size // 1024 // 1024} МБ), "
                    f"похоже на урезанную/неполную сборку, удалён"
                )

            try:
                check = subprocess.run(
                    [str(BIN_PATH), "version"], capture_output=True, timeout=10
                )
                if check.returncode != 0:
                    raise RuntimeError(f"sing-box version вернул код {check.returncode}")
            except Exception as e:
                BIN_PATH.unlink(missing_ok=True)
                raise RuntimeError(f"скачанный бинарник не рабочий, удалён: {e}")

            download_state["progress"] = 100
            download_state["done"] = True
            decky.logger.info("sing-box установлен успешно")
            last_error = None
            break
        except Exception as e:
            last_error = e
            decky.logger.error(f"download_singbox попытка {attempt} ОШИБКА: {e}")
            archive_path.unlink(missing_ok=True)
            if attempt < attempts:
                time.sleep(2 * attempt)  # небольшая пауза перед повтором, растёт с каждой попыткой

    if last_error:
        download_state["error"] = str(last_error)
    download_state["downloading"] = False
    return


# ---------- ПАРСЕРЫ КЛЮЧЕЙ ----------

GENERIC_NAMES = {"server", "node", "vpn", "proxy", "vless", "hysteria2", "trojan", "us", "de", "nl", "jp", "sg"}


def make_profile_name(fragment: str, host: str, port) -> str:
    """Имя из #fragment ссылки, но если оно пустое/слишком короткое/generic (типа
    просто "US" или "server") — используем host:port, чтобы разные серверы
    не выглядели одинаково и было проще их отличить в списке."""
    name = urllib.parse.unquote(fragment or "").strip()
    if not name or len(name) < 3 or name.lower() in GENERIC_NAMES:
        return f"{host}:{port}" if port else (host or "unknown")
    return name


def parse_vless(link: str) -> dict:
    # vless://uuid@host:port?params#name
    parsed = urllib.parse.urlparse(link)
    uuid = parsed.username
    host = parsed.hostname
    port = parsed.port
    q = urllib.parse.parse_qs(parsed.query)
    name = make_profile_name(parsed.fragment, host, port)

    outbound = {
        "type": "vless",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "uuid": uuid,
        "flow": q.get("flow", [""])[0],
        "packet_encoding": q.get("packetEncoding", ["xudp"])[0],
    }

    security = q.get("security", ["none"])[0]
    if security in ("tls", "reality"):
        tls = {
            "enabled": True,
            "server_name": q.get("sni", [host])[0],
            "insecure": q.get("allowInsecure", ["0"])[0] == "1",
        }
        if security == "reality":
            tls["reality"] = {
                "enabled": True,
                "public_key": q.get("pbk", [""])[0],
                "short_id": q.get("sid", [""])[0],
            }
        if q.get("fp"):
            tls["utls"] = {"enabled": True, "fingerprint": q.get("fp")[0]}
        outbound["tls"] = tls

    net = q.get("type", ["tcp"])[0]
    if net == "ws":
        outbound["transport"] = {
            "type": "ws",
            "path": q.get("path", ["/"])[0],
            "headers": {"Host": q.get("host", [host])[0]},
        }
    elif net == "grpc":
        outbound["transport"] = {
            "type": "grpc",
            "service_name": q.get("serviceName", [""])[0],
        }

    return {"name": name, "outbound": outbound}


def parse_hysteria2(link: str) -> dict:
    # hysteria2://password@host:port?params#name
    parsed = urllib.parse.urlparse(link)
    password = parsed.username
    host = parsed.hostname
    port = parsed.port
    q = urllib.parse.parse_qs(parsed.query)
    name = make_profile_name(parsed.fragment, host, port)

    outbound = {
        "type": "hysteria2",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "password": password,
        "tls": {
            "enabled": True,
            "server_name": q.get("sni", [host])[0],
            "insecure": q.get("insecure", ["0"])[0] == "1",
        },
    }
    if q.get("obfs"):
        outbound["obfs"] = {
            "type": q.get("obfs")[0],
            "password": q.get("obfs-password", [""])[0],
        }
    return {"name": name, "outbound": outbound}


def parse_trojan(link: str) -> dict:
    # trojan://password@host:port?params#name
    parsed = urllib.parse.urlparse(link)
    password = parsed.username
    host = parsed.hostname
    port = parsed.port
    q = urllib.parse.parse_qs(parsed.query)
    name = make_profile_name(parsed.fragment, host, port)

    outbound = {
        "type": "trojan",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "password": password,
    }

    security = q.get("security", ["tls"])[0]  # trojan почти всегда с tls
    if security != "none":
        outbound["tls"] = {
            "enabled": True,
            "server_name": q.get("sni", [host])[0],
            "insecure": q.get("allowInsecure", ["0"])[0] == "1",
        }

    net = q.get("type", ["tcp"])[0]
    if net == "ws":
        outbound["transport"] = {
            "type": "ws",
            "path": q.get("path", ["/"])[0],
            "headers": {"Host": q.get("host", [host])[0]},
        }
    elif net == "grpc":
        outbound["transport"] = {
            "type": "grpc",
            "service_name": q.get("serviceName", [""])[0],
        }

    return {"name": name, "outbound": outbound}


def parse_key(link: str) -> dict:
    link = link.strip()
    if link.startswith("vless://"):
        return parse_vless(link)
    elif link.startswith("hysteria2://") or link.startswith("hy2://"):
        return parse_hysteria2(link)
    elif link.startswith("trojan://"):
        return parse_trojan(link)
    else:
        raise ValueError(f"неподдерживаемый формат ключа: {link[:15]}...")


# ---------- ПОДПИСКИ ----------

def fetch_and_parse_subscription(url: str):
    import urllib.request
    import ssl

    source = urllib.parse.urlparse(url).hostname or "subscription"

    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            raw = resp.read()
    except Exception as e:
        # не удалось проверить сертификат штатными средствами —
        # откатываемся на незащищённый контекст (доверенный собственный сервер)
        decky.logger.error(f"fetch_and_parse_subscription: проверка SSL не прошла ({e}), пробуем без верификации")
        ctx_unverified = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=15, context=ctx_unverified) as resp:
            raw = resp.read()

    text = raw.decode("utf-8", errors="ignore").strip()

    # некоторые подписки отдают base64 одной строкой без переносов —
    # если в тексте нет "vless://"/"hysteria2://" прямым текстом, пробуем декодировать
    if "vless://" not in text and "hysteria2://" not in text and "hy2://" not in text and "trojan://" not in text:
        try:
            text = base64.b64decode(text + "===").decode("utf-8", errors="ignore")
        except Exception:
            pass

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    added = {}
    errors = []
    for line in lines:
        try:
            parsed = parse_key(line)
            added[parsed["name"]] = {
                "raw": line,
                "outbound": parsed["outbound"],
                "subscription": source,
                "type": parsed["outbound"].get("type", "?"),
            }
        except Exception as e:
            errors.append(str(e))

    return added, errors


ORIGINAL_IFACE = None  # физический интерфейс (wlan0 и т.п.), запоминаем ДО поднятия VPN


def detect_physical_iface():
    try:
        out = subprocess.check_output(["ip", "route", "show", "default"], text=True)
        for line in out.splitlines():
            if "tun-decky" in line:
                continue
            parts = line.split()
            if "dev" in parts:
                return parts[parts.index("dev") + 1]
    except Exception as e:
        decky.logger.error(f"detect_physical_iface ошибка: {e}")
    return None


def ping_host(host: str, timeout: float = 5.0):
    try:
        args = ["ping", "-4", "-c", "1", "-W", str(int(timeout))]
        if ORIGINAL_IFACE:
            args += ["-I", ORIGINAL_IFACE]
        args.append(host)
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 2)
        if result.returncode != 0:
            return {"ok": False, "error": "хост недоступен"}
        # ищем "time=41.2 ms" в выводе
        import re
        m = re.search(r"time[=<]([\d.]+)\s*ms", result.stdout)
        if m:
            return {"ok": True, "ms": round(float(m.group(1)), 1)}
        return {"ok": False, "error": "не удалось разобрать ответ ping"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "таймаут"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------- ХРАНЕНИЕ ПРОФИЛЕЙ (вынесено из класса — legacy RPC ломает self при внутренних вызовах) ----------

def load_profiles() -> dict:
    if PROFILES_PATH.exists():
        return json.loads(PROFILES_PATH.read_text())
    return {}


def save_profiles(profiles: dict):
    PROFILES_PATH.write_text(json.dumps(profiles, indent=2))


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_state(active_profile: str | None):
    STATE_PATH.write_text(json.dumps({"active_profile": active_profile}))


def load_subscriptions_meta() -> dict:
    if SUBSCRIPTIONS_META_PATH.exists():
        try:
            return json.loads(SUBSCRIPTIONS_META_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_subscriptions_meta(meta: dict):
    SUBSCRIPTIONS_META_PATH.write_text(json.dumps(meta, indent=2))


def remember_subscription(source: str, url: str):
    """Запоминаем URL подписки и время последнего обновления — чтобы потом
    можно было её фоново обновить, не заставляя юзера вставлять ссылку заново."""
    meta = load_subscriptions_meta()
    meta[source] = {"url": url, "last_updated": int(time.time())}
    save_subscriptions_meta(meta)


def refresh_all_subscriptions():
    """Проходит по всем сохранённым URL подписок и тихо обновляет профили.
    Если конкретная подписка недоступна — просто пропускаем её, старые профили
    этого источника не трогаем и last_updated не меняем."""
    meta = load_subscriptions_meta()
    if not meta:
        return

    profiles = load_profiles()
    changed = False

    for source, info in meta.items():
        url = info.get("url")
        if not url:
            continue
        try:
            new_profiles, errors = fetch_and_parse_subscription(url)
            if errors:
                decky.logger.error(f"refresh_all_subscriptions: {source} — ошибок парсинга: {len(errors)}")
            # убираем старые профили этого источника, кладём свежие —
            # так мёртвые/удалённые провайдером серверы не будут висеть вечно
            profiles = {name: data for name, data in profiles.items() if data.get("subscription") != source}
            profiles = merge_profiles_safely(profiles, new_profiles, source)
            meta[source]["last_updated"] = int(time.time())
            changed = True
            decky.logger.info(f"refresh_all_subscriptions: {source} обновлена, серверов: {len(new_profiles)}")
        except Exception as e:
            decky.logger.error(f"refresh_all_subscriptions: {source} недоступна, пропускаем ({e})")

    if changed:
        save_profiles(profiles)
        save_subscriptions_meta(meta)


def refresh_one_subscription(source: str) -> dict:
    """Обновляет одну конкретную подписку по её сохранённому URL."""
    meta = load_subscriptions_meta()
    info = meta.get(source)
    if not info or not info.get("url"):
        return {"ok": False, "error": "url подписки не найден"}

    try:
        new_profiles, errors = fetch_and_parse_subscription(info["url"])
        profiles = load_profiles()
        profiles = {name: data for name, data in profiles.items() if data.get("subscription") != source}
        profiles = merge_profiles_safely(profiles, new_profiles, source)
        save_profiles(profiles)
        meta[source]["last_updated"] = int(time.time())
        save_subscriptions_meta(meta)
        return {"ok": True, "added": len(new_profiles), "errors": errors}
    except Exception as e:
        decky.logger.error(f"refresh_one_subscription: {source} ошибка: {e}")
        return {"ok": False, "error": str(e)}


def delete_subscription_data(source: str):
    """Удаляет подписку целиком: её URL из метаданных и все профили, пришедшие из неё."""
    meta = load_subscriptions_meta()
    meta.pop(source, None)
    save_subscriptions_meta(meta)

    profiles = load_profiles()
    profiles = {name: data for name, data in profiles.items() if data.get("subscription") != source}
    save_profiles(profiles)


def merge_profiles_safely(profiles: dict, new_profiles: dict, source: str) -> dict:
    """Добавляет new_profiles в общий словарь profiles, избегая коллизий имён
    МЕЖДУ разными подписками (например обе подписки прислали сервер "US").
    Если имя уже занято профилем ИЗ ДРУГОГО источника — добавляет суффикс -2, -3...
    Профили того же source (перезалив той же подписки) просто заменяются как раньше."""
    for name, data in new_profiles.items():
        data.setdefault("display_name", name)  # оригинальное имя до суффикса, для UI
        existing = profiles.get(name)
        if existing is not None and existing.get("subscription") != source:
            counter = 2
            candidate = f"{name}-{counter}"
            while candidate in profiles and profiles[candidate].get("subscription") != source:
                counter += 1
                candidate = f"{name}-{counter}"
            profiles[candidate] = data
        else:
            profiles[name] = data
    return profiles


# ---------- СБОРКА КОНФИГА SING-BOX ----------

def build_config(outbound: dict) -> dict:
    return {
        "log": {"level": "warn"},
        "inbounds": [
            {
                "type": "tun",
                "tag": "tun-in",
                "interface_name": "tun-decky",
                "address": ["172.19.0.1/30"],
                "mtu": 1500,
                "auto_route": True,
                "strict_route": True,
                "stack": "system",
            }
        ],
        "outbounds": [
            outbound,
            {"type": "direct", "tag": "direct"},
        ],
        "route": {
            "rules": [
                {"protocol": "dns", "action": "hijack-dns"},
                {
                    "ip_cidr": [
                        "192.168.0.0/16",
                        "10.0.0.0/8",
                        "172.16.0.0/12",
                        "169.254.0.0/16"
                    ],
                    "outbound": "direct"
                }
            ],
            "final": "proxy",
            "auto_detect_interface": True,
        },
        "dns": {
            "servers": [{"type": "udp", "tag": "dns-proxy", "server": "1.1.1.1", "detour": "proxy"}]
        },
    }


# ---------- ПЛАГИН ----------

class Plugin:
    process: subprocess.Popen | None = None
    log_file = None
    active_profile: str | None = None

    async def add_profile(self, key: str):
        decky.logger.info(f"add_profile вызван, ключ (начало): {key[:20] if key else 'ПУСТО'}")
        key = (key or "").strip()

        # если это ссылка на подписку, а не одиночный ключ — уходим в отдельную ветку
        if key.startswith("http://") or key.startswith("https://"):
            decky.logger.info("add_profile обнаружил ссылку на подписку, парсим напрямую")
            try:
                new_profiles, errors = fetch_and_parse_subscription(key)
                profiles = load_profiles()
                if new_profiles:
                    source = next(iter(new_profiles.values()))["subscription"]
                    profiles = merge_profiles_safely(profiles, new_profiles, source)
                    remember_subscription(source, key)
                save_profiles(profiles)
                decky.logger.info(f"подписка обработана, добавлено: {len(new_profiles)}, ошибок: {len(errors)}")
                return {"ok": True, "added": list(new_profiles.keys()), "errors": errors}
            except Exception as e:
                decky.logger.error(f"add_profile (subscription) ОШИБКА: {e}")
                return {"ok": False, "error": str(e)}

        try:
            parsed = parse_key(key)
            profiles = load_profiles()
            profiles[parsed["name"]] = {
                "raw": key,
                "outbound": parsed["outbound"],
                "subscription": "manual",
                "type": parsed["outbound"].get("type", "?"),
                "display_name": parsed["name"],
            }
            save_profiles(profiles)
            decky.logger.info(f"add_profile успешно добавлен профиль: {parsed['name']}")
            return {"ok": True, "name": parsed["name"]}
        except Exception as e:
            decky.logger.error(f"add_profile ОШИБКА: {e}")
            return {"ok": False, "error": str(e)}

    async def add_subscription(self, url: str):
        decky.logger.info(f"add_subscription вызван, url: {url}")
        try:
            new_profiles, errors = fetch_and_parse_subscription(url)
            profiles = load_profiles()
            if new_profiles:
                source = next(iter(new_profiles.values()))["subscription"]
                profiles = merge_profiles_safely(profiles, new_profiles, source)
                remember_subscription(source, url)
            save_profiles(profiles)
            decky.logger.info(f"add_subscription добавлено профилей: {len(new_profiles)}, ошибок: {len(errors)}")
            return {"ok": True, "added": list(new_profiles.keys()), "errors": errors}
        except Exception as e:
            decky.logger.error(f"add_subscription ОШИБКА: {e}")
            return {"ok": False, "error": str(e)}

    async def list_profiles(self):
        return list(load_profiles().keys())

    async def list_profiles_full(self):
        profiles = load_profiles()
        return [
            {
                "name": name,
                "display_name": data.get("display_name", name),
                "subscription": data.get("subscription", "manual"),
                "type": data.get("type") or data.get("outbound", {}).get("type", "?"),
            }
            for name, data in profiles.items()
        ]

    async def list_subscriptions(self):
        profiles = load_profiles()
        subs = sorted(set(data.get("subscription", "manual") for data in profiles.values()))
        return subs

    async def get_subscriptions_info(self):
        meta = load_subscriptions_meta()
        return [{"name": source, "last_updated": info.get("last_updated")} for source, info in meta.items()]

    async def refresh_subscription(self, name: str):
        decky.logger.info(f"refresh_subscription вызван для {name}")
        # сетевой запрос — уводим в отдельный поток, чтобы не блокировать остальные RPC
        return await asyncio.to_thread(refresh_one_subscription, name)

    async def delete_subscription(self, name: str):
        decky.logger.info(f"delete_subscription вызван для {name}")
        delete_subscription_data(name)
        return {"ok": True}

    async def ping_profile(self, name: str):
        profiles = load_profiles()
        if name not in profiles:
            return {"ok": False, "error": "профиль не найден"}
        outbound = profiles[name]["outbound"]
        host = outbound.get("server")
        if not host:
            return {"ok": False, "error": "нет адреса сервера"}
        return ping_host(host)

    async def delete_profile(self, name: str):
        profiles = load_profiles()
        profiles.pop(name, None)
        save_profiles(profiles)
        return {"ok": True}

    async def connect(self, name: str):
        global ORIGINAL_IFACE
        if not BIN_PATH.exists():
            return {"ok": False, "error": "sing-box ещё не скачан, подожди немного"}
        profiles = load_profiles()
        if name not in profiles:
            return {"ok": False, "error": "профиль не найден"}

        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                decky.logger.error("connect: процесс не завершился по terminate, убиваем через kill")
                self.process.kill()
        else:
            # запоминаем физический интерфейс только если VPN сейчас реально выключен,
            # иначе рискуем запомнить сам tun-decky
            ORIGINAL_IFACE = detect_physical_iface()
            decky.logger.info(f"connect: физический интерфейс определён как {ORIGINAL_IFACE}")
            # процесс мёртв (сам упал), но tun-decky мог остаться висеть — подчищаем перед новым запуском
            try:
                subprocess.run(["ip", "link", "delete", "tun-decky"], capture_output=True, timeout=5)
            except Exception:
                pass

        config = build_config(profiles[name]["outbound"])
        CONFIG_PATH.write_text(json.dumps(config, indent=2))

        # на случай если при установке/распаковке плагина слетел флаг исполняемости
        if BIN_PATH.exists():
            try:
                os.chmod(BIN_PATH, 0o755)
            except Exception as e:
                decky.logger.error(f"connect: не удалось выставить права на {BIN_PATH}: {e}")
                return {"ok": False, "error": f"не удалось подготовить бинарник sing-box: {e}"}

        if self.log_file:
            try:
                self.log_file.close()
            except Exception:
                pass
        self.log_file = open(LOG_PATH, "w")
        self.process = subprocess.Popen(
            [str(BIN_PATH), "run", "-c", str(CONFIG_PATH)],
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
        )
        self.active_profile = name
        save_state(name)
        decky.logger.info(f"sing-box запущен, pid={self.process.pid}, профиль={name}")
        return {"ok": True}

    async def disconnect(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.log_file:
            try:
                self.log_file.close()
            except Exception:
                pass
            self.log_file = None
        self.process = None
        self.active_profile = None
        save_state(None)
        return {"ok": True}

    async def status(self):
        running = bool(self.process and self.process.poll() is None)
        if not running:
            self.active_profile = None
        return {"connected": running, "active_profile": self.active_profile}

    async def get_logs(self):
        if not LOG_PATH.exists():
            return {"ok": True, "lines": []}
        try:
            lines = LOG_PATH.read_text(errors="ignore").splitlines()
            return {"ok": True, "lines": lines[-100:]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def network_info(self):
        iface = detect_physical_iface()
        tun_up = False
        try:
            out = subprocess.check_output(["ip", "addr", "show", "tun-decky"], text=True, stderr=subprocess.DEVNULL)
            tun_up = "inet " in out
        except Exception:
            tun_up = False

        # реальная проверка интернета — короткий запрос наружу (через любой доступный маршрут,
        # не важно tun это или физический интерфейс — важен сам факт "инет вообще есть")
        internet_ok = False
        try:
            req = urllib.request.Request("https://1.1.1.1", headers={"User-Agent": "Mozilla/5.0"})
            ctx = _build_ssl_context()
            with urllib.request.urlopen(req, timeout=4, context=ctx):
                internet_ok = True
        except Exception:
            internet_ok = False

        # доступен ли именно сервер активного профиля, в обход туннеля,
        # напрямую через физический интерфейс — чтобы понять жив ли сам VPN-сервер
        server_ok = None
        if self.active_profile:
            profiles = load_profiles()
            data = profiles.get(self.active_profile)
            if data:
                host = data.get("outbound", {}).get("server")
                if host:
                    ping_result = await asyncio.to_thread(ping_host, host)
                    server_ok = bool(ping_result.get("ok"))

        return {
            "iface": iface,
            "tun_up": tun_up,
            "internet_ok": internet_ok,
            "server_ok": server_ok,
        }

    async def get_download_status(self):
        return dict(download_state)

    async def retry_download(self):
        if download_state.get("downloading"):
            return {"ok": False, "error": "уже качается"}
        thread = threading.Thread(target=download_singbox, daemon=True)
        thread.start()
        return {"ok": True}

    async def _deferred_startup(self):
        """Ждёт скачивания sing-box (если нужно) и восстанавливает соединение —
        вынесено из _main в отдельную задачу, чтобы сама _main не блокировала
        готовность плагина к RPC-вызовам на время долгого скачивания."""
        if not BIN_PATH.exists():
            while download_state.get("downloading") or not download_state.get("done"):
                await asyncio.sleep(0.3)
            if download_state.get("error"):
                decky.logger.error("автозапуск после скачивания пропущен из-за ошибки загрузки")
                return

        state = load_state()
        prev_active = state.get("active_profile")
        if prev_active:
            profiles = load_profiles()
            if prev_active in profiles:
                decky.logger.info(f"восстанавливаем соединение после запуска системы: {prev_active}")
                result = await self.connect(prev_active)
                if not result.get("ok"):
                    decky.logger.error(f"не удалось восстановить соединение: {result.get('error')}")
                    save_state(None)
            else:
                decky.logger.info(f"сохранённый профиль {prev_active} больше не существует, пропускаем автоподключение")
                save_state(None)

        # тихое фоновое обновление подписок — если какая-то подписка недоступна,
        # старые профили просто остаются как есть
        threading.Thread(target=refresh_all_subscriptions, daemon=True).start()

    async def _main(self):
        global ORIGINAL_IFACE
        ORIGINAL_IFACE = detect_physical_iface()
        decky.logger.info(f"VPN-плагин загружен, физический интерфейс: {ORIGINAL_IFACE}")

        if not BIN_PATH.exists():
            # качаем в отдельном потоке, фронт опрашивает get_download_status и показывает прогресс
            threading.Thread(target=download_singbox, daemon=True).start()

        # вся долгая часть (ожидание скачивания + автоконнект + обновление подписок)
        # уходит в фоновую задачу — сама _main завершается сразу же
        asyncio.create_task(self._deferred_startup())

    async def _unload(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                decky.logger.error("_unload: процесс не завершился по terminate, убиваем через kill")
                self.process.kill()
        if self.log_file:
            try:
                self.log_file.close()
            except Exception:
                pass
        decky.logger.info("VPN-плагин выгружен")
