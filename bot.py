"""
Марк — Telegram-бот, справочник для персонала отеля «Тополя».

Этап 1 (каркас): бот принимает сообщения, различает личку и группы,
в группах отвечает только по упоминанию (@bot_username).
Подключение к Notion/Drive и Claude будет добавлено на следующих этапах.
"""

import logging
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "topolya_mark_bot")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("mark_bot")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Приветственное сообщение при команде /start."""
    text = (
        "Привет! Я Марк — ИИ-помощник отеля «Тополя».\n\n"
        "Я отвечаю на вопросы по правилам, тарифам и инструкциям отеля "
        "на основе документов из базы «Тополи».\n\n"
        "Важно: я искусственный интеллект, а не сотрудник. "
        "В сложных и спорных случаях уточняйте у администратора.\n\n"
        "Все вопросы к боту логируются (для улучшения базы знаний).\n\n"
        "Чтобы узнать, по каким темам можно спрашивать, напишите /help"
    )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Список тем, по которым можно спрашивать — заполним на этапе 2."""
    text = (
        "Я могу отвечать на вопросы по темам:\n\n"
        "• Правила проживания для гостей\n"
        "• Тарифы и доплаты\n"
        "• Wi-Fi\n"
        "• Парковка\n"
        "• Что делать при коммунальной аварии\n"
        "• Заселение и документы гостей\n"
        "• Чек-листы администратора и горничной\n"
        "• Правила для персонала\n\n"
        "(Список уточняется — база знаний ещё подключается)"
    )
    await update.message.reply_text(text)


def is_addressed_to_bot(update: Update) -> bool:
    """
    В личных сообщениях бот отвечает всегда.
    В группах — только если его явно упомянули (@bot_username).
    """
    chat_type = update.effective_chat.type
    if chat_type == "private":
        return True

    message_text = update.message.text or ""
    return f"@{BOT_USERNAME}" in message_text


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработка обычных текстовых сообщений.
    Пока — заглушка. На этапе 2-3 здесь появится:
    1. Проверка доступа (сотрудник ли отправитель, база «Марк — Сотрудники»).
    2. Определение темы вопроса.
    3. Чтение нужного документа из Notion/Drive.
    4. Запрос к Claude через Nodul.
    5. Запись в журнал Notion.
    """
    if not is_addressed_to_bot(update):
        return  # в группе бот молчит, если его не позвали

    user = update.effective_user
    question = update.message.text

    logger.info("Вопрос от %s (%s): %s", user.full_name, user.id, question)

    # Заглушка на этапе 1 — реальный ответ подключим на этапе 3
    await update.message.reply_text(
        "Пока я в разработке и ещё не подключён к базе знаний отеля. "
        "Скоро смогу отвечать на вопросы по правилам, тарифам и инструкциям."
    )


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "Не найден TELEGRAM_BOT_TOKEN. Проверьте файл .env "
            "(скопируйте .env.example в .env и заполните значения)."
        )

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Марк запущен и слушает сообщения...")
    application.run_polling()


if __name__ == "__main__":
    main()
