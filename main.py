# --- НАЧАЛО ПОЛНОГО КОДА main.py (v7 - Попытка исправить конфликт поллинга) ---
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
# from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application # Не нужно для поллинга
# from aiohttp import web # Не нужно для поллинга

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
HISTORY_LIMIT = 50
DEFAULT_AI_MODEL = "llama3-8b-8192"

# --- 2. Настройка логирования ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(name)s:%(lineno)d] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)
logger.info(f"--- Старт модуля main.py (Режим Поллинга) ---")

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
app = FastAPI(openapi_url="/api/v1/openapi.json", docs_url="/api/docs", redoc_url="/api/redoc")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- 5. Фоновая задача для запуска поллинга ---
polling_task: asyncio.Task | None = None # Указываем тип

async def start_bot_polling():
    global polling_task
    # ----- ИЗМЕНЕНИЕ: Добавляем проверку, что задача еще не запущена -----
    if polling_task and not polling_task.done():
        logger.warning("Попытка повторного запуска поллинга, когда он уже активен. Пропускаем.")
        return
    # -----------------------------------------------------------------
    logger.info("Запуск Telegram Bot Polling...")
    try:
        webhook_info = await bot.get_webhook_info()
        if webhook_info.url: logger.warning(f"Обнаружен вебхук ({webhook_info.url}). Удаляем..."); await bot.delete_webhook(drop_pending_updates=True); logger.info("Вебхук удален.")
        await bot.set_my_commands([BotCommand(command="start", description="Начать")]); logger.info("Команды бота установлены.")
        await dp.start_polling(bot)
    except asyncio.CancelledError: logger.info("Задача поллинга отменена.")
    except Exception as e: logger.error(f"Ошибка в задаче поллинга: {e}", exc_info=True)
    finally: logger.warning("Polling task завершился."); polling_task = None

@app.on_event("startup")
async def on_startup():
    global polling_task
    # ----- ИЗМЕНЕНИЕ: Добавляем проверку перед созданием задачи -----
    if polling_task is None:
        logger.info("FastAPI startup: Запуск фоновой задачи поллинга...")
        polling_task = asyncio.create_task(start_bot_polling())
    else:
         logger.warning("FastAPI startup: Задача поллинга уже существует. Не создаем новую.")
    # ----------------------------------------------------------

@app.on_event("shutdown")
async def on_shutdown():
    global polling_task; logger.info("FastAPI shutdown: Остановка поллинга...")
    if polling_task and not polling_task.done(): polling_task.cancel(); logger.info("Запрос на отмену поллинга.")
    try:
        if bot and bot.session: await bot.session.close(); logger.info("Сессия Aiogram Bot закрыта.")
    except Exception as e_close: logger.error(f"Ошибка закрытия сессии бота: {e_close}", exc_info=True)
    logger.info("FastAPI shutdown завершен.")


# --- 6. Функции Supabase (СИНХРОННЫЕ ВЫЗОВЫ) ---
async def get_settings():
    try: query = supabase.table("settings").select("groq_api_key, system_prompt, ai_model_name").eq("id", 1).limit(1); result = query.execute()
    except Exception as e: logger.error(f"Sync Supabase get_settings error: {e}", exc_info=True); return {"groq_api_key": None, "system_prompt": "...", "ai_model_name": DEFAULT_AI_MODEL}
    if result.data: data = result.data[0]; return {"groq_api_key": data.get("groq_api_key"), "system_prompt": data.get("system_prompt", "..."), "ai_model_name": data.get("ai_model_name") or DEFAULT_AI_MODEL }
    else: logger.warning("Settings not found (sync)."); return {"groq_api_key": None, "system_prompt": "...", "ai_model_name": DEFAULT_AI_MODEL}

async def update_settings_db(new_groq_key: str | None, new_system_prompt: str, new_ai_model: str | None):
    try: payload = {"groq_api_key": new_groq_key, "system_prompt": new_system_prompt, "ai_model_name": new_ai_model or DEFAULT_AI_MODEL}; query = supabase.table("settings").update(payload).eq("id", 1); result = query.execute(); logger.info(f"Settings updated (sync): {result.data}"); return True
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

# --- 7. Функция Groq (с моделью из настроек) ---
async def get_groq_response(system_prompt: str, history: list, groq_api_key: str, model_name: str):
    if not groq_api_key: return "Ошибка: Ключ API Groq не настроен."
    if not model_name: model_name = DEFAULT_AI_MODEL
    try: async_groq_client = AsyncGroq(api_key=groq_api_key); messages = [{"role": "system", "content": system_prompt}] + history; logger.info(f"Запрос к Groq с моделью '{model_name}'..."); chat_completion = await async_groq_client.chat.completions.create(messages=messages, model=model_name); return chat_completion.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка Groq API (модель: {model_name}): {e}", exc_info=True)
        if "model_not_found" in str(e).lower(): return f"Ошибка ИИ: Модель '{model_name}' не найдена."
        return f"Ошибка ИИ: {str(e)}"

# --- 8. Обработчики Telegram ---
@dp.message(CommandStart())
async def handle_start(message: types.Message): await message.answer(f"Привет, {message.from_user.full_name}!")
@dp.message(F.text)
async def handle_message(message: types.Message):
    user_id=str(message.from_user.id); user_input=message.text
    logger.info(f"Polling: Обработка сообщения от {user_id}...")
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    settings = await get_settings(); current_groq_key = settings.get("groq_api_key"); current_system_prompt = settings.get("system_prompt"); current_model_name = settings.get("ai_model_name")
    if not current_groq_key: await message.answer("Ошибка: Ключ API Groq не настроен."); return
    history = await get_message_history(user_id)
    await add_message_to_history(user_id, "user", user_input)
    history.append({"role": "user", "content": user_input});
    if len(history) > HISTORY_LIMIT: history = history[-HISTORY_LIMIT:]
    ai_response = await get_groq_response(current_system_prompt, history, current_groq_key, current_model_name)
    await add_message_to_history(user_id, "assistant", ai_response)
    try: await message.answer(ai_response); logger.info(f"Polling: Ответ отправлен {user_id}.")
    except Exception as e: logger.error(f"Polling: Ошибка отправки ответа {user_id}: {e}", exc_info=True); await message.answer("...")

# --- 9. Обработчики FastAPI (с моделью) ---
class SettingsUpdate(BaseModel): groq_api_key: str | None = None; system_prompt: str; ai_model_name: str | None = None
async def verify_admin_secret(x_admin_secret: str | None = Header(None)):
    if not x_admin_secret or x_admin_secret != ADMIN_SECRET_KEY: raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="...")
@app.get("/api/settings", dependencies=[Depends(verify_admin_secret)])
async def get_current_settings_api(): logger.info("API /api/settings GET"); return await get_settings()
@app.post("/api/settings", status_code=status.HTTP_200_OK, dependencies=[Depends(verify_admin_secret)])
async def update_settings_api(settings_data: SettingsUpdate): logger.info(f"API /api/settings POST (с моделью: {settings_data.ai_model_name})"); success = await update_settings_db(settings_data.groq_api_key, settings_data.system_prompt, settings_data.ai_model_name); return {"message": "OK"} if success else HTTPException(status_code=500, detail="Update failed")
@app.get("/")
async def read_root(): return {"message": "Бот жив (режим поллинга)!"}

# --- 10. Запуск ---
if __name__ == "__main__": logger.warning("Запуск через 'python main.py' не рекомендуется.")
logger.info("Скрипт main.py загружен. Используйте: uvicorn main:app --host 0.0.0.0 --port 8000")
# --- КОНЕЦ ПОЛНОГО КОДА main.py ---