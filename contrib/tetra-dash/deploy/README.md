# Развёртывание

Всё поднимается на управляющей машине рядом со станцией. Порядок важен:
конфигурация нужна скриптам, правило polkit — панели, юниты — всему остальному.

## 1. Конфигурация

```sh
sudo install -m 0640 -o root -g <пользователь> tetra-lab.conf.example /etc/tetra-lab.conf
sudo nano /etc/tetra-lab.conf
```

Права `0640` не случайны: в файле пароли Pluto и панели. Владелец root,
группа — пользователь, под которым работают станция и панель.

Пароль панели удобно сгенерировать так:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(12))"
```

## 2. Буферы UDP

```sh
sudo install -m 0644 sysctl/90-tetra-sdr.conf /etc/sysctl.d/
sudo sysctl --system
```

Штатных 212992 байт не хватает: поток IQ от Pluto начинает терять отсчёты,
потери накапливаются, и PHY через 10–15 минут паникует на `RxReadError`.
Настройки на самом Pluto скрипт `pluto-prepare.sh` поднимает при каждом старте —
там корневая ФС собирается из образа заново при каждой загрузке, поэтому
запомнить их на устройстве нельзя.

## 3. Скрипты

```sh
sudo install -m 0750 -o <пользователь> -g <пользователь> sbin/*.sh /usr/local/sbin/
```

Владелец — пользователь службы, а не root: скрипты запускаются из юнита
станции и должны быть ему доступны, но пароли внутри не должны читаться всеми.

## 4. Права панели

```sh
sudo install -m 0644 polkit/50-tetra-dash.rules /etc/polkit-1/rules.d/
sudo systemctl restart polkit
```

Правило разрешает панели пуск и останов **ровно одного** юнита — ни чужие
юниты, ни `enable`/`disable` не проходят. Имя пользователя в правиле
прописано явно, поменяйте под своё.

Почему не sudo: если у пользователя службы есть общий доступ через `NOPASSWD`,
то снятие `NoNewPrivileges` с юнита панели отдаёт ей root целиком. polkit
работает через D-Bus и закалку снимать не требует.

## 5. Флаг состояния

```sh
sudo mkdir -p /var/lib/tetra-lab
sudo chown <пользователь>:<пользователь> /var/lib/tetra-lab
sudo touch /var/lib/tetra-lab/station-wanted
sudo chown <пользователь>:<пользователь> /var/lib/tetra-lab/station-wanted
```

Наличие файла — и есть «станция нужна». Юнит проверяет его через
`ConditionPathExists`, поэтому решение оператора переживает перезагрузку,
а панели не нужны права на `enable`/`disable`.

## 6. Панель

```sh
python3 -m venv ~/tetra-dash-venv          # нужен пакет python3-venv
~/tetra-dash-venv/bin/pip install websockets
mkdir -p ~/tetra-dash && cp ../dash/*.py ../dash/*.html ~/tetra-dash/
```

## 7. Юниты

Пути внутри `systemd/*.service` рассчитаны на пользователя `claude`
и домашний каталог `/home/claude` — поправьте под себя.

```sh
sudo install -m 0644 systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tetra-dash
sudo systemctl enable bluestation-bs
```

Станцию отдельным `start` поднимать не нужно — она поднимется по флагу.

## Проверка

```sh
systemctl is-active bluestation-bs tetra-dash
python3 ~/tetra-dash/dashcli.py            # состояние глазами панели
journalctl -t tetra-notify -b -o cat       # что ушло в уведомления
```

Панель откроется на `http://<адрес>:8088`.

**Проверять надо перезагрузкой, причём дважды** — в запущенном состоянии
и в остановленном. Иначе не видно, что решение оператора действительно
переживает выключение питания.

## Безопасность

Панель умеет включать и выключать передатчик. При `DASH_BIND="0.0.0.0"`
пароль обязателен, иначе управление передатчиком доступно всей локальной сети.
Значение по умолчанию — `127.0.0.1`, то есть только с самой машины.

Отдельно стоит помнить: `pluto-prepare.sh` ходит на Pluto с отключённой
проверкой ключа хоста. Это осознанно — Pluto генерирует новый ключ при каждой
загрузке, закрепить его нечем, а связь прямая. Для соединения через
недоверенную сеть так делать нельзя.
