import os
import random
import logging
import traceback
import json
import asyncio
from datetime import datetime
import discord
from discord.ext import commands
import aiohttp
from dotenv import load_dotenv

# Отключаем прокси-переменные
for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(var, None)

# Загружаем переменные из .env
load_dotenv()

# Конфигурация
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
BASE_URL = os.getenv("BASE_URL", "https://api.electronhub.ai/").rstrip("/")
API_KEY = os.getenv("API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "claude-3-5-sonnet-20240620")
TEMPERATURE = float(os.getenv("TEMPERATURE", "1.2"))

# Системный промпт
# Чтение промпта из файла
PROMPT = ""
try:
    with open("prompt.txt", "r", encoding="utf-8") as f:
        PROMPT = f.read()
except FileNotFoundError:
    logging.error("Ошибка: файл 'prompt.txt' не найден. Убедитесь, что он находится в той же директории.")
    # Установите запасной промпт или выйдите, если файл критичен
    PROMPT = "Ошибка: файл промпта не найден. Проверьте конфигурацию." # Запасной промпт, если файл не найден
except Exception as e:
    logging.error(f"Ошибка при чтении файла промпта: {e}")
    PROMPT = "Ошибка: не удалось прочитать файл промпта." # Запасной промпт при других ошибках чтения

# Резервы API
backup_apis = []
for i in range(1, 4):
    url = os.getenv(f"BACKUP_API_URL_{i}")
    key = os.getenv(f"BACKUP_API_KEY_{i}")
    model = os.getenv(f"BACKUP_API_MODEL_{i}", MODEL_NAME)
    if url and key:
        backup_apis.append({"url": url.rstrip("/"), "key": key, "model": model})

logging.basicConfig(level=logging.INFO)

# Интенты
intents = discord.Intents.default()
intents.guilds = True
intents.guild_messages = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=commands.when_mentioned_or('!', '-'), intents=intents, help_command=None)

# Память
MAX_HISTORY = 60  # STM (краткосрочная память)
chat_history = {}

# Долгосрочная память (LTM)
LTM_FILE = "long_term_memory.json"
STM_FILE = "short_term_memory.json"

def load_memory(file_name, default={}):
    """Загружает память из файла"""
    if os.path.exists(file_name):
        try:
            with open(file_name, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Ошибка загрузки памяти ({file_name}): {e}")
    return default

def save_memory(data, file_name):
    """Сохраняет память в файл"""
    try:
        with open(file_name, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logging.error(f"Ошибка сохранения памяти ({file_name}): {e}")
        return False

def update_history(channel_id: int, role: str, content: str):
    """Обновляет STM (историю сообщений)"""
    history = chat_history.setdefault(channel_id, [])
    history.append({"role": role, "content": content})
    if len(history) > MAX_HISTORY * 2:
        chat_history[channel_id] = history[-MAX_HISTORY * 2:]
    
    # Сохраняем STM на диск
    save_memory(chat_history, STM_FILE)

def get_user_memory(user_id: str):
    """Возвращает LTM пользователя в виде строки"""
    ltm = load_memory(LTM_FILE)
    user_data = ltm.get(user_id, {})
    
    if not user_data:
        return ""
    
    # Формируем строку памяти
    memory_lines = ["\n\n=== [ ДОЛГОСРОЧНАЯ ПАМЯТЬ ] ==="]
    memory_lines.append(f"Имя: {user_data.get('name', 'Неизвестный')}")
    
    # Добавляем сводку в память если она есть
    if 'summary' in user_data:
        memory_lines.append(f"\nСводка: {user_data['summary']}")
    
    if 'traits' in user_data:
        memory_lines.append("\nХарактеристики:")
        for trait in user_data['traits']:
            memory_lines.append(f"- {trait['text']} (от {trait['author']}, {datetime.fromisoformat(trait['timestamp']).strftime('%d.%m.%Y')}")
    
    if 'facts' in user_data:
        memory_lines.append("\nФакты:")
        for fact in user_data['facts']:
            memory_lines.append(f"- {fact['text']} (запомнено {datetime.fromisoformat(fact['timestamp']).strftime('%d.%m.%Y')}")
    
    return "\n".join(memory_lines)

async def get_response(user_text: str, channel_id: int, user_id: str) -> str:
    """Получает ответ от API с учетом памяти"""
    history = chat_history.get(channel_id, [])
    apis = [{"url": BASE_URL, "key": API_KEY, "model": MODEL_NAME}] + backup_apis
    
    # Получаем LTM пользователя
    user_ltm = get_user_memory(user_id)
    
    # Формируем системный промпт с учетом памяти
    full_prompt = PROMPT + user_ltm
    
    # Собираем информацию о недоступных API
    failed_apis = []
    
    for api in apis:
        try:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api['key']}"
            }
            payload = {
                "model": api["model"],
                "temperature": TEMPERATURE,
                "messages": [
                    {"role": "system", "content": full_prompt},
                    *history,
                    {"role": "user", "content": user_text}
                ]
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url=api["url"] + "/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=20
                ) as resp:
                    
                    if resp.status != 200:
                        error_text = await resp.text()
                        logging.warning(f"API {api['url']} вернул статус {resp.status}: {error_text[:200]}")
                        failed_apis.append(api['url'])
                        continue
                        
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"]
                    
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logging.warning(f"Сетевая ошибка на API {api.get('url', 'unknown')}: {str(e)}")
            failed_apis.append(api.get('url', 'unknown'))
            await asyncio.sleep(1)  # Задержка между попытками
        except Exception as e:
            logging.error(f"Неизвестная ошибка API: {str(e)}")
            failed_apis.append(api.get('url', 'unknown'))
            await asyncio.sleep(1)
    
    # Формируем детальное сообщение об ошибке
    error_msg = "💀 Все API недоступны! Серверы, которые я пробовал:\n"
    error_msg += "\n".join(f"- {api}" for api in set(failed_apis))
    error_msg += "\n\nПопробуйте позже или проверьте настройки."
    
    raise RuntimeError(error_msg)

@bot.command(name='setprofile', aliases=['установитьпрофиль'])
async def set_profile(ctx, *, summary: str):
    """Устанавливает сводку профиля для пользователя"""
    user_id = str(ctx.author.id)
    ltm = load_memory(LTM_FILE)
    
    # Проверка длины сводки
    if len(summary) > 1000:
        await ctx.send("❌ Слишком длинная сводка! Максимум 1000 символов.")
        return
    
    # Обновляем или создаем запись
    if user_id not in ltm:
        ltm[user_id] = {
            "name": ctx.author.display_name,
            "traits": [],
            "facts": [],
            "summary": summary
        }
    else:
        ltm[user_id]["summary"] = summary
    
    # Сохраняем изменения
    save_memory(ltm, LTM_FILE)
    await ctx.send(f"✅ Сводка профиля для {ctx.author.mention} установлена!")

@bot.command(name='profile', aliases=['профиль'])
async def user_profile(ctx, member: discord.Member = None):
    """Показывает профиль пользователя с характеристиками"""
    target = member or ctx.author
    ltm = load_memory(LTM_FILE)
    user_id = str(target.id)
    
    # Создаем embed
    embed = discord.Embed(
        title=f"💀 Досье на {target.display_name}",
        color=discord.Color.dark_red(),
        timestamp=datetime.now()
    )
    
    # Добавляем аватар
    embed.set_thumbnail(url=target.display_avatar.url)
    
    # Основная информация
    embed.add_field(
        name="🪪 Основные данные",
        value=f"**Имя:** {target.name}\n"
              f"**ID:** {target.id}\n"
              f"**На сервере с:** {target.joined_at.strftime('%d.%m.%Y')}",
        inline=False
    )
    
    # Добавляем сводку если она есть
    if user_id in ltm and 'summary' in ltm[user_id]:
        embed.add_field(
            name="📝 Сводка",
            value=ltm[user_id]['summary'],
            inline=False
        )
    else:
        embed.add_field(
            name="📝 Сводка",
            value="*Пользователь еще не установил сводку*\n"
                  "Используй `!setprofile [текст]` чтобы добавить",
            inline=False
        )
    
    # Характеристики из LTM
    traits_section = ""
    if user_id in ltm and ltm[user_id].get('traits'):
        for i, trait in enumerate(ltm[user_id]['traits'], 1):
            traits_section += f"{i}. {trait['text']} (от {trait['author']})\n"
    
    if traits_section:
        embed.add_field(
            name="📌 Ярлыки",
            value=f"```\n{traits_section}\n```",
            inline=False
        )
    
    # Факты из LTM
    facts_section = ""
    if user_id in ltm and ltm[user_id].get('facts'):
        for i, fact in enumerate(ltm[user_id]['facts'], 1):
            facts_section += f"{i}. {fact['text']} (от {fact['author']})\n"
    
    if facts_section:
        embed.add_field(
            name="🗃️ Компромат",
            value=f"```\n{facts_section}\n```",
            inline=False
        )
    
    # Статистика
    traits_count = len(ltm[user_id]['traits']) if user_id in ltm and 'traits' in ltm[user_id] else 0
    facts_count = len(ltm[user_id]['facts']) if user_id in ltm and 'facts' in ltm[user_id] else 0
    
    embed.add_field(
        name="📊 Статистика",
        value=f"**Ярлыков:** {traits_count}\n"
              f"**Фактов:** {facts_count}",
        inline=False
    )
    
    # Футер с подписью бота
    embed.set_footer(text=f"Запросил: {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
    
    await ctx.send(embed=embed)
@bot.command(name='analyze')
@commands.has_permissions(administrator=True)  # Только для админов
async def analyze_chat(ctx, limit: int = 100):
    """Анализирует чат и автоматически создает характеристики участников"""
    await ctx.send(f"🔍 Начинаю анализ чата! Будет проверено {limit} сообщений.")
    
    # Собираем сообщения
    messages = []
    async for message in ctx.channel.history(limit=limit):
        if not message.author.bot and message.content:  # Игнорируем ботов и пустые сообщения
            messages.append(message)
    
    # Группируем по авторам
    user_messages = {}
    for message in messages:
        user_id = str(message.author.id)
        if user_id not in user_messages:
            user_messages[user_id] = {
                "user": message.author,
                "messages": []
            }
        user_messages[user_id]["messages"].append(message.content)
    
    # Для каждого пользователя генерируем характеристику
    # Этот промпт также может быть вынесен в отдельный файл, если потребуется
    analysis_prompt = """
Ты — опытный психолог. Твоя задача — анализировать сообщения и писать краткие, меткие характеристики на основе предоставленных сообщений пользователя.

Сообщения пользователя:
{}

Характеристика:
    """
    
    for user_id, data in user_messages.items():
        user = data["user"]
        # Берем не более 20 сообщений, чтобы не перегружать промпт
        sample_messages = data["messages"][:20]
        user_text = "\n".join(sample_messages)
        
        # Если сообщений слишком много, обрезаем общий текст
        if len(user_text) > 2000:
            user_text = user_text[:2000]
        
        prompt = analysis_prompt.format(user_text)
        
        try:
            # Используем отдельную функцию для запроса
            async with ctx.typing():
                trait = await get_analysis_response(prompt)
            
            # Сохраняем характеристику в LTM
            ltm = load_memory(LTM_FILE)
            if user_id not in ltm:
                ltm[user_id] = {
                    "name": user.display_name,
                    "traits": [],
                    "facts": []
                }
            
            # Проверяем, нет ли уже такой характеристики от бота
            existing = False
            for t in ltm[user_id]["traits"]:
                if t["author"] == bot.user.name and t["text"] == trait:
                    existing = True
                    break
            
            if not existing:
                new_trait = {
                    "text": trait,
                    "timestamp": datetime.now().isoformat(),
                    "author": bot.user.name
                }
                ltm[user_id]["traits"].append(new_trait)
                save_memory(ltm, LTM_FILE)
                await ctx.send(f"🔍 Характеристика для {user.mention}: *{trait}*")
            else:
                await ctx.send(f"ℹ️ Для {user.mention} уже есть такая характеристика. Пропускаю.")
                
        except Exception as e:
            logging.error(f"Ошибка анализа для {user_id}: {e}")
            await ctx.send(f"⚠️ Ошибка при анализе {user.mention}! Пропускаю.")
    
    await ctx.send("✅ Анализ завершен!")

async def get_analysis_response(prompt: str) -> str:
    """Получает ответ от API для анализа"""
    apis = [{"url": BASE_URL, "key": API_KEY, "model": MODEL_NAME}] + backup_apis
    
    for api in apis:
        try:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api['key']}"
            }
            payload = {
                "model": api["model"],
                "temperature": TEMPERATURE,
                "messages": [
                    {"role": "system", "content": "Ты — опытный психолог. Твоя задача — анализировать сообщения и писать краткие, меткие характеристики."},
                    {"role": "user", "content": prompt}
                ]
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url=api["url"] + "/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=30
                ) as resp:
                    
                    if resp.status != 200:
                        logging.warning(f"API {api['url']} вернул статус {resp.status}")
                        continue
                        
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"].strip()
                    
        except Exception as e:
            logging.error(f"Ошибка API {api['url']}: {str(e)}")
            continue
            
    raise RuntimeError("Все API недоступны")

@bot.event
async def on_ready():
    """Загружаем STM при запуске"""
    global chat_history
    chat_history = load_memory(STM_FILE, {})
    logging.info(f"Бот запущен как {bot.user.name}")
    logging.info(f"Загружена STM: {len(chat_history)} каналов")
    # Отладочная информация о зарегистрированных командах
    command_names = [cmd.name for cmd in bot.commands]
    logging.info(f"Зарегистрированные команды: {', '.join(command_names)}")

@bot.event
async def on_message(message: discord.Message):
    # Обработка команд
    if message.content.startswith(('!', '-')):
        await bot.process_commands(message)
        return
        
    if message.author.bot:
        return
        
    # Проверяем условия ответа
    mentioned = bot.user in message.mentions
    replied = (
        message.reference and 
        isinstance(message.reference.resolved, discord.Message) and 
        message.reference.resolved.author == bot.user
    )
    
    if not (mentioned or replied):
        return
        
    channel_id = str(message.channel.id)
    user_text = message.content.replace(f"<@{bot.user.id}>", "").strip()
    update_history(channel_id, "user", user_text)
        
    try:
        async with message.channel.typing():
            reply = await get_response(user_text, channel_id, str(message.author.id))
            
        # Обрезаем слишком длинные ответы
        truncated = reply[:2000]
        update_history(channel_id, "assistant", truncated)
        await message.reply(truncated)
        
    except RuntimeError as e:
        # Специальная обработка для ошибки недоступности API
        logging.critical(f"Критическая ошибка: {str(e)}")
        error_msg = (
            "⚠️ **Критическая ошибка!**\n\n"
            "Все мои серверы временно недоступны.\n"
            "Попробуйте через 5-10 минут, пока я:\n"
            "- Восстанавливаю соединение\n"
            "- Проверяю настройки\n"
            "- Контактирую с провайдерами\n\n"
            "Технические детали для админов:\n"
            f"`{str(e)[:500]}`"
        )
        await message.reply(error_msg[:2000])
    except Exception as e:
        logging.error(f"Общая ошибка: {str(e)}\n{traceback.format_exc()}")
        fallback = random.choice([
            "⚠️ Что-то пошло не так... Попробуй еще раз",
            "⚠️ Временная ошибка! Давай позже",
            "⚠️ Серверы временно недоступны... Попробуй через пару минут"
        ])
        update_history(channel_id, "assistant", fallback)
        await message.reply(fallback)

if __name__ == "__main__":
    if not DISCORD_TOKEN or not API_KEY:
        logging.critical("❌ DISCORD_TOKEN или API_KEY не установлены!")
        exit(1)
        
    bot.run(DISCORD_TOKEN, reconnect=True)