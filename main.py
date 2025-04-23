import os
import logging
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, Update
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from fastapi import FastAPI, HTTPException, Depends, Body, Request, status, Header, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ----- ВНИМАНИЕ: Убираем await из вызовов Supabase! -----
# Это для ДИАГНОСТИКИ проблемы с TypeError.
# Если это сработает, значит, есть проблема с async/await в библиотеке supabase-py или ее зависимостях.
# В идеале, await нужно вернуть, если проблема будет решена иначе.

from supabase import create_client, Client as SupabaseClient
logging.info("Используется supabase-py V2+ (поддерживает async)")

from groq import Groq, AsyncGroq

# --- 1. Константы и Настройки ---
load_dotenv()
# ... (остальные константы без изменений) ...
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "default_secret_key_please_change")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "")
WEBHOOK_PATH = f"/webhook/{TELEGRAM_BOT_TOKEN}"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "my-super-secret")
HISTORY_LIMIT = 50

if not all([TELEGRAM_BOT_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_KEY]):
    raise ValueError("TELEGRAM_BOT_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_KEY должны быть установлены в .env")
if ADMIN_SECRET_KEY == "default_secret_key_please_change":
     logger = logging.getLogger(__name__)
     logger.warning("Используется ADMIN_SECRET_KEY по умолчанию!")

# --- 2. Настройка логирования ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# --- 3. Инициализация клиентов и Aiogram ---
try:
    supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    logger.info("Клиент Supabase V2+ инициализирован.")
    default_properties = DefaultBotProperties(parse_mode=ParseMode.HTML)
    bot = Bot(token=TELEGRAM_BOT_TOKEN, default=default_properties)
    dp = Dispatcher()
    logger.info("Бот Telegram и Dispatcher инициализированы.")
except Exception as e:
    logger.error(f"Ошибка при инициализации: {e}", exc_info=True)
    raise

# --- 4. Настройка FastAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... (lifespan остается без изменений, управляет вебхуком) ...
    logger.info("FastAPI startup: Установка вебхука...")
    if not WEBHOOK_BASE_URL:
        logger.warning("WEBHOOK_BASE_URL не установлен в .env. Не удается установить вебхук.")
        webhook_info = await bot.get_webhook_info()
        if webhook_info.url:
             logger.info(f"Текущий вебхук: {webhook_info.url}. Удаляем его...")
             await bot.delete_webhook(drop_pending_updates=True)
    else:
        webhook_url = f"{WEBHOOK_BASE_URL.rstrip('/')}{WEBHOOK_PATH}"
        try:
            await bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET, allowed_updates=dp.resolve_used_update_types(), drop_pending_updates=True)
            logger.info(f"Вебхук установлен на: {webhook_url}")
            await bot.set_my_commands([BotCommand(command="start", description="Начать / Перезапустить")])
            logger.info("Команды бота установлены.")
        except Exception as e:
            logger.error(f"Ошибка установки вебхука на {webhook_url}: {e}", exc_info=True)
    yield
    logger.info("FastAPI shutdown: Удаление вебхука и закрытие сессий...")
    webhook_info = await bot.get_webhook_info()
    if webhook_info.url:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("Вебхук удален.")
        except Exception as e:
            logger.error(f"Ошибка удаления вебхука: {e}", exc_info=True)
    if bot and bot.session:
       await bot.session.close()
       logger.info("Сессия Aiogram Bot закрыта.")
    logger.info("FastAPI shutdown завершен.")

app = FastAPI(lifespan=lifespan, openapi_url="/api/v1/openapi.json", docs_url="/api/docs", redoc_url="/api/redoc")

# --- 5. Настройка CORS ---
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- 6. Функции Supabase (СИНХРОННЫЕ ВЫЗОВЫ ДЛЯ ТЕСТА) ---

# Важно: сами функции остаются async, т.к. вызываются из async FastAPI/Aiogram
# Но внутри мы убираем await перед execute()

async def get_settings():
    """Получает настройки из Supabase (синхронный вызов execute)."""
    try:
        query = supabase.table("settings").select("groq_api_key, system_prompt").eq("id", 1).limit(1)
        logger.debug("Выполняем get_settings execute() СИНХРОННО")
        result = query.execute() # <--- УБРАЛИ AWAIT
        logger.debug(f"Результат get_settings: {result}") # Логируем результат

        if result.data:
            logger.debug("Настройки получены из Supabase (синхронно).")
            data = result.data[0]
            return {
                "groq_api_key": data.get("groq_api_key"),
                "system_prompt": data.get("system_prompt", "You are a helpful assistant.")
            }
        else:
            logger.warning("Настройки в 'settings' (id=1) не найдены (синхронно).")
            return {"groq_api_key": None, "system_prompt": "You are a helpful assistant."}
    except Exception as e:
        logger.error(f"Ошибка получения настроек Supabase (синхронно): {e}", exc_info=True)
        return {"groq_api_key": None, "system_prompt": "You are a helpful assistant."}

async def update_settings_db(new_groq_key: str | None, new_system_prompt: str):
    """Обновляет настройки в Supabase (синхронный вызов execute)."""
    try:
        payload = {
            "groq_api_key": new_groq_key,
            "system_prompt": new_system_prompt,
        }
        if not payload:
             logger.warning("Нет данных для обновления настроек.")
             return False

        query = supabase.table("settings").update(payload).eq("id", 1)
        logger.debug("Выполняем update_settings_db execute() СИНХРОННО")
        result = query.execute() # <--- УБРАЛИ AWAIT
        logger.info(f"Настройки обновлены в Supabase (синхронно): {result.data}")
        return True
    except Exception as e:
        logger.error(f"Ошибка обновления настроек Supabase (синхронно): {e}", exc_info=True)
        return False

async def get_message_history(user_id: str):
    """Получает историю сообщений из Supabase (синхронный вызов execute)."""
    try:
        query = (
            supabase.table("message_history")
            .select("role, content")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(HISTORY_LIMIT)
        )
        logger.debug(f"Выполняем get_message_history execute() СИНХРОННО для {user_id}")
        result = query.execute() # <--- УБРАЛИ AWAIT
        history = [{"role": msg["role"], "content": msg["content"]} for msg in reversed(result.data)]
        logger.debug(f"Получена история ({len(history)} сообщений) для {user_id} (синхронно)")
        return history
    except Exception as e:
        logger.error(f"Ошибка получения истории Supabase для {user_id} (синхронно): {e}", exc_info=True)
        return []

async def add_message_to_history(user_id: str, role: str, content: str):
    """Добавляет сообщение в историю и удаляет старые (синхронные вызовы execute)."""
    try:
        # 1. Добавляем новое сообщение
        payload = {"user_id": user_id, "role": role, "content": content}
        insert_query = supabase.table("message_history").insert(payload)
        logger.debug(f"Выполняем add_message_to_history insert execute() СИНХРОННО для {user_id}")
        insert_query.execute() # <--- УБРАЛИ AWAIT
        logger.debug(f"Сообщение ({role}) добавлено для {user_id} (синхронно)")

        # 2. Проверяем и удаляем старые
        count_query = supabase.table("message_history").select("id", count="exact").eq("user_id", user_id)
        logger.debug(f"Выполняем add_message_to_history count execute() СИНХРОННО для {user_id}")
        count_result = count_query.execute() # <--- УБРАЛИ AWAIT
        current_count = count_result.count if count_result.count is not None else 0

        if current_count > HISTORY_LIMIT:
            num_to_delete = current_count - HISTORY_LIMIT
            logger.info(f"История {user_id} ({current_count}/{HISTORY_LIMIT}). Удаляем {num_to_delete} старых (синхронно).")

            select_oldest_query = (
                supabase.table("message_history")
                .select("id")
                .eq("user_id", user_id)
                .order("created_at", desc=False)
                .limit(num_to_delete)
            )
            logger.debug(f"Выполняем add_message_to_history select oldest execute() СИНХРОННО для {user_id}")
            oldest_result = select_oldest_query.execute() # <--- УБРАЛИ AWAIT
            ids_to_delete = [msg["id"] for msg in oldest_result.data]

            if ids_to_delete:
                delete_query = supabase.table("message_history").delete().in_("id", ids_to_delete)
                logger.debug(f"Выполняем add_message_to_history delete execute() СИНХРОННО для {user_id}")
                delete_query.execute() # <--- УБРАЛИ AWAIT
                logger.info(f"Удалено {len(ids_to_delete)} старых сообщений {user_id} (синхронно)")
            else:
                 logger.warning(f"Не найдены ID для удаления старых сообщений {user_id} (синхронно)")
        return True

    except Exception as e:
        logger.error(f"Ошибка добавления/очистки истории {user_id} (синхронно): {e}", exc_info=True)
        return False

# --- 7. Функция Groq (без изменений) ---
# ... (get_groq_response остается как была) ...
async def get_groq_response(system_prompt: str, history: list, groq_api_key: str):
    if not groq_api_key:
        logger.error("Groq API ключ не найден.")
        return "Ошибка: Ключ API Groq не настроен."
    try:
        async_groq_client = AsyncGroq(api_key=groq_api_key)
        messages = [{"role": "system", "content": system_prompt}] + history
        logger.debug(f"Запрос к Groq: {len(messages)} сообщений.")
        chat_completion = await async_groq_client.chat.completions.create(messages=messages, model="llama3-8b-8192")
        response_content = chat_completion.choices[0].message.content
        logger.debug("Ответ от Groq API получен.")
        return response_content
    except Exception as e:
        logger.error(f"Ошибка Groq API: {e}", exc_info=True)
        return f"Ошибка ИИ: {str(e)}"

# --- 8. Обработчики Telegram (без изменений, но теперь будут вызывать синхронные Supabase функции) ---
# ... (handle_start, handle_message остаются как были) ...
@dp.message(CommandStart())
async def handle_start(message: types.Message):
    user_id = str(message.from_user.id)
    user_name = message.from_user.full_name
    logger.info(f"Команда /start от {user_name} (ID: {user_id})")
    await message.answer(f"Привет, {user_name}! Напиши мне что-нибудь.")

@dp.message(F.text)
async def handle_message(message: types.Message):
    user_id = str(message.from_user.id)
    user_input = message.text
    logger.info(f"Сообщение от {user_id}: '{user_input[:50]}...'")
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    settings = await get_settings() # Вызовет синхронный execute внутри
    current_groq_key = settings.get("groq_api_key")
    current_system_prompt = settings.get("system_prompt")
    if not current_groq_key:
        await message.answer("Ошибка: Ключ API Groq не настроен.")
        logger.error(f"Нет Groq API ключа для {user_id}.")
        return
    history = await get_message_history(user_id) # Вызовет синхронный execute внутри
    await add_message_to_history(user_id, "user", user_input) # Вызовет синхронный execute внутри
    history.append({"role": "user", "content": user_input})
    if len(history) > HISTORY_LIMIT:
         history = history[-HISTORY_LIMIT:]
    ai_response = await get_groq_response(current_system_prompt, history, current_groq_key)
    await add_message_to_history(user_id, "assistant", ai_response) # Вызовет синхронный execute внутри
    try:
        await message.answer(ai_response)
        logger.info(f"Ответ для {user_id} отправлен.")
    except Exception as e:
        logger.error(f"Ошибка отправки ответа {user_id}: {e}", exc_info=True)
        await message.answer("Произошла ошибка при отправке ответа.")

# --- 9. Обработчики FastAPI (без изменений, но теперь вызывают синхронные Supabase функции) ---
# ... (SettingsUpdate, verify_admin_secret, get_current_settings_api, update_settings_api, read_root, bot_webhook остаются как были) ...
class SettingsUpdate(BaseModel):
    groq_api_key: str | None = None
    system_prompt: str

async def verify_admin_secret(x_admin_secret: str | None = Header(None)):
    if not x_admin_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Отсутствует X-Admin-Secret")
    if x_admin_secret != ADMIN_SECRET_KEY:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Неверный ключ")

@app.get("/api/settings", dependencies=[Depends(verify_admin_secret)])
async def get_current_settings_api():
    logger.info("API запрос на получение настроек (синхронный вызов Supabase)")
    settings = await get_settings()
    return settings

@app.post("/api/settings", status_code=status.HTTP_200_OK, dependencies=[Depends(verify_admin_secret)])
async def update_settings_api(settings_data: SettingsUpdate):
    key_status = "установлен/пуст"
    if settings_data.groq_api_key and len(settings_data.groq_api_key) > 4:
        key_status = f"{'*'*(len(settings_data.groq_api_key)-4)}{settings_data.groq_api_key[-4:]}"
    logger.info(f"API запрос на обновление (синхронный вызов Supabase): GROQ ключ {key_status}, Промпт: '{settings_data.system_prompt[:50]}...'")
    success = await update_settings_db(settings_data.groq_api_key, settings_data.system_prompt)
    if success:
        return {"message": "Настройки успешно обновлены"}
    else:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Не удалось обновить настройки (см. логи бэкенда)")

@app.get("/")
async def read_root():
    logger.info("Запрос на /")
    return {"message": "Бэкенд бота жив (использует вебхук)! Админка по /api/settings."}

@app.post(WEBHOOK_PATH)
async def bot_webhook(update: dict = Body(...), x_telegram_bot_api_secret_token: str | None = Header(None)):
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        logger.warning("Получен запрос на вебхук с неверным секретом!")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid secret token")
    telegram_update = types.Update(**update)
    logger.debug(f"Получено обновление через вебхук: {telegram_update.update_id}")
    try:
        await dp.feed_update(bot=bot, update=telegram_update)
        return Response(status_code=status.HTTP_200_OK)
    except Exception as e:
         logger.error(f"Ошибка обработки обновления {telegram_update.update_id}: {e}", exc_info=True)
         return Response(status_code=status.HTTP_200_OK)


# --- 10. Запуск ---
if __name__ == "__main__":
    logger.warning("Запуск через 'python main.py' не рекомендуется. Используйте ASGI сервер.")
logger.info("Скрипт main.py загружен. Используйте: uvicorn main:app --host 0.0.0.0 --port 8000 [--reload]")