#!/bin/sh
# Приводит Pluto+ в рабочее состояние перед запуском базовой станции.
#
# Зачем: корневая ФС Pluto живёт в памяти и собирается из образа прошивки
# заново при каждой загрузке. Поэтому сбрасываются буферы UDP, MAC и ключ
# хоста SSH. Всё, что во флеше (xo_correction, статический адрес), переживает
# перезагрузку и здесь только проверяется.
#
# Идемпотентен: если буферы уже подняты, гаджет не трогается.
# Запускается из ExecStartPre юнита базовой станции.
# Адреса и пароли — в /etc/tetra-lab.conf.

set -u

CONF=/etc/tetra-lab.conf
if [ ! -r "$CONF" ]; then
    echo "pluto-prepare: нет $CONF — скопируйте tetra-lab.conf.example и заполните" >&2
    exit 1
fi
. "$CONF"

# Адрес и пароль намеренно без значений по умолчанию: чужая лаборатория
# не должна унаследовать чьи-то настройки из исходников.
PLUTO="${PLUTO_HOST:?не задан PLUTO_HOST в /etc/tetra-lab.conf}"
PLUTO_PW="${PLUTO_PASSWORD:?не задан PLUTO_PASSWORD в /etc/tetra-lab.conf}"
XO_EXPECTED="${PLUTO_XO_EXPECTED:-}"

WANT_BUF=33554432
DEADLINE=$(( $(date +%s) + 120 ))
NOTIFY=/usr/local/sbin/tetra-notify.sh

log() { echo "pluto-prepare: $*"; }

# Ключ хоста у Pluto новый после каждой загрузки, закрепить его нельзя.
# Связь прямая, точка-точка, в лаборатории — проверку ключа снимаем осознанно.
PSH="sshpass -p $PLUTO_PW ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
     -o LogLevel=ERROR -o ConnectTimeout=5 root@$PLUTO"

"$NOTIFY" "запускаю станцию, готовлю Pluto $PLUTO"

# 1. Дождаться, пока Pluto поднимется и ответит по SSH
until $PSH true 2>/dev/null; do
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
        log "Pluto $PLUTO не отвечает по SSH за 120 с — выходим с ошибкой"
        "$NOTIFY" --with-url "ОШИБКА: Pluto $PLUTO не отвечает по SSH 120 с, старт отменён — проверить питание и кабель"
        exit 1
    fi
    log "жду Pluto $PLUTO..."
    sleep 5
done
log "Pluto доступен"

# 2. Прошивка должна быть с аппаратными метками времени
FW=$(iio_attr -u "ip:$PLUTO" -C 2>/dev/null | sed -n 's/^fw_version: //p')
case "$FW" in
    *timestamping*) log "прошивка: $FW" ;;
    "")             log "ВНИМАНИЕ: версию прошивки прочитать не удалось"
                    "$NOTIFY" "ПРЕДУПРЕЖДЕНИЕ: версию прошивки Pluto прочитать не удалось, продолжаю" ;;
    *)              log "ОШИБКА: прошивка '$FW' без таймстемпинга — станция работать не будет"
                    "$NOTIFY" --with-url "ОШИБКА: на Pluto прошивка '$FW' без таймстемпинга, старт отменён"
                    exit 1 ;;
esac

# 3. Буферы UDP на стороне Pluto + перезапуск гаджета, чтобы подхватил SO_RCVBUF
CUR=$($PSH 'cat /proc/sys/net/core/rmem_max' 2>/dev/null)
if [ "${CUR:-0}" -lt "$WANT_BUF" ]; then
    log "буферы Pluto: $CUR -> $WANT_BUF, перезапускаю sdr_ip_gadget"
    $PSH "echo $WANT_BUF > /proc/sys/net/core/rmem_max;
          echo $WANT_BUF > /proc/sys/net/core/rmem_default;
          echo $WANT_BUF > /proc/sys/net/core/wmem_max;
          echo $WANT_BUF > /proc/sys/net/core/wmem_default;
          echo 5000      > /proc/sys/net/core/netdev_max_backlog;
          /etc/init.d/S55sdr_ip_gadget restart" >/dev/null 2>&1
    sleep 3
else
    log "буферы Pluto уже подняты ($CUR)"
fi

# 4. Ловушки из раздела 4 журнала: цифровая петля и калибровка опоры
LB=$(iio_attr -u "ip:$PLUTO" -D ad9361-phy loopback 2>/dev/null | tail -1)
if [ "$LB" != "0" ]; then
    log "чип заперт в петле (loopback=$LB) — снимаю, иначе в эфир не выйдет ничего"
    iio_attr -u "ip:$PLUTO" -D ad9361-phy loopback 0 >/dev/null 2>&1
    "$NOTIFY" "ПРЕДУПРЕЖДЕНИЕ: Pluto был заперт в цифровой петле, снял"
fi

if [ -n "$XO_EXPECTED" ]; then
    XO=$(iio_attr -u "ip:$PLUTO" -d ad9361-phy xo_correction 2>/dev/null | tail -1)
    if [ "$XO" != "$XO_EXPECTED" ]; then
        log "ВНИМАНИЕ: xo_correction=$XO, ожидалось $XO_EXPECTED"
        "$NOTIFY" "ПРЕДУПРЕЖДЕНИЕ: xo_correction=$XO вместо $XO_EXPECTED, уход опоры собьёт синхронизацию раций"
    fi
fi

log "Pluto готов"
exit 0
