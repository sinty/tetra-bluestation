#!/bin/sh
# Чёрный ящик: снимок состояния машины раз в секунду, со сбросом на диск.
#
# Зачем: виртуалка десять раз замирала намертво. Диагноз установлен
# 10.09.2026 по данным с хоста: процесс qemu при этом ЖИВ и жжёт целое ядро,
# хранилище и хост чисты, а гость не отвечает даже на консоль. Так выглядит
# жёсткая блокировка ядра гостя — цикл с запрещёнными прерываниями.
#
# Обнаружить её способен только NMI-сторож, а ему нужен аппаратный счётчик
# производительности, которого у гостя KVM нет:
#
#     kernel: NMI watchdog: Perf NMI watchdog permanently disabled
#
# Программные детекторы (soft lockup, hung task) такое не ловят по определению:
# они сами работают через прерывания, которые запрещены. Отсюда десять аварий
# без единой записи в журнале.
#
# Этот скрипт пишет строку каждую секунду и сразу сбрасывает её на диск,
# поэтому переживает смерть машины: последняя строка = последний момент,
# когда ядро ещё планировало задачи.
#
# Что и зачем пишем:
#
#   steal    время, отобранное гипервизором. Рост перед обрывом означает,
#            что задыхался ХОСТ, а не гость.
#   cpu0/1   загрузка по ядрам порознь. Станция заперта на нулевом; если
#            умрёт только оно, а первое останется живым — это видно только так.
#   psi_*    сколько времени задачи ждали ресурс. Поле full (в отличие от some)
#            означает, что ждали ВСЕ — то есть машина встала целиком.
#   dstate   потоки в непрерываемом ожидании. Их рост означает, что кто-то
#            завис на вводе-выводе и не может быть прерван даже сигналом.
#   irq      всего прерываний за интервал. Обвал к нулю означает, что ядро
#            перестало их получать, — верный признак остановки вовне.
#   netdrop  потери и ошибки на интерфейсе. Поток IQ от Pluto идёт по UDP,
#            и его заминки вызывают штормы пропусков передачи.
#
# Интервал секунда, а не пять: при пятисекундном шаге короткая раскрутка
# перед смертью просто не попадала в запись — все семь раз последний снимок
# показывал совершенно спокойную машину.

LOG=/var/log/tetra-blackbox.log
INTERVAL=1
UNIT_PROC=bluestation-bs
IFACE=$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -1)
[ -n "$IFACE" ] || IFACE=eth0

prev_total=0; prev_idle=0; prev_steal=0
prev_c0t=0; prev_c0i=0; prev_c1t=0; prev_c1i=0
prev_irq=0; prev_drop=0

cpu_line() {   # $1 — имя строки в /proc/stat
    awk -v k="$1" '$1==k {print $2+$3+$4+$5+$6+$7+$8+$9, $5}' /proc/stat
}

while :; do
    now=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

    set -- $(awk '/^cpu /{print $2,$3,$4,$5,$6,$7,$8,$9}' /proc/stat)
    total=$(($1+$2+$3+$4+$5+$6+$7+$8)); idle=$4; steal=$8
    d_total=$((total-prev_total)); d_idle=$((idle-prev_idle)); d_steal=$((steal-prev_steal))
    if [ "$d_total" -gt 0 ]; then
        busy=$(( (d_total-d_idle)*100/d_total )); steal_p=$(( d_steal*100/d_total ))
    else
        busy=-1; steal_p=-1
    fi
    prev_total=$total; prev_idle=$idle; prev_steal=$steal

    # по ядрам порознь
    set -- $(cpu_line cpu0); c0t=${1:-0}; c0i=${2:-0}
    d=$((c0t-prev_c0t)); di=$((c0i-prev_c0i))
    [ "$d" -gt 0 ] && cpu0=$(( (d-di)*100/d )) || cpu0=-1
    prev_c0t=$c0t; prev_c0i=$c0i
    set -- $(cpu_line cpu1); c1t=${1:-0}; c1i=${2:-0}
    d=$((c1t-prev_c1t)); di=$((c1i-prev_c1i))
    [ "$d" -gt 0 ] && cpu1=$(( (d-di)*100/d )) || cpu1=-1
    prev_c1t=$c1t; prev_c1i=$c1i

    load=$(cut -d' ' -f1 /proc/loadavg)
    mem_avail=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)

    psi_cpu=$(awk '/^some/{print $2}' /proc/pressure/cpu 2>/dev/null | cut -d= -f2)
    psi_io=$(awk '/^full/{print $2}' /proc/pressure/io 2>/dev/null | cut -d= -f2)
    psi_mem=$(awk '/^full/{print $2}' /proc/pressure/memory 2>/dev/null | cut -d= -f2)

    # потоки в непрерываемом ожидании — признак залипания на вводе-выводе
    dstate=$(awk '$3=="D"' /proc/*/stat 2>/dev/null | wc -l)

    # прерывания: обвал к нулю = ядро перестало их получать
    irq=$(awk '/^intr /{print $2}' /proc/stat)
    d_irq=$((irq-prev_irq)); prev_irq=$irq
    [ "$d_irq" -lt 0 ] && d_irq=0

    pid=$(pgrep -x "$UNIT_PROC" 2>/dev/null | head -1)
    if [ -n "$pid" ]; then
        bs_cpu=$(awk '{print $14+$15}' "/proc/$pid/stat" 2>/dev/null)
        bs_thr=$(ls "/proc/$pid/task" 2>/dev/null | wc -l)
    else
        bs_cpu=-1; bs_thr=0
    fi

    printf '%s busy=%s%% steal=%s%% cpu0=%s%% cpu1=%s%% load=%s mem=%sM psi_cpu=%s psi_io_full=%s psi_mem_full=%s dstate=%s irq=%s bs_cpu=%s bs_thr=%s\n' \
        "$now" "$busy" "$steal_p" "$cpu0" "$cpu1" "$load" "$mem_avail" \
        "${psi_cpu:--}" "${psi_io:--}" "${psi_mem:--}" \
        "$dstate" "$d_irq" "$bs_cpu" "$bs_thr" >> "$LOG"

    # Без сброса на диск строка останется в кэше и при зависании пропадёт —
    # то есть ровно те секунды, ради которых всё это и затевалось.
    sync "$LOG" 2>/dev/null

    # --- рискованная часть, ПОСЛЕ записи основной строки ---
    #
    # Порядок принципиален. Раньше /proc/net/dev читался ДО записи, и когда
    # 10.09.2026 выяснилось, что аварии происходят от жёсткой блокировки
    # ядра гостя, стало понятно: при заклиненном сетевом стеке это чтение
    # блокируется — и регистратор умирает вместе с событием, которое должен
    # был записать. Все прежние разборы показывали «идеально спокойную
    # последнюю секунду»; она была не спокойная, а просто последняя,
    # до которой скрипт успел дойти.
    drop=$(awk -v i="$IFACE:" '$1==i {print $4+$5+$12+$13}' /proc/net/dev 2>/dev/null)
    drop=${drop:-$prev_drop}
    d_drop=$((drop-prev_drop)); prev_drop=$drop
    [ "$d_drop" -lt 0 ] && d_drop=0
    printf '%s netdrop=%s
' "$now" "$d_drop" >> "$LOG"
    sync "$LOG" 2>/dev/null

    sleep "$INTERVAL"
done
