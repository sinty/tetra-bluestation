#!/bin/sh
# Уведомление о состоянии через настраиваемый URL.
#
#   tetra-notify.sh "текст"              — просто отправить
#   tetra-notify.sh --with-url "текст"   — дописать адрес веб-панели
#
# Куда слать — задаётся в /etc/tetra-lab.conf переменной NOTIFY_URL, в которой
# {message} заменяется текстом. Шлюзом может быть что угодно, принимающее GET.
# Пустой NOTIFY_URL полностью выключает отправку.
#
# Два правила:
#  1. Никогда не возвращает ошибку. Недоступный шлюз не должен мешать станции
#     стартовать — уведомление это диагностика, а не часть тракта.
#  2. Антидребезг. При цикле перезапусков одинаковый текст уходит не чаще
#     раза в 5 минут, иначе падающая станция зафлудит чат.

CONF=/etc/tetra-lab.conf
[ -r "$CONF" ] && . "$CONF"

NOTIFY_URL="${NOTIFY_URL:-}"
NOTIFY_PREFIX="${NOTIFY_PREFIX:-}"
THROTTLE=300
STATE=/tmp/tetra-notify.state

# Адрес панели для сообщений о запуске: получив «станция не поднялась»,
# оператор должен сразу знать, куда идти смотреть и жать «Пуск».
dash_url() {
    if [ -n "${DASH_URL:-}" ]; then
        printf '%s' "$DASH_URL"
        return
    fi
    _port="${DASH_HTTP_PORT:-8088}"
    case "${DASH_BIND:-127.0.0.1}" in
        0.0.0.0|::|"")
            # Слушаем на всех интерфейсах — подставляем адрес того,
            # через который машина ходит наружу.
            _ip=$(ip route get 1.1.1.1 2>/dev/null \
                  | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)
            [ -n "$_ip" ] || _ip=127.0.0.1
            ;;
        *)  _ip="$DASH_BIND" ;;
    esac
    printf 'http://%s:%s' "$_ip" "$_port"
}

WITH_URL=0
if [ "${1:-}" = "--with-url" ]; then
    WITH_URL=1
    shift
fi

[ $# -gt 0 ] || exit 0
MSG="$NOTIFY_PREFIX$*"

if [ "$WITH_URL" = 1 ]; then
    URL_TEXT=$(dash_url)
    [ -n "$URL_TEXT" ] && MSG="$MSG · панель $URL_TEXT"
fi

# История отправленного видна всегда, даже когда шлюз не настроен
logger -t tetra-notify -- "$MSG" 2>/dev/null
[ -n "$NOTIFY_URL" ] || exit 0

KEY=$(printf '%s' "$MSG" | md5sum | cut -d' ' -f1)
NOW=$(date +%s)

if [ -f "$STATE" ]; then
    LAST=$(awk -v k="$KEY" '$1==k {print $2}' "$STATE" 2>/dev/null | tail -1)
    if [ -n "$LAST" ] && [ $((NOW - LAST)) -lt "$THROTTLE" ]; then
        exit 0
    fi
    awk -v k="$KEY" '$1!=k' "$STATE" > "$STATE.tmp" 2>/dev/null && mv "$STATE.tmp" "$STATE"
fi
printf '%s %s\n' "$KEY" "$NOW" >> "$STATE" 2>/dev/null

# Кодируем каждый байт вручную. curl --data-urlencode шлюз понимает неверно —
# кириллица приходит мусором в CP1251, проверено на обеих машинах.
# Побайтовый UTF-8 доходит читаемо.
ENC=$(printf '%s' "$MSG" | od -An -tx1 -v | tr -d ' \n' | sed 's/../%&/g')
URL=$(printf '%s' "$NOTIFY_URL" | sed "s|{message}|$ENC|")

curl -s -m 10 -o /dev/null "$URL" >/dev/null 2>&1
exit 0
