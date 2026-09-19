# Станция на Raspberry Pi / CM4 с SD-картой

Дополнение к [основной инструкции](../README.md). Проверено на Compute Module 4
(8 ГБ) на несущей плате Waveshare CM4-DUAL-ETH-MINI, Raspberry Pi OS на Debian 13,
Pluto+ подключён по USB.

Всё из основной инструкции остаётся в силе. Здесь — только отличия.

## Pluto по USB

Pluto поднимает сетевой интерфейс RNDIS: сам он `192.168.2.1`, машина получает
`192.168.2.10` по DHCP. В `/etc/tetra-lab.conf`:

```sh
PLUTO_HOST="192.168.2.1"
```

и в конфигурации станции:

```toml
device = "driver=plutosdr,uri=ip:192.168.2.1"
```

Модуль SoapySDR для Pluto в Debian не входит, и нужен форк с поддержкой меток
времени — того же коммита, что и прошивка Pluto:

```sh
git clone -b sdr_gadget_timestamping_with_iio_support https://github.com/pgreenland/SoapyPlutoSDR.git
cd SoapyPlutoSDR && git checkout c16932b
mkdir /tmp/build && cd /tmp/build
cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local ~/SoapyPlutoSDR
make -j4 && sudo make install
SoapySDRUtil --probe="driver=plutosdr,uri=ip:192.168.2.1"   # должен показать fw_version
```

## Сеть: профили NetworkManager

Два подвоха, оба проявляются только после перезагрузки.

**У второго порта нет своего MAC.** Realtek RTL8111 на несущей плате без EEPROM,
ядро назначает случайный адрес при каждой загрузке — и роутер выдаёт новый IP.
Адрес надо закрепить в профиле:

```sh
sudo nmcli con add type ethernet con-name lan-eth1 match.driver r8169 \
    ethernet.cloned-mac-address <любой локальный MAC> \
    ipv4.method auto ipv4.route-metric 100 connection.autoconnect-priority 10
```

**Штатный профиль netplan цепляется к любому порту.** В образе есть
`netplan-eth0` с пустым `match`, и при загрузке его первым забирает USB-интерфейс
Pluto. Править его через `nmcli con modify` бесполезно — после перезагрузки
netplan возвращает всё обратно. Надо удалить и завести свой:

```sh
sudo nmcli con add type ethernet con-name pluto-usb \
    match.driver "rndis_host cdc_ether cdc_ncm" \
    ipv4.method auto ipv4.never-default yes ipv6.never-default yes
sudo nmcli con delete netplan-eth0
```

Профили привязаны к драйверу, а не к имени `ethN`: имена зависят от порядка,
в котором ядро нашло устройства.

## Беречь SD-карту

SD-карту убивает не объём записи, а число принудительных сбросов. Штатная
система и наш чёрный ящик вместе делали их сотни тысяч в сутки.

| Источник | Мера | Файл |
|---|---|---|
| Своп сбрасывает страницы в `/var/swap` на карте | Только zram в памяти | `swap/90-zram-only.conf` → `/etc/rpi/swap.conf.d/` |
| ext4 сбрасывает журнал каждые 5 с | `commit=600` для корня в `/etc/fstab` | — |
| Ядро сбрасывает грязные страницы через 30 с | Через 10 минут | `sysctl/80-sd-wear.conf` → `/etc/sysctl.d/` |
| journald | На карте, но сброс раз в 10 минут, не больше 200 МБ | `journald/90-sd-wear.conf` → `/etc/systemd/journald.conf.d/` |
| Чёрный ящик: `sync` каждую секунду | Пишет в `/run`, на карту — при штатной остановке | `systemd/tetra-blackbox.service.d/`, `tetra-blackbox-ram.logrotate` |
| Сборка Rust — гигабайты мелких файлов | `CARGO_TARGET_DIR=/tmp/bs-target`, `/tmp` в памяти | — |
| Ежедневные таймеры apt и man-db | `systemctl disable --now apt-daily.timer apt-daily-upgrade.timer man-db.timer` | — |

Строка для `/etc/fstab`:

```
PARTUUID=...-02  /  ext4  defaults,noatime,commit=600  0  1
```

**Цена:** при внезапном отключении питания пропадают последние ~10 минут
журналов и истории температуры. Решение оператора «станция нужна» не пропадает —
панель после записи флага сама делает `sync`.

Когда станция переедет на SSD, файлы `sysctl/80-sd-wear.conf`,
`tetra-blackbox.service.d/10-sd-card.conf` и `commit=600` можно убрать.

## Сборка станции

Собирается прямо на CM4 с 8 ГБ, кросс-компиляция не нужна. Результаты сборки —
в памяти, на карту пишется только готовый файл:

```sh
export CARGO_TARGET_DIR=/tmp/bs-target
cargo build --release --locked
install -D /tmp/bs-target/release/bluestation-bs target/release/bluestation-bs
```

## Доступ панели к журналу

Панель восстанавливает список абонентов по журналу станции (`journalctl -u`).
На Ubuntu пользователь читает системный журнал благодаря ACL на
`/var/log/journal`, на Raspberry Pi OS такого нет — без группы панель после
перезапуска покажет пустую соту:

```sh
sudo usermod -aG systemd-journal claude
sudo systemctl restart tetra-dash
```

## Ядра

На виртуалке станция была заперта на ядре 0. На Pi прерывания USB и Ethernet
обрабатываются на ядре 0, и поток станции с приоритетом FIFO отнимал бы у них
время — а по USB идёт поток IQ от Pluto. Поэтому станции ядра 2 и 3:
`systemd/bluestation-bs.service.d/10-pi-cores.conf`.

## Вентилятор (Waveshare CM4-DUAL-ETH-MINI)

Микросхемы управления вентилятором на плате нет: PWM разведён на **GPIO19**,
датчик оборотов — на **GPIO17** (проверено замером; в сети встречается GPIO18 —
это неверно). Настройки лежат отдельным файлом `boot/tetra-fan.txt` — скопируйте
его в `/boot/firmware/` и подключите в `config.txt`:

```
include tetra-fan.txt
```

По умолчанию вентилятор **крутится всегда**: на холодной плате — минимальные
750 об/мин, с нагревом ускоряется ступенями до 7000. Штатный оверлей так не
умеет (нулевая ступень жёстко равна нулю), поэтому первый порог сдвинут к 0 °C.
Подробности и измеренные обороты — в комментариях самого файла.

**Грабли:** длинная строка `dtoverlay=…` со многими параметрами в `config.txt`
обрезается, и часть параметров молча теряется. Поэтому параметры задаются
отдельными строками `dtparam=` — они применяются к последнему оверлею.
Проверять надо по факту: `cat /sys/class/thermal/thermal_zone0/trip_point_*_temp`.
Пороги, кстати, доступны на запись — поправить их можно и без перезагрузки.

Кулер от Raspberry Pi 5 подходит механически, но **не по распиновке**:
у Pi 5 порядок 5V·PWM·GND·TACH, у Waveshare — PWM·FG·5V·GND. Контакты в штекере
надо переставить: напротив надписей PWM, FG, 5V, GND должны оказаться синий,
жёлтый, красный и чёрный провода.
