#!/usr/bin/env python3
"""Проверочный клиент панели: ходит так же, как браузер, с HTTP Basic.

    dashcli.py                  состояние
    dashcli.py start|stop       команда станции
    dashcli.py reset-callsigns  сбросить кэш позывных
"""
import asyncio
import base64
import json
import shlex
import sys

from websockets.asyncio.client import connect

conf = {}
for line in open("/etc/tetra-lab.conf", encoding="utf-8"):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    parts = shlex.split(v, comments=True)
    conf[k.strip()] = parts[0] if parts else ""

cred = conf["DASH_USER"] + ":" + conf["DASH_PASSWORD"]
auth = "Basic " + base64.b64encode(cred.encode()).decode()
url = "ws://127.0.0.1:" + conf.get("DASH_HTTP_PORT", "8088") + "/ws"

arg = sys.argv[1] if len(sys.argv) > 1 else ""


async def main():
    async with connect(url, additional_headers={"Authorization": auth}) as ws:
        s = json.loads(await ws.recv())
        print("станция={} нужна={} занята={!r} Brew={} телеметрия={} управление={}".format(
            s["station"], s["station_wanted"], s["station_busy"],
            s["backhaul"], s["telemetry_link"], s["control_link"]))

        subs = s.get("subscribers") or []
        if subs:
            for r in subs:
                print("  рация {} {} группы {}".format(
                    r["issi"], r.get("call") or "(позывной не найден)", r["groups"]))
        else:
            print("  раций в соте нет")
        print("  позывных в кэше:", s.get("callsign_cache", 0))

        t = s.get("temp_now")
        if t:
            print("  Pluto: радиотракт {} °C, кристалл {} °C  ({} измерений)".format(
                t.get("rf"), t.get("soc"), len(s.get("temps") or [])))

        if arg in ("start", "stop"):
            await ws.send(json.dumps({"cmd": "station", "action": arg}))
            want = "station_result"
        elif arg == "reset-callsigns":
            await ws.send(json.dumps({"cmd": "reset_callsigns"}))
            want = "callsign_result"
        else:
            return

        for _ in range(10):
            m = json.loads(await ws.recv())
            if m.get("type") == want:
                print("ответ:", m)
                break


asyncio.run(main())
