#!/bin/bash
# Автообновление бота «Марк»: git pull -> проверки -> перезапуск; при сбое откат и тревога владельцу.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
exec 9>/tmp/mark-update.lock
flock -n 9 || exit 0

alert() {  # текст -> Telegram владельцу
  set -a; . ./.env; set +a
  [ -n "$OWNER_TELEGRAM_ID" ] && curl -s -m 20 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${OWNER_TELEGRAM_ID}" --data-urlencode "text=$1" >/dev/null
}

git fetch -q origin main || { echo "fetch не удался"; exit 1; }
OLD=$(git rev-parse HEAD)
NEW=$(git rev-parse origin/main)
[ "$OLD" = "$NEW" ] && exit 0

rollback() {
  git reset -q --hard "$OLD"
  venv/bin/pip install -q -r requirements.txt >/dev/null 2>&1
  systemctl restart mark-bot
  alert "⚠️ Марк: обновление ${NEW:0:7} не прошло проверку ($1), возвращена версия ${OLD:0:7}. Бот работает по старому коду."
  echo "откат: $1"; exit 1
}

git reset -q --hard "$NEW"
venv/bin/pip install -q -r requirements.txt || rollback "pip install"
venv/bin/python -m py_compile *.py || rollback "синтаксис"
venv/bin/python selftest.py >/tmp/mark-selftest.log 2>&1 || rollback "selftest: $(grep -m1 FAIL /tmp/mark-selftest.log | cut -c1-150)"
systemctl restart mark-bot
sleep 6
systemctl is-active --quiet mark-bot || rollback "сервис не запустился"
alert "✅ Марк обновлён: ${OLD:0:7} → ${NEW:0:7}. $(git log -1 --pretty=%s)"
echo "обновлено до $NEW"
