#!/bin/sh
# Чёрный ящик: снимок состояния машины раз в N секунд, со сбросом на диск.
#
# Зачем: виртуалка дважды замирала бесследно. Детектор жёстких зависаний
# в KVM недоступен (нет аппаратного счётчика), журнал обрывается на полуслове,
# и после ресета от последних секунд не остаётся ничего. Этот скрипт пишет
# строку каждые N секунд и сразу сбрасывает её на диск, поэтому переживает
# внезапную смерть машины: последняя строка = последний момент, когда ядро
# ещё работало.
#
# Ключевое поле — steal. Это время, которое у гостя отобрал гипервизор.
# Рост steal перед обрывом означает, что задыхался ХОСТ, а не гость;
# отсутствие роста при высокой нагрузке RT-потоков указывает внутрь.
#
# Формат строки: пары «ключ=значение», разделённые пробелом.

LOG=/var/log/tetra-blackbox.log
INTERVAL=5
UNIT_PROC=bluestation-bs

prev_total=0
prev_idle=0
prev_steal=0

while :; do
    now=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

    # --- процессор: доли за интервал, включая отобранное гипервизором ---
    set -- $(awk '/^cpu /{print $2,$3,$4,$5,$6,$7,$8,$9}' /proc/stat)
    user=$1; nice=$2; sys=$3; idle=$4; iowait=$5; irq=$6; softirq=$7; steal=$8
    total=$((user+nice+sys+idle+iowait+irq+softirq+steal))
    d_total=$((total - prev_total))
    d_idle=$((idle - prev_idle))
    d_steal=$((steal - prev_steal))
    if [ "$d_total" -gt 0 ]; then
        busy_pct=$(( (d_total - d_idle) * 100 / d_total ))
        steal_pct=$(( d_steal * 100 / d_total ))
    else
        busy_pct=-1; steal_pct=-1
    fi
    prev_total=$total; prev_idle=$idle; prev_steal=$steal

    # --- нагрузка и память ---
    load=$(cut -d' ' -f1-3 /proc/loadavg | tr ' ' ',')
    mem_avail=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
    swap_free=$(awk '/^SwapFree:/{print int($2/1024)}' /proc/meminfo)

    # --- давление ресурсов: сколько времени задачи ждали (PSI) ---
    psi_cpu=$(awk '/^some/{print $2}' /proc/pressure/cpu 2>/dev/null | cut -d= -f2)
    psi_io=$(awk '/^some/{print $2}' /proc/pressure/io 2>/dev/null | cut -d= -f2)
    psi_mem=$(awk '/^some/{print $2}' /proc/pressure/memory 2>/dev/null | cut -d= -f2)

    # --- станция: сколько процессорного времени съели её потоки ---
    pid=$(pgrep -x "$UNIT_PROC" 2>/dev/null | head -1)
    if [ -n "$pid" ]; then
        st_jiffies=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null)
        st_threads=$(ls "/proc/$pid/task" 2>/dev/null | wc -l)
        st_rt=$(for t in /proc/"$pid"/task/*/stat; do
                    awk '{ if ($41 == 1 || $41 == 2) print $1 }' "$t" 2>/dev/null
                done | wc -l)
    else
        st_jiffies=-1; st_threads=0; st_rt=0
    fi

    printf '%s busy=%s%% steal=%s%% load=%s mem_avail=%sM swap_free=%sM psi_cpu=%s psi_io=%s psi_mem=%s bs_cpu=%s bs_threads=%s bs_rt=%s\n' \
        "$now" "$busy_pct" "$steal_pct" "$load" "$mem_avail" "$swap_free" \
        "${psi_cpu:--}" "${psi_io:--}" "${psi_mem:--}" \
        "$st_jiffies" "$st_threads" "$st_rt" >> "$LOG"

    # Без сброса на диск строка останется в кэше и при зависании пропадёт —
    # то есть ровно те секунды, ради которых всё это и затевалось.
    sync "$LOG" 2>/dev/null

    sleep "$INTERVAL"
done
