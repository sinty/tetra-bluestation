#!/bin/sh
# ExecStopPost: systemd передаёт сюда SERVICE_RESULT и EXIT_STATUS.
# Отличаем штатную остановку от падения (PHY паникует на RxReadError).

NOTIFY=/usr/local/sbin/tetra-notify.sh

case "${SERVICE_RESULT:-unknown}" in
    success)
        "$NOTIFY" "станция остановлена штатно"
        ;;
    *)
        "$NOTIFY" "ОШИБКА: станция упала (${SERVICE_RESULT:-?}, код ${EXIT_STATUS:-?}), systemd перезапускает через 5 с"
        ;;
esac
exit 0
