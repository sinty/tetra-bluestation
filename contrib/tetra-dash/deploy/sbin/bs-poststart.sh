#!/bin/sh
# ExecStartPost: дожидается, пока станция реально подключится к сети,
# и только тогда докладывает об удачном старте.
#
# Признак берём не из лога, а из состояния сокета: у процесса должно появиться
# установленное соединение на порт 443. Лог мог бы соврать о прошлом запуске.

CONF=/etc/tetra-lab.conf
[ -r "$CONF" ] && . "$CONF"

NOTIFY=/usr/local/sbin/tetra-notify.sh
DEADLINE=$(( $(date +%s) + 60 ))
FREQS="DL ${DL_FREQ_MHZ:-?} / UL ${UL_FREQ_MHZ:-?} МГц"

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    if ! pgrep -x bluestation-bs >/dev/null 2>&1; then
        "$NOTIFY" --with-url "ОШИБКА: станция не удержалась при старте, смотреть journalctl -u bluestation-bs -b"
        exit 0
    fi
    if ss -tnp 2>/dev/null | grep bluestation-bs | grep -q ':443'; then
        "$NOTIFY" --with-url "старт успешен, $FREQS, магистраль подключена"
        exit 0
    fi
    sleep 3
done

"$NOTIFY" --with-url "ПРЕДУПРЕЖДЕНИЕ: станция в эфире, но магистраль не поднялась за 60 с — проверить пароль и связь с ядром сети"
exit 0
