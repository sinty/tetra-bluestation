#!/usr/bin/env python3
"""
tetra-dash — панель наблюдения и управления базовой станцией TETRA.

Три сервера в одном процессе:

  :9001  телеметрия   сабпротокол bluestation-telemetry-v1   станция -> мы
  :9002  управление   сабпротокол bluestation-control-v1     мы <-> станция
  :8088  браузер      HTML + WebSocket /ws

Станция подключается к нам сама: в её конфигурации секции [telemetry]
и [command] задают host/port, а слушаем здесь мы. Кадры бинарные, внутри JSON —
внешне тегированные enum'ы serde, например {"MsRegistration":{"issi":1234567}}.

Телеметрия отдаёт всего четыре события (регистрация, дерегистрация, привязка
и отвязка групп). Этого мало для полезной панели, поэтому состояние Brew
и групповые вызовы дочитываются из журнала systemd — источник каждой строки
показан в интерфейсе, чтобы структурные данные не путались с разбором текста.

Все адреса, пароли и идентификаторы — в /etc/tetra-lab.conf.
"""

import asyncio
import base64
import hmac
import json
import os
import re
import secrets
import shlex
import sys
import time
from collections import deque

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

CONF_PATH = os.environ.get("TETRA_LAB_CONF", "/etc/tetra-lab.conf")

TELEMETRY_PROTOCOL = "bluestation-telemetry-v1"
CONTROL_PROTOCOL = "bluestation-control-v1"

MAX_EVENTS = 300
ANSI = re.compile(r"\x1b\[[0-9;]*m")


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

def load_conf(path):
    """Читает файл присваиваний shell. Полноценный разбор не нужен:
    формат намеренно простой, чтобы его читали и sh, и Python."""
    conf = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if not key.isidentifier():
                    continue
                try:
                    parts = shlex.split(value, comments=True)
                except ValueError:
                    continue
                conf[key] = parts[0] if parts else ""
    except OSError as e:
        print(f"tetra-dash: не читается {path}: {e}", file=sys.stderr)
    return conf


CONF = load_conf(CONF_PATH)


def cfg(key, default=""):
    return CONF.get(key, default)


def cfg_int(key, default):
    try:
        return int(CONF.get(key, default))
    except (TypeError, ValueError):
        return default


NOTIFY_URL = cfg("NOTIFY_URL")
NOTIFY_PREFIX = cfg("NOTIFY_PREFIX")
STATION_UNIT = cfg("STATION_UNIT", "bluestation-bs.service")
STATION_FLAG = cfg("STATION_FLAG", "/var/lib/tetra-lab/station-wanted")
DASH_BIND = cfg("DASH_BIND", "127.0.0.1")
HTTP_PORT = cfg_int("DASH_HTTP_PORT", 8088)
TELEMETRY_PORT = cfg_int("DASH_TELEMETRY_PORT", 9001)
CONTROL_PORT = cfg_int("DASH_CONTROL_PORT", 9002)
DASH_USER = cfg("DASH_USER")
DASH_PASSWORD = cfg("DASH_PASSWORD")
SDS_SOURCE_ISSI = cfg_int("SDS_SOURCE_ISSI", 0)
PLUTO_HOST = cfg("PLUTO_HOST")
TEMP_INTERVAL = cfg_int("PLUTO_TEMP_INTERVAL", 30)
TEMP_HISTORY = cfg("PLUTO_TEMP_HISTORY", "/var/lib/tetra-lab/pluto-temp.json")
TEMP_POINTS = cfg_int("PLUTO_TEMP_POINTS", 720)
CALLSIGN_URL = cfg("CALLSIGN_URL")
CALLSIGN_CACHE = cfg("CALLSIGN_CACHE", "/var/lib/tetra-lab/callsigns.json")
DL_FREQ = cfg("DL_FREQ_MHZ", "?")
UL_FREQ = cfg("UL_FREQ_MHZ", "?")

# Сессионный маркер: браузер получает его печеньем после Basic-аутентификации.
# Нужен потому, что JS не может добавить заголовок Authorization к WebSocket,
# а печенье к тому же origin браузер отправляет сам.
SESSION_TOKEN = secrets.token_urlsafe(24)
AUTH_REQUIRED = bool(DASH_PASSWORD)

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dash.html")


def now():
    return time.time()


# ---------------------------------------------------------------------------
# Состояние
# ---------------------------------------------------------------------------

class State:
    """Всё наблюдаемое состояние. Один процесс, одна петля asyncio —
    блокировки не нужны."""

    def __init__(self):
        self.started = now()
        self.telemetry_link = False
        self.control_link = False
        self.backhaul = "неизвестно"
        self.station = "неизвестно"      # из systemctl is-active
        self.station_wanted = False      # флаговый файл
        self.station_busy = ""           # "запускается" / "останавливается"
        self.subscribers = {}
        self.events = deque(maxlen=MAX_EVENTS)
        self.calls = deque(maxlen=50)
        self.browsers = set()
        self.pending = {}
        self.next_handle = 1
        self.control_ws = None

    def event(self, kind, text, source):
        item = {"ts": now(), "kind": kind, "text": text, "source": source}
        self.events.appendleft(item)
        return item


STATE = State()


# ---------------------------------------------------------------------------
# Уведомления наружу — тем же URL, что и у скриптов станции
# ---------------------------------------------------------------------------

async def notify(text):
    if not NOTIFY_URL:
        return
    msg = f"{NOTIFY_PREFIX}{text}"
    enc = "".join(f"%{b:02x}" for b in msg.encode("utf-8"))
    url = NOTIFY_URL.replace("{message}", enc)
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-m", "10", "-o", "/dev/null", url,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception:
        pass  # уведомление — диагностика, оно не имеет права ничего ломать


# ---------------------------------------------------------------------------
# Рассылка в браузеры
# ---------------------------------------------------------------------------

def snapshot():
    return {
        "type": "snapshot",
        "telemetry_link": STATE.telemetry_link,
        "control_link": STATE.control_link,
        "backhaul": STATE.backhaul,
        "station": STATE.station,
        "station_wanted": STATE.station_wanted,
        "station_busy": STATE.station_busy,
        "dl_freq": DL_FREQ,
        "ul_freq": UL_FREQ,
        "sds_source": SDS_SOURCE_ISSI,
        "uptime": now() - STATE.started,
        "subscribers": [
            {"issi": issi, "call": callsign_of(issi), **info}
            for issi, info in sorted(STATE.subscribers.items())
        ],
        "callsigns": {str(i): r.get("call") for i, r in CALLSIGNS.items() if r.get("call")},
        "callsign_cache": len(CALLSIGNS),
        "temps": list(TEMPS),
        "temp_now": TEMPS[-1] if TEMPS else None,
        "calls": list(STATE.calls),
        "events": list(STATE.events)[:120],
    }


async def broadcast():
    if not STATE.browsers:
        return
    payload = json.dumps(snapshot(), ensure_ascii=False)
    dead = []
    for ws in STATE.browsers:
        try:
            await ws.send(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        STATE.browsers.discard(ws)


# ---------------------------------------------------------------------------
# Управление станцией
# ---------------------------------------------------------------------------

async def systemctl(verb):
    """Пуск и останов разрешены правилом polkit ровно для этого юнита.
    --no-block обязателен: старт занимает до трёх минут (ExecStartPre ждёт
    Pluto, ExecStartPost ждёт Brew), и держать на нём запрос браузера нельзя."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", verb, "--no-block", STATION_UNIT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            return False, err.decode("utf-8", "replace").strip() or "неизвестная ошибка"
        return True, ""
    except Exception as e:
        return False, str(e)


async def station_control(action):
    if STATE.station_busy:
        return {"ok": False, "error": f"уже {STATE.station_busy}, подождите"}

    if action == "start":
        try:
            # Флаг ставим ПЕРЕД запуском: ConditionPathExists проверяется
            # при каждой попытке, включая автоматические перезапуски.
            open(STATION_FLAG, "w").close()
        except OSError as e:
            return {"ok": False, "error": f"не записать флаг {STATION_FLAG}: {e}"}

        STATE.station_busy = "запускается"
        STATE.event("control", "оператор запустил станцию", "панель")
        await broadcast()
        ok, err = await systemctl("start")
        if not ok:
            STATE.station_busy = ""
            STATE.event("error", f"запуск не удался: {err}", "панель")
            return {"ok": False, "error": err}
        await notify("оператор запустил станцию с панели")
        return {"ok": True}

    if action == "stop":
        STATE.station_busy = "останавливается"
        STATE.event("control", "оператор остановил станцию", "панель")
        await broadcast()
        ok, err = await systemctl("stop")
        if not ok:
            STATE.station_busy = ""
            STATE.event("error", f"останов не удался: {err}", "панель")
            return {"ok": False, "error": err}
        # Флаг снимаем ПОСЛЕ останова: иначе Restart=always успел бы
        # передумать между удалением файла и командой стоп.
        try:
            os.unlink(STATION_FLAG)
        except FileNotFoundError:
            pass
        except OSError as e:
            return {"ok": False, "error": f"станция остановлена, но флаг не снят: {e}"}
        await notify("оператор остановил станцию с панели, автозапуск выключен")
        return {"ok": True}

    return {"ok": False, "error": f"неизвестное действие {action}"}


async def backhaul_socket_up():
    """Есть ли у станции установленное TLS-соединение с ядром сети.
    Тот же признак, по которому докладывает об удачном старте bs-poststart.sh:
    сокет — это факт, а строка в логе — только память о прошлом."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ss", "-tn", "state", "established",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        for line in out.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[-1].endswith(":443"):
                return True
    except Exception:
        pass
    return False


async def station_watcher():
    """Опрашивает systemd. Права не нужны: is-active доступен всем."""
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "is-active", STATION_UNIT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            active = out.decode().strip() or "unknown"

            wanted = os.path.exists(STATION_FLAG)
            changed = (active != STATE.station or wanted != STATE.station_wanted)

            # Снимаем «запускается», когда systemd договорил
            if STATE.station_busy == "запускается" and active in ("active", "failed"):
                STATE.station_busy = ""
                changed = True
            elif STATE.station_busy == "останавливается" and active in ("inactive", "failed"):
                STATE.station_busy = ""
                changed = True

            # Состояние магистрали берём по факту установленного сокета, а не
            # из журнала: журнал читается только с момента запуска панели,
            # поэтому после её перезапуска давно поднятая связь выглядела бы
            # как «ждём подключения» навсегда.
            backhaul = "станция не работает" if active != "active" else (
                "подключён" if await backhaul_socket_up() else "нет связи")
            if backhaul != STATE.backhaul:
                STATE.backhaul = backhaul
                changed = True

            STATE.station = active
            STATE.station_wanted = wanted
            if changed:
                await broadcast()
        except Exception:
            pass
        await asyncio.sleep(3)


# ---------------------------------------------------------------------------
# Телеметрия
# ---------------------------------------------------------------------------

def apply_telemetry(event):
    (name, body), = event.items()
    issi = body.get("issi")
    sub = STATE.subscribers.setdefault(
        issi, {"groups": [], "since": now(), "last_seen": now()}
    )
    sub["last_seen"] = now()

    if name == "MsRegistration":
        sub["since"] = now()
        STATE.event("reg", f"рация {issi} зарегистрировалась", "телеметрия")
    elif name == "MsDeregistration":
        STATE.subscribers.pop(issi, None)
        STATE.event("dereg", f"рация {issi} отключилась", "телеметрия")
    elif name == "MsGroupAttach":
        gssis = body.get("gssis", [])
        for g in gssis:
            if g not in sub["groups"]:
                sub["groups"].append(g)
        STATE.event("attach", f"{issi} встала на группы {gssis}", "телеметрия")
    elif name == "MsGroupDetach":
        gssis = body.get("gssis", [])
        sub["groups"] = [g for g in sub["groups"] if g not in gssis]
        STATE.event("detach", f"{issi} снялась с групп {gssis}", "телеметрия")
    else:
        STATE.event("other", f"неизвестное событие {name}: {body}", "телеметрия")


async def telemetry_handler(ws):
    STATE.telemetry_link = True
    STATE.event("link", "станция подключилась к телеметрии", "сервис")
    # Станция не повторяет события о тех, кто зарегистрировался раньше нас
    await resync_subscribers()
    await broadcast()
    try:
        async for message in ws:
            if isinstance(message, str):
                continue
            try:
                apply_telemetry(json.loads(message))
            except Exception as e:
                STATE.event("error", f"разбор телеметрии: {e}", "сервис")
            await broadcast()
    except Exception:
        # Станцию перезапустили или связь оборвалась — это штатное событие,
        # разбираем его в finally, а не простынёй трассировки в журнале.
        pass
    finally:
        STATE.telemetry_link = False
        # Список НЕ чистим: рации остаются в соте и при оборванной телеметрии.
        # Достоверность восстановит пересказ журнала при следующем подключении.
        STATE.event("link", "телеметрия отключилась", "сервис")
        await broadcast()


# ---------------------------------------------------------------------------
# Управление станцией по Brew-подобному каналу: SDS
# ---------------------------------------------------------------------------

async def control_handler(ws):
    STATE.control_link = True
    STATE.control_ws = ws
    STATE.event("link", "станция подключилась к каналу управления", "сервис")
    await broadcast()
    try:
        async for message in ws:
            if isinstance(message, str):
                continue
            try:
                (name, body), = json.loads(message).items()
                fut = STATE.pending.pop(body.get("handle"), None)
                if fut and not fut.done():
                    fut.set_result(body)
                STATE.event("control", f"ответ станции {name}: {body}", "управление")
            except Exception as e:
                STATE.event("error", f"разбор ответа управления: {e}", "сервис")
            await broadcast()
    except Exception:
        pass  # см. telemetry_handler
    finally:
        STATE.control_link = False
        STATE.control_ws = None
        STATE.event("link", "канал управления отключился", "сервис")
        await broadcast()


def build_sds_text(text):
    """SDS-TL текстовое сообщение: протокольный идентификатор 0x82,
    затем схема кодирования. Латиница влезает в 8-битную (0x01),
    кириллица требует UCS-2 (0x1A) и вдвое больше места."""
    try:
        return bytes([0x82, 0x01]) + text.encode("ascii")
    except UnicodeEncodeError:
        return bytes([0x82, 0x1A]) + text.encode("utf-16-be")


async def send_sds(dest_ssi, text, dest_is_group=False):
    if STATE.control_ws is None:
        return {"ok": False, "error": "канал управления не подключён"}

    payload = build_sds_text(text)
    if len(payload) * 8 > 2047:
        return {"ok": False, "error": f"слишком длинно: {len(payload)} байт, "
                                      f"предел станции 2047 бит"}

    handle = STATE.next_handle
    STATE.next_handle += 1

    cmd = {"SendSds": {
        "handle": handle,
        "source_ssi": SDS_SOURCE_ISSI,
        "dest_ssi": dest_ssi,
        "dest_is_group": dest_is_group,
        "len_bits": len(payload) * 8,
        "payload": list(payload),
    }}

    fut = asyncio.get_running_loop().create_future()
    STATE.pending[handle] = fut
    await STATE.control_ws.send(json.dumps(cmd).encode())
    STATE.event("sds", f"SDS {SDS_SOURCE_ISSI} -> {dest_ssi}: {text!r}", "управление")

    try:
        body = await asyncio.wait_for(fut, timeout=10)
    except asyncio.TimeoutError:
        STATE.pending.pop(handle, None)
        return {"ok": False, "error": "станция не ответила за 10 с"}

    if not body.get("success"):
        return {"ok": False,
                "error": "станция отказала — почти всегда значит, что получатель "
                         "не зарегистрирован в соте"}
    return {"ok": True}


# ---------------------------------------------------------------------------
# Температура Pluto
# ---------------------------------------------------------------------------

# Два независимых датчика, и мерят они разное:
#   AD9363 — сам радиотракт, греется от передачи;
#   XADC   — кристалл Zynq, греется от обработки и от корпуса в целом.
# Расходятся они обычно градусов на двадцать, и следить полезно за обоими:
# рост радиотракта при спокойном SoC означает проблему в передаче, а общий
# подъём — что коробке нечем дышать.

TEMPS = deque(maxlen=TEMP_POINTS)   # [{"ts":…, "rf":…, "soc":…}]


def load_temps():
    try:
        with open(TEMP_HISTORY, encoding="utf-8") as f:
            for item in json.load(f)[-TEMP_POINTS:]:
                TEMPS.append(item)
    except (OSError, ValueError):
        pass


def save_temps():
    tmp = TEMP_HISTORY + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(TEMPS), f)
        os.replace(tmp, TEMP_HISTORY)
    except OSError:
        pass  # график — украшение, из-за него ничего ломаться не должно


async def _iio(dev, chan):
    proc = await asyncio.create_subprocess_exec(
        "iio_attr", "-u", f"ip:{PLUTO_HOST}", "-c", dev, chan,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    vals = {}
    for line in out.decode("utf-8", "replace").splitlines():
        m = re.search(r"attr '(\w+)', value '([-\d.]+)'", line)
        if m:
            vals[m.group(1)] = float(m.group(2))
    return vals


async def read_pluto_temps():
    """AD9363 отдаёт милliградусы напрямую. XADC — сырой отсчёт, который надо
    пересчитать по своим же offset и scale: (raw + offset) * scale / 1000."""
    rf = soc = None
    try:
        v = await _iio("ad9361-phy", "temp0")
        if "input" in v:
            rf = round(v["input"] / 1000.0, 1)
    except Exception:
        pass
    try:
        v = await _iio("xadc", "temp0")
        if {"raw", "offset", "scale"} <= set(v):
            soc = round((v["raw"] + v["offset"]) * v["scale"] / 1000.0, 1)
    except Exception:
        pass
    return rf, soc


async def temp_poller():
    if not PLUTO_HOST:
        return
    while True:
        rf, soc = await read_pluto_temps()
        if rf is not None or soc is not None:
            TEMPS.append({"ts": now(), "rf": rf, "soc": soc})
            save_temps()
            await broadcast()
        await asyncio.sleep(TEMP_INTERVAL)


# ---------------------------------------------------------------------------
# Позывные: разрешение ID через внешний справочник, с дисковым кэшем
# ---------------------------------------------------------------------------

# Кэш нужен не ради скорости, а ради приличия: справочник — чужой публичный
# сервис, и дёргать его на каждую перерисовку панели нельзя. Положительные
# записи живут вечно (позывной за ID закреплён), отрицательные перепроверяются
# через неделю — ID мог быть зарегистрирован уже после нашего запроса.
NEGATIVE_TTL = 7 * 24 * 3600

CALLSIGNS = {}          # issi -> {"call": str|None, "name": str, "city": str, "ts": float}
_resolving = set()      # чтобы не запрашивать один ID несколькими задачами сразу


def load_callsigns():
    try:
        with open(CALLSIGN_CACHE, encoding="utf-8") as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items()}
    except (OSError, ValueError):
        return {}


def save_callsigns():
    """Пишем через временный файл: обрыв посреди записи не должен оставить
    покалеченный кэш, который потом не прочитается."""
    tmp = CALLSIGN_CACHE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in CALLSIGNS.items()}, f, ensure_ascii=False)
        os.replace(tmp, CALLSIGN_CACHE)
    except OSError as e:
        STATE.event("error", f"кэш позывных не сохранён: {e}", "сервис")


def callsign_of(issi):
    rec = CALLSIGNS.get(issi)
    return rec.get("call") if rec else None


def needs_lookup(issi):
    rec = CALLSIGNS.get(issi)
    if rec is None:
        return True
    if rec.get("call"):
        return False
    return now() - rec.get("ts", 0) > NEGATIVE_TTL


async def resolve_callsign(issi):
    if not CALLSIGN_URL or issi in _resolving or not needs_lookup(issi):
        return False
    _resolving.add(issi)
    try:
        url = CALLSIGN_URL.replace("{id}", str(issi))
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-m", "12", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        rec = {"call": None, "name": "", "city": "", "ts": now()}
        try:
            results = json.loads(out.decode("utf-8", "replace")).get("results") or []
            if results:
                r = results[0]
                rec = {"call": r.get("callsign") or None,
                       "name": (r.get("fname") or "").strip(),
                       "city": (r.get("city") or "").strip(),
                       "ts": now()}
        except ValueError:
            pass  # справочник ответил не JSON — считаем, что не нашли
        CALLSIGNS[issi] = rec
        save_callsigns()
        if rec["call"]:
            STATE.event("callsign", f"{issi} = {rec['call']}"
                        + (f", {rec['name']}" if rec["name"] else ""), "справочник")
        return True
    except Exception as e:
        STATE.event("error", f"справочник позывных: {e}", "сервис")
        return False
    finally:
        _resolving.discard(issi)


def known_issis():
    """Все ID, которые где-то показываются: абоненты и участники вызовов."""
    ids = set(STATE.subscribers)
    for c in STATE.calls:
        ids.add(c.get("src"))
    ids.add(SDS_SOURCE_ISSI)
    return {i for i in ids if isinstance(i, int) and i > 0}


async def callsign_resolver():
    """Фоновое разрешение. По одному запросу за раз и с паузой — чужой сервис
    не должен получать от нас очередь."""
    while True:
        changed = False
        for issi in sorted(known_issis()):
            if needs_lookup(issi):
                if await resolve_callsign(issi):
                    changed = True
                await asyncio.sleep(1)
        if changed:
            await broadcast()
        await asyncio.sleep(10)


async def reset_callsigns():
    CALLSIGNS.clear()
    try:
        os.unlink(CALLSIGN_CACHE)
    except FileNotFoundError:
        pass
    except OSError as e:
        return {"ok": False, "error": str(e)}
    STATE.event("callsign", "кэш позывных сброшен, справочник опрошу заново", "панель")
    await broadcast()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Журнал systemd как источник того, чего нет в телеметрии
# ---------------------------------------------------------------------------

RE_BACKHAUL = re.compile(r"backhaul (CONNECTED|DISCONNECTED)")
RE_GROUP_TX = re.compile(r"GROUP_TX uuid=(\S+?)\s+src=(\d+) dst=(\d+)")
RE_LOCAL_CALL = re.compile(
    r"forwarding local call to TetraPack: call_id=(\d+) src=(\d+) gssi=(\d+)")
RE_SUB_UPDATE = re.compile(
    r"MmSubscriberUpdate \{ issi: (\d+), groups: \[([^\]]*)\], action: (\w+)")


def parse_journal_line(line):
    line = ANSI.sub("", line).rstrip()

    m = RE_BACKHAUL.search(line)
    if m:
        STATE.backhaul = "подключён" if m.group(1) == "CONNECTED" else "разорван"
        return STATE.event("backhaul", f"Brew: {STATE.backhaul}", "журнал")

    m = RE_GROUP_TX.search(line)
    if m:
        call = {"ts": now(), "dir": "из сети",
                "src": int(m.group(2)), "dst": int(m.group(3))}
        STATE.calls.appendleft(call)
        return STATE.event(
            "call", f"вызов из сети: {call['src']} -> группа {call['dst']}", "журнал")

    m = RE_LOCAL_CALL.search(line)
    if m:
        call = {"ts": now(), "dir": "в сеть",
                "src": int(m.group(2)), "dst": int(m.group(3))}
        STATE.calls.appendleft(call)
        return STATE.event(
            "call", f"вызов в сеть: {call['src']} -> группа {call['dst']}", "журнал")

    m = RE_SUB_UPDATE.search(line)
    if m:
        return STATE.event(
            "mm", f"MM: {m.group(3)} issi={m.group(1)} группы=[{m.group(2)}]", "журнал")

    return None


RE_AFFIL = re.compile(r"issi=(\d+)\s*→\s*(DE)?AFFILIATE groups=\[([0-9,\s]*)\]")
RE_MM_UPD = re.compile(
    r"MmSubscriberUpdate \{ issi: (\d+), groups: \[([0-9,\s]*)\], action: (\w+)")


def _groups(text):
    return [int(x) for x in text.replace(" ", "").split(",") if x]


async def station_started_at():
    """Момент запуска станции. Всё, что в журнале раньше, относится
    к прошлой её жизни и к текущему составу абонентов отношения не имеет."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "show", STATION_UNIT, "-p", "ActiveEnterTimestamp", "--value",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        return out.decode().strip()
    except Exception:
        return ""


async def resync_subscribers():
    """Перечитывает состав раций из журнала станции.

    Нужно потому, что телеметрия событийная: станция сообщает о регистрации
    один раз и больше не повторяет. Если панель перезапустилась позже рации,
    она о ней никогда не узнает — и показывает пустую соту при работающей связи.
    Проверять состав можно только пересказом журнала с момента старта станции.
    """
    since = await station_started_at()
    if not since:
        return
    unit = STATION_UNIT.removesuffix(".service")
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-u", unit, "--since", since, "--no-pager", "-o", "cat",
            "--grep", "AFFILIATE|MmSubscriberUpdate",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
    except Exception as e:
        STATE.event("error", f"пересказ журнала не удался: {e}", "сервис")
        return

    subs = {}
    for raw in out.decode("utf-8", "replace").splitlines():
        line = ANSI.sub("", raw)

        m = RE_MM_UPD.search(line)
        if m:
            issi, groups, action = int(m.group(1)), _groups(m.group(2)), m.group(3)
            if action.lower().startswith("dereg"):
                subs.pop(issi, None)
            else:
                sub = subs.setdefault(issi, {"groups": [], "since": now(), "last_seen": now()})
                for g in groups:
                    if g not in sub["groups"]:
                        sub["groups"].append(g)
            continue

        m = RE_AFFIL.search(line)
        if m:
            issi, off, groups = int(m.group(1)), bool(m.group(2)), _groups(m.group(3))
            sub = subs.setdefault(issi, {"groups": [], "since": now(), "last_seen": now()})
            if off:
                sub["groups"] = [g for g in sub["groups"] if g not in groups]
            else:
                for g in groups:
                    if g not in sub["groups"]:
                        sub["groups"].append(g)

    if subs != STATE.subscribers:
        STATE.subscribers = subs
        names = ", ".join(str(i) for i in sorted(subs)) or "никого"
        STATE.event("resync", f"состав раций восстановлен из журнала: {names}", "сервис")
        await broadcast()


async def journal_reader():
    unit = STATION_UNIT.removesuffix(".service")
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "journalctl", "-u", unit, "-f", "-n", "0", "-o", "cat",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            async for raw in proc.stdout:
                if parse_journal_line(raw.decode("utf-8", "replace")):
                    await broadcast()
        except Exception as e:
            STATE.event("error", f"чтение журнала: {e}", "сервис")
        await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Браузер: аутентификация и обработка
# ---------------------------------------------------------------------------

def check_basic(headers):
    header = headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        return False
    # Сравнение постоянного времени: пароль короткий, утечка по таймингу реальна
    return (hmac.compare_digest(user, DASH_USER)
            and hmac.compare_digest(password, DASH_PASSWORD))


def check_cookie(headers):
    for raw in headers.get_all("Cookie"):
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "dash" and hmac.compare_digest(value, SESSION_TOKEN):
                return True
    return False


def authorized(headers):
    if not AUTH_REQUIRED:
        return True
    return check_basic(headers) or check_cookie(headers)


def unauthorized():
    body = "Требуется вход\n".encode("utf-8")
    headers = Headers()
    headers["WWW-Authenticate"] = 'Basic realm="tetra-dash"'
    headers["Content-Type"] = "text/plain; charset=utf-8"
    headers["Content-Length"] = str(len(body))
    return Response(401, "Unauthorized", headers, body)


async def browser_handler(ws):
    STATE.browsers.add(ws)
    try:
        await ws.send(json.dumps(snapshot(), ensure_ascii=False))
        async for message in ws:
            try:
                req = json.loads(message)
            except Exception:
                continue

            cmd = req.get("cmd")
            if cmd == "send_sds":
                result = await send_sds(int(req["dest"]), req["text"],
                                        bool(req.get("is_group", False)))
                await ws.send(json.dumps({"type": "sds_result", **result},
                                         ensure_ascii=False))
            elif cmd == "reset_callsigns":
                result = await reset_callsigns()
                await ws.send(json.dumps({"type": "callsign_result", **result},
                                         ensure_ascii=False))
            elif cmd == "station":
                result = await station_control(req.get("action"))
                await ws.send(json.dumps({"type": "station_result", **result},
                                         ensure_ascii=False))
            await broadcast()
    finally:
        STATE.browsers.discard(ws)


def http_page(connection, request):
    """Аутентификация проверяется здесь для всех запросов, включая рукопожатие
    WebSocket, — так проверка ровно одна и обойти её нечем."""
    if not authorized(request.headers):
        return unauthorized()

    if request.path == "/ws":
        return None  # пропускаем в обработчик WebSocket

    try:
        with open(HTML_PATH, encoding="utf-8") as f:
            body = f.read().encode("utf-8")
    except OSError as e:
        body = f"dash.html не читается: {e}".encode("utf-8")
        headers = Headers()
        headers["Content-Type"] = "text/plain; charset=utf-8"
        headers["Content-Length"] = str(len(body))
        return Response(500, "Internal Server Error", headers, body)

    headers = Headers()
    headers["Content-Type"] = "text/html; charset=utf-8"
    headers["Content-Length"] = str(len(body))
    if AUTH_REQUIRED:
        # JS не умеет добавлять заголовки к WebSocket, а печенье к своему же
        # origin браузер отправляет сам — этим и авторизуем /ws
        headers["Set-Cookie"] = (
            f"dash={SESSION_TOKEN}; Path=/; SameSite=Strict; HttpOnly")
    return Response(200, "OK", headers, body)


# ---------------------------------------------------------------------------

async def main():
    if AUTH_REQUIRED:
        auth_note = f"вход {DASH_USER}"
    elif DASH_BIND in ("127.0.0.1", "localhost", "::1"):
        auth_note = "без пароля, только петля"
    else:
        auth_note = "ВНИМАНИЕ: без пароля и слушает сеть"

    async with (
        serve(telemetry_handler, "127.0.0.1", TELEMETRY_PORT,
              subprotocols=[TELEMETRY_PROTOCOL]),
        serve(control_handler, "127.0.0.1", CONTROL_PORT,
              subprotocols=[CONTROL_PROTOCOL]),
        serve(browser_handler, DASH_BIND, HTTP_PORT, process_request=http_page),
    ):
        print(f"tetra-dash: телеметрия :{TELEMETRY_PORT}, управление :{CONTROL_PORT}, "
              f"панель {DASH_BIND}:{HTTP_PORT} ({auth_note})", flush=True)
        load_temps()
        CALLSIGNS.update(load_callsigns())
        if CALLSIGNS:
            STATE.event("callsign", f"кэш позывных: {len(CALLSIGNS)} записей", "сервис")
        STATE.event("link", "сервис запущен", "сервис")
        await resync_subscribers()
        await asyncio.gather(journal_reader(), station_watcher(),
                             callsign_resolver(), temp_poller())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
