#!/bin/sh
# Наблюдение за виртуалкой станции СО СТОРОНЫ ХОСТА (Proxmox).
#
# Зачем. Гость восемь раз умирал мгновенно и бесследно: чёрный ящик внутри
# показывает совершенно спокойную машину вплоть до последней секунды — полный
# поток прерываний, нулевой steal, простаивающие ядра. Так выглядит только
# одно: гостя перестают исполнять СНАРУЖИ. Изнутри это принципиально
# незаписываемо, потому что записывать некому.
#
# Этот скрипт пишет на хосте, поэтому переживает смерть гостя и отвечает
# на вопрос, который изнутри неразрешим:
#
#   stat=R или S  процесс qemu жив и работает → гость крутится сам
#   stat=D        qemu заблокирован на вводе-выводе → ЗАЛИПЛО ХРАНИЛИЩЕ
#   процесса нет  qemu убит → искать, кто убил (OOM хоста?)
#
# Плюс состояние хоста в тот же момент: нагрузка, память, ожидание ввода-вывода
# и давление PSI. Если массив встаёт, это видно по iowait и io_full.
#
# Установка на хост:
#   install -m 0755 tetrabs-hostwatch.sh /usr/local/sbin/
#   install -m 0644 tetrabs-hostwatch.service /etc/systemd/system/
#   systemctl daemon-reload && systemctl enable --now tetrabs-hostwatch
#
# Разбор после аварии: последняя строка перед перерывом во времени.

VMID=${VMID:-121}
LOG=${LOG:-/var/log/tetrabs-hostwatch.log}
INTERVAL=${INTERVAL:-1}
PIDFILE=/var/run/qemu-server/$VMID.pid

prev_total=0; prev_idle=0; prev_iow=0

while :; do
    now=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

    # --- процессор хоста, включая ожидание ввода-вывода ---
    set -- $(awk '/^cpu /{print $2,$3,$4,$5,$6,$7,$8,$9}' /proc/stat)
    total=$(($1+$2+$3+$4+$5+$6+$7+$8)); idle=$4; iow=$5
    dt=$((total-prev_total)); di=$((idle-prev_idle)); dw=$((iow-prev_iow))
    if [ "$dt" -gt 0 ]; then
        h_busy=$(( (dt-di)*100/dt )); h_iowait=$(( dw*100/dt ))
    else
        h_busy=-1; h_iowait=-1
    fi
    prev_total=$total; prev_idle=$idle; prev_iow=$iow

    h_load=$(cut -d' ' -f1 /proc/loadavg)
    h_mem=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
    h_swap=$(awk '/^SwapFree:/{print int($2/1024)}' /proc/meminfo)
    h_psi_io=$(awk '/^full/{print $2}' /proc/pressure/io 2>/dev/null | cut -d= -f2)
    h_psi_cpu=$(awk '/^some/{print $2}' /proc/pressure/cpu 2>/dev/null | cut -d= -f2)

    # --- сколько процессов хоста залипло на вводе-выводе ---
    h_dstate=$(awk '$3=="D"' /proc/*/stat 2>/dev/null | wc -l)

    # --- наш qemu ---
    pid=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
        # 3-е поле /proc/pid/stat — состояние, 14+15 — процессорное время
        set -- $(awk '{print $3, $14+$15}' "/proc/$pid/stat" 2>/dev/null)
        q_stat=${1:-?}; q_cpu=${2:--1}
        q_thr=$(ls "/proc/$pid/task" 2>/dev/null | wc -l)
        # сколько потоков qemu сами залипли на вводе-выводе
        q_d=$(awk '$3=="D"' /proc/"$pid"/task/*/stat 2>/dev/null | wc -l)
    else
        q_stat=НЕТ; q_cpu=-1; q_thr=0; q_d=0
    fi

    printf '%s host_busy=%s%% host_iowait=%s%% host_load=%s host_mem=%sM host_swap=%sM host_psi_io_full=%s host_psi_cpu=%s host_dstate=%s qemu_stat=%s qemu_cpu=%s qemu_thr=%s qemu_dstate=%s\n' \
        "$now" "$h_busy" "$h_iowait" "$h_load" "$h_mem" "$h_swap" \
        "${h_psi_io:--}" "${h_psi_cpu:--}" "$h_dstate" \
        "$q_stat" "$q_cpu" "$q_thr" "$q_d" >> "$LOG"

    sync "$LOG" 2>/dev/null
    sleep "$INTERVAL"
done
