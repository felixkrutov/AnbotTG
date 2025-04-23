# --- НАЧАЛО ПОЛНОГО КОДА main.py (v4 - РУЧНОЙ ВЕБХУК + КРИТ ЛОГ) ---
import os
import logging
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import json

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

# --- Используем СИНХРОННЫЕ ВЫЗОВЫ Supabase ---
from supabase import create_client, Client as SupabaseClient
logging.info("Используется supabase-py V2+ (поддерживает async)")

from groq import Groq, AsyncGroq

# --- 1. Константы и Настройки ---
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "default_secret_key_please_change")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "") # Читаем, но не используем в lifespan для установки
WEBHOOK_PATH = f"/webhook/{TELEGRAM_BOT_TOKEN}"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "my-super-secret")
HISTORY_LIMIT = 50

# --- 2. Настройка логирования ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(name)s:%(lineno)d] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)

logger.info(f"--- Старт модуля main.py ---")

# --- 3. Инициализация ---
try:
    supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    logger.info("Клиент Supabase V2+ инициализирован.")
    default_properties = DefaultBotProperties(parse_mode=ParseMode.HTML)
    bot = Bot(token=TELEGRAM_BOT_TOKEN, default=default_properties)
    dp = Dispatcher()
    logger.info("Бот Telegram и Dispatcher инициализированы.")
except Exception as e: logger.error(f"Ошибка при инициализации: {e}", exc_info=True); raise

# --- 4. Настройка FastAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Упрощаем lifespan - НЕ УСТАНАВЛИВАЕМ И НЕ УДАЛЯЕМ вебхук отсюда
    logger.info(">>> Lifespan: Startup начался (вебхук устанавливается вручную).")
    yield
    logger.info(">>> Lifespan: Shutdown начался (вебхук удаляется вручную).")
    try:
        if bot and bot.session: await bot.session.close(); logger.info(">>> Lifespan: Сессия Aiogram Bot закрыта.")
    except Exception as e_close: logger.error(f">>> Lifespan: Ошибка закрытия сессии бота: {e_close}", exc_info=True)
    logger.info(">>> Lifespan: Shutdown завершен.")

app = FastAPI(lifespan=lifespan, openapi_url="/api/v1/openapi.json", docs_url="/api/docs", redoc_url="/api/redoc")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- 6. Функции Supabase (СИНХРОННЫЕ ВЫЗОВЫ) ---
# ... (get_settings, update_settings_db, get_message_history, add_message_to_history БЕЗ await перед execute) ...
async def get_settings():
    try: query = supabase.table("settings").select("groq_api_key, system_prompt").eq("id", 1).limit(1); result = query.execute()
    except Exception as e: logger.error(f"Sync Supabase get_settings error: {e}", exc_info=True); return {"groq_api_key": None, "system_prompt": "..."}
    if result.data: data = result.data[0]; return {"groq_api_key": data.get("groq_api_key"), "system_prompt": data.get("system_prompt", "...")}
    else: logger.warning("Settings not found (sync)."); return {"groq_api_key": None, "system_prompt": "..."}

async def update_settings_db(new_groq_key: str | None, new_system_prompt: str):
    try: payload = {"groq_api_key": new_groq_key, "system_prompt": new_system_prompt}; query = supabase.table("settings").update(payload).eq("id", 1); result = query.execute(); logger.info(f"Settings updated (sync): {result.data}"); return True
    except Exception as e: logger.error(f"Sync Supabase update_settings_db error: {e}", exc_info=True); return False

async def get_message_history(user_id: str):
    try: query = (supabase.table("message_history").select("role, content").eq("user_id", user_id).order("created_at", desc=True).limit(HISTORY_LIMIT)); result = query.execute(); return [{"role": msg["role"], "content": msg["content"]} for msg in reversed(result.data)]
    except Exception as e: logger.error(f"Sync Supabase get_message_history error for {user_id}: {e}", exc_info=True); return []

async def add_message_to_history(user_id: str, role: str, content: str):
    try: payload = {"user_id": user_id, "role": role, "content": content}; insert_query = supabase.table("message_history").insert(payload); insert_query.execute()
    except Exception as e_ins: logger.error(f"Sync Supabase add_message_to_history (insert) error for {user_id}: {e_ins}", exc_info=True); return False
    try: # Очистка старых
        count_query = supabase.table("message_history").select("id", count="exact").eq("user_id", user_id); count_result = count_query.execute(); current_count = count_result.count if count_result.count is not None else 0
        if current_count > HISTORY_LIMIT:
            num_to_delete = current_count - HISTORY_LIMIT; logger.info(f"History {user_id} ({current_count}/{HISTORY_LIMIT}). Deleting {num_to_delete} old (sync).")
            select_oldest_query = (supabase.table("message_history").select("id").eq("user_id", user_id).order("created_at", desc=False).limit(num_to_delete)); oldest_result = select_oldest_query.execute(); ids_to_delete = [msg["id"] for msg in oldest_result.data]
            if ids_to_delete: delete_query = supabase.table("message_history").delete().in_("id", ids_to_delete); delete_query.execute(); logger.info(f"Deleted {len(ids_to_delete)} old messages {user_id} (sync)")
    except Exception as e_clean: logger.error(f"Sync Supabase add_message_to_history (cleanup) error for {user_id}: {e_clean}", exc_info=True)
    return True

# --- 7. Функция Groq (без изменений) ---
async def get_groq_response(system_prompt: str, history: list, groq_api_key: str):
    if not groq_api_key: return "Ошибка: Ключ API Groq не настроен."
    try: async_groq_client = AsyncGroq(api_key=groq_api_key); messages = [{"role": "system", "content": system_prompt}] + history; chat_completion = await async_groq_client.chat.completions.create(messages=messages, model="llama3-8b-8192"); return chat_completion.choices[0].message.content
    except Exception as e: logger.error(f"Ошибка Groq API: {e}", exc_info=True); return f"Ошибка ИИ: {str(e)}"

# --- 8. Обработчики Telegram ---
@dp.message(CommandStart())
async def handle_start(message: types.Message): await message.answer(f"Привет, {message.from_user.full_name}!")
@dp.message(F.text)
async def handle_message(message: types.Message):
    user_id=str(message.from_user.id); user_input=message.text
    logger.info(f"Webhook: Обработка сообщения от {user_id}...")
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    settings = await get_settings() # Вызовет СИНХРОННЫЙ execute внутри
    if not settings.get("groq_api_key"): await message.answer("Ошибка: Ключ API Groq не настроен."); return
    history = await get_message_history(user_id) # Вызовет СИНХРОННЫЙ execute внутри
    await add_message_to_history(user_id, "user", user_input) # Вызовет СИНХРОННЫЙ execute внутри
    history.append({"role": "user", "content": user_input})
    if len(history) > HISTORY_LIMIT: history = history[-HISTORY_LIMIT:]
    ai_response = await get_groq_response(settings.get("system_prompt"), history, settings.get("groq_api_key"))
    await add_message_to_history(user_id, "assistant", ai_response) # Вызовет СИНХРОННЫЙ execute внутри
    try: await message.answer(ai_response); logger.info(f"Webhook: Ответ отправлен {user_id}.")
    except Exception as e: logger.error(f"Webhook: Ошибка отправки ответа {user_id}: {e}", exc_info=True); await message.answer("...")

# --- 9. Обработчики FastAPI ---
class SettingsUpdate(BaseModel): groq_api_key: str | None = None; system_prompt: str
async def verify_admin_secret(x_admin_secret: str | None = Header(None)):
    if not x_admin_secret or x_admin_secret != ADMIN_SECRET_KEY: raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="...")
@app.get("/api/settings", dependencies=[Depends(verify_admin_secret)])
async def get_current_settings_api(): logger.info("API /api/settings GET"); return await get_settings()
@app.post("/api/settings", status_code=status.HTTP_200_OK, dependencies=[Depends(verify_admin_secret)])
async def update_settings_api(settings_data: SettingsUpdate): logger.info("API /api/settings POST"); success = await update_settings_db(settings_data.groq_api_key, settings_data.system_prompt); return {"message": "OK"} if success else HTTPException(status_code=500, detail="Update failed")
@app.get("/")
async def read_root(): return {"message": "Бот жив!"}

@app.post(WEBHOOK_PATH)
async def bot_webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(None)):
    # ----- ДОБАВЛЕН КРИТИЧЕСКИЙ ЛОГ ЗДЕСЬ -----
    logger.critical("!!! ПОЛУЧЕН ЗАПРОС НА ВЕБХУК !!!")
    # ------------------------------------------
    logger.info(f">>> Webhook: Заголовок X-Telegram-Bot-Api-Secret-Token: {'Есть' if x_telegram_bot_api_secret_token else 'Нет'}")
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET: logger.warning(">>> Webhook: Неверный секрет!"); raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="...")
    try: update_data = await request.json(); logger.info(f">>> Webhook: Тело запроса (частично): {json.dumps(update_data, ensure_ascii=False)[:200]}...")
    except Exception as e_body: logger.error(f">>> Webhook: Ошибка чтения тела: {e_body}", exc_info=True); raise HTTPException(status_code=500, detail="...")
    telegram_update = types.Update(**update_data); logger.info(f">>> Webhook: Создан объект Update: ID={telegram_update.update_id}")
    try: asyncio.create_task(dp.feed_update(bot=bot, update=telegram_update)); logger.info(f">>> Webhook: Задача feed_update {telegram_update.update_id} создана. Возвращаем 200 OK.")
    except Exception as e_feed: logger.error(f">>> Webhook: Ошибка вызова feed_update для {telegram_update.update_id}: {e_feed}", exc_info=True)
    return Response(status_code=status.HTTP_200_OK)

# --- 10. Запуск ---
if __name__ == "__main__": logger.warning("Запуск через 'python main.py' не рекомендуется.")
logger.info("Скрипт main.py загружен. Используйте: uvicorn main:app --host 0.0.0.0 --port 8000")
# --- КОНЕЦ ПОЛНОГО КОДА main.py ---