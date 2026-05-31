import base64
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAIError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN. Добавьте токен Telegram-бота в переменные окружения.")
if not OPENAI_API_KEY:
    raise RuntimeError("Не задан OPENAI_API_KEY. Добавьте ключ OpenAI API в переменные окружения.")
if not ADMIN_ID_RAW:
    raise RuntimeError("Не задан ADMIN_ID. Добавьте Telegram user ID администратора в переменные окружения.")

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ADMIN_ID должен быть числом, например 123456789.") from exc

POST_PHOTO, REELS_INPUT = range(2)

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

STYLE_LABELS = {
    "family": "👨‍👧 семейный",
    "chef": "🧑‍🍳 шефский",
    "ai": "🤖 AI",
    "simple_recipe": "🍳 простой рецепт",
    "motivational": "🔥 мотивационный",
    "channel_growth": "📈 рост канала",
}

STYLE_DESCRIPTIONS = {
    "family": "тепло, про дом, дочку, семейный стол и живые бытовые детали",
    "chef": "уверенно и шефски: техника, вкус, подача, маленькие профессиональные лайфхаки",
    "ai": "с акцентом на AI как помощника на кухне, без техно-фанатизма",
    "simple_recipe": "максимально понятно, коротко, практично и без сложных терминов",
    "motivational": "энергично, вдохновляюще, с акцентом на регулярность и удовольствие от готовки",
    "channel_growth": "цепляюще, с сильным CTA, вовлечением и идеей для обсуждения",
}

VARIATION_PROMPTS = {
    "rewrite": "перепиши текст заметно свежее, сохрани факты и структуру",
    "family": "сделай текст более семейным: отец, дочка, домашняя кухня, тепло",
    "chef": "сделай текст более шефским: вкус, техника, подача, лайфхаки",
    "ai": "добавь больше AI-угла: как AI помогает в идее, балансе и описании блюда",
    "healthy": "добавь больше пользы без медицинских обещаний и без ПП-сектантства",
    "shorter": "сделай текст короче, плотнее и удобнее для Telegram",
    "emotional": "сделай текст эмоциональнее, живее и чуть смешнее",
}


@dataclass
class Draft:
    admin_id: int
    photo_file_id: Optional[str]
    draft_text: str
    style: str
    created_at: datetime


latest_draft: Optional[Draft] = None
current_style = "family"


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ADMIN_ID)


async def guard_admin(update: Update) -> bool:
    if is_admin(update):
        return True

    message = update.effective_message
    if message:
        await message.reply_text("Бот доступен только администратору.")
    elif update.callback_query:
        await update.callback_query.answer("Бот доступен только администратору.", show_alert=True)
    return False


def draft_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Опубликовать", callback_data="draft:publish")],
            [InlineKeyboardButton("✏️ Переписать", callback_data="draft:rewrite")],
            [InlineKeyboardButton("👨‍👧 Больше семейно", callback_data="draft:family")],
            [InlineKeyboardButton("🧑‍🍳 Больше шефски", callback_data="draft:chef")],
            [InlineKeyboardButton("🤖 Больше AI", callback_data="draft:ai")],
            [InlineKeyboardButton("🥗 Больше пользы", callback_data="draft:healthy")],
            [InlineKeyboardButton("✂️ Короче", callback_data="draft:shorter")],
            [InlineKeyboardButton("❤️ Эмоциональнее", callback_data="draft:emotional")],
            [InlineKeyboardButton("❌ Отмена", callback_data="draft:cancel")],
        ]
    )


def style_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, label in STYLE_LABELS.items():
        marker = "✅ " if key == current_style else ""
        rows.append([InlineKeyboardButton(f"{marker}{label}", callback_data=f"style:{key}")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_admin(update):
        return
    await update.message.reply_text(
        "Привет! Я внутренний AI-контент-бот для канала о еде, семье, готовке и AI.\n\n"
        "Главный сценарий: /post → отправь фото блюда → я подготовлю пост → ты подтверждаешь публикацию.\n"
        "Для подсказок используй /help."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_admin(update):
        return
    await update.message.reply_text(
        "Команды:\n"
        "/post — создать Telegram-пост по фото блюда\n"
        "/reels — сценарий Reels по фото или описанию\n"
        "/plan — контент-план на 7 дней\n"
        "/style — выбрать стиль генерации\n"
        "/publish — опубликовать последний черновик\n"
        "/cancel — отменить текущий сценарий\n\n"
        "Бот отвечает только администратору из ADMIN_ID."
    )


async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard_admin(update):
        return ConversationHandler.END
    await update.message.reply_text("Отправь фото блюда — разберу, что на тарелке, и соберу готовый пост для Telegram.")
    return POST_PHOTO


async def reels_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard_admin(update):
        return ConversationHandler.END
    await update.message.reply_text("Отправь фото блюда или текстовое описание — подготовлю hook, сценарий Reels, озвучку, caption и stories.")
    return REELS_INPUT


async def style_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_admin(update):
        return
    await update.message.reply_text(
        f"Текущий стиль: {STYLE_LABELS[current_style]}\n"
        f"Описание: {STYLE_DESCRIPTIONS[current_style]}\n\n"
        "Выбери новый стиль:",
        reply_markup=style_keyboard(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard_admin(update):
        return ConversationHandler.END
    await update.message.reply_text("Ок, отменил. Когда будет новое блюдо — жми /post или /reels.")
    return ConversationHandler.END


async def unknown_or_non_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await guard_admin(update)


async def download_photo_as_data_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[str, str]:
    photo = update.message.photo[-1]
    telegram_file = await context.bot.get_file(photo.file_id)
    file_bytes = await telegram_file.download_as_bytearray()
    encoded = base64.b64encode(file_bytes).decode("utf-8")
    return photo.file_id, f"data:image/jpeg;base64,{encoded}"


async def generate_post_from_photo(image_data_url: str, style: str) -> str:
    prompt = (
        "Ты пишешь по-русски для Telegram-канала о еде, семье, готовке и искусственном интеллекте. "
        "Проанализируй фото блюда: определи вероятное блюдо и примерные ингредиенты. "
        "Затем создай готовый пост. Тон: живой, теплый, современный, легкий юмор, как шеф и отец, "
        "который использует AI в готовке. Без медицинских обещаний и без ПП-сектантства. "
        f"Текущий стиль: {STYLE_LABELS[style]} — {STYLE_DESCRIPTIONS[style]}.\n\n"
        "Структура поста:\n"
        "1) живой заголовок\n"
        "2) описание блюда\n"
        "3) ингредиенты\n"
        "4) короткий рецепт\n"
        "5) примерное КБЖУ, если можно оценить; если нельзя — честно напиши, что это ориентир\n"
        "6) личная нотка: шеф, отец, дочка, домашняя еда, AI без фанатизма\n"
        "7) CTA в конце\n"
        "8) хештеги\n\n"
        "Не используй Markdown-таблицы. Текст должен быть готов к публикации."
    )
    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Ты опытный редактор Telegram-канала и food-контентмейкер."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            },
        ],
        temperature=0.8,
        max_tokens=1400,
    )
    return response.choices[0].message.content.strip()


async def rewrite_draft_text(draft_text: str, style: str, variation: str) -> str:
    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Ты редактор Telegram-канала о еде, семье, готовке и AI. Пиши по-русски."},
            {
                "role": "user",
                "content": (
                    f"Вот черновик поста:\n\n{draft_text}\n\n"
                    f"Задача: {VARIATION_PROMPTS[variation]}. "
                    f"Базовый стиль: {STYLE_LABELS[style]} — {STYLE_DESCRIPTIONS[style]}. "
                    "Сохрани русский язык, теплый тон, структуру поста, CTA и хештеги. "
                    "Без медицинских обещаний и без ПП-сектантства."
                ),
            },
        ],
        temperature=0.85,
        max_tokens=1300,
    )
    return response.choices[0].message.content.strip()


async def generate_reels_content(text: Optional[str], image_data_url: Optional[str], style: str) -> str:
    prompt = (
        "Создай контент для Instagram Reels на русском языке для проекта о еде, семье, готовке и AI. "
        f"Стиль: {STYLE_LABELS[style]} — {STYLE_DESCRIPTIONS[style]}. "
        "Тон: живой, теплый, современный, без медицинских обещаний, без ПП-сектантства, с легким юмором.\n\n"
        "Нужно выдать:\n"
        "- hook для первых 2 секунд\n"
        "- сценарий Reels на 20–30 секунд с таймингами\n"
        "- текст для озвучки\n"
        "- caption для Instagram\n"
        "- 3 идеи stories\n"
    )
    if text:
        prompt += f"\nОписание блюда/идеи от автора: {text}"

    content = [{"type": "text", "text": prompt}]
    if image_data_url:
        content.append({"type": "image_url", "image_url": {"url": image_data_url}})

    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Ты сценарист Reels и food-контентмейкер."},
            {"role": "user", "content": content},
        ],
        temperature=0.85,
        max_tokens=1300,
    )
    return response.choices[0].message.content.strip()


async def generate_plan_text(style: str) -> str:
    prompt = (
        "Сгенерируй контент-план на 7 дней для проекта о еде, семье, готовке и искусственном интеллекте. "
        "Темы проекта: готовка с дочкой, семейная еда, AI на кухне, простые рецепты, шефские лайфхаки, "
        "питание без фанатизма, тесты AI-бота по еде. "
        f"Стиль: {STYLE_LABELS[style]} — {STYLE_DESCRIPTIONS[style]}.\n\n"
        "Формат каждого дня:\n"
        "- День\n- Тема\n- Формат: Reels / Telegram / Stories\n- Hook\n- Краткая идея\n\n"
        "Пиши по-русски, конкретно и применимо."
    )
    response = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Ты контент-стратег для food/AI-проекта."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.85,
        max_tokens=1300,
    )
    return response.choices[0].message.content.strip()


async def handle_post_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    global latest_draft
    if not await guard_admin(update):
        return ConversationHandler.END

    if not update.message.photo:
        await update.message.reply_text("Нужно именно фото блюда. Пришли картинку — и я соберу пост.")
        return POST_PHOTO

    await update.message.chat.send_action(ChatAction.TYPING)
    await update.message.reply_text("Смотрю на блюдо и собираю черновик. AI уже нюхает пиксели 👀")

    try:
        photo_file_id, image_data_url = await download_photo_as_data_url(update, context)
        draft_text = await generate_post_from_photo(image_data_url, current_style)
    except OpenAIError:
        logger.exception("OpenAI failed while generating post")
        await update.message.reply_text("OpenAI сейчас не ответил. Проверь OPENAI_API_KEY, лимиты и попробуй еще раз.")
        return ConversationHandler.END
    except TelegramError:
        logger.exception("Telegram failed while downloading photo")
        await update.message.reply_text("Не получилось скачать фото из Telegram. Попробуй отправить фото еще раз.")
        return ConversationHandler.END

    latest_draft = Draft(
        admin_id=ADMIN_ID,
        photo_file_id=photo_file_id,
        draft_text=draft_text,
        style=current_style,
        created_at=datetime.now(timezone.utc),
    )
    await update.message.reply_text(f"Черновик готов:\n\n{draft_text}", reply_markup=draft_keyboard())
    return ConversationHandler.END


async def handle_reels_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await guard_admin(update):
        return ConversationHandler.END

    if not update.message.photo and not update.message.text:
        await update.message.reply_text("Пришли фото блюда или текстовое описание.")
        return REELS_INPUT

    await update.message.chat.send_action(ChatAction.TYPING)
    await update.message.reply_text("Готовлю Reels-сценарий: hook, тайминги, озвучку, caption и stories.")

    image_data_url = None
    text = update.message.text
    try:
        if update.message.photo:
            _, image_data_url = await download_photo_as_data_url(update, context)
        reels_text = await generate_reels_content(text, image_data_url, current_style)
    except OpenAIError:
        logger.exception("OpenAI failed while generating reels")
        await update.message.reply_text("OpenAI сейчас не ответил. Проверь ключ, лимиты и попробуй еще раз.")
        return ConversationHandler.END
    except TelegramError:
        logger.exception("Telegram failed while downloading reels photo")
        await update.message.reply_text("Не получилось скачать фото из Telegram. Попробуй еще раз.")
        return ConversationHandler.END

    await update.message.reply_text(reels_text)
    return ConversationHandler.END


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_admin(update):
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    await update.message.reply_text("Собираю контент-план на 7 дней.")
    try:
        plan_text = await generate_plan_text(current_style)
    except OpenAIError:
        logger.exception("OpenAI failed while generating plan")
        await update.message.reply_text("OpenAI сейчас не ответил. Проверь OPENAI_API_KEY, лимиты и попробуй еще раз.")
        return
    await update.message.reply_text(plan_text)


async def publish_latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard_admin(update):
        return
    await publish_draft(update, context)


async def publish_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global latest_draft
    message = update.effective_message
    if not latest_draft:
        if message:
            await message.reply_text("Нет черновика для публикации. Сначала создай пост через /post.")
        return
    if not CHANNEL_ID:
        if message:
            await message.reply_text("CHANNEL_ID не задан. Добавь ID или @username канала в переменные окружения.")
        return

    try:
        if len(latest_draft.draft_text) <= 1024:
            await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=latest_draft.photo_file_id,
                caption=latest_draft.draft_text,
                parse_mode=None,
            )
        else:
            await context.bot.send_photo(chat_id=CHANNEL_ID, photo=latest_draft.photo_file_id)
            await context.bot.send_message(chat_id=CHANNEL_ID, text=latest_draft.draft_text)
    except TelegramError:
        logger.exception("Publishing to channel failed")
        if message:
            await message.reply_text(
                "Не удалось опубликовать в канал. Проверь CHANNEL_ID и что бот добавлен администратором канала "
                "с правом публиковать сообщения."
            )
        return

    if message:
        await message.reply_text("Готово — пост опубликован в канал 🚀")


async def draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global latest_draft
    if not await guard_admin(update):
        return

    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "cancel":
        latest_draft = None
        await query.edit_message_text("Черновик отменен. Для нового поста используй /post.")
        return

    if action == "publish":
        await publish_draft(update, context)
        return

    if not latest_draft:
        await query.edit_message_text("Черновик уже не найден. Создай новый через /post.")
        return

    await query.edit_message_text("Переписываю черновик. Сейчас будет версия свежее ✍️")
    try:
        new_text = await rewrite_draft_text(latest_draft.draft_text, latest_draft.style, action)
    except OpenAIError:
        logger.exception("OpenAI failed while rewriting draft")
        await query.message.reply_text("OpenAI сейчас не ответил. Попробуй нажать кнопку еще раз чуть позже.")
        return

    latest_draft = Draft(
        admin_id=latest_draft.admin_id,
        photo_file_id=latest_draft.photo_file_id,
        draft_text=new_text,
        style=latest_draft.style,
        created_at=datetime.now(timezone.utc),
    )
    await query.message.reply_text(f"Новая версия:\n\n{new_text}", reply_markup=draft_keyboard())


async def style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global current_style
    if not await guard_admin(update):
        return

    query = update.callback_query
    await query.answer()
    style = query.data.split(":", 1)[1]
    if style not in STYLE_LABELS:
        await query.edit_message_text("Неизвестный стиль. Открой /style и выбери еще раз.")
        return

    current_style = style
    await query.edit_message_text(
        f"Стиль обновлен: {STYLE_LABELS[current_style]}\n"
        f"Описание: {STYLE_DESCRIPTIONS[current_style]}"
    )


def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    post_conversation = ConversationHandler(
        entry_points=[CommandHandler("post", post_command)],
        states={POST_PHOTO: [MessageHandler(filters.PHOTO | ~filters.COMMAND, handle_post_photo)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    reels_conversation = ConversationHandler(
        entry_points=[CommandHandler("reels", reels_command)],
        states={REELS_INPUT: [MessageHandler((filters.PHOTO | filters.TEXT) & ~filters.COMMAND, handle_reels_input)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(post_conversation)
    app.add_handler(reels_conversation)
    app.add_handler(CommandHandler("plan", plan_command))
    app.add_handler(CommandHandler("style", style_command))
    app.add_handler(CommandHandler("publish", publish_latest))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(draft_callback, pattern=r"^draft:"))
    app.add_handler(CallbackQueryHandler(style_callback, pattern=r"^style:"))
    app.add_handler(MessageHandler(filters.ALL, unknown_or_non_admin))
    return app


def main() -> None:
    app = build_application()
    logger.info("Starting food-ai-content-bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
