import re
import time
import urllib
import sqlite3
import json
import logging
import threading
import os
import asyncio

from datetime import datetime, timedelta
from functools import wraps
from vkbottle import Keyboard, Callback, KeyboardButtonColor, GroupEventType, GroupTypes, API, Text, User
from vkbottle.bot import Bot, Message, rules
from config import Config

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Уровни прав пользователей
PERMISSION_LEVELS = {
    'ZERO': 0,      # Обычный пользователь
    'ONE': 1, # Модератор (может использовать основные модераторские команды)
    'TWO': 2,     # Администратор (расширенные права)
    'THREE': 3,     # Владелец (почти полные права)
    'FOUR': 4     #Разработчик (полные права)
}

# Глобальные переменные
vk_token = Config.vk_token
bot = None
api = None
database = None
sql = None
bot_running = True

# Система регистрации команд
commands = {}

def register_command(command_names, permission_level=PERMISSION_LEVELS['ZERO']):
    def decorator(func):
        for cmd in command_names:
            commands[cmd.lower()] = (func, permission_level)  # Приводим к нижнему регистру
        return func
    return decorator

# Инициализация бота и базы данных
def initialize_bot():
    global bot, api, database, sql
    
    try:
        logger.info("Попытка инициализации бота с токеном: %s", vk_token[:10] + "..." if vk_token else "None")
        bot = Bot(token=vk_token)
        api = API(vk_token)
        logger.info("Бот и API успешно инициализированы")
    except Exception as e:
        logger.error(f"Ошибка при инициализации бота: {e}")
        exit(1)

    try:
        database = sqlite3.connect("database.db", check_same_thread=False)
        sql = database.cursor()
        logger.info("База данных успешно подключена")
    except Exception as e:
        logger.error(f"Ошибка при подключении к базе данных: {e}")
        exit(1)
    
    # Инициализация таблиц
    init_db()

def init_db():
    """Инициализация таблиц базы данных"""
    try:
        sql.execute('''
            CREATE TABLE IF NOT EXISTS warns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reason TEXT,
                warned_by INTEGER NOT NULL,
                warned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                active INTEGER DEFAULT 1  -- 1 - активное предупреждение, 0 - снятое
            )
        ''')
# В функции init_db() измените создание таблицы chats:
        sql.execute('''
            CREATE TABLE IF NOT EXISTS bans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                end_time INTEGER,  -- NULL означает бессрочный бан
                reason TEXT,
                banned_by INTEGER NOT NULL,
                banned_at INTEGER DEFAULT (strftime('%s', 'now')),  -- UNIX timestamp
                UNIQUE(chat_id, user_id)
            )
        ''')

        sql.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                peer_id INTEGER,
                owner_id INTEGER,
                silence INTEGER DEFAULT 0,
                welcome_message TEXT DEFAULT 'Добро пожаловать в беседу!',
                leave_kick INTEGER DEFAULT 1,  -- Новое поле: 1 - включено, 0 - выключено
                settings TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        sql.execute('''
            CREATE TABLE IF NOT EXISTS mutes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                end_time INTEGER NOT NULL,
                reason TEXT,
                UNIQUE(chat_id, user_id)
            )
        ''')
        
        sql.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER,
                chat_id INTEGER,
                permission_level INTEGER DEFAULT 0,
                nick TEXT,
                PRIMARY KEY (user_id, chat_id)
            )
        """)
        
        sql.execute("""
            CREATE TABLE IF NOT EXISTS devs (
                user_id INTEGER,
                chat_id INTEGER,
                previous_level INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, chat_id)
            )
        """)
        
        # Новая таблица для сообщений
        sql.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                chat_id INTEGER,
                user_id INTEGER,
                cmid INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, cmid)
            )
        """)
        
        database.commit()
        logger.info("Таблицы базы данных инициализированы")
    except Exception as e:
        logger.error(f"Ошибка при инициализации базы данных: {e}")


def format_time(seconds: int) -> str:
    """Форматирует время в читаемый вид"""
    minutes = seconds // 60
    hours = minutes // 60
    days = hours // 24

    if days > 0:
        return f"{days} д. {hours % 24} ч."
    elif hours > 0:
        return f"{hours} ч. {minutes % 60} мин."
    else:
        return f"{minutes} мин."
    
# Утилитные функции
async def kick_user(peer_id: int, user_id: int, reason: str = None) -> bool:
    """
    Универсальная функция для кика пользователя из беседы
    :param peer_id: ID беседы (в формате peer_id)
    :param user_id: ID пользователя для кика
    :param reason: Причина кика (опционально)
    :return: True если кик успешен, False в случае ошибки
    """
    try:
        # Преобразуем peer_id в chat_id (убираем 2000000000)
        chat_id_for_api = peer_id - 2000000000
        
        # Кикаем пользователя
        await bot.api.messages.remove_chat_user(
            chat_id=chat_id_for_api,
            user_id=user_id
        )
        
        logger.info(f"Пользователь {user_id} успешно кикнут из беседы {peer_id}. Причина: {reason}")
        return True
        
    except Exception as e:
        error_msg = str(e).lower()
        
        if "permissions" in error_msg:
            logger.error(f"Недостаточно прав для кика пользователя {user_id} из беседы {peer_id}")
        elif "not found" in error_msg:
            logger.error(f"Пользователь {user_id} или беседа {peer_id} не найдены")
        elif "kick yourself" in error_msg:
            logger.error(f"Попытка кикнуть самого себя (пользователь {user_id})")
        elif "user not in chat" in error_msg:
            logger.error(f"Пользователь {user_id} не находится в беседе {peer_id}")
        else:
            logger.error(f"Неизвестная ошибка при кике пользователя {user_id}: {e}")
        
        return False

async def check_ban_and_kick(message: Message, user_id: int):
    logger.info(f"Проверка бана для пользователя {user_id} в чате {message.chat_id}")
    
    chat_id = message.chat_id
    peer_id = message.peer_id
    
    # Проверяем, активирован ли бот в этом чате
    if not await check_chat(chat_id):
        logger.info(f"Бот не активирован в чате {chat_id}, пропускаем проверку бана")
        return
    
    current_time = int(time.time())
    
    # Проверяем, есть ли запись о бане для этого пользователя
    sql.execute("""
        SELECT end_time, reason, banned_by, banned_at 
        FROM bans 
        WHERE chat_id = ? AND user_id = ? AND (end_time IS NULL OR end_time > ?)
    """, (chat_id, user_id, current_time))
    ban_result = sql.fetchone()
    
    if ban_result:
        logger.info(f"Пользователь {user_id} забанен в чате {chat_id}")
        end_time, reason, banned_by, banned_at = ban_result
        
        # Преобразуем banned_at в datetime объект
        try:
            # Если banned_at - это timestamp (число)
            banned_at_dt = datetime.fromtimestamp(int(banned_at))
        except (ValueError, TypeError):
            # Если banned_at - это строка в формате SQLite
            try:
                banned_at_dt = datetime.strptime(banned_at, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                # Если не удалось распарсить, используем текущее время
                banned_at_dt = datetime.now()
        
        banned_at_str = banned_at_dt.strftime("%Y-%m-%d %H:%M:%S")
        
        # Получаем информацию о том, кто забанил
        banned_by_mention = await get_user_mention(banned_by, chat_id)
        target_mention = await get_user_mention(user_id, chat_id)
        
        # Формируем сообщение
        ban_message = (
            f"🚫 {target_mention} был забанен и не может присоединиться к беседе.\n"
            f"Забанен: {banned_by_mention}\n"
            f"Время бана: {banned_at_str}\n"
        )
        if end_time is None:
            ban_message += "Срок: навсегда\n"
        else:
            try:
                end_time_dt = datetime.fromtimestamp(int(end_time))
                end_time_str = end_time_dt.strftime("%Y-%m-%d %H:%M:%S")
                ban_message += f"Разбан: {end_time_str}\n"
            except (ValueError, TypeError):
                ban_message += "Срок: навсегда\n"
        
        if reason:
            ban_message += f"Причина: {reason}"
        
        # Кикаем пользователя с помощью универсальной функции
        kick_success = await kick_user(peer_id, user_id, f"Автоматический кик забаненного пользователя: {reason}")
        
        # Отправляем сообщение о блокировке (не как ответ на сервисное сообщение)
        try:
            await bot.api.messages.send(
                peer_id=peer_id,
                message=ban_message,
                random_id=0
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения о бане: {e}")
    else:
        logger.info(f"Пользователь {user_id} не забанен в чате {chat_id}")
        # Отправляем приветственное сообщение для незабаненного пользователя
        try:
            welcome_message = await get_welcome_message(chat_id)
            user_mention = await get_user_mention_name(user_id, chat_id)
            
            # Заменяем плейсхолдеры в приветственном сообщении
            formatted_message = welcome_message.replace("{user}", user_mention)
            
            await bot.api.messages.send(
                peer_id=peer_id,
                message=formatted_message,
                random_id=0
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке приветственного сообщения: {e}")

async def delete_messages(peer_id: int, cmids: list, group_id: int = None) -> bool:
    """
    Удаляет сообщения в беседе
    :param peer_id: ID беседы
    :param cmids: Список ID сообщений для удаления
    :param group_id: ID группы бота (если не указан, будет получен автоматически)
    :return: True если удаление прошло успешно, False в случае ошибки
    """
    try:
        # Если group_id не указан, получаем его
        if group_id is None:
            group_info = await bot.api.groups.get_by_id()
            group_id = group_info.groups[0].id
        
        # Удаляем сообщения
        await bot.api.messages.delete(
            group_id=group_id,
            peer_id=peer_id,
            delete_for_all=1,
            cmids=cmids
        )
        return True
    except Exception as e:
        logger.error(f"Ошибка при удалении сообщений: {e}")
        return False

async def is_global_developer(user_id: int) -> bool:
    """Проверяет, является ли пользователь глобальным разработчиком"""
    try:
        # Проверяем наличие пользователя в таблице devs (любая запись)
        sql.execute("SELECT * FROM devs WHERE user_id = ? LIMIT 1", (user_id,))
        return sql.fetchone() is not None
    except Exception as e:
        logger.error(f"Ошибка при проверке глобального разработчика {user_id}: {e}")
        return False

async def get_developer_previous_level(user_id: int, chat_id: int) -> int:
    """Получает предыдущий уровень прав разработчика в беседе"""
    try:
        sql.execute("SELECT previous_level FROM devs WHERE user_id = ? AND chat_id = ?", 
                   (user_id, chat_id))
        result = sql.fetchone()
        return result[0] if result else PERMISSION_LEVELS['ZERO']
    except Exception as e:
        logger.error(f"Ошибка при получении предыдущего уровня разработчика {user_id}: {e}")
        return PERMISSION_LEVELS['ZERO']

async def set_developer_previous_level(user_id: int, chat_id: int, level: int) -> bool:
    """Устанавливает предыдущий уровень прав разработчика в беседе"""
    try:
        sql.execute(
            """INSERT OR REPLACE INTO devs (user_id, chat_id, previous_level) 
               VALUES (?, ?, ?)""",
            (user_id, chat_id, level)
        )
        database.commit()
        return True
    except Exception as e:
        logger.error(f"Ошибка при установке предыдущего уровня разработчика {user_id}: {e}")
        return False

async def remove_developer(user_id: int, chat_id: int) -> bool:
    """Удаляет разработчика из беседы"""
    try:
        sql.execute("DELETE FROM devs WHERE user_id = ? AND chat_id = ?", 
                   (user_id, chat_id))
        database.commit()
        return True
    except Exception as e:
        logger.error(f"Ошибка при удалении разработчика {user_id}: {e}")
        return False

async def get_user_nick(user_id: int, chat_id: int) -> str:
    """Получение ника пользователя"""
    try:
        sql.execute("SELECT nick FROM users WHERE user_id = ? AND chat_id = ?", 
                   (user_id, chat_id))
        result = sql.fetchone()
        return result[0] if result else None
    except Exception as e:
        logger.error(f"Ошибка при получении ника пользователя {user_id}: {e}")
        return None
    
async def set_user_nick(user_id: int, chat_id: int, nick: str) -> bool:
    """Установка ника пользователю без изменения прав"""
    try:
        # Сначала проверяем, существует ли уже запись для этого пользователя
        sql.execute("SELECT permission_level FROM users WHERE user_id = ? AND chat_id = ?", 
                   (user_id, chat_id))
        result = sql.fetchone()
        
        if result:
            # Если запись существует, обновляем только ник
            sql.execute(
                "UPDATE users SET nick = ? WHERE user_id = ? AND chat_id = ?",
                (nick, user_id, chat_id)
            )
        else:
            # Если записи нет, создаем новую с уровнем прав по умолчанию (0) и ником
            sql.execute(
                "INSERT INTO users (user_id, chat_id, permission_level, nick) VALUES (?, ?, ?, ?)",
                (user_id, chat_id, 0, nick)
            )
        
        database.commit()
        return True
    except Exception as e:
        logger.error(f"Ошибка при установке ника пользователю {user_id}: {e}")
        return False

async def remove_user_nick(user_id: int, chat_id: int) -> bool:
    """Удаление ника пользователя"""
    try:
        sql.execute(
            "UPDATE users SET nick = NULL WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id)
        )
        database.commit()
        return True
    except Exception as e:
        logger.error(f"Ошибка при удалении ника пользователя {user_id}: {e}")
        return False

async def get_all_nicks(chat_id: int) -> list:
    """Получение всех ников в чате"""
    try:
        sql.execute("SELECT user_id, nick FROM users WHERE chat_id = ? AND nick IS NOT NULL", 
                   (chat_id,))
        return sql.fetchall()
    except Exception as e:
        logger.error(f"Ошибка при получении списка ников: {e}")
        return []

def console_listener():
    """Прослушивает команды из консоли для управления ботом"""
    while True:
        try:
            command = input().lower().strip()
            if command in ['с', 'stop', 'exit', 'quit']:
                logger.info("Получена команда остановки из консоли")
                # Закрываем соединение с базой данных
                if database:
                    database.close()
                # Выходим из программы
                os._exit(0)
        except Exception as e:
            logger.error(f"Ошибка в консольном слушателе: {e}")
            break

async def check_chat(chat_id):
    """Проверка, зарегистрирован ли чат в базе"""
    try:
        sql.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
        return sql.fetchone() is not None
    except Exception as e:
        logger.error(f"Ошибка при проверке чата {chat_id}: {e}")
        return False

async def get_user_permission(user_id, chat_id):
    """Получение уровня прав пользователя"""
    try:
        sql.execute("SELECT permission_level FROM users WHERE user_id = ? AND chat_id = ?", 
                   (user_id, chat_id))
        result = sql.fetchone()
        return result[0] if result else 0
    except Exception as e:
        logger.error(f"Ошибка при получении прав пользователя {user_id}: {e}")
        return 0

async def set_user_permission(user_id, chat_id, level):
    """Установка уровня прав пользователя с сохранением ника и обработкой разработчиков"""
    try:
        # Сначала получаем текущий ник пользователя (если есть)
        current_nick = await get_user_nick(user_id, chat_id)
        
        # Проверяем, существует ли запись
        sql.execute("SELECT * FROM users WHERE user_id = ? AND chat_id = ?", 
                   (user_id, chat_id))
        result = sql.fetchone()
        
        if result:
            # Если запись существует, обновляем только уровень прав
            sql.execute(
                "UPDATE users SET permission_level = ? WHERE user_id = ? AND chat_id = ?",
                (level, user_id, chat_id)
            )
        else:
            # Если записи нет, создаем новую
            if current_nick:
                sql.execute(
                    "INSERT INTO users (user_id, chat_id, permission_level, nick) VALUES (?, ?, ?, ?)",
                    (user_id, chat_id, level, current_nick)
                )
            else:
                sql.execute(
                    "INSERT INTO users (user_id, chat_id, permission_level) VALUES (?, ?, ?)",
                    (user_id, chat_id, level)
                )
        
        # Если пользователь является разработчиком и устанавливается не уровень 4,
        # обновляем предыдущий уровень в таблице devs
        if await is_global_developer(user_id) and level != PERMISSION_LEVELS['FOUR']:
            await set_developer_previous_level(user_id, chat_id, level)
        
        database.commit()
        return True
    except Exception as e:
        logger.error(f"Ошибка при установке прав пользователя {user_id}: {e}")
        return False
        return False
    
async def can_manage_user(initiator_id: int, target_id: int, chat_id: int, allow_self_action: bool = False) -> bool:
    """
    Проверяет, может ли пользователь управлять другим пользователем
    initiator_id - кто пытается выполнить действие
    target_id - над кем выполняется действие
    chat_id - ID чата
    allow_self_action - разрешить ли действие над самим собой
    """
    if initiator_id == target_id:
        return allow_self_action
    
    initiator_level = await get_user_permission(initiator_id, chat_id)
    target_level = await get_user_permission(target_id, chat_id)
    
    return initiator_level > target_level

async def extract_user_id(identifier: str, message: Message) -> int:
    """
    Извлекает ID пользователя или группы из различных форматов
    """
    if bot is None:
        logger.error("Бот не инициализирован")
        return None
        
    try:
        # Убираем возможные пробелы
        identifier = identifier.strip()
        
        # Если это числовой ID (может быть отрицательным для групп)
        if identifier.lstrip('-').isdigit():
            return int(identifier)
        
        # Если это упоминание в формате [id123|Name] или [club123|Name]
        mention_match = re.search(r'\[(id|club|public)(\d+)\|', identifier)
        if mention_match:
            entity_id = int(mention_match.group(2))
            # Для групп возвращаем отрицательный ID
            if mention_match.group(1) == 'id':
                return entity_id
            else:
                return -abs(entity_id)  # Гарантируем отрицательный ID для групп
        
        # Если это ссылка на профиль VK или группу
        vk_link_match = re.match(r'^(https?://)?(www\.)?vk\.com/(?P<username>[a-zA-Z0-9_\.]+)/?$', identifier)
        if vk_link_match:
            username = vk_link_match.group('username')
            
            # Если это группа (начинается с club или public)
            if username.startswith(('club', 'public')):
                try:
                    group_id = int(username[4:] if username.startswith('club') else username[6:])
                    return -abs(group_id)  # Гарантируем отрицательный ID для групп
                except ValueError:
                    pass
            # Если это пользователь
            else:
                try:
                    users = await bot.api.users.get(user_ids=username)
                    if users:
                        return users[0].id
                except Exception:
                    pass
        
        # Если это @username или @club123
        if identifier.startswith('@'):
            username = identifier[1:].strip()
            if username.startswith(('club', 'public')):
                try:
                    group_id = int(username[4:] if username.startswith('club') else username[6:])
                    return -abs(group_id)  # Гарантируем отрицательный ID для групп
                except ValueError:
                    pass
            else:
                try:
                    users = await bot.api.users.get(user_ids=username)
                    if users:
                        return users[0].id
                except Exception:
                    pass
    
    except Exception as e:
        logger.error(f"Ошибка при извлечении ID пользователя/группы: {e}")
    
    return None

async def get_user_mention(user_id: int, chat_id: int) -> str:
    """
    Возвращает упоминание пользователя с учетом ника
    """
    # Сначала проверяем, есть ли ник у пользователя
    nick = await get_user_nick(user_id, chat_id)
    if nick:
        return f"[id{user_id}|{nick}]"
    
    # Если ника нет, получаем имя и фамилию через API
    try:
        users = await bot.api.users.get(user_ids=user_id)
        if users:
            user = users[0]
            return f"[id{user_id}|{user.first_name} {user.last_name}]"
    except Exception as e:
        logger.error(f"Ошибка при получении информации о пользователе {user_id}: {e}")
    
    return f"[id{user_id}|Пользователь]"


async def get_user_mention_name(user_id: int, chat_id: int) -> str:
    """
    Возвращает упоминание пользователя с учетом ника
    """
    # Сначала проверяем, есть ли ник у пользователя
    nick = await get_user_nick(user_id, chat_id)
    if nick:
        return f"[id{user_id}|{nick}]"
    
    # Если ника нет, получаем имя и фамилию через API
    try:
        users = await bot.api.users.get(user_ids=user_id)
        if users:
            user = users[0]
            return f"[id{user_id}|{user.first_name}]"
    except Exception as e:
        logger.error(f"Ошибка при получении информации о пользователе {user_id}: {e}")
    
    return f"[id{user_id}|Пользователь]"

async def get_staff_members(chat_id: int) -> dict:
    """Получает участников с правами в беседе, сгруппированных по уровням"""
    try:
        sql.execute("""
            SELECT user_id, permission_level, nick 
            FROM users 
            WHERE chat_id = ? AND permission_level > 0 
            ORDER BY permission_level DESC
        """, (chat_id,))
        
        staff_members = {}
        for user_id, level, nick in sql.fetchall():
            if level not in staff_members:
                staff_members[level] = []
            staff_members[level].append((user_id, nick))
        
        return staff_members
    except Exception as e:
        logger.error(f"Ошибка при получении участников с правами: {e}")
        return {}
    
async def get_welcome_message(chat_id: int) -> str:
    """Получает приветственное сообщение для чата"""
    try:
        sql.execute("SELECT welcome_message FROM chats WHERE chat_id = ?", (chat_id,))
        result = sql.fetchone()
        return result[0] if result and result[0] else "Добро пожаловать в беседу!"
    except Exception as e:
        logger.error(f"Ошибка при получении приветственного сообщения: {e}")
        return "Добро пожаловать в беседу!"



#Команды

@register_command(['/help', '!help', '/помощь', '!помощь'])
async def help_command(message, args):
    """Показывает список доступных команд"""
    help_text = """
📋 Доступные команды:

👤 Для всех (уровень 0):
/help - Показать эту справку
/id  - Показать ID пользователя

⚙️ Для модераторов (уровень 1):
/kick  - Исключить пользователя или группу из беседы
/setnick  - Установить ник пользователю
/nicklist - Показать список ников в беседе
/removenick  - Удалить ник пользователя
/staff - Показать участников с правами в беседе
/clear - Удалить сообщение
/warn - Выдать предупреждение пользователю
/unwarn - Снять предупреждение с пользователя
/warnlist - Показать активные предупреждения в беседе
/warnhistory - Показать историю предупреждений пользователя

👑 Для администраторов (уровень 2):
/moder  - Выдать права модератора
/removerole  - Снять все права с пользователя
/ban - Забанить пользователя
/unban - Разбанить пользователя

🛠️ Для владельцев (уровень 3):
/admin - Выдать права администратора
/setwelcome - Установить приветственное сообщение
/leavekick - Включить/выключить автоматический кик при выходе
"""
    await message.reply(help_text)

@register_command(['/start', '!start', '/старт', '!старт', '/активировать', '!активировать'])
async def start_command(message, args):
    """Активация бота в беседе"""
    user_id = message.from_id
    chat_id = message.chat_id
    peer_id = message.peer_id

    # Проверка прав пользователя (только создатель беседы может активировать бота)
    try:
        chat_info = await bot.api.messages.get_conversations_by_id(peer_ids=peer_id)
        if not chat_info.items or chat_info.items[0].chat_settings.owner_id != user_id:
            await message.reply("❌ Только создатель беседы может активировать бота!")
            return
    except Exception as e:
        logger.error(f"Ошибка при проверке создателя беседы: {e}")
        await message.reply("❌ Ошибка при проверке прав. Убедитесь, что бот имеет права администратора.")
        return
    
    # Проверка, активирован ли уже бот
    if await check_chat(chat_id):
        await message.reply("✅ Бот уже активирован в этой беседе!")
        return
    
    # Активация бота
    try:
        sql.execute("INSERT INTO chats (chat_id, peer_id, owner_id, silence, welcome_message, leave_kick) VALUES (?, ?, ?, 0, 'Добро пожаловать в беседу!', 1)",
                   (chat_id, peer_id, user_id))
        
        # Получаем информацию о пользователе для установки ника
        try:
            users = await bot.api.users.get(user_ids=user_id)
            if users:
                user = users[0]
                nick = f"{user.first_name} {user.last_name}"
                sql.execute("INSERT INTO users (user_id, chat_id, permission_level, nick) VALUES (?, ?, ?, ?)",
                           (user_id, chat_id, PERMISSION_LEVELS['THREE'], nick))
            else:
                sql.execute("INSERT INTO users (user_id, chat_id, permission_level) VALUES (?, ?, ?)",
                           (user_id, chat_id, PERMISSION_LEVELS['THREE']))
        except Exception:
            sql.execute("INSERT INTO users (user_id, chat_id, permission_level) VALUES (?, ?, ?)",
                       (user_id, chat_id, PERMISSION_LEVELS['THREE']))
        
        database.commit()
        
        await message.reply("✅ Бот успешно активирован!\n\nДля просмотра доступных команд напишите /help")
    except Exception as e:
        logger.error(f"Ошибка при активации бота: {e}")
        await message.reply("❌ Произошла ошибка при активации бота. Попробуйте позже.")

@register_command(['/warn', '!warn', '/варн', '!варн'], permission_level=PERMISSION_LEVELS['ONE'])
async def warn_command(message, args):
    """Выдать предупреждение пользователю"""
    user_id = message.from_id
    chat_id = message.chat_id
    
    if not await check_chat(chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return

    # Определяем целевого пользователя и причину
    target_id = None
    reason = None
    
    # Если это ответ на сообщение
    if message.reply_message:
        target_id = message.reply_message.from_id
        reason = ' '.join(args) if args else "Причина не указана"
    # Если указан пользователь в аргументах
    elif args and len(args) >= 1:
        target_id = await extract_user_id(args[0], message)
        if not target_id or target_id < 0:
            await message.reply("❌ Не удалось распознать пользователя. Укажите @упоминание, ссылку на профиль VK или цифровой ID.")
            return
        
        # Остальные аргументы - причина
        reason = ' '.join(args[1:]) if len(args) > 1 else "Причина не указана"
    else:
        await message.reply("❌ Неправильный формат команды. Используйте: /warn [пользователь] [причина] или ответьте на сообщение пользователя с командой /warn [причина]")
        return

    # Проверяем, что пользователь не пытается выдать предупреждение себе
    if target_id == user_id:
        await message.reply("❌ Нельзя выдать предупреждение самому себе.")
        return

    # Проверяем, может ли пользователь управлять целевым пользователем
    if not await can_manage_user(user_id, target_id, chat_id, allow_self_action=False):
        await message.reply("❌ Недостаточно прав для выдачи предупреждения этому пользователю.")
        return

    # Получаем упоминания
    initiator_mention = await get_user_mention(user_id, chat_id)
    target_mention = await get_user_mention(target_id, chat_id)

    try:
        # Добавляем предупреждение в базу данных
        sql.execute(
            "INSERT INTO warns (chat_id, user_id, reason, warned_by, active) VALUES (?, ?, ?, ?, 1)",
            (chat_id, target_id, reason, user_id)
        )
        database.commit()
        
        # Получаем количество активных предупреждений пользователя
        sql.execute("SELECT COUNT(*) FROM warns WHERE chat_id = ? AND user_id = ? AND active = 1", 
                   (chat_id, target_id))
        warn_count = sql.fetchone()[0]
        
        # Формируем сообщение об успехе
        success_message = f"⚠️ {initiator_mention} выдал(а) предупреждение {target_mention}.\nВсего предупреждений: {warn_count}/3"
        if reason and reason != "Причина не указана":
            success_message += f"\nПричина: {reason}"
        
        # Проверяем, нужно ли кикнуть пользователя (3 и более предупреждений)
        if warn_count >= 3:
            success_message += f"\n\n🚫 {target_mention} получил(а) 3 или более предупреждений и будет исключен(а) из беседы."
            
            # Кикаем пользователя
            kick_success = await kick_user(message.peer_id, target_id, f"Получено {warn_count} предупреждений")
            
            # Снимаем все активные предупреждения пользователя (без уведомления в чат)
            if kick_success:
                sql.execute("UPDATE warns SET active = 0 WHERE chat_id = ? AND user_id = ? AND active = 1",
                           (chat_id, target_id))
                database.commit()
                logger.info(f"Сняты все предупреждения пользователя {target_id} после автоматического кика")
            else:
                success_message += "\n⚠️ Не удалось исключить пользователя из беседы."
        
        # Отправляем подтверждение
        await message.reply(success_message)
            
    except Exception as e:
        logger.error(f"Ошибка при выдаче предупреждения: {e}")
        await message.reply("❌ Произошла ошибка при выдаче предупреждения.")

@register_command(['/unwarn', '!unwarn', '/снятьварн', '!снятьварн'], permission_level=PERMISSION_LEVELS['ONE'])
async def unwarn_command(message, args):
    """Снять предупреждение с пользователя"""
    user_id = message.from_id
    chat_id = message.chat_id
    
    if not await check_chat(chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return

    # Определяем целевого пользователя
    target_id = None
    
    # Если это ответ на сообщение
    if message.reply_message:
        target_id = message.reply_message.from_id
    # Если указан пользователь в аргументах
    elif args:
        target_id = await extract_user_id(args[0], message)
        if not target_id or target_id < 0:
            await message.reply("❌ Не удалось распознать пользователя. Укажите @упоминание, ссылку на профиль VK или цифровой ID.")
            return
    else:
        await message.reply("❌ Неправильный формат команды. Используйте: /unwarn [пользователь] или ответьте на сообщение пользователя с командой /unwarn")
        return

    # Проверяем, может ли пользователь управлять целевым пользователем (разрешаем действие над собой)
    if not await can_manage_user(user_id, target_id, chat_id, allow_self_action=True):
        await message.reply("❌ Недостаточно прав для снятия предупреждения этому пользователю.")
        return

    # Получаем упоминания
    initiator_mention = await get_user_mention(user_id, chat_id)
    target_mention = await get_user_mention(target_id, chat_id)

    try:
        # Получаем последнее активное предупреждение пользователя
        sql.execute("""
            SELECT id FROM warns 
            WHERE chat_id = ? AND user_id = ? AND active = 1 
            ORDER BY warned_at DESC LIMIT 1
        """, (chat_id, target_id))
        warn_result = sql.fetchone()
        
        if not warn_result:
            await message.reply(f"❌ У {target_mention} нет активных предупреждений.")
            return
        
        warn_id = warn_result[0]
        
        # Деактивируем предупреждение
        sql.execute("UPDATE warns SET active = 0 WHERE id = ?", (warn_id,))
        database.commit()
        
        # Получаем новое количество активных предупреждений
        sql.execute("SELECT COUNT(*) FROM warns WHERE chat_id = ? AND user_id = ? AND active = 1", 
                   (chat_id, target_id))
        warn_count = sql.fetchone()[0]
        
        await message.reply(f"✅ {initiator_mention} снял(а) предупреждение с {target_mention}.\nОсталось предупреждений: {warn_count}/3")
            
    except Exception as e:
        logger.error(f"Ошибка при снятии предупреждения: {e}")
        await message.reply("❌ Произошла ошибка при снятии предупреждения.")

@register_command(['/warnlist', '!warnlist', '/списокварнов', '!списокварнов'], permission_level=PERMISSION_LEVELS['ONE'])
async def warn_list_command(message, args):
    """Показать все активные предупреждения в беседе с подробной информацией"""
    chat_id = message.chat_id
    
    if not await check_chat(chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return

    try:
        # Получаем все активные предупреждения с подробной информацией
        sql.execute("""
            SELECT w.user_id, w.reason, w.warned_by, w.warned_at, u.nick
            FROM warns w
            LEFT JOIN users u ON w.user_id = u.user_id AND w.chat_id = u.chat_id
            WHERE w.chat_id = ? AND w.active = 1 
            ORDER BY w.warned_at DESC
        """, (chat_id,))
        warn_results = sql.fetchall()
        
        if not warn_results:
            await message.reply("📝 В этой беседе нет активных предупреждений.")
            return
        
        # Группируем предупреждения по пользователям
        user_warns = {}
        for user_id, reason, warned_by, warned_at, nick in warn_results:
            if user_id not in user_warns:
                user_warns[user_id] = []
            user_warns[user_id].append((reason, warned_by, warned_at, nick))
        
        # Формируем список предупреждений
        warn_list = []
        for user_id, warns in user_warns.items():
            user_mention = await get_user_mention(user_id, chat_id)
            warn_list.append(f"👤 {user_mention} - {len(warns)} предупреждений:")
            
            for i, (reason, warned_by, warned_at, nick) in enumerate(warns, 1):
                warned_by_mention = await get_user_mention(warned_by, chat_id)
                warned_at_str = datetime.fromtimestamp(warned_at).strftime("%Y-%m-%d %H:%M") if isinstance(warned_at, (int, float)) else str(warned_at)
                
                warn_list.append(f"   {i}. Выдал: {warned_by_mention}")
                warn_list.append(f"      Время: {warned_at_str}")
                if reason:
                    warn_list.append(f"      Причина: {reason}")
                warn_list.append("")  # Пустая строка для разделения
        
        # Разбиваем список на части, чтобы сообщение не было слишком длинным
        chunk_size = 15
        chunks = [warn_list[i:i + chunk_size] for i in range(0, len(warn_list), chunk_size)]
        
        for i, chunk in enumerate(chunks):
            header = "📝 Активные предупреждения в беседе:\n\n" if i == 0 else ""
            await message.reply(header + "\n".join(chunk))
            
    except Exception as e:
        logger.error(f"Ошибка при получении списка предупреждений: {e}")
        await message.reply("❌ Произошла ошибка при получении списка предупреждений.")

@register_command(['/warnhistory', '!warnhistory', '/историяварнов', '!историяварнов'], permission_level=PERMISSION_LEVELS['ONE'])
async def warn_history_command(message, args):
    """Показать историю предупреждений пользователя (последние 10)"""
    chat_id = message.chat_id
    
    if not await check_chat(chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return

    # Определяем целевого пользователя
    target_id = None
    
    # Если это ответ на сообщение
    if message.reply_message:
        target_id = message.reply_message.from_id
    # Если указан пользователь в аргументах
    elif args:
        target_id = await extract_user_id(args[0], message)
        if not target_id or target_id < 0:
            await message.reply("❌ Не удалось распознать пользователя. Укажите @упоминание, ссылку на профиль VK или цифровой ID.")
            return
    else:
        await message.reply("❌ Неправильный формат команды. Используйте: /warnhistory [пользователь] или ответьте на сообщение пользователя с командой /warnhistory")
        return

    # Получаем информацию о целевом пользователе
    target_mention = await get_user_mention(target_id, chat_id)

    try:
        # Получаем последние 10 предупреждений пользователя
        sql.execute("""
            SELECT reason, warned_by, warned_at, active 
            FROM warns 
            WHERE chat_id = ? AND user_id = ? 
            ORDER BY warned_at DESC 
            LIMIT 10
        """, (chat_id, target_id))
        warn_results = sql.fetchall()
        
        if not warn_results:
            await message.reply(f"📝 У {target_mention} нет истории предупреждений.")
            return
        
        # Формируем сообщение
        history_message = f"📋 История предупреждений {target_mention} (последние 10):\n\n"
        
        active_count = 0
        inactive_count = 0
        
        for i, (reason, warned_by, warned_at, active) in enumerate(warn_results, 1):
            status = "✅" if not active else "⚠️"
            status_text = "Снято" if not active else "Активно"
            
            # Получаем информацию о том, кто выдал предупреждение
            warned_by_mention = await get_user_mention(warned_by, chat_id)
            
            # Форматируем время
            warned_at_str = datetime.fromtimestamp(warned_at).strftime("%Y-%m-%d %H:%M") if isinstance(warned_at, (int, float)) else str(warned_at)
            
            history_message += f"{i}. {status} {status_text}\n"
            history_message += f"   Выдал: {warned_by_mention}\n"
            history_message += f"   Время: {warned_at_str}\n"
            if reason:
                history_message += f"   Причина: {reason}\n"
            history_message += "\n"
            
            if active:
                active_count += 1
            else:
                inactive_count += 1
        
        history_message += f"📊 Статистика:\n"
        history_message += f"   Активные: {active_count}\n"
        history_message += f"   Снятые: {inactive_count}\n"
        history_message += f"   Всего: {active_count + inactive_count}"
        
        # Разбиваем сообщение на части, если оно слишком длинное
        if len(history_message) > 4096:
            parts = [history_message[i:i+4096] for i in range(0, len(history_message), 4096)]
            for part in parts:
                await message.reply(part)
        else:
            await message.reply(history_message)
            
    except Exception as e:
        logger.error(f"Ошибка при получении истории предупреждений: {e}")
        await message.reply("❌ Произошла ошибка при получении истории предупреждений.")

@register_command(['/leavekick', '!leavekick'], permission_level=PERMISSION_LEVELS['THREE'])
async def leave_kick_command(message, args):
    """Включить/выключить автоматический кик при выходе из беседы"""
    chat_id = message.chat_id
    
    if not await check_chat(chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return

    try:
        # Получаем текущее состояние функции
        sql.execute("SELECT leave_kick FROM chats WHERE chat_id = ?", (chat_id,))
        result = sql.fetchone()
        
        current_leave_kick = result[0] if result else 1
        
        # Переключаем функцию (0 -> 1, 1 -> 0)
        new_leave_kick = 1 - current_leave_kick
        
        # Обновляем значение в базе данных
        sql.execute("UPDATE chats SET leave_kick = ? WHERE chat_id = ?", 
                   (new_leave_kick, chat_id))
        database.commit()
        
        # Получаем упоминание инициатора
        initiator_mention = await get_user_mention(message.from_id, chat_id)
        
        if new_leave_kick == 1:
            await message.reply(f"✅ {initiator_mention} включил(а) функцию автоматического кика при выходе из беседы.")
        else:
            await message.reply(f"✅ {initiator_mention} выключил(а) функцию автоматического кика при выходе из беседы.")
            
    except Exception as e:
        logger.error(f"Ошибка при изменении функции leave_kick: {e}")
        await message.reply("❌ Произошла ошибка при изменении функции.")

@register_command(['/setwelcome', '!setwelcome', '/приветствие', '!приветствие'], permission_level=PERMISSION_LEVELS['THREE'])
async def set_welcome_command(message, args):
    """Установить приветственное сообщение для новых участников"""
    chat_id = message.chat_id
    
    if not await check_chat(chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return
    
    if not args:
        await message.reply("❌ Укажите текст приветственного сообщения.")
        return
    
    welcome_text = ' '.join(args)
    
    try:
        # Обновляем приветственное сообщение в базе данных
        sql.execute("UPDATE chats SET welcome_message = ? WHERE chat_id = ?", 
                   (welcome_text, chat_id))
        database.commit()
        
        # Получаем упоминание инициатора
        initiator_mention = await get_user_mention(message.from_id, chat_id)
        
        await message.reply(f"✅ {initiator_mention} установил(а) новое приветственное сообщение:\n\n{welcome_text}")
        
    except Exception as e:
        logger.error(f"Ошибка при установке приветственного сообщения: {e}")
        await message.reply("❌ Произошла ошибка при установке приветственного сообщения.")

@register_command(['/ban', '!ban', '/бан', '!бан'], permission_level=PERMISSION_LEVELS['TWO'])
async def ban_command(message, args):
    """Забанить пользователя"""
    user_id = message.from_id
    chat_id = message.chat_id
    peer_id = message.peer_id
    
    if not await check_chat(chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return

    # Определяем целевого пользователя
    target_id = None
    ban_time_days = None  # Если None - бессрочный бан
    reason = None
    
    # Если это ответ на сообщение
    if message.reply_message:
        target_id = message.reply_message.from_id
        # Первый аргумент - время, остальные - причина
        if args:
            try:
                ban_time_days = int(args[0])
                reason = ' '.join(args[1:]) if len(args) > 1 else None
            except ValueError:
                await message.reply("❌ Время бана должно быть числом (в днях).")
                return
    # Если указан пользователь в аргументах
    elif args and len(args) >= 1:
        target_id = await extract_user_id(args[0], message)
        if not target_id or target_id < 0:
            await message.reply("❌ Не удалось распознать пользователя. Укажите @упоминание, ссылку на профиль VK или цифровой ID.")
            return
        
        # Второй аргумент - время, остальные - причина
        if len(args) >= 2:
            try:
                ban_time_days = int(args[1])
                reason = ' '.join(args[2:]) if len(args) > 2 else None
            except ValueError:
                await message.reply("❌ Время бана должно быть числом (в днях).")
                return
    else:
        await message.reply("❌ Неправильный формат команды. Используйте: /ban [пользователь] [время_в_днях] [причина] или ответьте на сообщение пользователя с командой /ban [время_в_днях] [причина]")
        return

    # Проверяем, что пользователь не пытается забанить себя
    if target_id == user_id:
        await message.reply("❌ Нельзя забанить самого себя.")
        return

    # Проверяем, может ли пользователь управлять целевым пользователем (если пользователь в беседе)
    target_in_chat = True
    try:
        target_level = await get_user_permission(target_id, chat_id)
    except:
        target_in_chat = False
        target_level = 0

    if target_in_chat and not await can_manage_user(user_id, target_id, chat_id, allow_self_action=False):
        await message.reply("❌ Недостаточно прав для бана этого пользователя.")
        return

    # Получаем упоминания
    initiator_mention = await get_user_mention(user_id, chat_id)
    target_mention = await get_user_mention(target_id, chat_id)

    try:
        # Вычисляем время окончания бана (если указано время)
        end_time = None
        if ban_time_days is not None:
            end_time = int(time.time()) + ban_time_days * 24 * 60 * 60

        # Добавляем бан в базу данных
        sql.execute(
            "INSERT OR REPLACE INTO bans (chat_id, user_id, end_time, reason, banned_by, banned_at) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, target_id, end_time, reason, user_id, int(time.time()))
        )
        database.commit()
        
        # Формируем сообщение об успехе
        if ban_time_days is None:
            time_str = "навсегда"
        else:
            time_str = f"на {ban_time_days} д."
        success_message = f"✅ {initiator_mention} забанил(а) {target_mention} {time_str}."
        if reason:
            success_message += f" Причина: {reason}"
        
        # Отправляем подтверждение
        await message.reply(success_message)
        
        # Если пользователь в беседе, то кикаем его с помощью универсальной функции
        if target_in_chat:
            kick_success = await kick_user(peer_id, target_id, f"Бан: {reason}")
            if not kick_success:
                await message.reply("⚠️ Пользователь забанен, но не удалось его исключить из беседы.")
            
    except Exception as e:
        logger.error(f"Ошибка при бане пользователя: {e}")
        await message.reply("❌ Произошла ошибка при бане пользователя.")

@register_command(['/unban', '!unban', '/разбан', '!разбан'], permission_level=PERMISSION_LEVELS['TWO'])
async def unban_command(message, args):
    """Разбанить пользователя"""
    chat_id = message.chat_id
    
    if not await check_chat(chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return

    # Определяем целевого пользователя
    target_id = None
    
    # Если это ответ на сообщение
    if message.reply_message:
        target_id = message.reply_message.from_id
    # Если указан пользователь в аргументах
    elif args:
        target_id = await extract_user_id(args[0], message)
        if not target_id or target_id < 0:
            await message.reply("❌ Не удалось распознать пользователя. Укажите @упоминание, ссылку на профиль VK или цифровой ID.")
            return
    else:
        await message.reply("❌ Неправильный формат команды. Используйте: /unban [пользователь] или ответьте на сообщение пользователя с командой /unban")
        return

    # Получаем упоминания
    initiator_mention = await get_user_mention(message.from_id, chat_id)
    target_mention = await get_user_mention(target_id, chat_id)

    try:
        # Удаляем бан из базы данных
        sql.execute("DELETE FROM bans WHERE chat_id = ? AND user_id = ?", 
                   (chat_id, target_id))
        database.commit()
        
        # Проверяем, был ли пользователь забанен
        if sql.rowcount > 0:
            await message.reply(f"✅ {initiator_mention} разбанил(а) {target_mention}.")
        else:
            await message.reply(f"❌ {target_mention} не был забанен.")
            
    except Exception as e:
        logger.error(f"Ошибка при разбане пользователя: {e}")
        await message.reply("❌ Произошла ошибка при разбане пользователя.")

@register_command(['/mute', '!mute', '/мут', '!мут'], permission_level=PERMISSION_LEVELS['ONE'])
async def mute_command(message, args):
    """Замутить пользователя"""
    user_id = message.from_id
    chat_id = message.chat_id
    peer_id = message.peer_id
    
    if not await check_chat(chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return

    # Определяем целевого пользователя
    target_id = None
    mute_time = None
    reason = None
    
    # Если это ответ на сообщение
    if message.reply_message:
        target_id = message.reply_message.from_id
        # Первый аргумент - время, остальные - причина
        if args:
            try:
                mute_time = int(args[0])
                reason = ' '.join(args[1:]) if len(args) > 1 else None
            except ValueError:
                await message.reply("❌ Время мута должно быть числом (в минутах).")
                return
        else:
            await message.reply("❌ Укажите время мута в минутах.")
            return
    # Если указан пользователь в аргументах
    elif args and len(args) >= 2:
        target_id = await extract_user_id(args[0], message)
        if not target_id or target_id < 0:
            await message.reply("❌ Не удалось распознать пользователя. Укажите @упоминание, ссылку на профиль VK или цифровой ID.")
            return
        
        try:
            mute_time = int(args[1])
            reason = ' '.join(args[2:]) if len(args) > 2 else None
        except ValueError:
            await message.reply("❌ Время мута должно быть числом (в минутах).")
            return
    else:
        await message.reply("❌ Неправильный формат команды. Используйте: /mute [пользователь] [время_в_минутах] [причина] или ответьте на сообщение пользователя с командой /mute [время_в_минутах] [причина]")
        return

    # Проверяем, что пользователь не пытается замутить себя
    if target_id == user_id:
        await message.reply("❌ Нельзя замутить самого себя.")
        return

    # Проверяем, может ли пользователь управлять целевым пользователем
    if not await can_manage_user(user_id, target_id, chat_id, allow_self_action=False):
        await message.reply("❌ Недостаточно прав для мута этого пользователя.")
        return

    # Получаем упоминания
    initiator_mention = await get_user_mention(user_id, chat_id)
    target_mention = await get_user_mention(target_id, chat_id)

    try:
        # Получаем ID группы бота
        group_info = await bot.api.groups.get_by_id()
        group_id = group_info.groups[0].id
        
        # Вычисляем время окончания мута
        end_time = int(time.time()) + mute_time * 60
        
        # Добавляем мут в базу данных
        sql.execute(
            "INSERT OR REPLACE INTO mutes (chat_id, user_id, end_time, reason) VALUES (?, ?, ?, ?)",
            (chat_id, target_id, end_time, reason)
        )
        database.commit()
        
        # Формируем сообщение об успехе
        time_str = format_time(mute_time * 60)
        success_message = f"✅ {initiator_mention} замутил(а) {target_mention} на {time_str}."
        if reason:
            success_message += f" Причина: {reason}"
        
        # Отправляем подтверждение
        await message.reply(success_message)
    except Exception as e:
        logger.error(f"Ошибка при муте пользователя: {e}")
        await message.reply("❌ Произошла ошибка при муте пользователя.")

@register_command(['/unmute', '!unmute', '/размут', '!размут'], permission_level=PERMISSION_LEVELS['ONE'])
async def unmute_command(message, args):
    """Снять мут с пользователя"""
    user_id = message.from_id
    chat_id = message.chat_id
    
    if not await check_chat(chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return

    # Определяем целевого пользователя
    target_id = None
    
    # Если это ответ на сообщение
    if message.reply_message:
        target_id = message.reply_message.from_id
    # Если указан пользователь в аргументах
    elif args:
        target_id = await extract_user_id(args[0], message)
        if not target_id or target_id < 0:
            await message.reply("❌ Не удалось распознать пользователя. Укажите @упоминание, ссылку на профиль VK или цифровой ID.")
            return
    else:
        await message.reply("❌ Неправильный формат команды. Используйте: /unmute [пользователь] или ответьте на сообщение пользователя с командой /unmute")
        return

    # Проверяем, может ли пользователь управлять целевым пользователем (разрешаем действие над собой)
    if not await can_manage_user(user_id, target_id, chat_id, allow_self_action=True):
        await message.reply("❌ Недостаточно прав для размута этого пользователя.")
        return

    # Получаем упоминания
    initiator_mention = await get_user_mention(user_id, chat_id)
    target_mention = await get_user_mention(target_id, chat_id)

    try:
        # Проверяем, есть ли активный мут у пользователя
        current_time = int(time.time())
        sql.execute("SELECT * FROM mutes WHERE chat_id = ? AND user_id = ? AND end_time > ?", 
                   (chat_id, target_id, current_time))
        mute_result = sql.fetchone()
        
        if not mute_result:
            await message.reply(f"❌ У {target_mention} нет активного мута.")
            return
        
        # Удаляем мут из базы данных
        sql.execute("DELETE FROM mutes WHERE chat_id = ? AND user_id = ?", 
                   (chat_id, target_id))
        database.commit()
        
        # Отправляем подтверждение
        await message.reply(f"✅ {initiator_mention} снял(а) мут с {target_mention}.")
            
    except Exception as e:
        logger.error(f"Ошибка при снятии мута: {e}")
        await message.reply("❌ Произошла ошибка при снятии мута.")

@register_command(['/silence', '!silence', '/тишина', '!тишина'], permission_level=PERMISSION_LEVELS['THREE'])
async def silence_command(message, args):
    """Включить/выключить режим тишины (удаление сообщений пользователей без прав)"""
    chat_id = message.chat_id
    
    if not await check_chat(chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return

    try:
        # Получаем текущее состояние режима тишины
        sql.execute("SELECT silence FROM chats WHERE chat_id = ?", (chat_id,))
        result = sql.fetchone()
        
        current_silence = result[0] if result else 0
        
        # Переключаем режим тишины (0 -> 1, 1 -> 0)
        new_silence = 1 - current_silence
        
        # Обновляем значение в базе данных
        sql.execute("UPDATE chats SET silence = ? WHERE chat_id = ?", 
                   (new_silence, chat_id))
        database.commit()
        
        # Получаем упоминание инициатора
        initiator_mention = await get_user_mention(message.from_id, chat_id)
        
        if new_silence == 1:
            await message.reply(f"🔇 {initiator_mention} включил(а) режим тишины. Сообщения пользователей без прав будут автоматически удаляться.")
        else:
            await message.reply(f"🔊 {initiator_mention} выключил(а) режим тишины.")
            
    except Exception as e:
        logger.error(f"Ошибка при изменении режима тишины: {e}")
        await message.reply("❌ Произошла ошибка при изменении режима тишины.")

@register_command(['/staff', '!staff', '/штаб', '!штаб'], permission_level=PERMISSION_LEVELS['ONE'])
async def staff_command(message, args):
    """Показать участников с правами в беседе"""
    chat_id = message.chat_id
    
    # Проверяем, активирован ли бот в беседе
    if not await check_chat(chat_id):
        await message.reply("❌ Бот не активирован в этой беседе. Используйте /start для активации.")
        return
    
    try:
        # Получаем участников с правами
        staff_members = await get_staff_members(chat_id)
        
        if not staff_members:
            await message.reply("📋 В этой беседе нет участников с правами.")
            return
        
        # Названия и эмодзи для уровней прав (соответствуют /help)
        level_info = {
            PERMISSION_LEVELS['ONE']: {"name": "Модераторы", "emoji": "⚙️"},
            PERMISSION_LEVELS['TWO']: {"name": "Администраторы", "emoji": "👑"}, 
            PERMISSION_LEVELS['THREE']: {"name": "Владельцы", "emoji": "🛠️"},
            PERMISSION_LEVELS['FOUR']: {"name": "Разработчики", "emoji": "🚀"}
        }
        
        # Формируем сообщение
        staff_message = "📋 Участники с правами в беседе:\n\n"
        
        # Проходим по всем уровням прав от высшего к низшему
        for level in sorted(staff_members.keys(), reverse=True):
            # Пропускаем пустые группы
            if not staff_members[level]:
                continue
                
            level_data = level_info.get(level, {"name": f"Уровень {level}", "emoji": "🔹"})
            level_name = level_data["name"]
            level_emoji = level_data["emoji"]
            
            staff_message += f"{level_emoji} {level_name}:\n"
            
            for user_id, nick in staff_members[level]:
                # Получаем информацию о пользователе
                try:
                    users = await bot.api.users.get(user_ids=user_id)
                    if users:
                        user = users[0]
                        full_name = f"{user.first_name} {user.last_name}"
                    else:
                        full_name = "Пользователь"
                except Exception:
                    full_name = "Пользователь"
                
                # Используем ник, если он есть, иначе полное имя
                display_name = nick if nick else full_name
                # Формируем гиперссылку вместо простого текста
                staff_message += f"• [id{user_id}|{display_name}]\n"
            
            staff_message += "\n"
        
        await message.reply(staff_message)
        
    except Exception as e:
        logger.error(f"Ошибка при получении списка участников с правами: {e}")
        await message.reply("❌ Произошла ошибка при получении списка участников с правами.")

@register_command(['/id', '!id', '/айди', '!айди'], permission_level=PERMISSION_LEVELS['ZERO'])
async def id_command(message, args):
    """Показать ID пользователя"""
    chat_id = message.chat_id
    
    # Проверяем, активирован ли бот в беседе
    if not await check_chat(chat_id):
        await message.reply("❌ Бот не активирован в этой беседе. Используйте /start для активации.")
        return
    
    # Определяем целевого пользователя
    target_id = None
    
    # Если это ответ на сообщение
    if message.reply_message:
        target_id = message.reply_message.from_id
    # Если указан пользователь в аргументах
    elif args:
        target_id = await extract_user_id(args[0], message)
        if not target_id or target_id < 0:  # Исключаем группы
            await message.reply("❌ Не удалось распознать пользователя. Укажите @упоминание, ссылку на профиль VK или цифровой ID.")
            return
    # Если аргументов нет и нет ответа на сообщение - используем отправителя
    else:
        target_id = message.from_id
    
    try:
        # Получаем информацию о пользователе
        users = await bot.api.users.get(user_ids=target_id)
        if not users:
            await message.reply("❌ Пользователь не найден.")
            return
        
        user = users[0]
        user_name = f"{user.first_name} {user.last_name}"
        
        # Формируем сообщение
        response = (
            f"[id{target_id}|{user_name}]:\n"
            f"ID VK - {target_id}\n"
            f"Оригинальная ссылка - https://vk.com/id{target_id}"
        )
        
        await message.reply(response)
        
    except Exception as e:
        logger.error(f"Ошибка при получении информации о пользователе: {e}")
        await message.reply("❌ Произошла ошибка при получении информации о пользователе.")

@register_command(['/setnick', '!setnick', '/snick', '!snick', '/ник', '!ник'], permission_level=PERMISSION_LEVELS['ONE'])
async def set_nick_command(message, args):
    """Установить ник пользователю"""
    user_id = message.from_id
    chat_id = message.chat_id
    if not await check_chat(message.chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return
    
    # Проверяем, указан ли целевой пользователь и ник
    if not args or len(args) < 2:
        if not message.reply_message:
            await message.reply("❌ Укажите пользователя и ник через @упоминание, ссылку или ID, а затем ник.")
            return
        else:
            # Если это ответ на сообщение, то аргументом должен быть ник
            if len(args) < 1:
                await message.reply("❌ Укажите ник для пользователя.")
                return
    
    # Определяем ID целевого пользователя
    target_id = None
    nick = None
    
    if message.reply_message:
        target_id = message.reply_message.from_id
        nick = ' '.join(args)
    else:
        # Первый аргумент - пользователь, остальные - ник
        target_id = await extract_user_id(args[0], message)
        if not target_id:
            await message.reply("❌ Не удалось распознать пользователя. Укажите @упоминание, ссылку на профиль VK или цифровой ID.")
            return
        nick = ' '.join(args[1:])
    
    if not nick:
        await message.reply("❌ Ник не может быть пустым.")
        return
    
    # Проверяем, может ли пользователь управлять целевым пользователем (разрешаем действие над собой)
    if not await can_manage_user(user_id, target_id, chat_id, allow_self_action=True):
        await message.reply("❌ Недостаточно прав для установки ника этому пользователю.")
        return
    
    # Устанавливаем ник
    if await set_user_nick(target_id, chat_id, nick):
        # Получаем упоминание инициатора и цели
        initiator_mention = await get_user_mention(user_id, chat_id)
        target_mention = await get_user_mention(target_id, chat_id)
        await message.reply(f"✅ {initiator_mention} успешно установил(а) ник {target_mention} -> {nick}.")
    else:
        await message.reply("❌ Произошла ошибка при установке ника.")

@register_command(['/nicklist', '!nicklist', '/nlist', '!nlist', '/списокников', '!списокников'], permission_level=PERMISSION_LEVELS['ONE'])
async def nick_list_command(message, args):
    """Показать список ников в беседе"""
    chat_id = message.chat_id
    if not await check_chat(message.chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return
    
    try:
        # Получаем все ники из базы
        nicks = await get_all_nicks(chat_id)
        
        if not nicks:
            await message.reply("📝 В этой беседе никто не установил себе ник.")
            return
        
        # Формируем список
        nick_list = []
        for user_id, nick in nicks:
            # Для каждого пользователя получаем имя и фамилию
            try:
                users = await bot.api.users.get(user_ids=user_id)
                if users:
                    user = users[0]
                    user_mention = f"[id{user_id}|{user.first_name} {user.last_name}]"
                else:
                    user_mention = f"[id{user_id}|Пользователь]"
            except Exception:
                user_mention = f"[id{user_id}|Пользователь]"
            
            nick_list.append(f"{user_mention} - {nick}")
        
        # Разбиваем список на части, чтобы сообщение не было слишком длинным
        chunk_size = 10
        chunks = [nick_list[i:i + chunk_size] for i in range(0, len(nick_list), chunk_size)]
        
        for chunk in chunks:
            await message.reply("📝 Список ников в беседе:\n" + "\n".join(chunk))
            
    except Exception as e:
        logger.error(f"Ошибка при получении списка ников: {e}")
        await message.reply("❌ Произошла ошибка при получении списка ников.")

@register_command(['/removenick', '!removenick', '/rnick', '!rnick', '/снятьник', '!снятьник'], permission_level=PERMISSION_LEVELS['ONE'])
async def remove_nick_command(message, args):
    """Удалить ник пользователя"""
    user_id = message.from_id
    chat_id = message.chat_id
    if not await check_chat(message.chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return
    
    # Проверяем, указан ли целевой пользователь
    if not args and not message.reply_message:
        await message.reply("❌ Укажите пользователя для удаления ника через @упоминание, ссылку или ID.")
        return
    
    # Определяем ID целевого пользователя
    target_id = None
    if message.reply_message:
        target_id = message.reply_message.from_id
    else:
        target_id = await extract_user_id(args[0], message)
        if not target_id:
            await message.reply("❌ Не удалось распознать пользователя. Укажите @упоминание, ссылку на профиль VK или цифровой ID.")
            return
    
    # Проверяем, может ли пользователь управлять целевым пользователем (разрешаем действие над собой)
    if not await can_manage_user(user_id, target_id, chat_id, allow_self_action=True):
        await message.reply("❌ Недостаточно прав для удаления ника этому пользователю.")
        return
    
    # Проверяем, есть ли ник у пользователя
    current_nick = await get_user_nick(target_id, chat_id)
    if not current_nick:
        await message.reply("❌ У этого пользователя нет ника.")
        return
    
    # Удаляем ник
    if await remove_user_nick(target_id, chat_id):
        # Получаем упоминание инициатора и цели
        initiator_mention = await get_user_mention(user_id, chat_id)
        target_mention = await get_user_mention(target_id, chat_id)
        await message.reply(f"✅ {initiator_mention} успешно удалил(а) ник у {target_mention}.")
    else:
        await message.reply("❌ Произошла ошибка при удалении ника.")

@register_command(['/moder', '!moder', '/модер', '!модер'], permission_level=PERMISSION_LEVELS['TWO'])
async def set_moder_command(message, args):
    """Выдать права модератора"""
    user_id = message.from_id
    chat_id = message.chat_id
    if not await check_chat(message.chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return
    
    # Получаем информацию об инициаторе с учетом ника
    initiator_mention = await get_user_mention(user_id, chat_id)
    
    # Проверяем, указан ли целевой пользователь
    if not args and not message.reply_message:
        await message.reply("❌ Укажите пользователя для выдачи прав модератора через @упоминание, ссылку или ID.")
        return
    
    # Определяем ID целевого пользователя
    target_id = None
    if message.reply_message:
        target_id = message.reply_message.from_id
    else:
        target_id = await extract_user_id(args[0], message)
        if not target_id:
            await message.reply("❌ Не удалось распознать пользователя. Укажите @упоминание, ссылку на профиль VK или цифровой ID.")
            return
    
    # Проверяем, может ли пользователь управлять целевым пользователем
    if not await can_manage_user(user_id, target_id, chat_id):
        await message.reply("❌ Недостаточно прав для выдачи прав модератора этому пользователю.")
        return
    
    # Получаем информацию о целевом пользователе
    target_mention = await get_user_mention(target_id, chat_id)
    
    # Устанавливаем права модератора (ONE)
    if await set_user_permission(target_id, chat_id, PERMISSION_LEVELS['ONE']):
        await message.reply(f"✅ {initiator_mention} успешно выдал(а) права модератора (уровень ONE) {target_mention}.")
    else:
        await message.reply("❌ Произошла ошибка при выдаче прав.")

@register_command(['/admin', '!admin', '/админ', '!админ'], permission_level=PERMISSION_LEVELS['THREE'])
async def set_admin_command(message, args):
    """Выдать права администратора"""
    user_id = message.from_id
    chat_id = message.chat_id
    if not await check_chat(message.chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return
    
    # Получаем информацию об инициаторе с учетом ника
    initiator_mention = await get_user_mention(user_id, chat_id)
    
    # Проверяем, указан ли целевой пользователь
    if not args and not message.reply_message:
        await message.reply("❌ Укажите пользователя для выдачи прав администратора через @упоминание, ссылку или ID.")
        return
    
    # Определяем ID целевого пользователя
    target_id = None
    if message.reply_message:
        target_id = message.reply_message.from_id
    else:
        target_id = await extract_user_id(args[0], message)
        if not target_id:
            await message.reply("❌ Не удалось распознать пользователя. Укажите @упоминание, ссылку на профиль VK или цифровой ID.")
            return
    
    # Проверяем, может ли пользователь управлять целевым пользователем
    if not await can_manage_user(user_id, target_id, chat_id):
        await message.reply("❌ Недостаточно прав для выдачи прав администратора этому пользователю.")
        return
    
    # Получаем информацию о целевом пользователе
    target_mention = await get_user_mention(target_id, chat_id)
    
    # Устанавливаем права администратора (TWO)
    if await set_user_permission(target_id, chat_id, PERMISSION_LEVELS['TWO']):
        await message.reply(f"✅ {initiator_mention} успешно выдал(а) права администратора (уровень TWO) {target_mention}.")
    else:
        await message.reply("❌ Произошла ошибка при выдаче прав.")
        
@register_command(['/owner', '!owner', '/владелец', '!владелец'], permission_level=PERMISSION_LEVELS['FOUR'])
async def set_owner_command(message, args):
    """Выдать права владельца"""
    user_id = message.from_id
    chat_id = message.chat_id
    
    # Получаем информацию об инициаторе с учетом ника
    initiator_mention = await get_user_mention(user_id, chat_id)
    
    # Проверяем, указан ли целевой пользователь
    if not args and not message.reply_message:
        await message.reply("❌ Укажите пользователя для выдачи прав владельца через @упоминание, ссылку или ID.")
        return
    
    # Определяем ID целевого пользователя
    target_id = None
    if message.reply_message:
        target_id = message.reply_message.from_id
    else:
        target_id = await extract_user_id(args[0], message)
        if not target_id:
            await message.reply("❌ Не удалось распознать пользователя. Укажите @упоминание, ссылку на профиль VK или цифровой ID.")
            return
    
    # Для команды /owner разрешаем действие над собой
    if not await can_manage_user(user_id, target_id, chat_id, allow_self_action=True):
        await message.reply("❌ Недостаточно прав для выдачи прав владельца этому пользователю.")
        return
    
    # Если целевой пользователь - разработчик, устанавливаем уровень 3 вместо 4
    target_level = PERMISSION_LEVELS['THREE'] if await is_global_developer(target_id) else PERMISSION_LEVELS['THREE']
    
    # Получаем информацию о целевом пользователе
    target_mention = await get_user_mention(target_id, chat_id)
    
    # Устанавливаем права владельца
    if await set_user_permission(target_id, chat_id, target_level):
        level_name = "владельца (уровень THREE)" if not await is_global_developer(target_id) else "владельца (уровень THREE) с сохранением прав разработчика"
        await message.reply(f"✅ {initiator_mention} успешно выдал(а) права {level_name} {target_mention}.")
    else:
        await message.reply("❌ Произошла ошибка при выдаче прав.")

@register_command(['/removerole', '!removerole', '/rrole', '!rrole', '/снятьроль', '!снятьроль'], permission_level=PERMISSION_LEVELS['TWO'])
async def remove_role_command(message, args):
    """Снять права с пользователя"""
    user_id = message.from_id
    chat_id = message.chat_id
    if not await check_chat(message.chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return

    # Получаем информацию об инициаторе с учетом ника
    initiator_mention = await get_user_mention(user_id, chat_id)

    # Проверяем, указан ли целевой пользователь
    if not args and not message.reply_message:
        await message.reply("❌ Укажите пользователя для снятия прав через @упоминание, ссылку или ID.")
        return
    
    # Определяем ID целевого пользователя
    target_id = None
    if message.reply_message:
        target_id = message.reply_message.from_id
    else:
        target_id = await extract_user_id(args[0], message)
        if not target_id:
            await message.reply("❌ Не удалось распознать пользователя. Укажите @упоминание, ссылку на профиль VK или цифровой ID.")
            return
    
    # Запрещаем снятие прав с самого себя
    if user_id == target_id:
        await message.reply("❌ Нельзя снимать права с самого себя.")
        return
    
    # Проверяем права на управление целевым пользователем
    if not await can_manage_user(user_id, target_id, chat_id):
        await message.reply("❌ Недостаточно прав для снятия прав с этого пользователя.")
        return
    
    # Получаем информацию о целевом пользователе
    target_mention = await get_user_mention(target_id, chat_id)
    
    # Сохраняем ник пользователя перед снятием прав
    current_nick = await get_user_nick(target_id, chat_id)
    
    # Устанавливаем нулевой уровень прав (ZERO) с сохранением ника
    try:
        if current_nick is not None:
            # Если есть ник, обновляем только уровень прав
            sql.execute(
                "UPDATE users SET permission_level = ? WHERE user_id = ? AND chat_id = ?",
                (PERMISSION_LEVELS['ZERO'], target_id, chat_id)
            )
        else:
            # Если ника нет, используем INSERT OR REPLACE
            sql.execute(
                """INSERT OR REPLACE INTO users (user_id, chat_id, permission_level) 
                   VALUES (?, ?, ?)""",
                (target_id, chat_id, PERMISSION_LEVELS['ZERO'])
            )
        
        database.commit()
        await message.reply(f"✅ {initiator_mention} успешно снял(а) все права с {target_mention}.")
    except Exception as e:
        logger.error(f"Ошибка при снятии прав: {e}")
        await message.reply("❌ Произошла ошибка при снятии прав.")

@register_command(['/kick', '!kick', '/кик', '!кик'], permission_level=PERMISSION_LEVELS['ONE'])
async def kick_command(message, args):
    """Кикнуть пользователя или группу из беседы"""
    user_id = message.from_id
    chat_id = message.chat_id
    peer_id = message.peer_id
    if not await check_chat(message.chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return

    # Получаем информацию об инициаторе с учетом ника
    initiator_mention = await get_user_mention(user_id, chat_id)
    
    # Определяем целевого пользователя/группу и причину
    target_id = None
    reason = ""
    
    # Если это ответ на сообщение
    if message.reply_message:
        target_id = message.reply_message.from_id
        reason = ' '.join(args) if args else "Причина не указана"
    else:
        if not args:
            await message.reply("❌ Укажите пользователя или группу для кика через @упоминание, ссылку или ID.")
            return
        
        # Извлекаем ID пользователя/группы из первого аргумента
        target_id = await extract_user_id(args[0], message)
        if not target_id:
            await message.reply("❌ Не удалось распознать пользователя или группу. Укажите @упоминание, ссылку на профиль VK или цифровой ID.")
            return
        
        # Остальные аргументы - причина
        reason = ' '.join(args[1:]) if len(args) > 1 else "Причина не указана"
    
    # Для пользователей (положительные ID) проверяем права
    # Для групп (отрицательные ID) пропускаем проверку прав
    if target_id > 0:  # Это пользователь
        if not await can_manage_user(user_id, target_id, chat_id):
            await message.reply("❌ Недостаточно прав для кика этого пользователя.")
            return
    
    # Формируем упоминание цели с учетом ника
    if target_id > 0:  # Пользователь
        target_mention = await get_user_mention(target_id, chat_id)
    else:  # Группа
        target_mention = f"[club{abs(target_id)}|Группа]"
    
    # Выполняем кик с помощью универсальной функции
    success = await kick_user(peer_id, abs(target_id), reason)
    
    if success:
        # Формируем сообщение об успехе
        entity_type = "Группа" if target_id < 0 else "Пользователь"
        success_message = f"✅ {initiator_mention} успешно исключил(а) {target_mention} из беседы."
        if reason and reason != "Причина не указана":
            success_message += f"\nПричина: {reason}"
        
        await message.reply(success_message)
    else:
        error_message = "❌ Не удалось исключить. Убедитесь, что у бота есть права на исключение участников."
        await message.reply(error_message)

@register_command(['/dev', '!dev'], permission_level=PERMISSION_LEVELS['ZERO'])
async def dev_command(message, args):
    """Активировать режим разработчика"""
    user_id = message.from_id
    chat_id = message.chat_id
    
    # Проверяем, является ли пользователь глобальным разработчиком
    if not await is_global_developer(user_id):
        await message.reply("❌ Вы не являетесь разработчиком.")
        return
    
    # Проверяем, активирован ли бот в беседе
    if not await check_chat(chat_id):
        await message.reply("❌ Бот не активирован в этой беседе. Используйте /start для активации.")
        return
    
    # Получаем текущий уровень прав пользователя
    current_level = await get_user_permission(user_id, chat_id)
    
    # Если уже является разработчиком (уровень 4)
    if current_level == PERMISSION_LEVELS['FOUR']:
        await message.reply("✅ Вы уже в режиме разработчика.")
        return
    
    # Сохраняем текущий уровень как предыдущий
    if not await set_developer_previous_level(user_id, chat_id, current_level):
        await message.reply("❌ Ошибка при сохранении предыдущего уровня прав.")
        return
    
    # Устанавливаем уровень разработчика
    if await set_user_permission(user_id, chat_id, PERMISSION_LEVELS['FOUR']):
        await message.reply("✅ Режим разработчика активирован.")
    else:
        await message.reply("❌ Ошибка при активации режима разработчика.")

@register_command(['/deldev', '!deldev'], permission_level=PERMISSION_LEVELS['FOUR'])
async def deldev_command(message, args):
    """Деактивировать режим разработчика"""
    user_id = message.from_id
    chat_id = message.chat_id
    
    # Проверяем, активирован ли бот в беседе
    if not await check_chat(chat_id):
        await message.reply("❌ Бот не активирован в этой беседе. Используйте /start для активации.")
        return
    
    # Получаем предыдущий уровень прав
    previous_level = await get_developer_previous_level(user_id, chat_id)
    
    # Восстанавливаем предыдущий уровень прав
    if await set_user_permission(user_id, chat_id, previous_level):
        # Удаляем запись о разработчике в этой беседе
        await remove_developer(user_id, chat_id)
        await message.reply("✅ Режим разработчика деактивирован. Права восстановлены.")
    else:
        await message.reply("❌ Ошибка при деактивации режима разработчика.")

@register_command(['/clear', '!clear', '/cls', '!cls', '/удалить', '!удалить'], permission_level=PERMISSION_LEVELS['ONE'])
async def clear_command(message, args):
    """Удалить сообщения пользователя"""
    user_id = message.from_id
    chat_id = message.chat_id
    peer_id = message.peer_id
    
    if not await check_chat(chat_id):
        await message.reply("❌ Бот не активирован в этом чате. Используйте /start")
        return

    # Определяем целевого пользователя
    target_id = None
    delete_specific_message = False
    specific_cmid = None
    
    # Если это ответ на сообщение - удаляем только это сообщение
    if message.reply_message:
        target_id = message.reply_message.from_id
        delete_specific_message = True
        specific_cmid = message.reply_message.conversation_message_id
    # Если указан пользователь в аргументах - удаляем все его сообщения
    elif args:
        target_id = await extract_user_id(args[0], message)
        if not target_id or target_id < 0:
            await message.reply("❌ Не удалось распознать пользователя. Укажите @упоминание, ссылку на профиль VK или цифровой ID.")
            return
    # Если аргументов нет и нет ответа на сообщение - удаляем все сообщения отправителя
    else:
        target_id = message.from_id

    # Проверяем, может ли пользователь управлять целевым пользователем
    if not await can_manage_user(user_id, target_id, chat_id, allow_self_action=True):
        await message.reply("❌ Недостаточно прав для удаления сообщений этого пользователя.")
        return

    # Получаем упоминания
    initiator_mention = await get_user_mention(user_id, chat_id)
    target_mention = await get_user_mention(target_id, chat_id)

    try:
        # Получаем ID группы бота один раз
        group_info = await bot.api.groups.get_by_id()
        group_id = group_info.groups[0].id
        
        # Если это ответ на конкретное сообщение - удаляем только его
        if delete_specific_message:
            # Удаляем конкретное сообщение
            success = await delete_messages(peer_id, [specific_cmid], group_id)
            
            if success:
                # Удаляем запись из базы данных
                sql.execute("DELETE FROM messages WHERE chat_id = ? AND user_id = ? AND cmid = ?", 
                           (chat_id, target_id, specific_cmid))
                database.commit()
                
                # Отправляем подтверждение
                success_message = f"✅ {initiator_mention} удалил(а) сообщение от {target_mention}."
            else:
                success_message = "❌ Не удалось удалить сообщение."
        
        # Иначе удаляем все сообщения пользователя
        else:
            # Получаем все cmid сообщений целевого пользователя
            sql.execute("SELECT cmid FROM messages WHERE chat_id = ? AND user_id = ?", 
                       (chat_id, target_id))
            result = sql.fetchall()
            
            if not result:
                await message.reply(f"❌ Не найдено сообщений от {target_mention} для удаления.")
                return
            
            # Формируем список cmid
            cmids = [row[0] for row in result]
            
            # Удаляем сообщения
            success = await delete_messages(peer_id, cmids, group_id)
            
            if success:
                # Удаляем записи из базы данных
                sql.execute("DELETE FROM messages WHERE chat_id = ? AND user_id = ?", 
                           (chat_id, target_id))
                database.commit()
                
                # Отправляем подтверждение
                success_message = f"✅ {initiator_mention} удалил(а) {len(cmids)} сообщений от {target_mention}."
            else:
                success_message = "❌ Не удалось удалить сообщения."
        
        # Отправляем сообщение об успехе (оно больше не будет удаляться)
        await message.reply(success_message)
            
    except Exception as e:
        logger.error(f"Ошибка при удалении сообщений: {e}", exc_info=True)
        await message.reply("❌ Произошла ошибка при удалении сообщений. Убедитесь, что бот является администратором беседы.")



# Запуск бота
if __name__ == "__main__":
    initialize_bot()
    logger.info("Бот запускается...")
    
    # Запускаем прослушиватель консоли в отдельном потоке
    console_thread = threading.Thread(target=console_listener, daemon=True)
    console_thread.start()

    @bot.on.chat_message(rules.ChatActionRule("chat_kick_user"))
    async def handle_user_kick(message: Message):
        """Обработчик события исключения пользователя из беседы"""
        logger.info(f"Обработчик chat_kick_user сработал для пользователя {message.action.member_id}")
        
        user_id = message.action.member_id
        chat_id = message.chat_id
        peer_id = message.peer_id
        
        # Проверяем, активирован ли бот в этом чате
        if not await check_chat(chat_id):
            logger.info(f"Бот не активирован в чате {chat_id}, пропускаем обработку")
            return
        
        # Если пользователь исключил сам себя, это эквивалентно выходу
        if user_id == message.from_id:
            logger.info(f"Пользователь {user_id} исключил сам себя (эквивалентно выходу)")
            
            # Проверяем, включена ли функция leave_kick
            try:
                sql.execute("SELECT leave_kick FROM chats WHERE chat_id = ?", (chat_id,))
                result = sql.fetchone()
                
                leave_kick_enabled = result[0] if result else 1
                
                if leave_kick_enabled != 1:
                    logger.info(f"Функция leave_kick отключена в чате {chat_id}, пропускаем кик")
                    return
            except Exception as e:
                logger.error(f"Ошибка при проверке функции leave_kick: {e}")
                return
            
            # Получаем информацию о пользователе
            try:
                user_mention = await get_user_mention(user_id, chat_id)
                
                # Пытаемся кикнуть пользователя
                kick_success = await kick_user(peer_id, user_id, "Автоматический кик при выходе из беседы")
                
                if kick_success:
                    logger.info(f"Пользователь {user_id} успешно кикнут после выхода")
                    # Отправляем сообщение напрямую, не как ответ
                    await bot.api.messages.send(
                        peer_id=peer_id,
                        message=f"{user_mention} вышел(а) из беседы и был(а) кикнут(а).",
                        random_id=0
                    )
                else:
                    logger.info(f"Не удалось кикнуть пользователя {user_id} после выхода")
                    # Отправляем сообщение напрямую, не как ответ
                    await bot.api.messages.send(
                        peer_id=peer_id,
                        message=f"{user_mention} вышел(а) из беседы, но не удалось его кикнуть.",
                        random_id=0
                    )
                    
            except Exception as e:
                logger.error(f"Ошибка при обработке выхода пользователя: {e}")
        else:
            logger.info(f"Пользователь {user_id} был исключен пользователем {message.from_id}, это не выход")
        
        # Обработчики событий входа пользователей (должны быть зарегистрированы ДО основного обработчика)
    @bot.on.chat_message(rules.ChatActionRule("chat_invite_user_by_link"))
    async def handle_user_join_by_link(message: Message):
        logger.info(f"Обработчик chat_invite_user_by_link сработал для пользователя {message.action.member_id}")
        user_id = message.action.member_id
        await check_ban_and_kick(message, user_id)

    @bot.on.chat_message(rules.ChatActionRule("chat_invite_user"))
    async def handle_user_join(message: Message):
        logger.info(f"Обработчик chat_invite_user сработал для пользователя {message.action.member_id}")
        user_id = message.action.member_id
        await check_ban_and_kick(message, user_id)
    
    # Регистрируем обработчик для сохранения сообщений и обработки команд
    @bot.on.chat_message()
    async def combined_handler(message: Message):
        # Логируем все входящие сообщения для отладки
        logger.info(f"Получено сообщение: {message.text} от пользователя {message.from_id} в чате {message.chat_id}")
        
        # Проверяем, не находится ли пользователь в муте
        current_time = int(time.time())
        chat_id = message.chat_id
        user_id = message.from_id
        
        # Проверяем активные муты для этого пользователя
        sql.execute("SELECT end_time FROM mutes WHERE chat_id = ? AND user_id = ? AND end_time > ?", 
                   (chat_id, user_id, current_time))
        mute_result = sql.fetchone()
        
        if mute_result:
            # Пользователь в муте - удаляем сообщение
            try:
                group_info = await bot.api.groups.get_by_id()
                group_id = group_info.groups[0].id
                await delete_messages(message.peer_id, [message.conversation_message_id], group_id)
                return  # Прекращаем обработку сообщения
            except Exception as e:
                logger.error(f"Ошибка при удалении сообщения замученного пользователя: {e}")
            return
        
        # Удаляем истекшие муты
        sql.execute("DELETE FROM mutes WHERE end_time <= ?", (current_time,))
        database.commit()
        
        # Проверяем режим тишины
        try:
            sql.execute("SELECT silence FROM chats WHERE chat_id = ?", (chat_id,))
            result = sql.fetchone()
            
            silence_mode = result[0] if result else 0
            
            # Если включен режим тишины и у пользователя уровень прав 0
            if silence_mode == 1:
                user_level = await get_user_permission(user_id, chat_id)
                if user_level == PERMISSION_LEVELS['ZERO']:
                    # Удаляем сообщение
                    try:
                        group_info = await bot.api.groups.get_by_id()
                        group_id = group_info.groups[0].id
                        await delete_messages(message.peer_id, [message.conversation_message_id], group_id)
                        return  # Прекращаем обработку сообщения
                    except Exception as e:
                        logger.error(f"Ошибка при удалении сообщения в режиме тишины: {e}")
                        return
        except Exception as e:
            logger.error(f"Ошибка при проверке режима тишины: {e}")
        
        # Сохраняем сообщение в базу данных
        try:
            if message.conversation_message_id and message.chat_id:
                sql.execute(
                    "INSERT OR IGNORE INTO messages (chat_id, user_id, cmid) VALUES (?, ?, ?)",
                    (message.chat_id, message.from_id, message.conversation_message_id)
                )
                database.commit()
        except Exception as e:
            logger.error(f"Ошибка при сохранении сообщения: {e}")
        
        # Обрабатываем команды
        if not message.text:
            return

        parts = message.text.split()
        command = parts[0].lower()
        args = parts[1:] if len(parts) > 1 else []
        
        if command in commands:
            func, required_level = commands[command]
            user_level = await get_user_permission(message.from_id, message.chat_id)
            
            if user_level >= required_level:
                await func(message, args)
            else:
                await message.reply("❌ Недостаточно прав для выполнения этой команды!")
    
    try:
        # Запускаем бота
        logger.info("Запуск основного цикла бота...")
        bot.run_forever()
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")
    finally:
        logger.info("Бот остановлен")
        # Закрываем соединение с базой данных
        if database:
            database.close()