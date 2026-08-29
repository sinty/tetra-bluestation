#!/usr/bin/env python3
"""Проверочный клиент панели: ходит так же, как браузер, с HTTP Basic.
Без аргумента — печатает состояние. С аргументом start|stop — шлёт команду."""
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


async def main():
    async with connect(url, additional_headers={"Authorization": auth}) as ws:
        s = json.loads(await ws.recv())
        print("станция={} нужна={} занята={!r} Brew={} телеметрия={} управление={}".format(
            s["station"], s["station_wanted"], s["station_busy"],
            s["backhaul"], s["telemetry_link"], s["control_link"]))

        if len(sys.argv) > 1:
            await ws.send(json.dumps({"cmd": "station", "action": sys.argv[1]}))
            for _ in range(10):
                m = json.loads(await ws.recv())
                if m.get("type") == "station_result":
                    print("ответ:", m)
                    break


asyncio.run(main())
