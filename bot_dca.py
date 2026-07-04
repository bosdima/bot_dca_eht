#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DCA Bybit Trading Bot - МАРТИНГЕЙЛ ЛЕСЕНКОЙ
Версия 5.16.0 (04.07.2026)
ИСПРАВЛЕНИЯ:
- Исправлена ошибка подписи API Bybit (ErrCode: 10004)
- Исправлена обработка Markdown в Telegram сообщениях
- Улучшена обработка ошибок API
- Настройки вынесены в начало файла для удобства
- Исправлено: ожидание зачисления монет перед созданием ордера на продажу
- Исправлено: убрана лишняя информация из статистики
- Исправлено: добавлена версия бота и статус API в статус бота
"""

import os
import sys
import asyncio
import logging
import json
import sqlite3
import re
import time
import math
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Dict, List, Optional, Tuple
from colorama import init, Fore, Style
from logging.handlers import RotatingFileHandler

try:
    import pytz
except ImportError:
    os.system(f"{sys.executable} -m pip install pytz")
    import pytz

from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)
from telegram.request import HTTPXRequest
from pybit.unified_trading import HTTP

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

init(autoreset=True)
load_dotenv()

# =============================================================================
#                       НАСТРОЙКИ БОТА (РЕДАКТИРУЙТЕ ЗДЕСЬ)
# =============================================================================

# --- 1. Токены (Основные параметры) ---
# DEFAULT_SYMBOL - токен, с которым работает бот по умолчанию.
# POPULAR_SYMBOLS - список популярных токенов для быстрого выбора в меню.
DEFAULT_SYMBOL = "ETHUSDT"
POPULAR_SYMBOLS = ["ETHUSDT", "GRAMUSDT", "XRPUSDT", "BTCUSDT"]

# --- 2. Настройки Авто DCA ---
# INVEST_AMOUNT: Сумма для автоматической покупки в USDT (минимум 5).
# SCHEDULE_TIME: Время для автоматической покупки в формате ЧЧ:ММ (Московское время).
# FREQUENCY_HOURS: Как часто выполнять покупку (в часах).
INVEST_AMOUNT = 5.0          # Изменить: укажите нужную сумму (например, 10.0)
SCHEDULE_TIME = "05:00"      # Изменить: укажите время (например, "06:00")
FREQUENCY_HOURS = 24         # Изменить: укажите частоту (например, 12)

# --- 3. Настройки лестницы Мартингейла ---
# LADDER_BASE_AMOUNT: Базовая сумма для первого ордера.
# LADDER_MAX_AMOUNT: Максимальная сумма для последнего уровня (рассчитывается как base * 3, но можно изменить).
# LADDER_MAX_DEPTH: Максимальная глубина просадки в процентах (30-95%).
LADDER_BASE_AMOUNT = 5.0     # Изменить: укажите базовую сумму
LADDER_MAX_AMOUNT = 15.0     # Изменить: укажите максимальную сумму (рассчитывается как base * 3 по умолчанию)
LADDER_MAX_DEPTH = 80        # Изменить: укажите глубину (например, 70)

# --- 4. Настройки торговли ---
# PROFIT_PERCENT: Целевой процент прибыли для ордера на продажу.
# TRADING_MODE: Режим торговли ('real' - реальный, 'demo' - демо-режим).
# MANUAL_AMOUNT: Сумма для ручного ордера по умолчанию.
PROFIT_PERCENT = 5           # Изменить: укажите процент прибыли (например, 7)
TRADING_MODE = "real"        # Изменить: "real" или "demo"
MANUAL_AMOUNT = 1.1          # Изменить: укажите сумму для ручного ордера (минимум 1.1)

# --- 5. Настройки уведомлений о покупке ---
# PURCHASE_NOTIFY_ENABLED: Включить/выключить ежедневное уведомление о покупке.
# PURCHASE_NOTIFY_TIME: Время отправки уведомления в формате ЧЧ:ММ (МСК).
PURCHASE_NOTIFY_ENABLED = False   # Изменить: True (вкл) или False (выкл)
PURCHASE_NOTIFY_TIME = "06:00"   # Изменить: укажите время (например, "07:00")

# --- 6. Настройки отслеживания ордеров ---
# ORDER_EXECUTION_NOTIFY: Включить/выключить уведомления об исполненных ордерах.
# ORDER_CHECK_INTERVAL_MINUTES: Интервал проверки новых исполненных ордеров (в минутах).
ORDER_EXECUTION_NOTIFY = True    # Изменить: True (вкл) или False (выкл)
ORDER_CHECK_INTERVAL_MINUTES = 60 # Изменить: интервал в минутах (от 5 до 1440)

# --- 7. Настройки отслеживания продаж ---
# SELL_TRACKING_ENABLED: Включить/выключить отслеживание выполненных продаж.
SELL_TRACKING_ENABLED = True     # Изменить: True (вкл) или False (выкл)

# --- 8. Настройки API ---
# BYBIT_TESTNET_DEFAULT: Использовать тестовую сеть Bybit (true/false).
# API ключи загружаются из .env файла.
BYBIT_TESTNET_DEFAULT = False    # Изменить: True (тестнет) или False (реальная сеть)

# =============================================================================
#               КОНЕЦ БЛОКА НАСТРОЕК (ДАЛЬШЕ НЕ РЕДАКТИРОВАТЬ)
# =============================================================================

# Настройка логов с ротацией
log_handler = RotatingFileHandler("bot_errors.log", encoding='utf-8', maxBytes=200*1024, backupCount=2)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

logging.basicConfig(
    level=logging.INFO,
    handlers=[
        log_handler,
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
AUTHORIZED_USER = os.getenv('AUTHORIZED_USER', '@bosdima')
BYBIT_TESTNET_DEFAULT = os.getenv('BYBIT_TESTNET', 'false').lower() == 'true'

BOT_VERSION = "5.16.0 (04.07.2026)"
CONVERSATION_TIMEOUT = 180
MIN_ORDER_AMOUNT = 5.0
SELL_DECIMALS_FALLBACK = 5

MOSCOW_TZ = pytz.timezone('Europe/Moscow')

def get_moscow_time() -> datetime:
    return datetime.now(MOSCOW_TZ)

def get_moscow_time_naive() -> datetime:
    return datetime.now(MOSCOW_TZ).replace(tzinfo=None)

def get_api_keys():
    load_dotenv()
    api_key = os.getenv('BYBIT_API_KEY')
    api_secret = os.getenv('BYBIT_API_SECRET')
    return api_key, api_secret

# Состояния
(
    SELECTING_ACTION,
    SET_SYMBOL,
    SET_SYMBOL_MANUAL,
    SET_AMOUNT,
    SET_PROFIT_PERCENT,
    SET_MAX_DROP,
    SET_SCHEDULE_TIME,
    SET_FREQUENCY_HOURS,
    MANAGE_ORDERS,
    EDIT_ORDER_PRICE,
    MANUAL_BUY_PRICE,
    MANUAL_BUY_AMOUNT,
    MANUAL_ADD_PRICE,
    MANUAL_ADD_AMOUNT,
    EDIT_PURCHASE_SELECT,
    EDIT_PRICE,
    EDIT_AMOUNT,
    EDIT_DATE,
    DELETE_CONFIRM,
    SETTINGS_MENU,
    NOTIFICATION_SETTINGS_MENU,
    WAITING_ALERT_PERCENT,
    WAITING_ALERT_INTERVAL,
    WAITING_IMPORT_FILE,
    SELECTING_SYMBOL,
    LADDER_MENU,
    SET_LADDER_DEPTH,
    SET_LADDER_BASE_AMOUNT,
    MANUAL_ADD_RECOMMENDATION,
    WAITING_ORDER_CHECK_INTERVAL,
    WAITING_ORDER_ID_TO_CANCEL,
    WAITING_SELL_CONFIRMATION,
    WAITING_CLEAR_STATS_CONFIRMATION,
    WAITING_PURCHASE_NOTIFY_TIME,
    AUTO_DCA_SETTINGS,
    SET_MANUAL_AMOUNT,
) = range(36)

DB_EXPORT_FILE = 'dca_data_export.json'
MAX_DROP_DEPTH = 80

MAIN_MENU_BUTTONS = [
    "📊 Мой Портфель", "🚀 Запустить Авто DCA", "⏹ Остановить Авто DCA",
    "💰 Ручная покупка (лимит)", "📈 Статистика DCA", "➕ Добавить покупку вручную",
    "✏️ Редактировать покупки", "⚙️ Настройки", "📋 Статус бота",
    "📝 Управление ордерами", "✅ Отслеживание ордеров Вкл", "⏳ Отслеживание ордеров Выкл",
    "💰 Отслеживание продаж Вкл", "⏳ Отслеживание продаж Выкл", "🏠 Главное меню",
    "🔙 Назад в меню", "🔙 Назад в настройки", "🔙 Назад к списку", "❌ Отмена"
]

async def safe_send_message(bot, chat_id, text, parse_mode=None, reply_markup=None, **kwargs):
    try:
        if parse_mode:
            return await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup, **kwargs)
        else:
            return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, **kwargs)
    except Exception as e:
        if "Can't parse entities" in str(e) or "Bad Request" in str(e):
            logger.warning(f"Markdown parse error, sending without formatting: {e}")
            clean_text = text.replace('*', '').replace('`', '').replace('_', '')
            return await bot.send_message(chat_id=chat_id, text=clean_text, reply_markup=reply_markup)
        else:
            raise e

def format_price(price: float, decimals: int = 4) -> str:
    if price is None: return "N/A"
    return f"{price:.{decimals}f}"

def format_quantity(qty: float, decimals: int = 5) -> str:
    if qty is None: return "N/A"
    return f"{qty:.{decimals}f}"

def round_price_up(price: float) -> float:
    return math.ceil(price * 100) / 100

def get_ladder_levels(drop_percent: float, max_depth: float = MAX_DROP_DEPTH) -> Tuple[int, float]:
    if drop_percent <= 0: return 0, 0.0
    effective_drop = min(drop_percent, max_depth)
    ratio = (effective_drop / max_depth) * 3.0
    ratio = min(ratio, 3.0)
    level = int(effective_drop)
    return level, ratio

def get_amount_by_drop(drop_percent: float, base_amount: float, max_amount: float, max_depth: float = MAX_DROP_DEPTH) -> float:
    if drop_percent <= 0:
        return base_amount
    effective_drop = min(drop_percent, max_depth)
    fraction = effective_drop / max_depth
    amount = base_amount + (max_amount - base_amount) * fraction
    return min(amount, max_amount)

def calculate_current_drop(current_price: float, avg_price: float) -> float:
    if avg_price <= 0: return 0
    drop = ((avg_price - current_price) / avg_price) * 100
    return max(0, drop)

def calculate_apy(profit_usdt: float, total_invested: float, days: int) -> float:
    if days <= 0 or total_invested <= 0:
        return 0.0
    return (profit_usdt / total_invested) * (365 / days) * 100

def format_time_remaining(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}ч {minutes}м {secs}с"
    elif minutes > 0:
        return f"{minutes}м {secs}с"
    else:
        return f"{secs}с"


class Database:
    def __init__(self, db_file: str = "dca_bot.db"):
        self.db_file = db_file
        self.init_db()
    
    def init_db(self):
        try:
            conn = sqlite3.connect(self.db_file, timeout=10)
            cursor = conn.cursor()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS dca_purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    amount_usdt REAL NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    multiplier REAL DEFAULT 1.0,
                    drop_percent REAL DEFAULT 0,
                    step_level INTEGER DEFAULT 0,
                    date TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    order_id TEXT
                )
            ''')
            
            cursor.execute("PRAGMA table_info(dca_purchases)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'order_id' not in columns:
                cursor.execute("ALTER TABLE dca_purchases ADD COLUMN order_id TEXT")
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sell_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    order_id TEXT NOT NULL UNIQUE,
                    quantity REAL NOT NULL,
                    target_price REAL NOT NULL,
                    profit_percent REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'active'
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS pending_sell_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    target_price REAL NOT NULL,
                    profit_percent REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'pending',
                    retry_count INTEGER DEFAULT 0,
                    last_retry TIMESTAMP,
                    fail_reason TEXT
                )
            ''')
            
            cursor.execute("PRAGMA table_info(pending_sell_orders)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'retry_count' not in columns:
                cursor.execute("ALTER TABLE pending_sell_orders ADD COLUMN retry_count INTEGER DEFAULT 0")
            if 'last_retry' not in columns:
                cursor.execute("ALTER TABLE pending_sell_orders ADD COLUMN last_retry TIMESTAMP")
            if 'fail_reason' not in columns:
                cursor.execute("ALTER TABLE pending_sell_orders ADD COLUMN fail_reason TEXT")
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS completed_sells (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    sell_price REAL NOT NULL,
                    profit_percent REAL NOT NULL,
                    profit_usdt REAL NOT NULL,
                    sold_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notified BOOLEAN DEFAULT 0,
                    stats_cleared BOOLEAN DEFAULT 0,
                    clear_deadline TIMESTAMP
                )
            ''')
            
            cursor.execute("PRAGMA table_info(completed_sells)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'clear_deadline' not in columns:
                cursor.execute("ALTER TABLE completed_sells ADD COLUMN clear_deadline TIMESTAMP")
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    symbol TEXT,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS dca_start (
                    id INTEGER PRIMARY KEY,
                    start_date TIMESTAMP,
                    symbol TEXT,
                    initial_price REAL
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    enabled BOOLEAN DEFAULT 1,
                    alert_percent REAL DEFAULT 10.0,
                    alert_interval_minutes INTEGER DEFAULT 30,
                    last_check TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS ladder_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    max_depth REAL NOT NULL,
                    base_amount REAL NOT NULL,
                    max_amount REAL NOT NULL,
                    step_percent REAL DEFAULT 1.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute("PRAGMA table_info(ladder_settings)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'step_percent' not in columns:
                cursor.execute("ALTER TABLE ladder_settings ADD COLUMN step_percent REAL DEFAULT 1.0")
            
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='executed_orders'")
            table_exists = cursor.fetchone()
            
            if not table_exists:
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS executed_orders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_id TEXT NOT NULL UNIQUE,
                        symbol TEXT NOT NULL,
                        price REAL NOT NULL,
                        quantity REAL NOT NULL,
                        amount_usdt REAL NOT NULL,
                        executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        added_to_stats BOOLEAN DEFAULT 0,
                        skipped BOOLEAN DEFAULT 0,
                        notified_at TIMESTAMP
                    )
                ''')
            else:
                cursor.execute("PRAGMA table_info(executed_orders)")
                columns = [col[1] for col in cursor.fetchall()]
                if 'skipped' not in columns:
                    cursor.execute("ALTER TABLE executed_orders ADD COLUMN skipped BOOLEAN DEFAULT 0")
                if 'notified_at' not in columns:
                    cursor.execute("ALTER TABLE executed_orders ADD COLUMN notified_at TIMESTAMP")
                if 'added_to_stats' not in columns:
                    cursor.execute("ALTER TABLE executed_orders ADD COLUMN added_to_stats BOOLEAN DEFAULT 0")
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            
            defaults = [
                ('symbol', DEFAULT_SYMBOL),
                ('invest_amount', str(INVEST_AMOUNT)),
                ('manual_amount', str(MANUAL_AMOUNT)),
                ('profit_percent', str(PROFIT_PERCENT)),
                ('max_drop_percent', str(LADDER_MAX_DEPTH)),
                ('max_multiplier', '3'),
                ('schedule_time', SCHEDULE_TIME),
                ('frequency_hours', str(FREQUENCY_HOURS)),
                ('price_alert_enabled', 'false'),
                ('dca_active', 'false'),
                ('last_purchase_price', '0'),
                ('initial_reference_price', '0'),
                ('last_purchase_time', '0'),
                ('ladder_base_amount', str(LADDER_BASE_AMOUNT)),
                ('ladder_max_depth', str(LADDER_MAX_DEPTH)),
                ('ladder_max_amount', str(LADDER_MAX_AMOUNT)),
                ('order_execution_notify', str(ORDER_EXECUTION_NOTIFY).lower()),
                ('order_check_interval_minutes', str(ORDER_CHECK_INTERVAL_MINUTES)),
                ('sell_tracking_enabled', str(SELL_TRACKING_ENABLED).lower()),
                ('purchase_notify_enabled', str(PURCHASE_NOTIFY_ENABLED).lower()),
                ('purchase_notify_time', PURCHASE_NOTIFY_TIME),
                ('last_order_check_time', ''),
                ('last_full_check_time', ''),
                ('last_sell_check_time', ''),
                ('last_purchase_notify_date', ''),
                ('first_order_date', ''),
                ('next_dca_purchase_time', ''),
                ('trading_mode', TRADING_MODE),
                ('last_api_check_time', ''),
                ('api_status', 'unknown'),
                ('api_error_message', ''),
            ]
            
            for key, value in defaults:
                cursor.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))
            
            cursor.execute('''
                INSERT OR IGNORE INTO notifications (id, enabled, alert_percent, alert_interval_minutes, last_check)
                VALUES (1, 1, 10.0, 30, CURRENT_TIMESTAMP)
            ''')
            
            conn.commit()
            conn.close()
            logger.info(f"Database initialized successfully")
        except Exception as e:
            logger.error(f"DB init error: {e}")
    
    def get_setting(self, key: str, default: str = '') -> str:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
            result = cursor.fetchone()
            conn.close()
            return result[0] if result else default
        except Exception:
            return default
    
    def set_setting(self, key: str, value: str):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)', (key, value))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error setting {key}: {e}")
    
    def get_trading_mode(self) -> str:
        return self.get_setting('trading_mode', 'real')
    
    def set_trading_mode(self, mode: str):
        self.set_setting('trading_mode', mode)
    
    def is_demo_mode(self) -> bool:
        return self.get_trading_mode() == 'demo'
    
    def get_first_order_date(self) -> Optional[datetime]:
        date_str = self.get_setting('first_order_date', '')
        if date_str:
            try:
                return datetime.fromisoformat(date_str)
            except:
                return None
        return None
    
    def set_first_order_date(self, date: datetime):
        self.set_setting('first_order_date', date.isoformat())
    
    def update_first_order_date(self):
        purchases = self.get_purchases()
        if purchases:
            first_purchase = min(purchases, key=lambda x: x['date'])
            try:
                first_date = datetime.strptime(first_purchase['date'], "%Y-%m-%d %H:%M:%S")
                self.set_first_order_date(first_date)
            except Exception as e:
                logger.error(f"Error updating first order date: {e}")
        else:
            self.set_setting('first_order_date', '')
    
    def is_order_already_added(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT 1 FROM dca_purchases WHERE order_id = ?', (order_id,))
            exists = cursor.fetchone() is not None
            conn.close()
            if exists:
                logger.info(f"Order {order_id} already exists in dca_purchases")
            return exists
        except Exception as e:
            logger.error(f"Error checking order already added: {e}")
            return False
    
    def add_purchase(self, symbol: str, amount_usdt: float, price: float, 
                     quantity: float, multiplier: float = 1.0, drop_percent: float = 0,
                     step_level: int = 0, date: str = None, order_id: str = None) -> int:
        if date is None:
            date = get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")
        
        if order_id and self.is_order_already_added(order_id):
            logger.warning(f"Order {order_id} already added, skipping duplicate")
            return None
        
        try:
            conn = sqlite3.connect(self.db_file, timeout=10)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO dca_purchases 
                (symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date, order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date, order_id))
            purchase_id = cursor.lastrowid
            conn.commit()
            conn.close()
            self.update_first_order_date()
            logger.info(f"Покупка добавлена: ID={purchase_id}, {quantity} {symbol} по {price}, order_id={order_id}")
            return purchase_id
        except sqlite3.IntegrityError as e:
            if "UNIQUE constraint failed" in str(e):
                logger.warning(f"Duplicate order_id {order_id}, skipping")
                return None
            logger.error(f"SQLite error adding purchase: {e}")
            return None
        except Exception as e:
            logger.error(f"Error adding purchase: {e}")
            return None
    
    def get_purchases(self, symbol: str = None) -> List[Dict]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=10)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT * FROM dca_purchases WHERE symbol = ? ORDER BY date ASC', (symbol,))
            else:
                cursor.execute('SELECT * FROM dca_purchases ORDER BY date ASC')
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting purchases: {e}")
            return []
    
    def get_purchase_by_id(self, purchase_id: int) -> Optional[Dict]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM dca_purchases WHERE id = ?', (purchase_id,))
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting purchase {purchase_id}: {e}")
            return None
    
    def update_purchase(self, purchase_id: int, **kwargs) -> bool:
        allowed_fields = ['symbol', 'amount_usdt', 'price', 'quantity', 'multiplier', 'drop_percent', 'step_level', 'date', 'order_id']
        updates = []
        values = []
        for key, value in kwargs.items():
            if key in allowed_fields:
                updates.append(f"{key} = ?")
                values.append(value)
        if not updates:
            return False
        values.append(purchase_id)
        query = f"UPDATE dca_purchases SET {', '.join(updates)} WHERE id = ?"
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute(query, values)
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            if success:
                self.update_first_order_date()
            return success
        except Exception as e:
            logger.error(f"Error updating purchase {purchase_id}: {e}")
            return False
    
    def delete_purchase(self, purchase_id: int) -> bool:
        try:
            purchase = self.get_purchase_by_id(purchase_id)
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM dca_purchases WHERE id = ?', (purchase_id,))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            if success and purchase:
                self.reset_executed_order_status(purchase['price'], purchase['quantity'], purchase['symbol'])
            if success:
                self.update_first_order_date()
            return success
        except Exception as e:
            logger.error(f"Error deleting purchase {purchase_id}: {e}")
            return False
    
    def reset_executed_order_status(self, price: float, quantity: float, symbol: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE executed_orders 
                SET added_to_stats = 0, skipped = 0, notified_at = NULL 
                WHERE symbol = ? AND ABS(price - ?) < 0.0001 AND ABS(quantity - ?) < 0.0001
            ''', (symbol, price, quantity))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return success
        except Exception as e:
            logger.error(f"Error resetting executed order status: {e}")
            return False
    
    def get_dca_stats(self, symbol: str) -> Dict:
        purchases = self.get_purchases(symbol)
        if not purchases:
            return None
        total_usdt = sum(p['amount_usdt'] for p in purchases)
        total_qty = sum(p['quantity'] for p in purchases)
        avg_price = total_usdt / total_qty if total_qty > 0 else 0
        return {
            'total_purchases': len(purchases),
            'total_usdt': total_usdt,
            'total_quantity': total_qty,
            'avg_price': avg_price,
        }
    
    def add_sell_order(self, symbol: str, order_id: str, quantity: float, target_price: float, profit_percent: float):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO sell_orders (symbol, order_id, quantity, target_price, profit_percent)
                    VALUES (?, ?, ?, ?, ?)
                ''', (symbol, order_id, quantity, target_price, profit_percent))
                conn.commit()
            except sqlite3.IntegrityError:
                cursor.execute('''
                    UPDATE sell_orders SET target_price = ?, profit_percent = ?, status = 'active'
                    WHERE order_id = ?
                ''', (target_price, profit_percent, order_id))
                conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error adding sell order: {e}")
    
    def add_pending_sell_order(self, symbol: str, quantity: float, target_price: float, profit_percent: float, fail_reason: str = None) -> int:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO pending_sell_orders (symbol, quantity, target_price, profit_percent, status, retry_count, last_retry, fail_reason)
                VALUES (?, ?, ?, ?, 'pending', 0, CURRENT_TIMESTAMP, ?)
            ''', (symbol, quantity, target_price, profit_percent, fail_reason))
            order_id = cursor.lastrowid
            conn.commit()
            conn.close()
            logger.info(f"Added pending sell order for {symbol}: {quantity} @ {target_price}, reason: {fail_reason}")
            return order_id
        except Exception as e:
            logger.error(f"Error adding pending sell order: {e}")
            return 0
    
    def get_pending_sell_orders(self, symbol: str = None) -> List[Dict]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT * FROM pending_sell_orders WHERE symbol = ? AND status = "pending" ORDER BY created_at ASC', (symbol,))
            else:
                cursor.execute('SELECT * FROM pending_sell_orders WHERE status = "pending" ORDER BY created_at ASC')
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting pending sell orders: {e}")
            return []
    
    def update_pending_sell_order_status(self, order_id: int, status: str):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE pending_sell_orders SET status = ? WHERE id = ?', (status, order_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error updating pending sell order: {e}")
    
    def update_pending_sell_retry(self, order_id: int, fail_reason: str = None):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE pending_sell_orders 
                SET retry_count = retry_count + 1, last_retry = CURRENT_TIMESTAMP, fail_reason = ?
                WHERE id = ?
            ''', (fail_reason, order_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error updating pending sell retry: {e}")
    
    def delete_pending_sell_order(self, order_id: int):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM pending_sell_orders WHERE id = ?', (order_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error deleting pending sell order: {e}")
    
    def get_active_sell_orders(self, symbol: str = None) -> List[Dict]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT * FROM sell_orders WHERE symbol = ? AND status = "active" ORDER BY created_at DESC', (symbol,))
            else:
                cursor.execute('SELECT * FROM sell_orders WHERE status = "active" ORDER BY created_at DESC')
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting active sell orders: {e}")
            return []
    
    def update_sell_order_status(self, order_id: str, status: str):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE sell_orders SET status = ? WHERE order_id = ?', (status, order_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error updating order status: {e}")
    
    def delete_sell_order(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM sell_orders WHERE order_id = ?', (order_id,))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return success
        except Exception as e:
            logger.error(f"Error deleting sell order: {e}")
            return False
    
    def update_order_price(self, order_id: str, new_price: float, new_profit_percent: float):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE sell_orders SET target_price = ?, profit_percent = ? WHERE order_id = ?', 
                          (new_price, new_profit_percent, order_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error updating order price: {e}")
    
    def add_completed_sell(self, symbol: str, order_id: str, quantity: float, 
                           sell_price: float, profit_percent: float, profit_usdt: float) -> int:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO completed_sells (symbol, order_id, quantity, sell_price, profit_percent, profit_usdt, notified, stats_cleared)
                VALUES (?, ?, ?, ?, ?, ?, 0, 0)
            ''', (symbol, order_id, quantity, sell_price, profit_percent, profit_usdt))
            sell_id = cursor.lastrowid
            conn.commit()
            conn.close()
            logger.info(f"Added completed sell with ID {sell_id} for {symbol}")
            return sell_id
        except Exception as e:
            logger.error(f"Error adding completed sell: {e}")
            return 0
    
    def mark_completed_sell_notified(self, sell_id: int):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE completed_sells SET notified = 1 WHERE id = ?', (sell_id,))
            conn.commit()
            conn.close()
            logger.info(f"Marked completed sell {sell_id} as notified")
        except Exception as e:
            logger.error(f"Error marking sell notified: {e}")
    
    def mark_completed_sell_stats_cleared(self, sell_id: int):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE completed_sells SET stats_cleared = 1 WHERE id = ?', (sell_id,))
            conn.commit()
            conn.close()
            logger.info(f"Marked completed sell {sell_id} as stats cleared")
        except Exception as e:
            logger.error(f"Error marking sell stats cleared: {e}")
    
    def set_clear_deadline(self, sell_id: int, deadline: datetime):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE completed_sells SET clear_deadline = ? WHERE id = ?', (deadline.isoformat(), sell_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error setting clear deadline: {e}")
    
    def get_clear_deadline(self, sell_id: int) -> Optional[datetime]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT clear_deadline FROM completed_sells WHERE id = ?', (sell_id,))
            row = cursor.fetchone()
            conn.close()
            if row and row[0]:
                return datetime.fromisoformat(row[0])
            return None
        except Exception as e:
            logger.error(f"Error getting clear deadline: {e}")
            return None
    
    def is_sell_notified(self, sell_id: int) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT notified FROM completed_sells WHERE id = ?', (sell_id,))
            row = cursor.fetchone()
            conn.close()
            return row and row[0] == 1
        except Exception as e:
            logger.error(f"Error checking sell notified: {e}")
            return False
    
    def is_sell_notified_by_order_id(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT notified FROM completed_sells WHERE order_id = ? LIMIT 1', (order_id,))
            row = cursor.fetchone()
            conn.close()
            return row and row[0] == 1
        except Exception as e:
            logger.error(f"Error checking sell notified by order_id: {e}")
            return False
    
    def get_completed_sells_not_notified(self, symbol: str = None) -> List[Dict]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT * FROM completed_sells WHERE symbol = ? AND notified = 0 ORDER BY sold_at DESC', (symbol,))
            else:
                cursor.execute('SELECT * FROM completed_sells WHERE notified = 0 ORDER BY sold_at DESC')
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting completed sells not notified: {e}")
            return []
    
    def clear_all_purchases(self, symbol: str) -> int:
        try:
            conn = sqlite3.connect(self.db_file, timeout=10)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM dca_purchases WHERE symbol = ?', (symbol,))
            deleted_count = cursor.rowcount
            cursor.execute("DELETE FROM sqlite_sequence WHERE name='dca_purchases'")
            conn.commit()
            conn.close()
            self.update_first_order_date()
            logger.info(f"Cleared {deleted_count} purchases for {symbol}, reset autoincrement")
            return deleted_count
        except Exception as e:
            logger.error(f"Error clearing purchases: {e}")
            return 0
    
    def reset_autoincrement(self):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sqlite_sequence WHERE name='dca_purchases'")
            cursor.execute("DELETE FROM sqlite_sequence WHERE name='sell_orders'")
            cursor.execute("DELETE FROM sqlite_sequence WHERE name='pending_sell_orders'")
            cursor.execute("DELETE FROM sqlite_sequence WHERE name='completed_sells'")
            cursor.execute("DELETE FROM sqlite_sequence WHERE name='executed_orders'")
            cursor.execute("DELETE FROM sqlite_sequence WHERE name='ladder_settings'")
            conn.commit()
            conn.close()
            logger.info("Autoincrement reset for all tables")
        except Exception as e:
            logger.error(f"Error resetting autoincrement: {e}")
    
    def get_sell_tracking_enabled(self) -> bool:
        return self.get_setting('sell_tracking_enabled', 'true') == 'true'
    
    def set_sell_tracking_enabled(self, enabled: bool):
        self.set_setting('sell_tracking_enabled', 'true' if enabled else 'false')
    
    def get_last_sell_check_time(self) -> Optional[datetime]:
        time_str = self.get_setting('last_sell_check_time', '')
        if time_str:
            try:
                return datetime.fromisoformat(time_str)
            except:
                return None
        return None
    
    def set_last_sell_check_time(self, check_time: datetime):
        self.set_setting('last_sell_check_time', check_time.isoformat())
    
    def get_purchase_notify_enabled(self) -> bool:
        return self.get_setting('purchase_notify_enabled', 'true') == 'true'
    
    def set_purchase_notify_enabled(self, enabled: bool):
        self.set_setting('purchase_notify_enabled', 'true' if enabled else 'false')
    
    def get_purchase_notify_time(self) -> str:
        return self.get_setting('purchase_notify_time', '06:00')
    
    def set_purchase_notify_time(self, notify_time: str):
        self.set_setting('purchase_notify_time', notify_time)
    
    def get_last_purchase_notify_date(self) -> Optional[str]:
        return self.get_setting('last_purchase_notify_date', '')
    
    def set_last_purchase_notify_date(self, date_str: str):
        self.set_setting('last_purchase_notify_date', date_str)
    
    def get_manual_amount(self) -> float:
        return float(self.get_setting('manual_amount', '1.1'))
    
    def set_manual_amount(self, amount: float):
        self.set_setting('manual_amount', str(amount))
    
    def get_last_api_check_time(self) -> Optional[datetime]:
        time_str = self.get_setting('last_api_check_time', '')
        if time_str:
            try:
                return datetime.fromisoformat(time_str)
            except:
                return None
        return None
    
    def set_last_api_check_time(self, check_time: datetime):
        self.set_setting('last_api_check_time', check_time.isoformat())
    
    def get_api_status(self) -> str:
        return self.get_setting('api_status', 'unknown')
    
    def set_api_status(self, status: str):
        self.set_setting('api_status', status)
    
    def get_api_error_message(self) -> str:
        return self.get_setting('api_error_message', '')
    
    def set_api_error_message(self, message: str):
        self.set_setting('api_error_message', message)
    
    def log_action(self, action: str, symbol: str = None, details: str = None):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('INSERT INTO history (action, symbol, details) VALUES (?, ?, ?)', (action, symbol, details))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error logging action: {e}")
    
    def set_dca_start(self, symbol: str, initial_price: float):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM dca_start')
            cursor.execute('INSERT INTO dca_start (id, start_date, symbol, initial_price) VALUES (1, CURRENT_TIMESTAMP, ?, ?)', 
                          (symbol, initial_price))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error setting dca start: {e}")
    
    def get_ladder_settings(self, symbol: str = None) -> Dict:
        if symbol is None:
            symbol = self.get_setting('symbol', DEFAULT_SYMBOL)
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM ladder_settings WHERE symbol = ? ORDER BY created_at DESC LIMIT 1', (symbol,))
            row = cursor.fetchone()
            conn.close()
            if row:
                return dict(row)
            else:
                return {
                    'symbol': symbol,
                    'max_depth': float(self.get_setting('ladder_max_depth', str(LADDER_MAX_DEPTH))),
                    'base_amount': float(self.get_setting('invest_amount', str(LADDER_BASE_AMOUNT))),
                    'max_amount': float(self.get_setting('invest_amount', str(LADDER_BASE_AMOUNT))) * 3,
                    'step_percent': 1.0,
                }
        except Exception as e:
            logger.error(f"Error getting ladder settings: {e}")
            return {
                'symbol': symbol,
                'max_depth': LADDER_MAX_DEPTH,
                'base_amount': LADDER_BASE_AMOUNT,
                'max_amount': LADDER_MAX_AMOUNT,
                'step_percent': 1.0,
            }
    
    def save_ladder_settings(self, settings: Dict):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM ladder_settings WHERE symbol = ?', (settings['symbol'],))
            cursor.execute('''
                INSERT INTO ladder_settings 
                (symbol, max_depth, base_amount, max_amount, step_percent)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                settings['symbol'],
                settings['max_depth'],
                settings['base_amount'],
                settings['max_amount'],
                settings.get('step_percent', 1.0),
            ))
            conn.commit()
            conn.close()
            
            self.set_setting('ladder_max_depth', str(settings['max_depth']))
            self.set_setting('ladder_base_amount', str(settings['base_amount']))
            self.set_setting('ladder_max_amount', str(settings['max_amount']))
            self.set_setting('invest_amount', str(settings['base_amount']))
        except Exception as e:
            logger.error(f"Error saving ladder settings: {e}")
    
    def calculate_ladder_purchase(self, current_price: float, symbol: str = None) -> Dict:
        if symbol is None:
            symbol = self.get_setting('symbol', DEFAULT_SYMBOL)
        
        stats = self.get_dca_stats(symbol)
        if not stats or stats['total_quantity'] <= 0:
            return {
                'should_buy': True,
                'step_level': 0,
                'amount_usdt': self.get_ladder_settings(symbol)['base_amount'],
                'target_price': current_price,
                'drop_percent': 0,
                'reason': 'Первая покупка'
            }
        
        settings = self.get_ladder_settings(symbol)
        avg_price = stats['avg_price']
        current_drop = calculate_current_drop(current_price, avg_price)
        
        purchases = self.get_purchases(symbol)
        max_purchased_drop = max([p.get('drop_percent', 0) for p in purchases], default=0)
        
        if current_drop > max_purchased_drop + 0.01:
            amount_usdt = get_amount_by_drop(current_drop, settings['base_amount'], settings['max_amount'], settings['max_depth'])
            
            if current_drop >= settings['max_depth']:
                return {
                    'should_buy': False,
                    'step_level': int(current_drop),
                    'amount_usdt': amount_usdt,
                    'target_price': current_price,
                    'reason': f'Достигнута максимальная глубина ({settings["max_depth"]}%)'
                }
            
            return {
                'should_buy': True,
                'step_level': int(current_drop),
                'amount_usdt': amount_usdt,
                'target_price': current_price,
                'drop_percent': current_drop,
                'current_drop': current_drop,
                'reason': f'Падение {current_drop:.1f}% от средней цены (превышает {max_purchased_drop:.1f}%)'
            }
        
        next_drop = max_purchased_drop + 1.0
        next_price = avg_price * (1 - next_drop / 100)
        
        return {
            'should_buy': False,
            'step_level': 0,
            'amount_usdt': 0,
            'target_price': next_price,
            'current_drop': current_drop,
            'next_drop': next_drop,
            'reason': f'Ждем падения до {next_drop:.1f}% ({format_price(next_price)}) от средней цены {format_price(avg_price)}'
        }
    
    def get_recommendation_for_current_drop(self, current_price: float, symbol: str = None, for_manual: bool = False) -> Dict:
        if symbol is None:
            symbol = self.get_setting('symbol', DEFAULT_SYMBOL)
        
        stats = self.get_dca_stats(symbol)
        
        if for_manual:
            base_amount = self.get_manual_amount()
            max_amount = base_amount * 3
            max_depth = float(self.get_setting('ladder_max_depth', str(LADDER_MAX_DEPTH)))
        else:
            settings = self.get_ladder_settings(symbol)
            base_amount = settings['base_amount']
            max_amount = settings['max_amount']
            max_depth = settings['max_depth']
        
        if not stats or stats['total_quantity'] <= 0:
            return {
                'success': True,
                'drop_percent': 0,
                'ratio': 0,
                'amount_usdt': base_amount,
                'level': 0,
                'avg_price': 0,
                'is_first': True,
                'base_amount': base_amount,
                'max_amount': max_amount,
                'max_depth': max_depth
            }
        
        avg_price = stats['avg_price']
        drop_percent = calculate_current_drop(current_price, avg_price)
        
        amount = get_amount_by_drop(drop_percent, base_amount, max_amount, max_depth)
        level, ratio = get_ladder_levels(drop_percent, max_depth)
        
        return {
            'success': True,
            'drop_percent': drop_percent,
            'ratio': ratio,
            'amount_usdt': amount,
            'level': level,
            'avg_price': avg_price,
            'current_drop': drop_percent,
            'is_first': False,
            'base_amount': base_amount,
            'max_amount': max_amount,
            'max_depth': max_depth
        }
    
    def get_ladder_summary(self, symbol: str = None, current_price: float = None) -> Dict:
        if symbol is None:
            symbol = self.get_setting('symbol', DEFAULT_SYMBOL)
        
        settings = self.get_ladder_settings(symbol)
        stats = self.get_dca_stats(symbol)
        avg_price = stats['avg_price'] if stats else 0
        
        purchases = self.get_purchases(symbol)
        
        levels = {}
        for p in purchases:
            drop = int(p.get('drop_percent', 0))
            if drop not in levels:
                levels[drop] = []
            levels[drop].append(p)
        
        max_depth_int = int(settings['max_depth'])
        steps = []
        
        for drop_percent in range(0, max_depth_int + 1, 1):
            level, ratio = get_ladder_levels(drop_percent, settings['max_depth'])
            amount = get_amount_by_drop(drop_percent, settings['base_amount'], settings['max_amount'], settings['max_depth'])
            
            if drop_percent in levels:
                step_purchases = levels[drop_percent]
                total_amount = sum(p['amount_usdt'] for p in step_purchases)
                total_qty = sum(p['quantity'] for p in step_purchases)
                step_avg_price = total_amount / total_qty if total_qty > 0 else 0
                steps.append({
                    'step': drop_percent,
                    'drop_percent': drop_percent,
                    'ratio': ratio,
                    'price': step_avg_price,
                    'amount': amount,
                    'quantity': total_qty,
                    'status': 'completed'
                })
            else:
                target_price = avg_price * (1 - drop_percent / 100) if avg_price > 0 else 0
                steps.append({
                    'step': drop_percent,
                    'drop_percent': drop_percent,
                    'ratio': ratio,
                    'price': target_price,
                    'amount': amount,
                    'quantity': 0,
                    'status': 'pending'
                })
        
        max_purchase_drop = max([p.get('drop_percent', 0) for p in purchases], default=0)
        current_drop = 0
        if current_price and avg_price > 0:
            current_drop = calculate_current_drop(current_price, avg_price)
        
        return {
            'symbol': symbol,
            'avg_price': avg_price,
            'step_percent': 1,
            'max_depth': settings['max_depth'],
            'base_amount': settings['base_amount'],
            'max_amount': settings['max_amount'],
            'current_step': int(max_purchase_drop),
            'max_purchase_drop': max_purchase_drop,
            'current_drop': current_drop,
            'steps': steps
        }
    
    def reset_ladder(self, symbol: str = None):
        if symbol is None:
            symbol = self.get_setting('symbol', DEFAULT_SYMBOL)
        self.clear_all_purchases(symbol)
    
    def add_executed_order(self, order_id: str, symbol: str, price: float, quantity: float, amount_usdt: float, executed_at: str = None) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            if executed_at:
                cursor.execute('''
                    INSERT OR IGNORE INTO executed_orders (order_id, symbol, price, quantity, amount_usdt, executed_at, added_to_stats, skipped, notified_at)
                    VALUES (?, ?, ?, ?, ?, ?, 0, 0, NULL)
                ''', (order_id, symbol, price, quantity, amount_usdt, executed_at))
            else:
                cursor.execute('''
                    INSERT OR IGNORE INTO executed_orders (order_id, symbol, price, quantity, amount_usdt, added_to_stats, skipped, notified_at)
                    VALUES (?, ?, ?, ?, ?, 0, 0, NULL)
                ''', (order_id, symbol, price, quantity, amount_usdt))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            logger.info(f"Executed order {order_id} added to database")
            return success
        except Exception as e:
            logger.error(f"Error adding executed order: {e}")
            return False
    
    def is_order_notified(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT 1 FROM executed_orders WHERE order_id = ? AND (added_to_stats = 1 OR skipped = 1)', (order_id,))
            exists = cursor.fetchone() is not None
            conn.close()
            return exists
        except Exception as e:
            logger.error(f"Error checking order notified: {e}")
            return False
    
    def mark_order_as_added(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE executed_orders SET added_to_stats = 1, notified_at = CURRENT_TIMESTAMP WHERE order_id = ?', (order_id,))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            logger.info(f"Order {order_id} marked as added to stats")
            return success
        except Exception as e:
            logger.error(f"Error marking order as added: {e}")
            return False
    
    def mark_order_as_skipped(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE executed_orders SET skipped = 1, notified_at = CURRENT_TIMESTAMP WHERE order_id = ?', (order_id,))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            logger.info(f"Order {order_id} marked as skipped")
            return success
        except Exception as e:
            logger.error(f"Error marking order as skipped: {e}")
            return False
    
    def get_order_execution_notify(self) -> bool:
        return self.get_setting('order_execution_notify', 'true') == 'true'
    
    def set_order_execution_notify(self, enabled: bool):
        self.set_setting('order_execution_notify', 'true' if enabled else 'false')
    
    def get_order_check_interval(self) -> int:
        return int(self.get_setting('order_check_interval_minutes', str(ORDER_CHECK_INTERVAL_MINUTES)))
    
    def set_order_check_interval(self, minutes: int):
        self.set_setting('order_check_interval_minutes', str(minutes))
    
    def get_last_full_check_time(self) -> Optional[datetime]:
        time_str = self.get_setting('last_full_check_time', '')
        if time_str:
            try:
                return datetime.fromisoformat(time_str)
            except:
                return None
        return None
    
    def set_last_full_check_time(self, check_time: datetime):
        self.set_setting('last_full_check_time', check_time.isoformat())
    
    def get_last_incremental_check_time(self) -> Optional[datetime]:
        time_str = self.get_setting('last_order_check_time', '')
        if time_str:
            try:
                return datetime.fromisoformat(time_str)
            except:
                return None
        return None
    
    def set_last_incremental_check_time(self, check_time: Optional[datetime]):
        if check_time is None:
            self.set_setting('last_order_check_time', '')
        else:
            self.set_setting('last_order_check_time', check_time.isoformat())
    
    def reset_incremental_check_time(self):
        self.set_last_incremental_check_time(None)
        logger.info("Last incremental check time reset for full rescan")
    
    def get_authorized_user_id(self) -> Optional[int]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM bot_state WHERE key = "authorized_user_id"')
            row = cursor.fetchone()
            conn.close()
            return int(row[0]) if row else None
        except Exception:
            return None
    
    def set_authorized_user_id(self, user_id: int):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)', ('authorized_user_id', str(user_id)))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error saving authorized user id: {e}")
    
    def export_database(self) -> Tuple[bool, int, str]:
        try:
            purchases = self.get_purchases()
            sell_orders = self.get_active_sell_orders()
            pending_sells = self.get_pending_sell_orders()
            completed_sells = self.get_completed_sells_not_notified()
            settings = {}
            
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT key, value FROM settings')
            for key, value in cursor.fetchall():
                settings[key] = value
            
            cursor.execute('SELECT enabled, alert_percent, alert_interval_minutes FROM notifications WHERE id = 1')
            notification_row = cursor.fetchone()
            notifications = {
                'enabled': bool(notification_row[0]) if notification_row else True,
                'alert_percent': notification_row[1] if notification_row else 10.0,
                'alert_interval_minutes': notification_row[2] if notification_row else 30
            }
            
            cursor.execute('SELECT start_date, symbol, initial_price FROM dca_start WHERE id = 1')
            dca_start_row = cursor.fetchone()
            dca_start = {
                'start_date': dca_start_row[0] if dca_start_row else None,
                'symbol': dca_start_row[1] if dca_start_row else None,
                'initial_price': dca_start_row[2] if dca_start_row else None
            } if dca_start_row else None
            
            cursor.execute('SELECT * FROM ladder_settings')
            ladder_rows = cursor.fetchall()
            ladder_settings = []
            for row in ladder_rows:
                ladder_settings.append({
                    'id': row[0],
                    'symbol': row[1],
                    'max_depth': row[2],
                    'base_amount': row[3],
                    'max_amount': row[4],
                    'step_percent': row[5] if len(row) > 5 else 1.0,
                    'created_at': row[6] if len(row) > 6 else None
                })
            
            cursor.execute('SELECT * FROM executed_orders')
            executed_rows = cursor.fetchall()
            executed_orders = []
            for row in executed_rows:
                if len(row) >= 10:
                    executed_orders.append({
                        'id': row[0],
                        'order_id': row[1],
                        'symbol': row[2],
                        'price': row[3],
                        'quantity': row[4],
                        'amount_usdt': row[5],
                        'executed_at': row[6],
                        'added_to_stats': row[7],
                        'skipped': row[8] if len(row) > 8 else 0,
                        'notified_at': row[9] if len(row) > 9 else None
                    })
                else:
                    executed_orders.append({
                        'id': row[0],
                        'order_id': row[1],
                        'symbol': row[2],
                        'price': row[3],
                        'quantity': row[4],
                        'amount_usdt': row[5],
                        'executed_at': row[6],
                        'added_to_stats': row[7] if len(row) > 7 else 0,
                        'skipped': 0,
                        'notified_at': None
                    })
            
            conn.close()
            
            export_data = {
                'export_date': get_moscow_time_naive().strftime('%Y-%m-%d %H:%M:%S'),
                'version': BOT_VERSION,
                'purchases': purchases,
                'sell_orders': sell_orders,
                'pending_sell_orders': pending_sells,
                'completed_sells': completed_sells,
                'settings': settings,
                'notifications': notifications,
                'dca_start': dca_start,
                'ladder_settings': ladder_settings,
                'executed_orders': executed_orders
            }
            
            with open(DB_EXPORT_FILE, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, indent=2, ensure_ascii=False, default=str)
            
            return True, len(purchases), DB_EXPORT_FILE
        except Exception as e:
            logger.error(f"Error exporting database: {e}")
            return False, 0, str(e)
    
    def import_database(self, file_path: str) -> Tuple[bool, str]:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            conn = sqlite3.connect(self.db_file, timeout=10)
            cursor = conn.cursor()
            
            cursor.execute("PRAGMA foreign_keys = OFF")
            
            cursor.execute("DELETE FROM dca_purchases")
            cursor.execute("DELETE FROM sell_orders")
            cursor.execute("DELETE FROM pending_sell_orders")
            cursor.execute("DELETE FROM completed_sells")
            cursor.execute("DELETE FROM settings")
            cursor.execute("DELETE FROM dca_start")
            cursor.execute("DELETE FROM ladder_settings")
            cursor.execute("DELETE FROM executed_orders")
            cursor.execute("DELETE FROM history")
            cursor.execute("DELETE FROM notifications")
            
            self.reset_autoincrement()
            
            purchases_imported = 0
            for purchase in data.get('purchases', []):
                try:
                    cursor.execute('''
                        INSERT INTO dca_purchases 
                        (id, symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date, created_at, order_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        purchase.get('id'),
                        purchase.get('symbol', DEFAULT_SYMBOL),
                        purchase.get('amount_usdt', 0),
                        purchase.get('price', 0),
                        purchase.get('quantity', 0),
                        purchase.get('multiplier', 1.0),
                        purchase.get('drop_percent', 0),
                        purchase.get('step_level', 0),
                        purchase.get('date', get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")),
                        purchase.get('created_at', get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")),
                        purchase.get('order_id')
                    ))
                    purchases_imported += 1
                except Exception as e:
                    logger.warning(f"Error importing purchase: {e}")
                    continue
            
            orders_imported = 0
            for order in data.get('sell_orders', []):
                try:
                    cursor.execute('''
                        INSERT OR IGNORE INTO sell_orders 
                        (id, symbol, order_id, quantity, target_price, profit_percent, created_at, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        order.get('id'),
                        order.get('symbol', DEFAULT_SYMBOL),
                        order.get('order_id', f"imported_{order.get('id', 0)}"),
                        order.get('quantity', 0),
                        order.get('target_price', 0),
                        order.get('profit_percent', 5),
                        order.get('created_at', get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")),
                        order.get('status', 'active')
                    ))
                    orders_imported += 1
                except Exception as e:
                    logger.warning(f"Error importing order: {e}")
                    continue
            
            for pending in data.get('pending_sell_orders', []):
                try:
                    cursor.execute('''
                        INSERT OR IGNORE INTO pending_sell_orders 
                        (id, symbol, quantity, target_price, profit_percent, created_at, status, retry_count, fail_reason)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        pending.get('id'),
                        pending.get('symbol', DEFAULT_SYMBOL),
                        pending.get('quantity', 0),
                        pending.get('target_price', 0),
                        pending.get('profit_percent', 5),
                        pending.get('created_at', get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")),
                        pending.get('status', 'pending'),
                        pending.get('retry_count', 0),
                        pending.get('fail_reason')
                    ))
                except Exception as e:
                    logger.warning(f"Error importing pending order: {e}")
                    continue
            
            for sell in data.get('completed_sells', []):
                try:
                    cursor.execute('''
                        INSERT INTO completed_sells 
                        (id, symbol, order_id, quantity, sell_price, profit_percent, profit_usdt, sold_at, notified, stats_cleared)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        sell.get('id'),
                        sell.get('symbol', DEFAULT_SYMBOL),
                        sell.get('order_id'),
                        sell.get('quantity', 0),
                        sell.get('sell_price', 0),
                        sell.get('profit_percent', 0),
                        sell.get('profit_usdt', 0),
                        sell.get('sold_at', get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")),
                        sell.get('notified', 0),
                        sell.get('stats_cleared', 0)
                    ))
                except Exception as e:
                    logger.warning(f"Error importing completed sell: {e}")
                    continue
            
            for key, value in data.get('settings', {}).items():
                try:
                    cursor.execute('INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)', (key, value))
                except Exception:
                    pass
            
            dca_start = data.get('dca_start')
            if dca_start and dca_start.get('start_date'):
                try:
                    cursor.execute('INSERT OR REPLACE INTO dca_start (id, start_date, symbol, initial_price) VALUES (1, ?, ?, ?)',
                                  (dca_start['start_date'], dca_start.get('symbol', DEFAULT_SYMBOL), dca_start.get('initial_price', 0)))
                except Exception:
                    pass
            
            notifications = data.get('notifications', {})
            if notifications:
                try:
                    cursor.execute('''
                        INSERT OR REPLACE INTO notifications (id, enabled, alert_percent, alert_interval_minutes, last_check)
                        VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP)
                    ''', (1 if notifications.get('enabled', True) else 0, notifications.get('alert_percent', 10.0), notifications.get('alert_interval_minutes', 30)))
                except Exception as e:
                    logger.warning(f"Error importing notifications: {e}")
            else:
                cursor.execute('''
                    INSERT OR IGNORE INTO notifications (id, enabled, alert_percent, alert_interval_minutes, last_check)
                    VALUES (1, 1, 10.0, 30, CURRENT_TIMESTAMP)
                ''')
            
            for ladder in data.get('ladder_settings', []):
                try:
                    cursor.execute('''
                        INSERT OR REPLACE INTO ladder_settings 
                        (id, symbol, max_depth, base_amount, max_amount, step_percent, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        ladder.get('id'),
                        ladder.get('symbol', DEFAULT_SYMBOL),
                        ladder.get('max_depth', 80),
                        ladder.get('base_amount', 1.1),
                        ladder.get('max_amount', 3.3),
                        ladder.get('step_percent', 1.0),
                        ladder.get('created_at', get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S"))
                    ))
                except Exception as e:
                    logger.warning(f"Error importing ladder: {e}")
                    continue
            
            for executed in data.get('executed_orders', []):
                try:
                    cursor.execute('''
                        INSERT OR IGNORE INTO executed_orders 
                        (id, order_id, symbol, price, quantity, amount_usdt, executed_at, added_to_stats, skipped, notified_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        executed.get('id'),
                        executed.get('order_id'),
                        executed.get('symbol', DEFAULT_SYMBOL),
                        executed.get('price', 0),
                        executed.get('quantity', 0),
                        executed.get('amount_usdt', 0),
                        executed.get('executed_at', get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")),
                        executed.get('added_to_stats', 0),
                        executed.get('skipped', 0),
                        executed.get('notified_at')
                    ))
                except Exception as e:
                    logger.warning(f"Error importing executed order: {e}")
                    continue
            
            cursor.execute("PRAGMA foreign_keys = ON")
            conn.commit()
            conn.close()
            
            self.update_first_order_date()
            return True, f"Импортировано: {purchases_imported} покупок, {orders_imported} ордеров"
        except Exception as e:
            logger.error(f"Error importing database: {e}")
            return False, str(e)


class BybitClient:
    def __init__(self, api_key: str = None, api_secret: str = None, testnet: bool = False):
        if api_key is None or api_secret is None:
            api_key, api_secret = get_api_keys()
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.session = None
        self._price_cache = {}
        self._cache_time = {}
        self._cache_ttl = 5
        self._instrument_cache = {}
        self._instrument_cache_time = {}
        self._instrument_cache_ttl = 3600
        self._init_session()
    
    def _init_session(self):
        try:
            api_key, api_secret = get_api_keys()
            if api_key and api_secret:
                self.api_key = api_key
                self.api_secret = api_secret
                self.session = HTTP(testnet=self.testnet, api_key=self.api_key, api_secret=self.api_secret, recv_window=10000)
                logger.info(f"Bybit session initialized (testnet={self.testnet})")
            else:
                logger.warning("API key or secret missing")
                self.session = None
        except Exception as e:
            logger.error(f"Session init error: {e}")
            self.session = None
    
    def _refresh_session(self):
        logger.info("Refreshing Bybit session...")
        self.session = None
        self._init_session()
        return self.session is not None
    
    def _is_api_available(self) -> bool:
        if not self.session:
            self._refresh_session()
        return self.session is not None and self.api_key and self.api_secret
    
    async def check_api_health(self) -> Dict:
        self._refresh_session()
        
        if not self._is_api_available():
            return {
                'success': False,
                'error': 'API ключи не настроены',
                'user_message': 'API ключи не настроены в .env файле',
                'is_api_error': True
            }
        
        try:
            if not self.session:
                self._init_session()
                if not self.session:
                    return {
                        'success': False,
                        'error': 'Не удалось инициализировать сессию',
                        'user_message': 'Ошибка инициализации API',
                        'is_api_error': True
                    }
            
            response = self.session.get_wallet_balance(accountType="UNIFIED")
            
            if response['retCode'] == 0:
                return {'success': True, 'message': 'API ключ работает'}
            else:
                error_code = response.get('retCode', 0)
                error_msg = response.get('retMsg', 'Неизвестная ошибка')
                
                error_descriptions = {
                    10003: 'API ключ не найден',
                    10004: 'API ключ истек (expired) или неверный (проверьте секретный ключ)',
                    10005: 'Неверный API ключ или секрет',
                    10006: 'Недостаточно прав для этого действия',
                    10010: 'IP-адрес не в белом списке',
                    10016: 'Превышен лимит запросов',
                }
                
                user_message = error_descriptions.get(error_code, error_msg)
                
                return {
                    'success': False,
                    'error': error_msg,
                    'error_code': error_code,
                    'user_message': user_message,
                    'is_api_error': error_code in [10003, 10004, 10005, 10006, 10010, 10016]
                }
                
        except Exception as e:
            logger.error(f"API health check error: {e}")
            return {
                'success': False,
                'error': str(e),
                'user_message': f'Ошибка соединения: {str(e)[:100]}',
                'is_api_error': True
            }
    
    async def get_symbol_price(self, symbol: str) -> Optional[float]:
        if not self._is_api_available():
            return None
        now = time.time()
        if symbol in self._cache_time and now - self._cache_time.get(symbol, 0) < self._cache_ttl:
            return self._price_cache.get(symbol)
        try:
            if not self.session:
                self._init_session()
            response = self.session.get_tickers(category="spot", symbol=symbol)
            if response['retCode'] == 0 and response['result']['list']:
                price = float(response['result']['list'][0]['lastPrice'])
                self._price_cache[symbol] = price
                self._cache_time[symbol] = now
                return price
            return None
        except Exception as e:
            logger.error(f"Error getting price for {symbol}: {e}")
            return None
    
    async def cancel_all_sell_orders(self, symbol: str) -> Tuple[int, List[str]]:
        if not self._is_api_available():
            return 0, []
        try:
            open_orders = await self.get_open_orders(symbol)
            sell_orders = [o for o in open_orders if o.get('side') == 'Sell']
            cancelled_ids = []
            for order in sell_orders:
                order_id = order.get('orderId')
                result = await self.cancel_order(symbol, order_id)
                if result['success']:
                    cancelled_ids.append(order_id)
                    logger.info(f"Cancelled sell order {order_id} for {symbol}")
                else:
                    logger.warning(f"Failed to cancel order {order_id}: {result.get('error')}")
            return len(cancelled_ids), cancelled_ids
        except Exception as e:
            logger.error(f"Error cancelling sell orders: {e}")
            return 0, []
    
    async def get_balance(self, coin: str = None) -> Dict:
        if not self._is_api_available():
            return {'error': 'API не доступен'}
        
        try:
            if not self.session:
                self._init_session()
            
            try:
                response = self.session.get_wallet_balance(accountType="UNIFIED")
                if response['retCode'] == 0:
                    result_list = response['result']['list']
                    if result_list:
                        account_data = result_list[0]
                        coins = account_data.get('coin', [])
                        
                        if coin:
                            for c in coins:
                                if c.get('coin') == coin:
                                    wallet_balance = float(c.get('walletBalance', 0) or 0)
                                    equity = float(c.get('equity', 0) or 0) or wallet_balance
                                    locked = float(c.get('locked', 0) or 0)
                                    available = wallet_balance - locked
                                    usd_value = float(c.get('usdValue', 0) or 0)
                                    logger.info(f"Balance for {coin} (UNIFIED): available={available}, equity={equity}")
                                    return {'coin': coin, 'equity': equity, 'available': available, 'usdValue': usd_value}
                            
                            logger.warning(f"Coin {coin} not found in UNIFIED response")
                            return {'coin': coin, 'equity': 0, 'available': 0, 'usdValue': 0}
                        else:
                            total_equity = float(account_data.get('totalEquity', 0) or 0)
                            return {'total_equity': total_equity, 'coins': coins}
            except Exception as e:
                logger.warning(f"Error getting balance with UNIFIED: {e}")
                try:
                    response = self.session.get_wallet_balance(accountType="SPOT")
                    if response['retCode'] == 0:
                        result_list = response['result']['list']
                        if result_list:
                            account_data = result_list[0]
                            coins = account_data.get('coin', [])
                            
                            if coin:
                                for c in coins:
                                    if c.get('coin') == coin:
                                        wallet_balance = float(c.get('walletBalance', 0) or 0)
                                        equity = float(c.get('equity', 0) or 0) or wallet_balance
                                        locked = float(c.get('locked', 0) or 0)
                                        available = wallet_balance - locked
                                        usd_value = float(c.get('usdValue', 0) or 0)
                                        logger.info(f"Balance for {coin} (SPOT): available={available}, equity={equity}")
                                        return {'coin': coin, 'equity': equity, 'available': available, 'usdValue': usd_value}
                            else:
                                total_equity = float(account_data.get('totalEquity', 0) or 0)
                                return {'total_equity': total_equity, 'coins': coins}
                except Exception as e2:
                    logger.warning(f"Error getting balance with SPOT: {e2}")
            
            return {'error': 'Не удалось получить баланс'}
            
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return {'error': str(e)}
    
    async def get_open_orders(self, symbol: str = None) -> List[Dict]:
        if not self._is_api_available():
            return []
        try:
            if not self.session:
                self._init_session()
            params = {"category": "spot"}
            if symbol:
                params['symbol'] = symbol
            response = self.session.get_open_orders(**params)
            if response['retCode'] == 0:
                return response['result']['list']
            return []
        except Exception as e:
            logger.error(f"Error getting open orders: {e}")
            return []
    
    async def get_open_orders_by_side(self, symbol: str = None) -> Dict[str, List[Dict]]:
        orders = await self.get_open_orders(symbol)
        buy_orders = [o for o in orders if o.get('side') == 'Buy']
        sell_orders = [o for o in orders if o.get('side') == 'Sell']
        return {'buy': buy_orders, 'sell': sell_orders}
    
    async def get_sell_orders(self, symbol: str = None) -> List[Dict]:
        orders = await self.get_open_orders(symbol)
        return [o for o in orders if o.get('side') == 'Sell']
    
    async def get_order_history(self, symbol: str = None, limit: int = 500) -> List[Dict]:
        if not self._is_api_available():
            return []
        try:
            if not self.session:
                self._init_session()
            params = {"category": "spot", "limit": limit}
            if symbol:
                params['symbol'] = symbol
            response = self.session.get_order_history(**params)
            if response['retCode'] == 0:
                return response['result']['list']
            return []
        except Exception as e:
            logger.error(f"Error getting order history: {e}")
            return []
    
    async def get_instrument_info(self, symbol: str) -> Dict:
        if not self._is_api_available():
            return {'min_qty': 0.01, 'min_amt': 5, 'qty_step': 0.01, 'qty_decimals': 2, 'tick_size': 0.0001, 'price_decimals': 4}
        
        now = time.time()
        if symbol in self._instrument_cache_time and now - self._instrument_cache_time.get(symbol, 0) < self._instrument_cache_ttl:
            return self._instrument_cache.get(symbol, {})
        
        try:
            if not self.session:
                self._init_session()
            response = self.session.get_instruments_info(category="spot", symbol=symbol)
            if response['retCode'] == 0 and response['result']['list']:
                info = response['result']['list'][0]
                lot_size_filter = info.get('lotSizeFilter', {})
                price_filter = info.get('priceFilter', {})
                
                qty_step_str = lot_size_filter.get('qtyStep', '0.01')
                qty_step = float(qty_step_str)
                
                qty_decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 2
                
                min_qty = float(lot_size_filter.get('minOrderQty', 0.01))
                min_amt = float(lot_size_filter.get('minOrderAmt', 5))
                
                tick_size_str = price_filter.get('tickSize', '0.0001')
                tick_size = float(tick_size_str)
                price_decimals = len(str(tick_size).split('.')[-1]) if '.' in str(tick_size) else 4
                
                result = {
                    'min_qty': min_qty,
                    'min_amt': min_amt,
                    'qty_step': qty_step,
                    'qty_decimals': qty_decimals,
                    'tick_size': tick_size,
                    'price_decimals': price_decimals,
                }
                
                self._instrument_cache[symbol] = result
                self._instrument_cache_time[symbol] = now
                logger.info(f"Instrument info for {symbol}: qty_step={qty_step}, qty_decimals={qty_decimals}, tick_size={tick_size}")
                return result
            return {'min_qty': 0.01, 'min_amt': 5, 'qty_step': 0.01, 'qty_decimals': 2, 'tick_size': 0.0001, 'price_decimals': 4}
        except Exception as e:
            logger.error(f"Error getting instrument info: {e}")
            return {'min_qty': 0.01, 'min_amt': 5, 'qty_step': 0.01, 'qty_decimals': 2, 'tick_size': 0.0001, 'price_decimals': 4}
    
    def _round_price_by_tick(self, price: float, tick_size: float) -> float:
        if tick_size <= 0:
            return round(price, 4)
        rounded = (math.floor(price / tick_size) * tick_size)
        if rounded <= 0:
            rounded = tick_size
        decimal_places = len(str(tick_size).split('.')[-1]) if '.' in str(tick_size) else 4
        return round(rounded, decimal_places)
    
    def _round_quantity_for_buy(self, quantity: float, qty_step: float, min_qty: float) -> float:
        if qty_step <= 0:
            qty_step = 0.01
        
        qty_str = str(qty_step)
        if '.' in qty_str:
            decimals = len(qty_str.split('.')[-1])
        else:
            decimals = 0
        
        rounded = math.ceil(quantity / qty_step) * qty_step
        
        if rounded < min_qty:
            rounded = math.ceil(min_qty / qty_step) * qty_step
        
        return round(rounded, decimals)
    
    def _round_quantity_for_sell(self, quantity: float, qty_decimals: int = SELL_DECIMALS_FALLBACK) -> float:
        if quantity <= 0:
            return 0.0
        
        factor = 10 ** qty_decimals
        rounded = math.floor(quantity * factor) / factor
        
        return rounded
    
    async def wait_for_order_filled(self, symbol: str, order_id: str, timeout: int = 10, check_interval: float = 0.5) -> bool:
        """Ожидает исполнения ордера на бирже"""
        try:
            start_time = time.time()
            while time.time() - start_time < timeout:
                open_orders = await self.get_open_orders(symbol)
                is_open = any(o.get('orderId') == order_id for o in open_orders)
                
                if not is_open:
                    logger.info(f"Order {order_id} is no longer in open orders (likely filled or cancelled)")
                    return True
                
                await asyncio.sleep(check_interval)
            
            logger.warning(f"Timeout waiting for order {order_id} to fill")
            return False
        except Exception as e:
            logger.error(f"Error waiting for order fill: {e}")
            return False
    
    async def get_all_executed_orders(self, symbol: str, from_date: datetime = None) -> List[Dict]:
        if not self._is_api_available():
            return []
        try:
            check_date = from_date if from_date else get_moscow_time_naive() - timedelta(days=90)
            orders = await self.get_order_history(symbol, limit=500)
            executed = []
            for order in orders:
                order_status = order.get('orderStatus', '')
                side = order.get('side', '')
                if order_status in ['Filled', 'PartiallyFilled'] and side == 'Buy':
                    created_time_str = order.get('createdTime', '')
                    if created_time_str:
                        try:
                            created_time_ms = int(created_time_str)
                            created_time = datetime.fromtimestamp(created_time_ms / 1000)
                            if created_time >= check_date:
                                avg_price = float(order.get('avgPrice', 0))
                                if avg_price == 0:
                                    avg_price = float(order.get('price', 0))
                                qty = float(order.get('cumExecQty', 0))
                                if qty == 0:
                                    qty = float(order.get('qty', 0))
                                amount_usdt = float(order.get('cumExecValue', 0))
                                if amount_usdt == 0 and avg_price > 0:
                                    amount_usdt = avg_price * qty
                                if qty > 0 and avg_price > 0:
                                    executed.append({
                                        'order_id': order.get('orderId'),
                                        'symbol': order.get('symbol'),
                                        'price': avg_price,
                                        'quantity': qty,
                                        'amount_usdt': amount_usdt,
                                        'executed_at': created_time,
                                        'order_status': order_status
                                    })
                        except Exception as e:
                            logger.error(f"Error parsing order time: {e}")
                            continue
            return executed
        except Exception as e:
            logger.error(f"Error getting executed orders: {e}")
            return []
    
    async def get_completed_sell_orders(self, symbol: str = None, from_date: datetime = None) -> List[Dict]:
        if not self._is_api_available():
            return []
        try:
            check_date = from_date if from_date else get_moscow_time_naive() - timedelta(days=90)
            orders = await self.get_order_history(symbol, limit=500)
            completed = []
            for order in orders:
                order_status = order.get('orderStatus', '')
                side = order.get('side', '')
                if order_status in ['Filled'] and side == 'Sell':
                    created_time_str = order.get('createdTime', '')
                    if created_time_str:
                        try:
                            created_time_ms = int(created_time_str)
                            created_time = datetime.fromtimestamp(created_time_ms / 1000)
                            if created_time >= check_date:
                                avg_price = float(order.get('avgPrice', 0))
                                if avg_price == 0:
                                    avg_price = float(order.get('price', 0))
                                qty = float(order.get('cumExecQty', 0))
                                if qty == 0:
                                    qty = float(order.get('qty', 0))
                                amount_usdt = float(order.get('cumExecValue', 0))
                                if amount_usdt == 0 and avg_price > 0:
                                    amount_usdt = avg_price * qty
                                if qty > 0 and avg_price > 0:
                                    completed.append({
                                        'order_id': order.get('orderId'),
                                        'symbol': order.get('symbol'),
                                        'sell_price': avg_price,
                                        'quantity': qty,
                                        'amount_usdt': amount_usdt,
                                        'executed_at': created_time,
                                    })
                        except Exception as e:
                            logger.error(f"Error parsing order time: {e}")
                            continue
            return completed
        except Exception as e:
            logger.error(f"Error getting completed sell orders: {e}")
            return []
    
    async def cancel_order(self, symbol: str, order_id: str) -> Dict:
        if not self._is_api_available():
            return {'success': False, 'error': 'API не доступен'}
        try:
            if not self.session:
                self._init_session()
            response = self.session.cancel_order(category="spot", symbol=symbol, orderId=order_id)
            if response['retCode'] == 0:
                return {'success': True}
            return {'success': False, 'error': response['retMsg']}
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    async def amend_order_price(self, symbol: str, order_id: str, new_price: float) -> Dict:
        if not self._is_api_available():
            return {'success': False, 'error': 'API не доступен'}
        try:
            if not self.session:
                self._init_session()
            response = self.session.amend_order(category="spot", symbol=symbol, orderId=order_id, price=str(new_price))
            if response['retCode'] == 0:
                return {'success': True}
            return {'success': False, 'error': response['retMsg']}
        except Exception as e:
            return {'success': False, 'error': str(e)}
    
    async def place_limit_sell(self, symbol: str, quantity: float, price: float) -> Dict:
        if not self._is_api_available():
            return {'success': False, 'error': 'API не доступен'}
        try:
            if not self.session:
                self._init_session()
            instrument_info = await self.get_instrument_info(symbol)
            min_qty = instrument_info['min_qty']
            min_amt = instrument_info['min_amt']
            tick_size = instrument_info['tick_size']
            qty_decimals = instrument_info.get('qty_decimals', SELL_DECIMALS_FALLBACK)
            
            rounded_price = self._round_price_by_tick(price, tick_size)
            
            rounded_quantity = self._round_quantity_for_sell(quantity, qty_decimals)
            
            if rounded_quantity < min_qty and quantity >= min_qty:
                for decimals in range(qty_decimals, 0, -1):
                    factor = 10 ** decimals
                    test_rounded = math.floor(quantity * factor) / factor
                    if test_rounded >= min_qty:
                        rounded_quantity = test_rounded
                        break
            
            if rounded_quantity < min_qty:
                return {'success': False, 'error': f'Минимальное количество: {min_qty} {symbol.replace("USDT", "")}'}
            
            if rounded_quantity <= 0:
                return {'success': False, 'error': f'Недостаточно средств для продажи. Доступно: {quantity} {symbol.replace("USDT", "")}'}
            
            order_value = rounded_quantity * rounded_price
            if order_value < min_amt:
                return {'success': False, 'error': 'min_amount_error', 'min_amt': min_amt, 'order_value': order_value, 'quantity': rounded_quantity, 'price': rounded_price}
            
            logger.info(f"Placing sell order: {rounded_quantity} {symbol} @ {rounded_price} (decimals={qty_decimals})")
            
            response = self.session.place_order(
                category="spot", symbol=symbol, side="Sell", orderType="Limit", 
                qty=str(rounded_quantity), price=str(rounded_price), timeInForce="GTC"
            )
            if response['retCode'] == 0:
                return {'success': True, 'order_id': response['result']['orderId'], 'quantity': rounded_quantity, 'price': rounded_price}
            if response['retCode'] == 170140:
                return {'success': False, 'error': 'min_amount_error', 'min_amt': min_amt, 'order_value': order_value, 'quantity': rounded_quantity, 'price': rounded_price}
            if response['retCode'] == 170131:
                return {'success': False, 'error': 'insufficient_balance', 'message': response['retMsg']}
            if response['retCode'] == 170137:
                for decimals in range(qty_decimals - 1, 0, -1):
                    factor = 10 ** decimals
                    retry_quantity = math.floor(rounded_quantity * factor) / factor
                    if retry_quantity >= min_qty and retry_quantity != rounded_quantity:
                        logger.info(f"Retrying with quantity: {retry_quantity} (decimals={decimals})")
                        return await self.place_limit_sell(symbol, retry_quantity, price)
                return {'success': False, 'error': 'quantity_decimals_error', 'message': response['retMsg'], 'quantity': rounded_quantity}
            return {'success': False, 'error': f"{response['retMsg']} (Код: {response['retCode']})"}
        except Exception as e:
            logger.error(f"Error placing sell order: {e}")
            return {'success': False, 'error': str(e)}
    
    async def place_limit_buy(self, symbol: str, price: float, amount_usdt: float, is_auto: bool = True) -> Dict:
        if not self._is_api_available():
            return {'success': False, 'error': 'API не доступен'}
        try:
            if not self.session:
                self._init_session()
            instrument_info = await self.get_instrument_info(symbol)
            min_qty = instrument_info['min_qty']
            min_amt = instrument_info['min_amt']
            qty_step = instrument_info['qty_step']
            qty_decimals = instrument_info['qty_decimals']
            tick_size = instrument_info['tick_size']
            
            rounded_price = self._round_price_by_tick(price, tick_size)
            
            if not is_auto and amount_usdt < min_amt:
                return {'success': False, 'error': f'Сумма {amount_usdt:.2f} USDT меньше минимальной {min_amt} USDT.'}
            
            if is_auto:
                if amount_usdt < min_amt:
                    amount_usdt = min_amt
                    logger.info(f"Авто DCA: сумма увеличена до минимальной {min_amt} USDT")
            
            quantity = amount_usdt / rounded_price
            
            rounded_quantity = self._round_quantity_for_buy(quantity, qty_step, min_qty)
            
            if rounded_quantity > 0:
                rounded_quantity = round(rounded_quantity, qty_decimals)
            
            order_value = rounded_quantity * rounded_price
            if order_value < min_amt:
                needed_quantity = min_amt / rounded_price
                rounded_needed = self._round_quantity_for_buy(needed_quantity, qty_step, min_qty)
                if rounded_needed * rounded_price >= min_amt:
                    rounded_quantity = rounded_needed
                    order_value = rounded_quantity * rounded_price
                    logger.info(f"Скорректировано количество: {rounded_quantity} (~{order_value:.2f} USDT)")
                else:
                    rounded_quantity += qty_step
                    order_value = rounded_quantity * rounded_price
                    if order_value < min_amt:
                        return {'success': False, 'error': f'Минимальная сумма ордера: {min_amt} USDT'}
            
            logger.info(f"Placing buy order: {rounded_quantity} {symbol} @ {rounded_price}")
            
            response = self.session.place_order(
                category="spot", symbol=symbol, side="Buy", orderType="Limit", 
                qty=str(rounded_quantity), price=str(rounded_price), timeInForce="GTC"
            )
            if response['retCode'] == 0:
                return {'success': True, 'order_id': response['result']['orderId'], 'quantity': float(rounded_quantity), 'price': rounded_price, 'total_usdt': order_value}
            if response['retCode'] == 170131:
                return {'success': False, 'error': 'insufficient_balance', 'message': response['retMsg']}
            return {'success': False, 'error': response['retMsg'], 'code': response['retCode']}
        except Exception as e:
            logger.error(f"Error placing buy order: {e}")
            return {'success': False, 'error': str(e)}


class DCAStrategy:
    def __init__(self, db: Database, bybit: BybitClient):
        self.db = db
        self.bybit = bybit
        self._pending_sell_retry_interval = 300
    
    async def _send_sell_order_notification(self, symbol: str, quantity: float, price: float, profit_percent: float, avg_price: float, bot):
        user_id = self.db.get_authorized_user_id()
        if not user_id:
            return
        
        coin = symbol.replace('USDT', '')
        total_receive = quantity * price
        profit_amount = (price - avg_price) * quantity
        
        message = (
            f"✅ *ОРДЕР НА ПРОДАЖУ УСПЕШНО ВЫСТАВЛЕН!*\n\n"
            f"🪙 Пара: `{symbol}`\n"
            f"📊 Количество: `{format_quantity(quantity, 5)}` {coin}\n"
            f"💰 Цена продажи: `{format_price(price, 4)}` USDT\n"
            f"📈 Прибыль: `{profit_percent}%` от средней цены\n\n"
            f"📊 *ДЕТАЛИ СДЕЛКИ:*\n"
            f"📉 Средняя цена входа: `{format_price(avg_price, 4)}` USDT\n"
            f"💵 Получу при продаже: `{total_receive:.2f}` USDT\n"
            f"📈 Прибыль: `{profit_amount:.2f}` USDT\n\n"
            f"✅ Ордер активен!"
        )
        
        await safe_send_message(bot, user_id, message, parse_mode='Markdown')
        logger.info(f"Sell order notification sent")
    
    async def _send_no_sell_order_notification(self, symbol: str, reason: str, bot):
        user_id = self.db.get_authorized_user_id()
        if not user_id:
            return
        
        message = (
            f"ℹ️ *ОРДЕР НА ПРОДАЖУ НЕ СОЗДАН*\n\n"
            f"🪙 Пара: `{symbol}`\n"
            f"❗ *Причина:*\n`{reason}`\n\n"
            f"🔄 Проверка будет выполнена через 1 час."
        )
        
        await safe_send_message(bot, user_id, message, parse_mode='Markdown')
        logger.info(f"No sell order notification sent")
    
    async def _send_sell_order_removed_notification(self, symbol: str, bot):
        user_id = self.db.get_authorized_user_id()
        if not user_id:
            return
        
        message = (
            f"⚠️ *ОРДЕР НА ПРОДАЖУ БЫЛ УДАЛЕН!*\n\n"
            f"🪙 Пара: `{symbol}`\n"
            f"❗ Ордер на продажу был удален вручную.\n\n"
            f"🔄 Бот восстановит ордер автоматически.\n"
            f"✅ Новый ордер будет создан с +5% прибыли."
        )
        
        await safe_send_message(bot, user_id, message, parse_mode='Markdown')
        logger.info(f"Sell order removed notification sent")
    
    async def _send_purchase_skipped_notification(self, symbol: str, reason: str, current_price: float, avg_price: float, bot):
        user_id = self.db.get_authorized_user_id()
        if not user_id:
            return
        
        message = (
            f"⏭ *ПОКУПКА ПРОПУЩЕНА*\n\n"
            f"🪙 Пара: `{symbol}`\n"
            f"💰 Текущая цена: `{format_price(current_price, 4)}` USDT\n"
            f"📊 Средняя цена: `{format_price(avg_price, 4)}` USDT\n"
            f"❗ *Причина:* {reason}\n\n"
            f"🔄 Следующая проверка по расписанию."
        )
        
        await safe_send_message(bot, user_id, message, parse_mode='Markdown')
        logger.info(f"Purchase skipped notification sent: {reason}")
    
    async def check_and_create_sell_order(self, symbol: str, bot, silent: bool = False) -> Dict:
        try:
            coin = symbol.replace('USDT', '')
            
            stats = self.db.get_dca_stats(symbol)
            
            if not stats or stats['total_quantity'] <= 0:
                error_msg = 'Нет статистики DCA для расчета цены (нет покупок)'
                logger.info(f"No purchases for {symbol}, skipping sell order creation")
                if not silent:
                    await self._send_no_sell_order_notification(symbol=symbol, reason=error_msg, bot=bot)
                return {'success': False, 'error': error_msg, 'no_purchases': True}
            
            avg_price = stats['avg_price']
            profit_percent = float(self.db.get_setting('profit_percent', str(PROFIT_PERCENT)))
            target_price = avg_price * (1 + profit_percent / 100)
            
            balance_info = await self.bybit.get_balance(coin)
            if not balance_info or 'equity' not in balance_info:
                error_msg = 'Не удалось получить баланс монеты'
                if not silent:
                    await self._send_no_sell_order_notification(symbol=symbol, reason=error_msg, bot=bot)
                return {'success': False, 'error': error_msg}
            
            actual_balance = balance_info.get('equity', 0)
            logger.info(f"Balance {coin}: available={balance_info.get('available', 0)}, equity={actual_balance}")
            
            if actual_balance <= 0:
                error_msg = f'Нет монет {coin} на балансе для продажи'
                if not silent:
                    await self._send_no_sell_order_notification(symbol=symbol, reason=error_msg, bot=bot)
                return {'success': False, 'error': error_msg}
            
            logger.info(f"Balance {coin}: {actual_balance}, Target: {target_price} ({profit_percent}%)")
            
            open_orders = await self.bybit.get_open_orders(symbol)
            existing_sell_orders = [o for o in open_orders if o.get('side') == 'Sell']
            
            if existing_sell_orders:
                logger.info(f"Found {len(existing_sell_orders)} sell orders")
                return {'success': True, 'message': f'Уже есть {len(existing_sell_orders)} ордер(ов) на продажу'}
            
            logger.info("No sell order found, creating new one...")
            
            instrument_info = await self.bybit.get_instrument_info(symbol)
            min_qty = instrument_info['min_qty']
            min_amt = instrument_info['min_amt']
            tick_size = instrument_info['tick_size']
            qty_decimals = instrument_info.get('qty_decimals', SELL_DECIMALS_FALLBACK)
            
            rounded_price = self.bybit._round_price_by_tick(target_price, tick_size)
            if rounded_price <= 0:
                rounded_price = tick_size
            
            sell_quantity = self.bybit._round_quantity_for_sell(actual_balance, qty_decimals)
            
            if sell_quantity < min_qty and actual_balance >= min_qty:
                for decimals in range(qty_decimals, 0, -1):
                    factor = 10 ** decimals
                    test_rounded = math.floor(actual_balance * factor) / factor
                    if test_rounded >= min_qty:
                        sell_quantity = test_rounded
                        break
            
            logger.info(f"Selling {sell_quantity} {coin} (actual_balance={actual_balance}, decimals={qty_decimals})")
            
            if sell_quantity < min_qty:
                error_msg = f'Количество ({sell_quantity}) меньше минимального ({min_qty})'
                if not silent:
                    await self._send_no_sell_order_notification(symbol=symbol, reason=error_msg, bot=bot)
                return {'success': False, 'error': error_msg}
            
            if sell_quantity <= 0:
                error_msg = f'Недостаточно средств для продажи. Доступно: {actual_balance} {coin}'
                if not silent:
                    await self._send_no_sell_order_notification(symbol=symbol, reason=error_msg, bot=bot)
                return {'success': False, 'error': error_msg}
            
            order_value = sell_quantity * rounded_price
            if order_value < min_amt:
                needed_quantity = min_amt / rounded_price
                needed_quantity = self.bybit._round_quantity_for_sell(needed_quantity, qty_decimals)
                if needed_quantity <= actual_balance and needed_quantity > 0:
                    sell_quantity = needed_quantity
                    order_value = sell_quantity * rounded_price
                    logger.info(f"Adjusted quantity: {sell_quantity} ({order_value:.2f} USDT)")
                else:
                    error_msg = f'Сумма ({order_value:.2f} USDT) меньше минимальной ({min_amt} USDT)'
                    if not silent:
                        await self._send_no_sell_order_notification(symbol=symbol, reason=error_msg, bot=bot)
                    return {'success': False, 'error': error_msg}
            
            result = await self.bybit.place_limit_sell(symbol, sell_quantity, rounded_price)
            
            if result['success']:
                self.db.add_sell_order(
                    symbol=symbol,
                    order_id=result['order_id'],
                    quantity=result['quantity'],
                    target_price=result['price'],
                    profit_percent=profit_percent
                )
                
                await self._send_sell_order_notification(
                    symbol=symbol,
                    quantity=result['quantity'],
                    price=result['price'],
                    profit_percent=profit_percent,
                    avg_price=avg_price,
                    bot=bot
                )
                
                self.db.log_action('SELL_ORDER_CREATED', symbol, 
                                  f"Ордер {result['quantity']} по {result['price']} (+{profit_percent}%)")
                
                return {
                    'success': True,
                    'order_id': result['order_id'],
                    'quantity': result['quantity'],
                    'price': result['price'],
                    'profit_percent': profit_percent
                }
            else:
                error_msg = result.get('error', 'Неизвестная ошибка')
                if result.get('error') == 'insufficient_balance':
                    pending_id = self.db.add_pending_sell_order(
                        symbol=symbol,
                        quantity=sell_quantity,
                        target_price=rounded_price,
                        profit_percent=profit_percent,
                        fail_reason='Недостаточно средств на балансе (баланс обновляется)'
                    )
                    if not silent:
                        await self._send_no_sell_order_notification(
                            symbol=symbol,
                            reason='Недостаточно средств на балансе. Ордер сохранен как отложенный.',
                            bot=bot
                        )
                    return {'success': False, 'pending': True, 'pending_id': pending_id, 'error': error_msg}
                
                if not silent:
                    await self._send_no_sell_order_notification(symbol=symbol, reason=error_msg, bot=bot)
                return {'success': False, 'error': error_msg}
                    
        except Exception as e:
            logger.error(f"Error in check_and_create_sell_order: {e}")
            return {'success': False, 'error': str(e)}
    
    async def sell_order_check_loop(self, symbol: str, user_id: int, bot):
        logger.info(f"Sell order check loop started for {symbol} (every 1 hour)")
        
        await self.check_and_create_sell_order(symbol, bot, silent=False)
        
        while True:
            try:
                await asyncio.sleep(3600)
                
                logger.info(f"Hourly check for {symbol} sell order...")
                
                stats = self.db.get_dca_stats(symbol)
                if not stats or stats['total_quantity'] <= 0:
                    logger.info(f"No DCA stats for {symbol}, skipping sell order check (no purchases)")
                    continue
                
                open_orders = await self.bybit.get_open_orders(symbol)
                existing_sell = [o for o in open_orders if o.get('side') == 'Sell']
                
                if existing_sell:
                    logger.info(f"Sell order exists, all good")
                else:
                    logger.warning("Sell order not found! Recreating...")
                    await self._send_sell_order_removed_notification(symbol, bot)
                    await self.check_and_create_sell_order(symbol, bot, silent=False)
                
            except asyncio.CancelledError:
                logger.info("Sell order check loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in sell order check loop: {e}")
                await asyncio.sleep(60)
    
    async def cancel_old_sell_orders(self, symbol: str) -> int:
        try:
            open_orders = await self.bybit.get_open_orders(symbol)
            sell_orders = [o for o in open_orders if o.get('side') == 'Sell']
            
            if not sell_orders:
                return 0
            
            logger.info(f"Found {len(sell_orders)} old sell orders for {symbol}, cancelling...")
            cancelled_count, cancelled_ids = await self.bybit.cancel_all_sell_orders(symbol)
            
            for order_id in cancelled_ids:
                self.db.update_sell_order_status(order_id, 'cancelled')
            
            if cancelled_count > 0:
                logger.info(f"Cancelled {cancelled_count} old sell orders, waiting 3 seconds for balance update...")
                await asyncio.sleep(3)
            
            return cancelled_count
        except Exception as e:
            logger.error(f"Error cancelling old sell orders: {e}")
            return 0
    
    def calculate_target_price(self, symbol: str, profit_percent: float) -> Tuple[float, float]:
        stats = self.db.get_dca_stats(symbol)
        if not stats or stats['total_quantity'] <= 0:
            return None, None
        
        avg_price = stats['avg_price']
        target_price_raw = avg_price * (1 + profit_percent / 100)
        return avg_price, target_price_raw
    
    async def _try_place_sell_order(self, symbol: str, quantity: float, target_price: float, profit_percent: float, bot) -> Dict:
        instrument_info = await self.bybit.get_instrument_info(symbol)
        min_qty = instrument_info['min_qty']
        min_amt = instrument_info['min_amt']
        tick_size = instrument_info['tick_size']
        qty_decimals = instrument_info.get('qty_decimals', SELL_DECIMALS_FALLBACK)
        
        rounded_quantity = self.bybit._round_quantity_for_sell(quantity, qty_decimals)
        
        if rounded_quantity <= 0:
            error_msg = f'Недостаточно средств для продажи. Доступно: {quantity} {symbol.replace("USDT", "")}'
            await self._send_sell_order_failed_notification(
                symbol=symbol,
                quantity=quantity,
                target_price=target_price,
                profit_percent=profit_percent,
                error=error_msg,
                bot=bot
            )
            return {
                'success': False, 
                'error': error_msg
            }
        
        if rounded_quantity < min_qty and quantity >= min_qty:
            for decimals in range(qty_decimals, 0, -1):
                factor = 10 ** decimals
                test_rounded = math.floor(quantity * factor) / factor
                if test_rounded >= min_qty:
                    rounded_quantity = test_rounded
                    break
        
        if rounded_quantity < min_qty:
            error_msg = f'Минимальное количество: {min_qty} {symbol.replace("USDT", "")}'
            await self._send_sell_order_failed_notification(
                symbol=symbol,
                quantity=quantity,
                target_price=target_price,
                profit_percent=profit_percent,
                error=error_msg,
                bot=bot
            )
            return {
                'success': False, 
                'error': error_msg
            }
        
        rounded_price = self.bybit._round_price_by_tick(target_price, tick_size)
        
        order_value = rounded_quantity * rounded_price
        if order_value < min_amt:
            pending_id = self.db.add_pending_sell_order(
                symbol=symbol,
                quantity=rounded_quantity,
                target_price=rounded_price,
                profit_percent=profit_percent,
                fail_reason=f'Сумма ордера ({order_value:.2f} USDT) меньше минимальной ({min_amt} USDT)'
            )
            
            await self._send_pending_sell_notification(
                symbol=symbol,
                quantity=rounded_quantity,
                target_price=rounded_price,
                profit_percent=profit_percent,
                reason=f'Сумма ордера ({order_value:.2f} USDT) меньше минимальной ({min_amt} USDT)',
                bot=bot
            )
            
            return {
                'success': False,
                'pending': True,
                'pending_id': pending_id,
                'reason': f'Сумма ордера ({order_value:.2f} USDT) меньше минимальной ({min_amt} USDT)'
            }
        
        result = await self.bybit.place_limit_sell(symbol, rounded_quantity, rounded_price)
        
        if result['success']:
            self.db.add_sell_order(
                symbol=symbol,
                order_id=result['order_id'],
                quantity=result['quantity'],
                target_price=result['price'],
                profit_percent=profit_percent
            )
            return {
                'success': True,
                'order_id': result['order_id'],
                'quantity': result['quantity'],
                'price': result['price']
            }
        elif result.get('error') == 'insufficient_balance':
            pending_id = self.db.add_pending_sell_order(
                symbol=symbol,
                quantity=rounded_quantity,
                target_price=rounded_price,
                profit_percent=profit_percent,
                fail_reason='Недостаточно средств на балансе (баланс обновляется)'
            )
            
            await self._send_pending_sell_notification(
                symbol=symbol,
                quantity=rounded_quantity,
                target_price=rounded_price,
                profit_percent=profit_percent,
                reason='Недостаточно средств на балансе (баланс обновляется)',
                bot=bot
            )
            
            return {
                'success': False,
                'pending': True,
                'pending_id': pending_id,
                'reason': 'Недостаточно средств на балансе (баланс обновляется)'
            }
        elif result.get('error') == 'min_amount_error':
            pending_id = self.db.add_pending_sell_order(
                symbol=symbol,
                quantity=rounded_quantity,
                target_price=rounded_price,
                profit_percent=profit_percent,
                fail_reason=f'Минимальная сумма ордера: {min_amt} USDT'
            )
            
            await self._send_pending_sell_notification(
                symbol=symbol,
                quantity=rounded_quantity,
                target_price=rounded_price,
                profit_percent=profit_percent,
                reason=f'Минимальная сумма ордера: {min_amt} USDT',
                bot=bot
            )
            
            return {
                'success': False,
                'pending': True,
                'pending_id': pending_id,
                'reason': f'Минимальная сумма ордера: {min_amt} USDT'
            }
        elif result.get('error') == 'quantity_decimals_error':
            for decimals in range(qty_decimals - 1, 0, -1):
                factor = 10 ** decimals
                retry_quantity = math.floor(rounded_quantity * factor) / factor
                if retry_quantity >= min_qty and retry_quantity != rounded_quantity:
                    logger.info(f"Retrying with rounded quantity: {retry_quantity} (decimals={decimals})")
                    return await self._try_place_sell_order(symbol, retry_quantity, target_price, profit_percent, bot)
            
            pending_id = self.db.add_pending_sell_order(
                symbol=symbol,
                quantity=rounded_quantity,
                target_price=rounded_price,
                profit_percent=profit_percent,
                fail_reason=f'Ошибка формата количества: {result.get("message")}'
            )
            return {
                'success': False,
                'pending': True,
                'pending_id': pending_id,
                'reason': f'Ошибка формата количества'
            }
        else:
            error_msg = result.get('error', 'Неизвестная ошибка')
            pending_id = self.db.add_pending_sell_order(
                symbol=symbol,
                quantity=rounded_quantity,
                target_price=rounded_price,
                profit_percent=profit_percent,
                fail_reason=error_msg
            )
            
            await self._send_sell_order_failed_notification(
                symbol=symbol,
                quantity=rounded_quantity,
                target_price=rounded_price,
                profit_percent=profit_percent,
                error=error_msg,
                bot=bot
            )
            
            return {
                'success': False,
                'pending': True,
                'pending_id': pending_id,
                'reason': error_msg
            }
    
    async def _send_sell_order_failed_notification(self, symbol: str, quantity: float, target_price: float, profit_percent: float, error: str, bot):
        user_id = self.db.get_authorized_user_id()
        if not user_id:
            return
        
        coin = symbol.replace('USDT', '')
        message = (
            f"❌ *НЕ УДАЛОСЬ СОЗДАТЬ ОРДЕР НА ПРОДАЖУ!*\n\n"
            f"🪙 Пара: `{symbol}`\n"
            f"📊 Количество: `{format_quantity(quantity, 5)}` {coin}\n"
            f"💰 Целевая цена: `{format_price(target_price, 4)}` USDT\n"
            f"📈 Прибыль: `{profit_percent}%` от средней цены\n\n"
            f"❗ *Ошибка:*\n`{error}`\n\n"
            f"🔄 Будет выполнена повторная попытка через 5 минут.\n"
            f"✅ Ордер сохранен и будет автоматически восстановлен."
        )
        
        await safe_send_message(bot, user_id, message, parse_mode='Markdown')
        logger.info(f"Failed sell notification sent")
    
    async def _send_pending_sell_notification(self, symbol: str, quantity: float, target_price: float, profit_percent: float, reason: str, bot):
        user_id = self.db.get_authorized_user_id()
        if not user_id:
            return
        
        coin = symbol.replace('USDT', '')
        message = (
            f"⚠️ *ОРДЕР НА ПРОДАЖУ ОТЛОЖЕН*\n\n"
            f"🪙 Пара: `{symbol}`\n"
            f"📊 Количество: `{format_quantity(quantity, 5)}` {coin}\n"
            f"💰 Целевая цена: `{format_price(target_price, 4)}` USDT\n"
            f"📈 Прибыль: `{profit_percent}%` от средней цены\n\n"
            f"❗ *Причина отложения:*\n`{reason}`\n\n"
            f"🔄 Повторная попытка будет выполнена через 5 минут.\n"
            f"✅ Ордер сохранен и будет автоматически выставлен при возможности."
        )
        
        await safe_send_message(bot, user_id, message, parse_mode='Markdown')
        logger.info(f"Pending sell notification sent")
    
    async def execute_scheduled_purchase(self, symbol: str, profit_percent: float, bot) -> Dict:
        if not self.bybit._is_api_available():
            return {'success': False, 'error': 'API Bybit не доступен (проверьте ключи в .env)'}
        
        current_price = await self.bybit.get_symbol_price(symbol)
        if not current_price:
            return {'success': False, 'error': 'Не удалось получить цену'}
        
        stats = self.db.get_dca_stats(symbol)
        settings = self.db.get_ladder_settings(symbol)
        base_amount = settings['base_amount']
        
        instrument_info = await self.bybit.get_instrument_info(symbol)
        min_amt = instrument_info['min_amt']
        tick_size = instrument_info['tick_size']
        
        if stats and stats['total_quantity'] > 0:
            avg_price = stats['avg_price']
            if current_price > avg_price:
                reason = f'Текущая цена ({format_price(current_price, 4)}) ВЫШЕ средней цены ({format_price(avg_price, 4)})'
                logger.info(f"Scheduled purchase skipped: price above avg")
                await self._send_purchase_skipped_notification(symbol, reason, current_price, avg_price, bot)
                return {
                    'success': False, 
                    'error': 'skip_price_above_avg',
                    'message': f'⚠️ Покупка пропущена: {reason}'
                }
        
        if not stats or stats['total_quantity'] <= 0:
            amount_usdt = max(base_amount, min_amt)
            drop_percent = 0
            step_level = 0
        else:
            avg_price = stats['avg_price']
            current_drop = calculate_current_drop(current_price, avg_price)
            if current_price < avg_price:
                amount_usdt = get_amount_by_drop(current_drop, base_amount, settings['max_amount'], settings['max_depth'])
                drop_percent = current_drop
                step_level = int(current_drop)
                logger.info(f"Расчет суммы для Авто DCA: падение={current_drop:.1f}%, сумма={amount_usdt:.2f} USDT")
            else:
                amount_usdt = base_amount
                drop_percent = 0
                step_level = 0
        
        if amount_usdt < min_amt:
            amount_usdt = min_amt
            logger.warning(f"Сумма покупки увеличена до минимальной {min_amt} USDT")
        
        usdt_balance = await self.bybit.get_balance('USDT')
        available_usdt = usdt_balance.get('available', 0) if usdt_balance else 0
        
        if available_usdt < amount_usdt:
            return {'success': False, 'error': f'Недостаточно средств. Нужно {amount_usdt:.2f} USDT, доступно {available_usdt:.2f} USDT'}
        
        limit_price = self.bybit._round_price_by_tick(current_price, tick_size)
        if limit_price <= 0:
            limit_price = tick_size
        
        cancelled_old = await self.cancel_old_sell_orders(symbol)
        if cancelled_old > 0:
            logger.info(f"Cancelled {cancelled_old} old sell orders before new purchase")
        
        result = await self.bybit.place_limit_buy(symbol, limit_price, amount_usdt, is_auto=True)
        
        if result['success']:
            current_date = get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")
            purchase_id = self.db.add_purchase(
                symbol=symbol,
                amount_usdt=result['total_usdt'],
                price=result['price'],
                quantity=result['quantity'],
                multiplier=1.0,
                drop_percent=drop_percent,
                step_level=step_level,
                date=current_date,
                order_id=result.get('order_id')
            )
            
            if purchase_id is None:
                logger.warning(f"Purchase with order_id {result.get('order_id')} already exists, skipping")
                return {'success': False, 'error': 'Order already in database'}
            
            self.db.set_setting('last_purchase_price', str(result['price']))
            self.db.set_setting('last_purchase_time', str(get_moscow_time_naive().timestamp()))
            
            # === ИСПРАВЛЕНИЕ 1: Ожидание зачисления монет ===
            # Ждем, пока ордер будет исполнен (закрыт) на бирже
            logger.info(f"Waiting for order {result['order_id']} to be filled...")
            order_filled = await self.bybit.wait_for_order_filled(symbol, result['order_id'], timeout=10, check_interval=0.5)
            
            if not order_filled:
                logger.warning(f"Order {result['order_id']} not filled within timeout, proceeding anyway...")
            
            # Несколько попыток получить баланс с задержкой
            coin = symbol.replace('USDT', '')
            total_quantity_for_sell = 0
            max_balance_retries = 5
            
            for attempt in range(max_balance_retries):
                if attempt > 0:
                    await asyncio.sleep(1)  # Ждем 1 секунду между попытками
                
                balance_after = await self.bybit.get_balance(coin)
                total_quantity_for_sell = balance_after.get('equity', 0) if balance_after else 0
                
                if total_quantity_for_sell > 0:
                    logger.info(f"Баланс {coin} обновился: {total_quantity_for_sell} (попытка {attempt+1})")
                    break
                else:
                    logger.warning(f"Попытка {attempt+1}/{max_balance_retries}: Баланс {coin} еще 0, ждем...")
            
            if total_quantity_for_sell <= 0:
                logger.error(f"После {max_balance_retries} попыток баланс {coin} все еще 0")
                result['sell_warning'] = f"⚠️ Баланс не обновился. Ордер на продажу не создан."
                result['sell_skipped'] = True
                result['amount_usdt'] = amount_usdt
                result['drop_percent'] = drop_percent
                return result
            
            # === КОНЕЦ ИСПРАВЛЕНИЯ 1 ===
            
            updated_stats = self.db.get_dca_stats(symbol)
            if updated_stats and updated_stats['total_quantity'] > 0:
                avg_price = updated_stats['avg_price']
                target_price_sell = avg_price * (1 + profit_percent / 100)
                logger.info(f"Updated stats total: {updated_stats['total_quantity']} {coin}, Avg: {avg_price}, Target: {target_price_sell}")
            else:
                target_price_sell = result['price'] * (1 + profit_percent / 100)
                logger.info(f"Using purchase price for target: {target_price_sell}")
            
            logger.info(f"Balance for sell: {total_quantity_for_sell} {coin}")
            
            if total_quantity_for_sell <= 0:
                logger.warning(f"No coins available for sell order after purchase")
                result['sell_warning'] = f"⚠️ Монеты не зачислены на баланс. Ордер на продажу не создан."
                result['sell_skipped'] = True
                result['amount_usdt'] = amount_usdt
                result['drop_percent'] = drop_percent
                return result
            
            open_orders = await self.bybit.get_open_orders(symbol)
            existing_sell = [o for o in open_orders if o.get('side') == 'Sell']
            if existing_sell:
                logger.warning(f"Found {len(existing_sell)} sell orders still open after cancellation!")
                await self.cancel_old_sell_orders(symbol)
                await asyncio.sleep(2)
            
            sell_result = await self._try_place_sell_order(symbol, total_quantity_for_sell, target_price_sell, profit_percent, bot)
            
            if sell_result['success']:
                result['sell_order_id'] = sell_result['order_id']
                result['target_price'] = sell_result['price']
                result['sell_quantity'] = sell_result['quantity']
                result['sell_order_placed'] = True
                logger.info(f"Successfully placed sell order for {sell_result['quantity']:.5f} {coin} @ {sell_result['price']:.4f}")
                
                await self._send_sell_order_notification(
                    symbol=symbol,
                    quantity=sell_result['quantity'],
                    price=sell_result['price'],
                    profit_percent=profit_percent,
                    avg_price=avg_price if updated_stats else result['price'],
                    bot=bot
                )
            elif sell_result.get('pending'):
                result['pending_order_id'] = sell_result['pending_id']
                result['sell_warning'] = f"⚠️ Ордер на продажу отложен"
                result['sell_order_placed'] = False
                logger.info(f"Sell order pending: {sell_result.get('reason')}")
            else:
                result['sell_warning'] = sell_result.get('error', 'Не удалось создать ордер на продажу')
                result['sell_order_placed'] = False
                logger.error(f"Failed to place sell order: {sell_result.get('error')}")
            
            result['amount_usdt'] = amount_usdt
            result['drop_percent'] = drop_percent
            
            self.db.log_action('SCHEDULED_PURCHASE', symbol, f"Сумма: {result['total_usdt']:.2f} USDT, падение: {drop_percent:.1f}%")
        elif result.get('error') == 'insufficient_balance':
            logger.error(f"Insufficient balance for purchase: need {amount_usdt} USDT")
            return {'success': False, 'error': f'Недостаточно USDT на балансе. Нужно {amount_usdt:.2f} USDT'}
        else:
            logger.error(f"Scheduled purchase failed: {result.get('error')}")
        
        return result
    
    async def execute_ladder_purchase(self, symbol: str, profit_percent: float, bot) -> Dict:
        current_price = await self.bybit.get_symbol_price(symbol)
        if not current_price:
            return {'success': False, 'error': 'Не удалось получить цену'}
        
        ladder_info = self.db.calculate_ladder_purchase(current_price, symbol)
        if not ladder_info['should_buy']:
            return {'success': False, 'error': ladder_info['reason']}
        
        amount_usdt = ladder_info['amount_usdt']
        drop_percent = ladder_info.get('drop_percent', 0)
        step_level = ladder_info['step_level']
        
        instrument_info = await self.bybit.get_instrument_info(symbol)
        min_amt = instrument_info['min_amt']
        tick_size = instrument_info['tick_size']
        
        if amount_usdt < min_amt:
            amount_usdt = min_amt
        
        usdt_balance = await self.bybit.get_balance('USDT')
        available_usdt = usdt_balance.get('available', 0) if usdt_balance else 0
        
        if available_usdt < amount_usdt:
            return {'success': False, 'error': f'Недостаточно средств. Нужно {amount_usdt:.2f} USDT'}
        
        limit_price = self.bybit._round_price_by_tick(current_price, tick_size)
        if limit_price <= 0:
            limit_price = tick_size
        
        cancelled_old = await self.cancel_old_sell_orders(symbol)
        if cancelled_old > 0:
            logger.info(f"Cancelled {cancelled_old} old sell orders before new purchase")
        
        result = await self.bybit.place_limit_buy(symbol, limit_price, amount_usdt, is_auto=True)
        
        if result['success']:
            current_date = get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")
            purchase_id = self.db.add_purchase(symbol=symbol, amount_usdt=result['total_usdt'], price=result['price'],
                                quantity=result['quantity'], multiplier=1.0, drop_percent=drop_percent,
                                step_level=step_level, date=current_date, order_id=result.get('order_id'))
            
            if purchase_id is None:
                return {'success': False, 'error': 'Order already in database'}
            
            self.db.set_setting('last_purchase_price', str(result['price']))
            self.db.set_setting('last_purchase_time', str(get_moscow_time_naive().timestamp()))
            
            # === ИСПРАВЛЕНИЕ 1: Ожидание зачисления монет ===
            logger.info(f"Waiting for order {result['order_id']} to be filled...")
            order_filled = await self.bybit.wait_for_order_filled(symbol, result['order_id'], timeout=10, check_interval=0.5)
            
            if not order_filled:
                logger.warning(f"Order {result['order_id']} not filled within timeout, proceeding anyway...")
            
            coin = symbol.replace('USDT', '')
            total_quantity_for_sell = 0
            max_balance_retries = 5
            
            for attempt in range(max_balance_retries):
                if attempt > 0:
                    await asyncio.sleep(1)
                
                balance_after = await self.bybit.get_balance(coin)
                total_quantity_for_sell = balance_after.get('equity', 0) if balance_after else 0
                
                if total_quantity_for_sell > 0:
                    logger.info(f"Баланс {coin} обновился: {total_quantity_for_sell} (попытка {attempt+1})")
                    break
                else:
                    logger.warning(f"Попытка {attempt+1}/{max_balance_retries}: Баланс {coin} еще 0, ждем...")
            
            if total_quantity_for_sell <= 0:
                logger.error(f"После {max_balance_retries} попыток баланс {coin} все еще 0")
                result['sell_warning'] = f"⚠️ Баланс не обновился. Ордер на продажу не создан."
                result['sell_skipped'] = True
                return result
            # === КОНЕЦ ИСПРАВЛЕНИЯ 1 ===
            
            updated_stats = self.db.get_dca_stats(symbol)
            if updated_stats and updated_stats['total_quantity'] > 0:
                avg_price = updated_stats['avg_price']
                target_price_sell = avg_price * (1 + profit_percent / 100)
                logger.info(f"Updated stats total: {updated_stats['total_quantity']} {coin}, Avg: {avg_price}, Target: {target_price_sell}")
            else:
                target_price_sell = result['price'] * (1 + profit_percent / 100)
                logger.info(f"Using purchase price for target: {target_price_sell}")
            
            logger.info(f"Balance for sell: {total_quantity_for_sell} {coin}")
            
            if total_quantity_for_sell <= 0:
                result['sell_warning'] = f"⚠️ Монеты не зачислены на баланс. Ордер на продажу не создан."
                result['sell_skipped'] = True
            else:
                open_orders = await self.bybit.get_open_orders(symbol)
                existing_sell = [o for o in open_orders if o.get('side') == 'Sell']
                if existing_sell:
                    await self.cancel_old_sell_orders(symbol)
                    await asyncio.sleep(2)
                
                sell_result = await self._try_place_sell_order(symbol, total_quantity_for_sell, target_price_sell, profit_percent, bot)
                
                if sell_result['success']:
                    result['sell_order_id'] = sell_result['order_id']
                    result['target_price'] = sell_result['price']
                    result['sell_quantity'] = sell_result['quantity']
                    result['sell_order_placed'] = True
                    
                    await self._send_sell_order_notification(
                        symbol=symbol,
                        quantity=sell_result['quantity'],
                        price=sell_result['price'],
                        profit_percent=profit_percent,
                        avg_price=updated_stats['avg_price'] if updated_stats else result['price'],
                        bot=bot
                    )
                elif sell_result.get('pending'):
                    result['pending_order_id'] = sell_result['pending_id']
                    result['sell_warning'] = f"⚠️ Ордер на продажу отложен"
                    result['sell_order_placed'] = False
                else:
                    result['sell_warning'] = sell_result.get('error', 'Не удалось создать ордер на продажу')
                    result['sell_order_placed'] = False
            
            result['step_level'] = step_level
            result['amount_usdt'] = amount_usdt
            result['drop_percent'] = drop_percent
            
            self.db.log_action('LADDER_PURCHASE', symbol, f"Уровень {drop_percent:.1f}%: {result['total_usdt']:.2f} USDT")
        
        return result
    
    async def check_pending_sell_orders(self, symbol: str, user_id: int, bot) -> List[Dict]:
        pending_orders = self.db.get_pending_sell_orders(symbol)
        executed_orders = []
        if not pending_orders:
            return []
        
        current_price = await self.bybit.get_symbol_price(symbol)
        if not current_price:
            return []
        
        instrument_info = await self.bybit.get_instrument_info(symbol)
        min_amt = instrument_info['min_amt']
        tick_size = instrument_info['tick_size']
        qty_decimals = instrument_info.get('qty_decimals', SELL_DECIMALS_FALLBACK)
        
        for order in pending_orders:
            last_retry = order.get('last_retry')
            if last_retry:
                try:
                    if isinstance(last_retry, str):
                        last_retry_time = datetime.fromisoformat(last_retry)
                    else:
                        last_retry_time = last_retry
                    time_since_last = (get_moscow_time_naive() - last_retry_time).total_seconds()
                    if time_since_last < self._pending_sell_retry_interval:
                        continue
                except Exception as e:
                    logger.error(f"Error parsing last_retry: {e}")
            
            open_orders = await self.bybit.get_open_orders(symbol)
            existing_sell = [o for o in open_orders if o.get('side') == 'Sell']
            if existing_sell:
                self.db.delete_pending_sell_order(order['id'])
                logger.info(f"Pending order {order['id']} removed because sell order already exists")
                continue
            
            if current_price >= order['target_price']:
                new_target_price = current_price * (1 + order['profit_percent'] / 100)
                rounded_price = self.bybit._round_price_by_tick(new_target_price, tick_size)
                if rounded_price <= 0:
                    rounded_price = tick_size
                
                quantity = self.bybit._round_quantity_for_sell(order['quantity'], qty_decimals)
                
                sell_result = await self._try_place_sell_order(symbol, quantity, rounded_price, order['profit_percent'], bot)
                
                if sell_result['success']:
                    self.db.delete_pending_sell_order(order['id'])
                    executed_orders.append({
                        'id': order['id'],
                        'quantity': quantity,
                        'target_price': rounded_price,
                        'profit_percent': order['profit_percent']
                    })
                    
                    stats = self.db.get_dca_stats(symbol)
                    avg_price = stats['avg_price'] if stats and stats['avg_price'] > 0 else rounded_price / (1 + order['profit_percent'] / 100)
                    await self._send_sell_order_notification(
                        symbol=symbol,
                        quantity=quantity,
                        price=rounded_price,
                        profit_percent=order['profit_percent'],
                        avg_price=avg_price,
                        bot=bot
                    )
                    
                    msg = (f"✅ *ОТЛОЖЕННЫЙ ОРДЕР ВЫПОЛНЕН!*\n\n"
                           f"🪙 Токен: `{symbol}`\n"
                           f"📊 Количество: `{format_quantity(quantity, 5)}`\n"
                           f"💰 Цена продажи: `{format_price(rounded_price, 4)}` USDT\n"
                           f"📈 Целевая прибыль: `{order['profit_percent']}%`\n\n"
                           f"✅ Ордер успешно выставлен!")
                    await safe_send_message(bot, user_id, msg, parse_mode='Markdown')
                    logger.info(f"Sent pending order notification for {symbol} to user {user_id}")
                elif sell_result.get('pending'):
                    fail_reason = sell_result.get('reason', 'Неизвестная причина')
                    self.db.update_pending_sell_retry(order['id'], fail_reason)
                    
                    retry_count = order.get('retry_count', 0) + 1
                    if retry_count % 3 == 0:
                        await self._send_pending_sell_notification(
                            symbol=symbol,
                            quantity=quantity,
                            target_price=rounded_price,
                            profit_percent=order['profit_percent'],
                            reason=f'Попытка #{retry_count}: {fail_reason}',
                            bot=bot
                        )
                else:
                    fail_reason = sell_result.get('error', 'Неизвестная ошибка')
                    self.db.update_pending_sell_retry(order['id'], fail_reason)
                    
                    retry_count = order.get('retry_count', 0) + 1
                    if retry_count % 3 == 0:
                        await self._send_sell_order_failed_notification(
                            symbol=symbol,
                            quantity=quantity,
                            target_price=rounded_price,
                            profit_percent=order['profit_percent'],
                            error=f'Попытка #{retry_count}: {fail_reason}',
                            bot=bot
                        )
            else:
                self.db.update_pending_sell_retry(order['id'], f'Цена {format_price(current_price, 4)} < {format_price(order["target_price"], 4)}')
        
        return executed_orders
    
    async def check_and_update_sell_orders(self, symbol: str):
        active_orders = self.db.get_active_sell_orders(symbol)
        open_orders = await self.bybit.get_open_orders(symbol)
        open_order_ids = {o['orderId'] for o in open_orders}
        for order in active_orders:
            if order['order_id'] not in open_order_ids:
                self.db.update_sell_order_status(order['order_id'], 'completed')
                self.db.log_action('SELL_COMPLETED', symbol, f"Продано по {format_price(order['target_price'])}")
    
    def _format_sell_notification(self, sell: Dict, symbol: str) -> str:
        profit_emoji = "🟢" if sell['profit_usdt'] >= 0 else "🔴"
        profit_color = "+" if sell['profit_usdt'] >= 0 else ""
        
        days_invested = sell.get('days_invested', 0)
        if days_invested <= 0:
            days_invested = 1
        
        apy = sell.get('apy', 0)
        if apy == 0:
            apy = calculate_apy(sell['profit_usdt'], sell['total_invested'], days_invested)
        
        message = f"💰 <b>СДЕЛКА ПРОДАНА!</b>\n\n"
        message += f"🪙 Токен: <code>{symbol}</code>\n"
        message += f"📊 Количество: <code>{format_quantity(sell['quantity'], 5)}</code>\n"
        message += f"💰 Цена продажи: <code>{format_price(sell['sell_price'], 4)}</code> USDT\n"
        message += f"💵 Сумма продажи: <code>{sell['amount_usdt']:.2f}</code> USDT\n\n"
        message += f"📈 <b>СТАТИСТИКА СДЕЛКИ:</b>\n"
        message += f"💰 Всего инвестировано: <code>{sell['total_invested']:.2f}</code> USDT\n"
        message += f"💵 Получено: <code>{sell['amount_usdt']:.2f}</code> USDT\n"
        message += f"{profit_emoji} Прибыль: <code>{profit_color}{sell['profit_usdt']:.2f}</code> USDT\n"
        message += f"📊 Процент прибыли: <code>{profit_color}{sell['profit_percent']:.2f}%</code>\n"
        message += f"📅 Период инвестиций: <code>{days_invested}</code> дн.\n"
        message += f"📈 Годовая ставка (APY): <code>{profit_color}{apy:.2f}%</code>\n\n"
        message += f"❗ <b>Очистить статистику DCA по этому токену?</b>\n"
        message += f"После очистки начнется новый цикл накопления.\n"
        message += f"⚠️ <b>ВНИМАНИЕ: ID покупок будут сброшены и начнутся с 1!</b>"
        
        return message
    
    async def check_completed_sells(self, symbol: str, user_id: int, bot, force: bool = False) -> List[Dict]:
        first_order_date = self.db.get_first_order_date()
        
        if first_order_date is None:
            check_date = get_moscow_time_naive() - timedelta(days=30)
            logger.info(f"No first order date, checking last 30 days from {check_date}")
        else:
            check_date = first_order_date - timedelta(days=1)
            logger.info(f"Checking completed sells from first order date: {check_date}")
        
        all_completed = await self.bybit.get_completed_sell_orders(symbol, from_date=check_date)
        logger.info(f"Found {len(all_completed)} completed sell orders for {symbol} since {check_date}")
        
        if not all_completed:
            logger.info("No completed sell orders found")
            return []
        
        active_sell_orders = self.db.get_active_sell_orders(symbol)
        active_order_ids = {o['order_id'] for o in active_sell_orders}
        
        conn = sqlite3.connect(self.db.db_file, timeout=5)
        cursor = conn.cursor()
        cursor.execute('SELECT order_id FROM sell_orders WHERE symbol = ?', (symbol,))
        all_our_order_ids = {row[0] for row in cursor.fetchall()}
        conn.close()
        
        our_completed = []
        for sell in all_completed:
            is_our_order = sell['order_id'] in active_order_ids or sell['order_id'] in all_our_order_ids
            
            if not is_our_order:
                logger.info(f"Skipping sell order {sell['order_id']} - not our order")
                continue
            
            already_notified = self.db.is_sell_notified_by_order_id(sell['order_id'])
            if already_notified:
                logger.info(f"Sell order {sell['order_id']} already notified, skipping")
                continue
            
            stats = self.db.get_dca_stats(symbol)
            if stats and stats['total_quantity'] > 0:
                avg_price = stats['avg_price']
                profit_percent = ((sell['sell_price'] - avg_price) / avg_price) * 100
                profit_usdt = (sell['sell_price'] - avg_price) * sell['quantity']
                total_invested = stats['total_usdt']
            else:
                profit_percent = 0
                profit_usdt = 0
                total_invested = 0
            
            days_invested = 0
            if first_order_date:
                days_invested = (get_moscow_time_naive() - first_order_date).days
                if days_invested <= 0:
                    days_invested = 1
            
            apy = calculate_apy(profit_usdt, total_invested, days_invested) if total_invested > 0 else 0.0
            
            sell_id = self.db.add_completed_sell(
                symbol=symbol,
                order_id=sell['order_id'],
                quantity=sell['quantity'],
                sell_price=sell['sell_price'],
                profit_percent=profit_percent,
                profit_usdt=profit_usdt
            )
            
            now = get_moscow_time_naive()
            deadline = now.replace(hour=23, minute=59, second=59, microsecond=0)
            if now.hour >= 23 and now.minute >= 59:
                deadline = deadline + timedelta(days=1)
            self.db.set_clear_deadline(sell_id, deadline)
            
            our_completed.append({
                'id': sell_id,
                'order_id': sell['order_id'],
                'quantity': sell['quantity'],
                'sell_price': sell['sell_price'],
                'amount_usdt': sell['amount_usdt'],
                'executed_at': sell['executed_at'],
                'profit_percent': profit_percent,
                'profit_usdt': profit_usdt,
                'total_invested': total_invested,
                'apy': apy,
                'days_invested': days_invested
            })
            
            self.db.update_sell_order_status(sell['order_id'], 'completed')
        
        for sell in our_completed:
            message = self._format_sell_notification(sell, symbol)
            
            deadline = self.db.get_clear_deadline(sell['id'])
            if deadline:
                seconds_left = max(0, int((deadline - get_moscow_time_naive()).total_seconds()))
                time_left_str = format_time_remaining(seconds_left)
                message += f"\n\n⏰ *Автоматическая очистка через:* {time_left_str}"
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Да, очистить статистику сейчас", callback_data=f"confirm_clear_stats_{symbol}_{sell['id']}"),
                 InlineKeyboardButton("❌ Нет, оставить", callback_data=f"skip_clear_stats_{symbol}_{sell['id']}")]
            ])
            await safe_send_message(bot, user_id, message, parse_mode='HTML', reply_markup=keyboard)
            logger.info(f"Sent completed sell notification for {symbol} to user {user_id}")
            self.db.mark_completed_sell_notified(sell['id'])
        
        return our_completed
    
    async def auto_clear_expired_stats(self, symbol: str, user_id: int, bot):
        conn = sqlite3.connect(self.db.db_file, timeout=5)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        now = get_moscow_time_naive()
        cursor.execute('''
            SELECT id, symbol, order_id FROM completed_sells 
            WHERE notified = 1 AND stats_cleared = 0 AND clear_deadline IS NOT NULL AND clear_deadline <= ?
        ''', (now.isoformat(),))
        
        expired_sells = cursor.fetchall()
        conn.close()
        
        for sell in expired_sells:
            sell_id = sell['id']
            sym = sell['symbol']
            
            deleted_count = self.db.clear_all_purchases(sym)
            if deleted_count > 0:
                self.db.log_action('AUTO_STATS_CLEARED', sym, f"Автоматическая очистка после дедлайна, удалено {deleted_count} покупок")
                self.db.mark_completed_sell_stats_cleared(sell_id)
                
                msg = (f"🔄 *Автоматическая очистка статистики*\n\n"
                       f"🪙 Токен: `{sym}`\n"
                       f"🗑 Удалено покупок: `{deleted_count}`\n\n"
                       f"📊 Начинаем новый цикл накопления.")
                await safe_send_message(bot, user_id, msg, parse_mode='Markdown')
                logger.info(f"Auto cleared stats for {sym}")
    
    async def get_recommended_purchase(self, symbol: str) -> Dict:
        current_price = await self.bybit.get_symbol_price(symbol)
        if not current_price:
            return {'success': False, 'error': 'Не удалось получить цену'}
        ladder_info = self.db.calculate_ladder_purchase(current_price, symbol)
        if ladder_info['should_buy']:
            return {'success': True, 'should_buy': True, 'amount_usdt': ladder_info['amount_usdt'],
                   'step_level': ladder_info['step_level'], 'target_price': ladder_info['target_price'],
                   'drop_percent': ladder_info.get('drop_percent', 0), 'reason': ladder_info['reason'],
                   'current_price': current_price, 'current_drop': ladder_info.get('current_drop', 0)}
        else:
            return {'success': True, 'should_buy': False, 'reason': ladder_info['reason'],
                   'current_price': current_price, 'next_buy_price': ladder_info['target_price'],
                   'next_drop': ladder_info.get('next_drop', 0), 'current_drop': ladder_info.get('current_drop', 0)}
    
    def calculate_target_info(self, stats: Dict, profit_percent: float) -> Dict:
        if not stats or stats['total_quantity'] <= 0:
            return None
        total_qty = stats['total_quantity']
        avg_price = stats['avg_price']
        target_price = avg_price * (1 + profit_percent / 100)
        target_value = total_qty * target_price
        total_cost = stats['total_usdt']
        target_profit = target_value - total_cost
        return {
            'target_price': target_price,
            'target_value': target_value,
            'target_profit': target_profit,
            'total_qty': total_qty,
            'avg_price': avg_price,
            'profit_percent': profit_percent
        }
    
    async def check_new_orders_incremental(self, symbol: str, user_id: int, bot) -> List[Dict]:
        last_check = self.db.get_last_incremental_check_time()
        first_order_date = self.db.get_first_order_date()
        
        if last_check is None:
            if first_order_date is None:
                last_check = get_moscow_time_naive() - timedelta(days=90)
            else:
                last_check = first_order_date - timedelta(days=1)
        
        check_date = last_check
        all_orders = await self.bybit.get_all_executed_orders(symbol, from_date=check_date)
        
        self.db.set_last_incremental_check_time(get_moscow_time_naive())
        
        purchases = self.db.get_purchases(symbol)
        added_orders = set()
        for p in purchases:
            added_orders.add(f"{round(p['price'], 4)}_{round(p['quantity'], 8)}")
        
        conn = sqlite3.connect(self.db.db_file, timeout=5)
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT order_id, added_to_stats, skipped FROM executed_orders WHERE symbol = ?', (symbol,))
            executed_records = cursor.fetchall()
        except Exception as e:
            executed_records = []
        conn.close()
        
        processed_order_ids = set()
        for record in executed_records:
            added_to_stats = record[1] if len(record) > 1 else 0
            skipped = record[2] if len(record) > 2 else 0
            if added_to_stats == 1 or skipped == 1:
                processed_order_ids.add(record[0])
        
        new_orders = []
        for order in all_orders:
            if order['order_id'] in processed_order_ids:
                continue
            
            if f"{round(order['price'], 4)}_{round(order['quantity'], 8)}" in added_orders:
                self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'], order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                self.db.mark_order_as_added(order['order_id'])
                continue
            
            if self.db.is_order_already_added(order['order_id']):
                self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'], order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                self.db.mark_order_as_added(order['order_id'])
                logger.info(f"Order {order['order_id']} already in dca_purchases, marking as added")
                continue
            
            self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'], order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
            new_orders.append(order)
        
        for order in new_orders:
            msg = (f"✅ *ОРДЕР ИСПОЛНЕН!*\n\n"
                   f"🪙 Токен: `{symbol}`\n"
                   f"💰 Цена: `{format_price(order['price'], 4)}` USDT\n"
                   f"📊 Количество: `{format_quantity(order['quantity'], 5)}`\n"
                   f"💵 Сумма: `{order['amount_usdt']:.2f}` USDT\n"
                   f"🕐 Время: `{order['executed_at'].strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
                   f"❗ *Добавить в статистику покупок?*")
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Добавить", callback_data=f"add_order_{order['order_id']}"),
                InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_order_{order['order_id']}")
            ]])
            if user_id:
                await safe_send_message(bot, user_id, msg, parse_mode='Markdown', reply_markup=keyboard)
                logger.info(f"Sent order notification for {order['order_id']} to user {user_id}")
        
        return new_orders
    
    async def full_check_missing_orders(self, symbol: str, user_id: int, bot) -> List[Dict]:
        first_order_date = self.db.get_first_order_date()
        if first_order_date is None:
            first_order_date = get_moscow_time_naive() - timedelta(days=90)
        check_date = first_order_date - timedelta(days=1)
        all_orders = await self.bybit.get_all_executed_orders(symbol, from_date=check_date)
        
        purchases = self.db.get_purchases(symbol)
        added_orders = set()
        for p in purchases:
            added_orders.add(f"{round(p['price'], 4)}_{round(p['quantity'], 8)}")
        
        conn = sqlite3.connect(self.db.db_file, timeout=5)
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT order_id, added_to_stats, skipped FROM executed_orders WHERE symbol = ?', (symbol,))
            executed_records = cursor.fetchall()
        except Exception as e:
            executed_records = []
        conn.close()
        
        processed_order_ids = set()
        for record in executed_records:
            added_to_stats = record[1] if len(record) > 1 else 0
            skipped = record[2] if len(record) > 2 else 0
            if added_to_stats == 1 or skipped == 1:
                processed_order_ids.add(record[0])
        
        missing_orders = []
        for order in all_orders:
            if order['order_id'] in processed_order_ids:
                continue
            
            if f"{round(order['price'], 4)}_{round(order['quantity'], 8)}" in added_orders:
                self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'], order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                self.db.mark_order_as_added(order['order_id'])
                continue
            
            if self.db.is_order_already_added(order['order_id']):
                self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'], order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                self.db.mark_order_as_added(order['order_id'])
                logger.info(f"Order {order['order_id']} already in dca_purchases, marking as added")
                continue
            
            existing = False
            for record in executed_records:
                if record[0] == order['order_id']:
                    existing = True
                    break
            if not existing:
                self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'], order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
            missing_orders.append(order)
        
        for order in missing_orders:
            msg = (f"✅ *ОРДЕР ИСПОЛНЕН!*\n\n"
                   f"🪙 Токен: `{symbol}`\n"
                   f"💰 Цена: `{format_price(order['price'], 4)}` USDT\n"
                   f"📊 Количество: `{format_quantity(order['quantity'], 5)}`\n"
                   f"💵 Сумма: `{order['amount_usdt']:.2f}` USDT\n"
                   f"🕐 Время: `{order['executed_at'].strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
                   f"❗ *Добавить в статистику покупок?*")
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Добавить", callback_data=f"add_order_{order['order_id']}"),
                InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_order_{order['order_id']}")
            ]])
            if user_id:
                await safe_send_message(bot, user_id, msg, parse_mode='Markdown', reply_markup=keyboard)
                logger.info(f"Sent full-check order notification for {order['order_id']} to user {user_id}")
        
        self.db.set_last_full_check_time(get_moscow_time_naive())
        return missing_orders
    
    async def auto_check_and_notify(self, symbol: str, user_id: int, bot) -> Dict:
        last_full_check = self.db.get_last_full_check_time()
        now = get_moscow_time_naive()
        need_full_check = False
        if last_full_check is None:
            need_full_check = True
        else:
            if now.date() > last_full_check.date():
                if now.hour >= 19:
                    need_full_check = True
            elif now.date() == last_full_check.date() and last_full_check.hour < 19 and now.hour >= 19:
                need_full_check = True
        
        if need_full_check:
            missing_orders = await self.full_check_missing_orders(symbol, user_id, bot)
            return {'type': 'full', 'count': len(missing_orders), 'orders': missing_orders}
        else:
            new_orders = await self.check_new_orders_incremental(symbol, user_id, bot)
            return {'type': 'incremental', 'count': len(new_orders), 'orders': new_orders}
    
    async def force_check_executed_orders(self, symbol: str, bot, user_id: int) -> Dict:
        first_order_date = self.db.get_first_order_date()
        if first_order_date is None:
            first_order_date = get_moscow_time_naive() - timedelta(days=90)
        check_date = first_order_date - timedelta(days=1)
        all_orders = await self.bybit.get_all_executed_orders(symbol, from_date=check_date)
        purchases = self.db.get_purchases(symbol)
        added_orders = set()
        for p in purchases:
            added_orders.add(f"{round(p['price'], 4)}_{round(p['quantity'], 8)}")
        conn = sqlite3.connect(self.db.db_file, timeout=5)
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT order_id, added_to_stats, skipped, price, quantity FROM executed_orders WHERE symbol = ?', (symbol,))
            executed_records = cursor.fetchall()
        except Exception as e:
            executed_records = []
        conn.close()
        processed_order_ids = set()
        for record in executed_records:
            added_to_stats = record[1] if len(record) > 1 else 0
            skipped = record[2] if len(record) > 2 else 0
            if added_to_stats == 1 or skipped == 1:
                processed_order_ids.add(record[0])
        missing_orders = []
        already_added = []
        for order in all_orders:
            if order['order_id'] in processed_order_ids:
                already_added.append(order)
                continue
            
            if self.db.is_order_already_added(order['order_id']):
                already_added.append(order)
                self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'], order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                self.db.mark_order_as_added(order['order_id'])
                continue
            
            if f"{round(order['price'], 4)}_{round(order['quantity'], 8)}" in added_orders:
                already_added.append(order)
                self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'], order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                self.db.mark_order_as_added(order['order_id'])
            else:
                existing = False
                for record in executed_records:
                    if record[0] == order['order_id']:
                        existing = True
                        break
                if not existing:
                    self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'], order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                missing_orders.append(order)
        
        return {
            'total_found': len(all_orders),
            'already_added': len(already_added),
            'missing': missing_orders,
            'check_date': check_date
        }
    
    async def force_check_completed_sells(self, symbol: str, bot, user_id: int) -> Dict:
        first_order_date = self.db.get_first_order_date()
        
        if first_order_date is None:
            check_date = get_moscow_time_naive() - timedelta(days=30)
            logger.info(f"No first order date, checking last 30 days from {check_date}")
        else:
            check_date = first_order_date - timedelta(days=1)
            logger.info(f"Force check: checking from first order date {check_date}")
        
        all_completed = await self.bybit.get_completed_sell_orders(symbol, from_date=check_date)
        logger.info(f"Force check: found {len(all_completed)} completed sell orders for {symbol} since {check_date}")
        
        conn = sqlite3.connect(self.db.db_file, timeout=5)
        cursor = conn.cursor()
        cursor.execute('SELECT order_id FROM sell_orders WHERE symbol = ?', (symbol,))
        our_order_ids = {row[0] for row in cursor.fetchall()}
        conn.close()
        
        already_processed = self.db.get_completed_sells_not_notified(symbol)
        processed_order_ids = set([s['order_id'] for s in already_processed])
        
        missing_sells = []
        for sell in all_completed:
            if sell['order_id'] in processed_order_ids:
                logger.info(f"Sell order {sell['order_id']} already processed, skipping")
                continue
            
            if sell['order_id'] not in our_order_ids:
                logger.info(f"Skipping sell order {sell['order_id']} - not our order")
                continue
            
            stats = self.db.get_dca_stats(symbol)
            if stats and stats['total_quantity'] > 0:
                avg_price = stats['avg_price']
                profit_percent = ((sell['sell_price'] - avg_price) / avg_price) * 100
                profit_usdt = (sell['sell_price'] - avg_price) * sell['quantity']
                total_invested = stats['total_usdt']
            else:
                profit_percent = 0
                profit_usdt = 0
                total_invested = 0
            
            days_invested = 0
            if first_order_date:
                days_invested = (get_moscow_time_naive() - first_order_date).days
                if days_invested <= 0:
                    days_invested = 1
            
            apy = calculate_apy(profit_usdt, total_invested, days_invested) if total_invested > 0 else 0.0
            
            sell_id = self.db.add_completed_sell(
                symbol=symbol,
                order_id=sell['order_id'],
                quantity=sell['quantity'],
                sell_price=sell['sell_price'],
                profit_percent=profit_percent,
                profit_usdt=profit_usdt
            )
            
            now = get_moscow_time_naive()
            deadline = now.replace(hour=23, minute=59, second=59, microsecond=0)
            if now.hour >= 23 and now.minute >= 59:
                deadline = deadline + timedelta(days=1)
            self.db.set_clear_deadline(sell_id, deadline)
            
            missing_sells.append({
                'id': sell_id,
                'order_id': sell['order_id'],
                'quantity': sell['quantity'],
                'sell_price': sell['sell_price'],
                'amount_usdt': sell['amount_usdt'],
                'executed_at': sell['executed_at'],
                'profit_percent': profit_percent,
                'profit_usdt': profit_usdt,
                'total_invested': total_invested,
                'apy': apy,
                'days_invested': days_invested
            })
            self.db.update_sell_order_status(sell['order_id'], 'completed')
        
        return {
            'total_found': len(all_completed),
            'already_processed': len(already_processed),
            'missing': missing_sells,
            'check_date': check_date
        }
    
    async def place_full_sell_order(self, update, symbol: str, profit_percent: float, auto_cancel_old: bool = True) -> Dict:
        try:
            stats = self.db.get_dca_stats(symbol)
            if not stats or stats['total_quantity'] <= 0:
                return {'success': False, 'error': 'Нет купленных активов для продажи'}
            
            coin = symbol.replace('USDT', '')
            
            if auto_cancel_old:
                open_orders = await self.bybit.get_open_orders(symbol)
                existing_sell_orders = [o for o in open_orders if o.get('side') == 'Sell']
                if existing_sell_orders:
                    if update and hasattr(update, 'message'):
                        await update.message.reply_text(f"🔄 Обнаружено {len(existing_sell_orders)} старых ордеров на продажу. Отменяю их...")
                    cancelled_count, cancelled_ids = await self.bybit.cancel_all_sell_orders(symbol)
                    if cancelled_count > 0:
                        for order_id in cancelled_ids:
                            self.db.update_sell_order_status(order_id, 'cancelled')
                        if update and hasattr(update, 'message'):
                            await update.message.reply_text(f"✅ Отменено {cancelled_count} старых ордеров.")
                        await asyncio.sleep(2)
                    else:
                        logger.warning("Не удалось отменить старые ордера, но продолжаем...")
            
            balance_info = await self.bybit.get_balance(coin)
            if not balance_info or 'equity' not in balance_info:
                return {'success': False, 'error': 'Не удалось получить баланс монеты'}
            
            actual_balance = balance_info.get('equity', 0)
            logger.info(f"Actual balance for {coin}: {actual_balance}")
            
            if actual_balance <= 0:
                return {'success': False, 'error': f'Доступный баланс {coin} равен 0.'}
            
            avg_price = stats['avg_price']
            raw_target_price = avg_price * (1 + profit_percent / 100)
            instrument_info = await self.bybit.get_instrument_info(symbol)
            tick_size = instrument_info['tick_size']
            qty_decimals = instrument_info.get('qty_decimals', SELL_DECIMALS_FALLBACK)
            rounded_price = self.bybit._round_price_by_tick(raw_target_price, tick_size)
            if rounded_price <= 0:
                rounded_price = tick_size
            
            min_qty = instrument_info['min_qty']
            min_amt = instrument_info['min_amt']
            
            sell_qty = self.bybit._round_quantity_for_sell(actual_balance, qty_decimals)
            
            if sell_qty < min_qty and actual_balance >= min_qty:
                for decimals in range(qty_decimals, 0, -1):
                    factor = 10 ** decimals
                    test_rounded = math.floor(actual_balance * factor) / factor
                    if test_rounded >= min_qty:
                        sell_qty = test_rounded
                        break
            
            logger.info(f"Selling {sell_qty} {coin} (actual_balance={actual_balance}, decimals={qty_decimals})")
            
            if sell_qty <= 0:
                return {'success': False, 'error': f'Недостаточно средств для продажи. Доступно: {actual_balance:.8f} {coin}'}
            
            if sell_qty < min_qty:
                return {'success': False, 'error': f'Доступное количество ({actual_balance:.8f}) меньше минимального ({min_qty})'}
            
            order_value = sell_qty * rounded_price
            if order_value < min_amt:
                pending_id = self.db.add_pending_sell_order(
                    symbol=symbol,
                    quantity=sell_qty,
                    target_price=rounded_price,
                    profit_percent=profit_percent,
                    fail_reason=f'Сумма ордера ({order_value:.2f} USDT) меньше минимальной ({min_amt} USDT)'
                )
                msg = (f"⏳ *ОРДЕР ОТЛОЖЕН*\n\n"
                       f"🪙 Токен: `{symbol}`\n"
                       f"📊 Количество: `{format_quantity(sell_qty, 5)}` {coin}\n"
                       f"💰 Целевая цена: `{format_price(rounded_price, 4)}` USDT\n"
                       f"📈 Целевая прибыль: `{profit_percent}%`\n\n"
                       f"⚠️ *Сумма ордера ({order_value:.2f} USDT) меньше минимальной ({min_amt} USDT)*\n\n"
                       f"🔄 Ордер будет автоматически выставлен при достижении целевой цены.\n"
                       f"🔄 Повторная попытка через 5 минут.")
                if update and hasattr(update, 'message'):
                    await update.message.reply_text(msg, parse_mode='Markdown')
                return {'success': False, 'pending': True, 'pending_id': pending_id, 'error': 'min_amount_error', 'message': msg}
            
            if update and hasattr(update, 'message'):
                await update.message.reply_text(f"📤 Выставляю ордер на продажу {format_quantity(sell_qty, 5)} {coin} по {format_price(rounded_price, 4)} USDT...")
            
            result = await self.bybit.place_limit_sell(symbol, sell_qty, rounded_price)
            if result['success']:
                self.db.add_sell_order(
                    symbol=symbol,
                    order_id=result['order_id'],
                    quantity=sell_qty,
                    target_price=rounded_price,
                    profit_percent=profit_percent
                )
                self.db.log_action('FULL_SELL_ORDER', symbol, f"Ордер на продажу {sell_qty:.5f} {coin} по {rounded_price:.4f} USDT")
                
                warning_msg = ""
                if sell_qty < stats['total_quantity']:
                    diff = stats['total_quantity'] - sell_qty
                    warning_msg = f"\n⚠️ Продано только {format_quantity(sell_qty, 5)} из {format_quantity(stats['total_quantity'], 5)} {coin}."
                
                return {
                    'success': True,
                    'order_id': result['order_id'],
                    'quantity': sell_qty,
                    'price': rounded_price,
                    'raw_price': raw_target_price,
                    'profit_percent': profit_percent,
                    'warning': warning_msg
                }
            elif result.get('error') == 'min_amount_error':
                pending_id = self.db.add_pending_sell_order(
                    symbol=symbol,
                    quantity=sell_qty,
                    target_price=rounded_price,
                    profit_percent=profit_percent,
                    fail_reason=f'Минимальная сумма ордера: {min_amt} USDT'
                )
                msg = (f"⏳ *ОРДЕР ОТЛОЖЕН*\n\n"
                       f"🪙 Токен: `{symbol}`\n"
                       f"📊 Количество: `{format_quantity(sell_qty, 5)}` {coin}\n"
                       f"💰 Целевая цена: `{format_price(rounded_price, 4)}` USDT\n"
                       f"📈 Целевая прибыль: `{profit_percent}%`\n\n"
                       f"⚠️ *Сумма ордера ({order_value:.2f} USDT) меньше минимальной ({min_amt} USDT)*\n\n"
                       f"🔄 Ордер будет автоматически выставлен при достижении целевой цены.\n"
                       f"🔄 Повторная попытка через 5 минут.")
                if update and hasattr(update, 'message'):
                    await update.message.reply_text(msg, parse_mode='Markdown')
                return {'success': False, 'pending': True, 'pending_id': pending_id, 'error': result.get('error'), 'message': msg}
            elif result.get('error') == 'insufficient_balance':
                pending_id = self.db.add_pending_sell_order(
                    symbol=symbol,
                    quantity=sell_qty,
                    target_price=rounded_price,
                    profit_percent=profit_percent,
                    fail_reason='Недостаточно средств на балансе (баланс обновляется)'
                )
                msg = (f"⏳ *ОРДЕР ОТЛОЖЕН*\n\n"
                       f"🪙 Токен: `{symbol}`\n"
                       f"📊 Количество: `{format_quantity(sell_qty, 5)}` {coin}\n"
                       f"💰 Целевая цена: `{format_price(rounded_price, 4)}` USDT\n"
                       f"📈 Целевая прибыль: `{profit_percent}%`\n\n"
                       f"⚠️ *Недостаточно средств на балансе (баланс обновляется)*\n\n"
                       f"✅ Ордер сохранен и будет автоматически создан после обновления баланса.\n"
                       f"🔄 Повторная попытка через 5 минут.")
                if update and hasattr(update, 'message'):
                    await update.message.reply_text(msg, parse_mode='Markdown')
                return {'success': False, 'pending': True, 'pending_id': pending_id, 'error': result.get('error'), 'message': msg}
            elif result.get('error') == 'quantity_decimals_error':
                for decimals in range(qty_decimals - 1, 0, -1):
                    factor = 10 ** decimals
                    retry_qty = math.floor(sell_qty * factor) / factor
                    if retry_qty >= min_qty and retry_qty != sell_qty:
                        logger.info(f"Retrying with rounded quantity: {retry_qty} (decimals={decimals})")
                        return await self.place_full_sell_order(update, symbol, profit_percent, auto_cancel_old=False)
                
                pending_id = self.db.add_pending_sell_order(
                    symbol=symbol,
                    quantity=sell_qty,
                    target_price=rounded_price,
                    profit_percent=profit_percent,
                    fail_reason=f'Ошибка формата количества'
                )
                msg = (f"⏳ *ОРДЕР ОТЛОЖЕН*\n\n"
                       f"🪙 Токен: `{symbol}`\n"
                       f"📊 Количество: `{format_quantity(sell_qty, 5)}` {coin}\n"
                       f"💰 Целевая цена: `{format_price(rounded_price, 4)}` USDT\n"
                       f"📈 Целевая прибыль: `{profit_percent}%`\n\n"
                       f"⚠️ *Ошибка формата количества*\n\n"
                       f"✅ Ордер сохранен и будет автоматически восстановлен.")
                if update and hasattr(update, 'message'):
                    await update.message.reply_text(msg, parse_mode='Markdown')
                return {'success': False, 'pending': True, 'pending_id': pending_id, 'error': result.get('error'), 'message': msg}
            else:
                return {'success': False, 'error': result.get('error', 'Ошибка создания ордера')}
        except Exception as e:
            logger.error(f"Error placing full sell order: {e}")
            return {'success': False, 'error': str(e)}


class FastDCABot:
    def __init__(self):
        self.db = Database()
        self.bybit = None
        self.strategy = None
        self.bybit_initialized = False
        self.import_waiting = False
        self.scheduler_running = False
        self.background_tasks = []
        self._sell_check_task = None
        self._api_check_task = None
        self._api_was_working = False
        self._api_error_count = 0
        self._is_running = False
        
        request_kwargs = {'connect_timeout': 60.0, 'read_timeout': 60.0, 'write_timeout': 60.0, 'pool_timeout': 60.0}
        request = HTTPXRequest(**request_kwargs)
        builder = Application.builder().token(TELEGRAM_TOKEN).request(request)
        self.application = builder.build()
        
        self.authorized_user_id = self.db.get_authorized_user_id()
        self.pending_executed_order = None
        
        self.setup_handlers()
    
    def _init_bybit(self, force_reload: bool = False):
        api_key, api_secret = get_api_keys()
        
        if not api_key or not api_secret:
            logger.warning("API keys missing in .env")
            self.bybit_initialized = False
            self.bybit = None
            return
        
        try:
            testnet = self.db.is_demo_mode()
            self.bybit = BybitClient(api_key, api_secret, testnet)
            self.strategy = DCAStrategy(self.db, self.bybit)
            self.bybit_initialized = True
            logger.info(f"Bybit client initialized with fresh keys (demo={testnet})")
        except Exception as e:
            logger.error(f"Bybit init error: {e}")
            self.bybit_initialized = False
    
    def refresh_api_session(self):
        logger.info("Refreshing API session...")
        self.bybit_initialized = False
        self.bybit = None
        self._init_bybit(force_reload=True)
        return self.bybit_initialized
    
    async def check_api_and_notify(self, is_startup: bool = False) -> bool:
        self.refresh_api_session()
        
        if not self.bybit_initialized:
            self._init_bybit()
            if not self.bybit_initialized:
                return False
        
        health = await self.bybit.check_api_health()
        user_id = self.authorized_user_id
        
        if health['success']:
            if not self._api_was_working:
                self._api_was_working = True
                self._api_error_count = 0
                self.db.set_api_status('working')
                self.db.set_api_error_message('')
                
                if user_id and not is_startup:
                    message = (
                        "✅ *API Bybit восстановлен!*\n\n"
                        "🔑 Ключи работают корректно.\n"
                        "🕐 Время проверки: `{}`"
                    ).format(get_moscow_time().strftime('%H:%M:%S'))
                    await safe_send_message(self.application.bot, user_id, message, parse_mode='Markdown')
                    logger.info("API recovery notification sent")
            return True
        else:
            self._api_was_working = False
            self._api_error_count += 1
            self.db.set_api_status('error')
            self.db.set_api_error_message(health.get('user_message', 'Неизвестная ошибка'))
            
            if user_id and (is_startup or self._api_error_count % 3 == 0):
                error_code = health.get('error_code', 'N/A')
                user_message = health.get('user_message', 'Неизвестная ошибка')
                
                message = (
                    "🚨 *ОШИБКА API BYBIT!*\n\n"
                    "❌ Статус: НЕ РАБОТАЕТ\n"
                    "📝 Ошибка: {}\n"
                    "🔢 Код: {}\n\n"
                    "⚠️ *Что делать:*\n"
                    "1️⃣ Проверьте API ключ в файле `.env`\n"
                    "2️⃣ Убедитесь, что ключ активен\n"
                    "3️⃣ Проверьте права доступа\n"
                    "4️⃣ Проверьте IP в белом списке Bybit\n\n"
                    "🔄 Бот будет проверять доступ каждые 6 часов."
                ).format(user_message, error_code)
                await safe_send_message(self.application.bot, user_id, message, parse_mode='Markdown')
                logger.info(f"API error notification sent (attempt {self._api_error_count})")
            return False
    
    async def cmd_check_api(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        
        await update.message.reply_text("🔍 Проверяю API ключ...")
        
        self.refresh_api_session()
        
        if not self.bybit_initialized:
            await update.message.reply_text("❌ API не инициализирован. Проверьте .env файл.")
            return
        
        health = await self.bybit.check_api_health()
        
        if health['success']:
            self._api_was_working = True
            self.db.set_api_status('working')
            self.db.set_api_error_message('')
            message = (
                "✅ *API Bybit работает корректно!*\n\n"
                "🔑 Ключ активен и имеет необходимые права.\n"
                "🕐 Время проверки: `{}`"
            ).format(get_moscow_time().strftime('%H:%M:%S'))
            await safe_send_message(self.application.bot, update.effective_user.id, message, parse_mode='Markdown')
        else:
            error_code = health.get('error_code', 'N/A')
            user_message = health.get('user_message', 'Неизвестная ошибка')
            
            message = (
                "🚨 *КРИТИЧЕСКАЯ ОШИБКА API BYBIT!*\n\n"
                "❌ Статус: `НЕ РАБОТАЕТ`\n"
                "📝 Ошибка: `{}`\n"
                "🔢 Код: `{}`\n\n"
                "⚠️ *Что делать:*\n"
                "1️⃣ Проверьте API ключ в файле `.env`\n"
                "2️⃣ Убедитесь, что ключ активен (выдается на 90 дней)\n"
                "3️⃣ Проверьте права доступа (нужны: spot trade, wallet read)\n"
                "4️⃣ Проверьте IP в белом списке Bybit"
            ).format(user_message, error_code)
            await safe_send_message(self.application.bot, update.effective_user.id, message, parse_mode='Markdown')
    
    async def cmd_refresh_api(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        
        await update.message.reply_text("🔄 Обновляю API ключи из .env...")
        
        load_dotenv()
        api_key = os.getenv('BYBIT_API_KEY')
        api_secret = os.getenv('BYBIT_API_SECRET')
        
        if not api_key or not api_secret:
            await update.message.reply_text("❌ Ключи не найдены в .env файле!")
            return
        
        await update.message.reply_text(f"✅ Ключи найдены:\nAPI Key: {api_key[:8]}...{api_key[-4:]}")
        
        self.refresh_api_session()
        
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Не удалось создать сессию Bybit")
            return
        
        await update.message.reply_text("🔍 Проверяю работоспособность ключей...")
        health = await self.bybit.check_api_health()
        
        if health['success']:
            self._api_was_working = True
            self.db.set_api_status('working')
            self.db.set_api_error_message('')
            await update.message.reply_text(
                "✅ *API Bybit работает корректно!*\n\n"
                "🔑 Ключи актуальны и имеют необходимые права.",
                parse_mode='Markdown'
            )
        else:
            error_code = health.get('error_code', 'N/A')
            user_message = health.get('user_message', 'Неизвестная ошибка')
            message = (
                "❌ *Ошибка API*\n\n"
                "📝 {}\n"
                "🔢 Код: {}\n\n"
                "Проверьте ключи в .env файле."
            ).format(user_message, error_code)
            await safe_send_message(self.application.bot, update.effective_user.id, message, parse_mode='Markdown')
    
    async def _check_user_fast(self, update: Update) -> bool:
        user = update.effective_user
        username = f"@{user.username}" if user.username else f"ID:{user.id}"
        if self.authorized_user_id is None:
            if username == AUTHORIZED_USER:
                self.authorized_user_id = user.id
                self.db.set_authorized_user_id(user.id)
                logger.info(f"Authorized user ID saved: {user.id}")
                return True
        elif user.id == self.authorized_user_id:
            return True
        await update.message.reply_text("⛔ Доступ запрещен")
        return False
    
    async def _reset_bot_state(self, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        self.import_waiting = False
    
    def get_main_keyboard(self):
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        dca_button = "⏹ Остановить Авто DCA" if is_active else "🚀 Запустить Авто DCA"
        keyboard = [
            [KeyboardButton("📊 Мой Портфель"), KeyboardButton(dca_button)],
            [KeyboardButton("💰 Ручная покупка (лимит)"), KeyboardButton("📈 Статистика DCA")],
            [KeyboardButton("➕ Добавить покупку вручную"), KeyboardButton("✏️ Редактировать покупки")],
            [KeyboardButton("⚙️ Настройки"), KeyboardButton("📝 Управление ордерами")],
            [KeyboardButton("📋 Статус бота")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_order_management_keyboard(self):
        keyboard = [
            [KeyboardButton("📋 Список открытых ордеров"), KeyboardButton("❌ Удалить ордер")],
            [KeyboardButton("🔙 Назад в меню")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_tracking_settings_keyboard(self):
        current_status = self.db.get_order_execution_notify()
        sell_tracking = self.db.get_sell_tracking_enabled()
        current_interval = self.db.get_order_check_interval()
        tracking_button = "✅ Отслеживание ордеров Вкл" if current_status else "❌ Отслеживание ордеров Выкл"
        sell_tracking_button = "💰 Отслеживание продаж Вкл" if sell_tracking else "⏳ Отслеживание продаж Выкл"
        keyboard = [
            [KeyboardButton(tracking_button)],
            [KeyboardButton(sell_tracking_button)],
            [KeyboardButton(f"⏱ Интервал проверки Ордеров {current_interval} мин")],
            [KeyboardButton("🔍 Тест отслеживания")],
            [KeyboardButton("🔙 Назад в настройки")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_purchase_notify_settings_keyboard(self):
        enabled = self.db.get_purchase_notify_enabled()
        notify_time = self.db.get_purchase_notify_time()
        status_button = "🔔 Уведомления Вкл" if enabled else "🔕 Уведомления Выкл"
        keyboard = [
            [KeyboardButton(status_button)],
            [KeyboardButton(f"⏰ Время уведомления ({notify_time})")],
            [KeyboardButton("🔙 Назад в настройки")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_auto_dca_keyboard(self):
        schedule_time = self.db.get_setting('schedule_time', '09:00')
        frequency_hours = self.db.get_setting('frequency_hours', '24')
        invest_amount = self.db.get_setting('invest_amount', '5.0')
        keyboard = [
            [KeyboardButton(f"💵 Сумма покупки авто ({invest_amount} USDT)")],
            [KeyboardButton(f"⏰ Время покупки ({schedule_time})")],
            [KeyboardButton(f"🔄 Частота покупки ({frequency_hours} ч)")],
            [KeyboardButton("🔙 Назад в настройки")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_cancel_keyboard(self):
        return ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    
    def get_sell_confirmation_keyboard(self):
        return ReplyKeyboardMarkup([
            [KeyboardButton("✅ Да, выставить ордер на продажу")],
            [KeyboardButton("❌ Нет, отмена")]
        ], resize_keyboard=True)
    
    def get_settings_keyboard(self):
        mode = self.db.get_trading_mode()
        mode_button = "🌐 Режим: Демо" if mode == 'demo' else "🌐 Режим: Обычный"
        manual_amount = self.db.get_manual_amount()
        keyboard = [
            [KeyboardButton("🪙 Выбор токена"), KeyboardButton("🚀 Настройки Авто DCA")],
            [KeyboardButton("📊 Процент прибыли"), KeyboardButton("🪜 Лестница Мартингейла")],
            [KeyboardButton("💵 Сумма для ручного ордера"), KeyboardButton("⚙️ Настройки отслеживания")],
            [KeyboardButton("🔔 Уведомления о покупке"), KeyboardButton(mode_button)],
            [KeyboardButton("📤 Экспорт базы"), KeyboardButton("📥 Импорт базы")],
            [KeyboardButton("🔙 Назад в меню")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_ladder_settings_keyboard(self):
        keyboard = [
            [KeyboardButton("📉 Глубина просадки (%)"), KeyboardButton("💵 Базовая сумма")],
            [KeyboardButton("📋 Текущие настройки"), KeyboardButton("🔄 Сбросить лестницу")],
            [KeyboardButton("🔙 Назад в настройки")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_symbol_selection_keyboard(self):
        keyboard = []
        for symbol in POPULAR_SYMBOLS:
            keyboard.append([KeyboardButton(symbol)])
        keyboard.append([KeyboardButton("✏️ Ввести свой токен")])
        keyboard.append([KeyboardButton("❌ Отмена")])
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_edit_purchases_keyboard(self):
        keyboard = [
            [KeyboardButton("💰 Изменить цену"), KeyboardButton("📊 Изменить количество")],
            [KeyboardButton("📅 Изменить дату"), KeyboardButton("❌ Удалить покупку")],
            [KeyboardButton("🔙 Назад к списку"), KeyboardButton("🏠 Главное меню")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_confirm_delete_keyboard(self):
        return ReplyKeyboardMarkup([[KeyboardButton("✅ Да, удалить"), KeyboardButton("❌ Нет, отмена")]], resize_keyboard=True)
    
    def get_purchases_list_keyboard(self, purchases):
        keyboard = []
        for p in purchases:
            try:
                date_display = datetime.strptime(p['date'], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y")
            except:
                date_display = p['date'][:10] if p['date'] else "N/A"
            btn_text = f"ID{p['id']}: {date_display} - {format_quantity(p['quantity'], 5)} по {format_price(p['price'], 4)}"
            keyboard.append([KeyboardButton(btn_text)])
        keyboard.append([KeyboardButton("🏠 Главное меню")])
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    def get_manual_buy_keyboard(self):
        return ReplyKeyboardMarkup([[KeyboardButton("❌ Отмена")]], resize_keyboard=True)
    
    async def _end_conversation_gracefully(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        await update.message.reply_text("Действие отменено", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END
    
    def _calculate_next_purchase_time(self) -> datetime:
        schedule_time_str = self.db.get_setting('schedule_time', SCHEDULE_TIME)
        frequency_hours = int(self.db.get_setting('frequency_hours', str(FREQUENCY_HOURS)))
        
        schedule_hour, schedule_minute = map(int, schedule_time_str.split(':'))
        now = get_moscow_time()
        
        next_time = now.replace(hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0)
        
        while next_time <= now:
            next_time += timedelta(hours=frequency_hours)
        
        return next_time
    
    async def cmd_start_fast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        next_purchase_str = self.db.get_setting('next_dca_purchase_time', '')
        if next_purchase_str:
            try:
                next_time = datetime.fromisoformat(next_purchase_str)
                if get_moscow_time() >= next_time:
                    self.db.set_setting('next_dca_purchase_time', '')
                    logger.info("Reset next_dca_purchase_time because it was in the past")
            except:
                pass
        
        await self._reset_bot_state(context)
        current_time = get_moscow_time()
        mode = self.db.get_trading_mode()
        mode_text = "Демо-режим" if mode == 'demo' else "Обычный режим"
        
        api_status_text = "❌ НЕ РАБОТАЕТ"
        health = None
        self._init_bybit()
        if self.bybit_initialized:
            health = await self.bybit.check_api_health()
            if health['success']:
                api_status_text = "✅ РАБОТАЕТ"
                self._api_was_working = True
                self.db.set_api_status('working')
            else:
                api_status_text = f"❌ {health.get('user_message', 'Ошибка')}"
                self._api_was_working = False
                self.db.set_api_status('error')
                self.db.set_api_error_message(health.get('user_message', 'Неизвестная ошибка'))
        
        status_emoji = api_status_text.split()[0] if api_status_text.split() else api_status_text
        
        start_message = (
            f"👋 Привет, {update.effective_user.first_name}!\n\n"
            f"🤖 DCA Bybit Bot (Мартингейл лесенкой)\n"
            f"📌 Версия: {BOT_VERSION}\n"
            f"🌐 Режим: {mode_text}\n"
            f"🕐 Московское время: {current_time.strftime('%H:%M')}\n\n"
            f"🔑 *Статус API Bybit:* {api_status_text}\n\n"
            f"✅ Бот запущен и готов к работе!\n"
            f"🌐 Доступ к бирже Bybit по API ключу {status_emoji}\n\n"
            f"📋 Уведомления об исполненных ордерах будут приходить сюда.\n"
            f"🔄 Проверка API выполняется каждые 6 часов."
        )
        
        await safe_send_message(self.application.bot, update.effective_user.id, start_message, parse_mode='Markdown', reply_markup=self.get_main_keyboard())
        
        if self.bybit_initialized and health and not health['success']:
            await self.check_api_and_notify(is_startup=True)
        
        if self.authorized_user_id:
            try:
                await self.application.bot.send_message(
                    chat_id=self.authorized_user_id,
                    text="✅ Бот запущен и готов к работе!\nУведомления об исполненных ордерах будут приходить сюда.",
                    parse_mode='Markdown'
                )
                logger.info(f"Test notification sent to user {self.authorized_user_id}")
            except Exception as e:
                logger.error(f"Failed to send test notification: {e}")
    
    async def cmd_check_sells(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        
        await update.message.reply_text("🔍 *Запускаю проверку продаж с даты первого ордера...*", parse_mode='Markdown')
        
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return
        
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        
        try:
            first_order_date = self.db.get_first_order_date()
            if first_order_date:
                date_str = first_order_date.strftime('%d.%m.%Y')
                await update.message.reply_text(f"📅 Первый ордер от: *{date_str}*\nПроверяю продажи с этой даты...", parse_mode='Markdown')
            else:
                await update.message.reply_text("📅 Первый ордер не найден. Проверяю за последние 30 дней...", parse_mode='Markdown')
            
            result = await self.strategy.force_check_completed_sells(symbol, self.application.bot, self.authorized_user_id)
            
            if result['missing']:
                await update.message.reply_text(f"✅ *Найдено {len(result['missing'])} новых продаж!*\nУведомления отправлены.", parse_mode='Markdown')
            else:
                await update.message.reply_text("✅ *Новых продаж не найдено.*", parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error checking sells: {e}")
            await update.message.reply_text(f"❌ Ошибка при проверке продаж: {str(e)}")
    
    async def toggle_trading_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return SELECTING_ACTION
        
        current_mode = self.db.get_trading_mode()
        new_mode = 'demo' if current_mode == 'real' else 'real'
        self.db.set_trading_mode(new_mode)
        
        self.bybit_initialized = False
        self._init_bybit()
        
        mode_text = "Демо-режим" if new_mode == 'demo' else "Обычный режим"
        await update.message.reply_text(
            f"✅ Режим изменён на: *{mode_text}*\n\n"
            f"Клиент Bybit переподключён.",
            reply_markup=self.get_settings_keyboard(),
            parse_mode='Markdown'
        )
        return SELECTING_ACTION
    
    async def purchase_notify_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        await self._reset_bot_state(context)
        enabled = self.db.get_purchase_notify_enabled()
        notify_time = self.db.get_purchase_notify_time()
        status_text = "🔔 Включены" if enabled else "🔕 Выключены"
        current_time = get_moscow_time()
        await update.message.reply_text(
            f"🔔 *Уведомления о покупке*\n\n"
            f"📋 Статус: {status_text}\n"
            f"⏰ Время уведомления: `{notify_time}` (МСК)\n"
            f"🕐 Текущее московское время: `{current_time.strftime('%H:%M')}`\n\n"
            f"Выберите действие:",
            reply_markup=self.get_purchase_notify_settings_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_PURCHASE_NOTIFY_TIME
    
    async def toggle_purchase_notify(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current = self.db.get_purchase_notify_enabled()
        new_status = not current
        self.db.set_purchase_notify_enabled(new_status)
        status_text = "🔔 Включены" if new_status else "🔕 Выключены"
        await update.message.reply_text(f"🔔 Уведомления о покупке: {status_text}", reply_markup=self.get_purchase_notify_settings_keyboard())
        return WAITING_PURCHASE_NOTIFY_TIME
    
    async def set_purchase_notify_time_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current_time = self.db.get_purchase_notify_time()
        moscow_time = get_moscow_time()
        await update.message.reply_text(
            f"⏰ Введите время уведомления (формат ЧЧ:ММ):\n"
            f"*Текущее время:* `{current_time}` (МСК)\n"
            f"*Текущее московское время:* `{moscow_time.strftime('%H:%M')}`\n\n"
            f"Пример: 06:00 или 18:30",
            reply_markup=self.get_cancel_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_PURCHASE_NOTIFY_TIME
    
    async def set_purchase_notify_time_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_purchase_notify_settings_keyboard())
            return WAITING_PURCHASE_NOTIFY_TIME
        try:
            new_time_str = text
            datetime.strptime(new_time_str, "%H:%M")
            self.db.set_purchase_notify_time(new_time_str)
            
            now = get_moscow_time()
            new_hour, new_minute = map(int, new_time_str.split(':'))
            new_time_today = now.replace(hour=new_hour, minute=new_minute, second=0, microsecond=0)
            if new_time_today > now:
                self.db.set_last_purchase_notify_date('')
                logger.info(f"Сброшен last_purchase_notify_date для повторной отправки сегодня в {new_time_str}")
            
            await update.message.reply_text(f"✅ Время уведомления установлено: {new_time_str} (МСК)", reply_markup=self.get_purchase_notify_settings_keyboard())
            return WAITING_PURCHASE_NOTIFY_TIME
        except ValueError:
            await update.message.reply_text("❌ Некорректный формат. Используйте ЧЧ:ММ (например: 06:00)", reply_markup=self.get_cancel_keyboard())
            return WAITING_PURCHASE_NOTIFY_TIME
    
    async def back_to_settings_from_purchase(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⚙️ *Настройки*", reply_markup=self.get_settings_keyboard(), parse_mode='Markdown')
        return ConversationHandler.END
    
    async def auto_dca_settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        await self._reset_bot_state(context)
        schedule_time = self.db.get_setting('schedule_time', SCHEDULE_TIME)
        frequency_hours = self.db.get_setting('frequency_hours', str(FREQUENCY_HOURS))
        invest_amount = self.db.get_setting('invest_amount', str(INVEST_AMOUNT))
        await update.message.reply_text(
            f"🚀 *Настройки Авто DCA*\n\n"
            f"💵 Сумма покупки авто: `{invest_amount}` USDT\n"
            f"⏰ Время покупки: `{schedule_time}` (МСК)\n"
            f"🔄 Частота покупки: `{frequency_hours}` часов\n\n"
            f"Выберите параметр:",
            reply_markup=self.get_auto_dca_keyboard(),
            parse_mode='Markdown'
        )
        return AUTO_DCA_SETTINGS
    
    async def set_amount_start_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"💵 Введите сумму для Авто DCA (текущая: {self.db.get_setting('invest_amount', str(INVEST_AMOUNT))}):\n*Минимальная сумма: 5 USDT*", reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
        return SET_AMOUNT
    
    async def set_amount_done_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text in ["❌ ОТМЕНА", "❌ Отмена"]:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_auto_dca_keyboard())
            return AUTO_DCA_SETTINGS
        try:
            amount = float(text)
            if amount < 5:
                raise ValueError("Минимальная сумма 5 USDT")
            self.db.set_setting('invest_amount', str(amount))
            symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
            ladder = self.db.get_ladder_settings(symbol)
            ladder['base_amount'] = amount
            ladder['max_amount'] = amount * 3
            self.db.save_ladder_settings(ladder)
            await update.message.reply_text(f"✅ Сумма изменена на {amount} USDT", reply_markup=self.get_auto_dca_keyboard())
            return AUTO_DCA_SETTINGS
        except ValueError as e:
            await update.message.reply_text(f"❌ {str(e)}", reply_markup=self.get_cancel_keyboard())
            return SET_AMOUNT
    
    async def set_time_start_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"⏰ Введите время (текущее: {self.db.get_setting('schedule_time', SCHEDULE_TIME)}, формат ЧЧ:ММ):", reply_markup=self.get_cancel_keyboard())
        return SET_SCHEDULE_TIME
    
    async def set_time_done_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        time_str = update.message.text.strip()
        if time_str in ["❌ ОТМЕНА", "❌ Отмена"]:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_auto_dca_keyboard())
            return AUTO_DCA_SETTINGS
        try:
            datetime.strptime(time_str, "%H:%M")
            self.db.set_setting('schedule_time', time_str)
            if self.db.get_setting('dca_active', 'false') == 'true':
                next_time = self._calculate_next_purchase_time()
                self.db.set_setting('next_dca_purchase_time', next_time.isoformat())
            await update.message.reply_text(f"✅ Время изменено на {time_str}", reply_markup=self.get_auto_dca_keyboard())
            return AUTO_DCA_SETTINGS
        except ValueError:
            await update.message.reply_text("❌ Некорректный формат. Используйте ЧЧ:ММ", reply_markup=self.get_cancel_keyboard())
            return SET_SCHEDULE_TIME
    
    async def set_frequency_start_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"🔄 Введите частоту в часах (текущая: {self.db.get_setting('frequency_hours', str(FREQUENCY_HOURS))}):", reply_markup=self.get_cancel_keyboard())
        return SET_FREQUENCY_HOURS
    
    async def set_frequency_done_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text in ["❌ ОТМЕНА", "❌ Отмена"]:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_auto_dca_keyboard())
            return AUTO_DCA_SETTINGS
        try:
            hours = int(text)
            if hours < 1 or hours > 720:
                raise ValueError
            self.db.set_setting('frequency_hours', str(hours))
            if self.db.get_setting('dca_active', 'false') == 'true':
                next_time = self._calculate_next_purchase_time()
                self.db.set_setting('next_dca_purchase_time', next_time.isoformat())
            await update.message.reply_text(f"✅ Частота изменена на {hours} часов", reply_markup=self.get_auto_dca_keyboard())
            return AUTO_DCA_SETTINGS
        except ValueError:
            await update.message.reply_text("❌ Введите число от 1 до 720", reply_markup=self.get_cancel_keyboard())
            return SET_FREQUENCY_HOURS
    
    async def set_manual_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current_amount = self.db.get_manual_amount()
        await update.message.reply_text(
            f"💵 *Настройка суммы для ручного ордера*\n\n"
            f"Текущая сумма: `{current_amount}` USDT\n"
            f"Введите новую сумму (минимум: 1.1 USDT):",
            reply_markup=self.get_cancel_keyboard(),
            parse_mode='Markdown'
        )
        return SET_MANUAL_AMOUNT
    
    async def set_manual_amount_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text in ["❌ ОТМЕНА", "❌ Отмена"]:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        try:
            amount = float(text)
            if amount < 1.1:
                raise ValueError("Минимальная сумма 1.1 USDT")
            self.db.set_manual_amount(amount)
            await update.message.reply_text(f"✅ Сумма для ручного ордера изменена на {amount} USDT", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        except ValueError as e:
            await update.message.reply_text(f"❌ {str(e)}", reply_markup=self.get_cancel_keyboard())
            return SET_MANUAL_AMOUNT
    
    async def handle_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        await update.message.reply_text("⏳ Экспортирую базу данных...")
        success, count, file_path = self.db.export_database()
        if success:
            await update.message.reply_text(f"✅ Экспортировано! Записей: {count}")
            try:
                with open(file_path, 'rb') as f:
                    await update.message.reply_document(document=InputFile(f, filename=DB_EXPORT_FILE), caption=f"💾 Файл базы данных от {get_moscow_time_naive().strftime('%d.%m.%Y %H:%M')}")
            except Exception as e:
                await update.message.reply_text(f"❌ Ошибка отправки файла: {e}")
        else:
            await update.message.reply_text(f"❌ Ошибка экспорта: {file_path}")
    
    async def handle_import_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        self.import_waiting = True
        await update.message.reply_text(
            "📥 *ИМПОРТ БАЗЫ ДАННЫХ*\n\n"
            "Отправьте файл .json\n"
            "⚠️ *ВНИМАНИЕ! Все текущие данные будут заменены!*\n\n"
            "Или нажмите ❌ Отмена для отмены",
            reply_markup=self.get_cancel_keyboard(),
            parse_mode='Markdown'
        )
    
    async def handle_import_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        if not self.import_waiting:
            await update.message.reply_text("Сначала нажмите кнопку '📥 Импорт базы' в меню настроек")
            return
        if not update.message.document:
            await update.message.reply_text("Пожалуйста, отправьте файл .json", reply_markup=self.get_cancel_keyboard())
            return
        if not update.message.document.file_name.endswith('.json'):
            await update.message.reply_text("❌ Файл должен иметь расширение .json", reply_markup=self.get_cancel_keyboard())
            return
        try:
            await update.message.reply_text("⏳ Импортирую данные...")
            file = await context.bot.get_file(update.message.document.file_id)
            temp_file = f"temp_import_{get_moscow_time_naive().strftime('%Y%m%d%H%M%S')}.json"
            await file.download_to_drive(temp_file)
            success, message = self.db.import_database(temp_file)
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except:
                    pass
            self.import_waiting = False
            if success:
                await update.message.reply_text(f"✅ {message}", reply_markup=self.get_main_keyboard())
                self.bybit_initialized = False
                self._init_bybit()
            else:
                await update.message.reply_text(f"❌ Ошибка импорта: {message}", reply_markup=self.get_main_keyboard())
        except Exception as e:
            logger.error(f"Error in import: {e}")
            self.import_waiting = False
            await update.message.reply_text(f"❌ Ошибка при импорте: {str(e)}", reply_markup=self.get_main_keyboard())
    
    async def handle_import_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.import_waiting:
            self.import_waiting = False
            await update.message.reply_text("❌ Импорт отменен", reply_markup=self.get_main_keyboard())
        else:
            await self._reset_bot_state(context)
            await update.message.reply_text("Главное меню:", reply_markup=self.get_main_keyboard())
    
    async def handle_sell_confirmation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        text = update.message.text.strip()
        if text == "❌ Нет, отмена":
            await update.message.reply_text("❌ Продажа отменена", reply_markup=self.get_main_keyboard())
            return
        if text == "✅ Да, выставить ордер на продажу":
            sell_data = context.user_data.get('pending_sell_data')
            if not sell_data:
                await update.message.reply_text("❌ Данные о продаже не найдены", reply_markup=self.get_main_keyboard())
                return
            
            symbol = sell_data['symbol']
            coin = symbol.replace('USDT', '')
            balance_info = await self.bybit.get_balance(coin)
            if balance_info and balance_info.get('equity', 0) > 0:
                instrument_info = await self.bybit.get_instrument_info(symbol)
                min_qty = instrument_info['min_qty']
                qty_decimals = instrument_info.get('qty_decimals', SELL_DECIMALS_FALLBACK)
                actual_quantity = self.bybit._round_quantity_for_sell(balance_info['equity'], qty_decimals)
                if actual_quantity < min_qty and balance_info['equity'] >= min_qty:
                    for decimals in range(qty_decimals, 0, -1):
                        factor = 10 ** decimals
                        test_rounded = math.floor(balance_info['equity'] * factor) / factor
                        if test_rounded >= min_qty:
                            actual_quantity = test_rounded
                            break
                if actual_quantity > 0 and actual_quantity != sell_data.get('total_quantity'):
                    sell_data['total_quantity'] = actual_quantity
                    sell_data['display_quantity'] = actual_quantity
                    logger.info(f"Updated sell quantity from balance: {actual_quantity}")
            
            await update.message.reply_text("⏳ Выставляю ордер на продажу...")
            self._init_bybit()
            if not self.bybit_initialized:
                await update.message.reply_text("❌ Bybit API не инициализирован.", reply_markup=self.get_main_keyboard())
                return
            result = await self.strategy.place_full_sell_order(update, sell_data['symbol'], sell_data['profit_percent'], auto_cancel_old=True)
            if result['success']:
                msg = (f"✅ *Ордер на продажу успешно создан!*\n\n"
                       f"🪙 Токен: `{sell_data['symbol']}`\n"
                       f"📊 Количество: `{format_quantity(result['quantity'], 5)}`\n"
                       f"💰 Цена: `{format_price(result['price'], 4)}` USDT\n"
                       f"📈 Целевая прибыль: `{result['profit_percent']}%`\n"
                       f"🆔 ID ордера: `{result['order_id']}`\n"
                       f"{result.get('warning', '')}\n\n"
                       f"✅ Ордер успешно выставлен!")
                await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=self.get_main_keyboard())
                self.db.log_action('SELL_ORDER_PLACED', sell_data['symbol'], f"Ордер на продажу {result['quantity']:.5f} по {result['price']:.4f} USDT")
            elif result.get('pending'):
                pass
            else:
                await update.message.reply_text(f"❌ *Ошибка при создании ордера*\n\n{result['error']}", parse_mode='Markdown', reply_markup=self.get_main_keyboard())
            context.user_data.pop('pending_sell_data', None)
            await self._reset_bot_state(context)
    
    async def toggle_order_execution(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        current = self.db.get_order_execution_notify()
        new_status = not current
        self.db.set_order_execution_notify(new_status)
        status_text = "✅ Включено" if new_status else "⏹ Выключено"
        interval = self.db.get_order_check_interval()
        await update.message.reply_text(
            f"📋 *Отслеживание исполненных ордеров*: {status_text}\n\n"
            f"🕐 Интервал проверки: {interval} минут\n\n"
            f"При включенной настройке бот каждые {interval} минут проверяет новые исполненные ордера",
            parse_mode='Markdown',
            reply_markup=self.get_tracking_settings_keyboard()
        )
    
    async def toggle_sell_tracking(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        current = self.db.get_sell_tracking_enabled()
        new_status = not current
        self.db.set_sell_tracking_enabled(new_status)
        status_text = "✅ Включено" if new_status else "⏹ Выключено"
        await update.message.reply_text(
            f"💰 *Отслеживание выполненных продаж*: {status_text}",
            parse_mode='Markdown',
            reply_markup=self.get_tracking_settings_keyboard()
        )
    
    async def tracking_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        await self._reset_bot_state(context)
        current_status = self.db.get_order_execution_notify()
        sell_tracking = self.db.get_sell_tracking_enabled()
        current_interval = self.db.get_order_check_interval()
        status_text = "✅ Включено" if current_status else "⏹ Выключено"
        sell_tracking_text = "💰 Включено" if sell_tracking else "⏹ Выключено"
        await update.message.reply_text(
            f"⚙️ *Настройки отслеживания*\n\n"
            f"📋 Отслеживание ордеров: {status_text}\n"
            f"💰 Отслеживание продаж: {sell_tracking_text}\n"
            f"🕐 Интервал проверки: `{current_interval}` минут\n\n"
            f"Выберите действие:",
            reply_markup=self.get_tracking_settings_keyboard(),
            parse_mode='Markdown'
        )
        return NOTIFICATION_SETTINGS_MENU
    
    async def toggle_tracking(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current = self.db.get_order_execution_notify()
        new_status = not current
        self.db.set_order_execution_notify(new_status)
        status_text = "✅ Включено" if new_status else "⏹ Выключено"
        await update.message.reply_text(f"📋 Отслеживание ордеров: {status_text}", reply_markup=self.get_tracking_settings_keyboard())
        return NOTIFICATION_SETTINGS_MENU
    
    async def toggle_sell_tracking_in_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current = self.db.get_sell_tracking_enabled()
        new_status = not current
        self.db.set_sell_tracking_enabled(new_status)
        status_text = "💰 Включено" if new_status else "⏹ Выключено"
        await update.message.reply_text(f"💰 Отслеживание продаж: {status_text}", reply_markup=self.get_tracking_settings_keyboard())
        return NOTIFICATION_SETTINGS_MENU
    
    async def set_tracking_interval_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"⏱ Введите интервал проверки в минутах (от 5 до 1440):\n"
            f"*Текущий интервал: {self.db.get_order_check_interval()} минут*",
            reply_markup=self.get_cancel_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_ORDER_CHECK_INTERVAL
    
    async def set_tracking_interval_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_tracking_settings_keyboard())
            return NOTIFICATION_SETTINGS_MENU
        try:
            minutes = int(text)
            if minutes < 5 or minutes > 1440:
                raise ValueError
            self.db.set_order_check_interval(minutes)
            self.db.reset_incremental_check_time()
            await update.message.reply_text(f"✅ Интервал проверки изменен на {minutes} минут", reply_markup=self.get_tracking_settings_keyboard())
            return NOTIFICATION_SETTINGS_MENU
        except ValueError:
            await update.message.reply_text("❌ Некорректное значение. Введите число от 5 до 1440.", reply_markup=self.get_cancel_keyboard())
            return WAITING_ORDER_CHECK_INTERVAL
    
    async def test_tracking(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return NOTIFICATION_SETTINGS_MENU
        
        msg = await update.message.reply_text("🔍 *Запускаю полную проверку...*", parse_mode='Markdown')
        
        self._init_bybit()
        if not self.bybit_initialized:
            await msg.edit_text("❌ Bybit API не инициализирован.")
            return NOTIFICATION_SETTINGS_MENU
        
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        first_order_date = self.db.get_first_order_date()
        check_date_str = first_order_date.strftime('%d.%m.%Y') if first_order_date else "начала торгов"
        
        buy_result = await self.strategy.force_check_executed_orders(symbol, self.application.bot, self.authorized_user_id)
        sell_result = await self.strategy.force_check_completed_sells(symbol, self.application.bot, self.authorized_user_id)
        
        summary = (
            f"📊 *РЕЗУЛЬТАТ ПРОВЕРКИ*\n"
            f"🪙 `{symbol}` | 📅 с `{check_date_str}`\n\n"
            f"🟢 *Покупки:* найдено `{buy_result['total_found']}`, новых `{len(buy_result['missing'])}`\n"
            f"🔴 *Продажи:* найдено `{sell_result['total_found']}`, новых `{len(sell_result['missing'])}`"
        )
        
        if buy_result['missing'] or sell_result['missing']:
            summary += f"\n\n⚠️ *Найдены новые ордера!* Сейчас пришлю уведомления..."
        
        await msg.edit_text(summary, parse_mode='Markdown')
        
        notified_count = 0
        for order in buy_result['missing']:
            if notified_count >= 10:
                break
            msg_text = (f"✅ *ОРДЕР ИСПОЛНЕН!*\n\n"
                       f"🪙 Токен: `{symbol}`\n"
                       f"💰 Цена: `{format_price(order['price'], 4)}` USDT\n"
                       f"📊 Количество: `{format_quantity(order['quantity'], 5)}`\n"
                       f"💵 Сумма: `{order['amount_usdt']:.2f}` USDT\n"
                       f"🕐 Время: `{order['executed_at'].strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
                       f"❗ *Добавить в статистику покупок?*")
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Добавить", callback_data=f"add_order_{order['order_id']}"),
                InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_order_{order['order_id']}")
            ]])
            await safe_send_message(self.application.bot, self.authorized_user_id, msg_text, parse_mode='Markdown', reply_markup=keyboard)
            notified_count += 1
            await asyncio.sleep(0.3)
        
        for sell in sell_result['missing']:
            profit_emoji = "🟢" if sell['profit_usdt'] >= 0 else "🔴"
            profit_color = "+" if sell['profit_usdt'] >= 0 else ""
            msg_text = (f"💰 *СДЕЛКА ПРОДАНА!*\n\n"
                       f"🪙 Токен: `{symbol}`\n"
                       f"📊 Количество: `{format_quantity(sell['quantity'], 5)}`\n"
                       f"💰 Цена продажи: `{format_price(sell['sell_price'], 4)}` USDT\n"
                       f"💵 Сумма: `{sell['amount_usdt']:.2f}` USDT\n"
                       f"{profit_emoji} Прибыль: `{profit_color}{sell['profit_usdt']:.2f}` USDT\n"
                       f"📈 Процент: `{profit_color}{sell['profit_percent']:.2f}%`\n"
                       f"📅 Период: `{sell['days_invested']}` дн.\n"
                       f"📈 APY: `{profit_color}{sell['apy']:.2f}%`\n"
                       f"🕐 Время: `{sell['executed_at'].strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
                       f"❗ *Очистить статистику DCA по этому токену?*")
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, очистить", callback_data=f"confirm_clear_stats_{symbol}_{sell['id']}"),
                InlineKeyboardButton("❌ Нет, оставить", callback_data=f"skip_clear_stats_{symbol}_{sell['id']}")
            ]])
            await safe_send_message(self.application.bot, self.authorized_user_id, msg_text, parse_mode='Markdown', reply_markup=keyboard)
            await asyncio.sleep(0.3)
        
        if notified_count == 0 and not sell_result['missing']:
            await self.application.bot.send_message(chat_id=self.authorized_user_id, text="✨ *Отлично!* Все ордера синхронизированы.", parse_mode='Markdown')
        
        return NOTIFICATION_SETTINGS_MENU
    
    async def back_to_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⚙️ *Настройки*", reply_markup=self.get_settings_keyboard(), parse_mode='Markdown')
        return ConversationHandler.END
    
    async def orders_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return ConversationHandler.END
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        try:
            orders_by_side = await self.bybit.get_open_orders_by_side(symbol)
            sell_count = len(orders_by_side.get('sell', []))
            buy_count = len(orders_by_side.get('buy', []))
            await update.message.reply_text(
                f"📝 *Управление ордерами*\n\n"
                f"🪙 Токен: `{symbol}`\n"
                f"🔴 Ордера на продажу: `{sell_count}`\n"
                f"🟢 Ордера на покупку: `{buy_count}`\n\n"
                f"Выберите действие:",
                reply_markup=self.get_order_management_keyboard(),
                parse_mode='Markdown'
            )
            return MANAGE_ORDERS
        except Exception as e:
            logger.error(f"Error in orders_menu: {e}")
            await update.message.reply_text(f"❌ Ошибка: {e}")
            return ConversationHandler.END
    
    async def show_open_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.", reply_markup=self.get_order_management_keyboard())
            return
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        coin = symbol.replace('USDT', '')
        try:
            orders_by_side = await self.bybit.get_open_orders_by_side(symbol)
            message = f"📋 *ОТКРЫТЫЕ ОРДЕРА*\n🪙 {symbol}\n\n"
            sell_orders = orders_by_side.get('sell', [])
            if sell_orders:
                message += f"🔴 *ОРДЕРА НА ПРОДАЖУ ({len(sell_orders)})*\n"
                for i, order in enumerate(sell_orders[:20], 1):
                    order_id = order.get('orderId', 'N/A')
                    price = float(order.get('price', 0))
                    qty = float(order.get('qty', 0))
                    message += f"{i}. `{order_id}` - {format_quantity(qty, 5)} {coin} @ {format_price(price, 4)} USDT\n"
                if len(sell_orders) > 20:
                    message += f"_...и еще {len(sell_orders) - 20}_\n"
                message += f"\n"
            else:
                message += f"🔴 *Нет ордеров на продажу*\n\n"
            buy_orders = orders_by_side.get('buy', [])
            if buy_orders:
                message += f"🟢 *ОРДЕРА НА ПОКУПКУ ({len(buy_orders)})*\n"
                for i, order in enumerate(buy_orders[:20], 1):
                    order_id = order.get('orderId', 'N/A')
                    price = float(order.get('price', 0))
                    qty = float(order.get('qty', 0))
                    message += f"{i}. `{order_id}` - {format_quantity(qty, 5)} {coin} @ {format_price(price, 4)} USDT\n"
                if len(buy_orders) > 20:
                    message += f"_...и еще {len(buy_orders) - 20}_\n"
            else:
                message += f"🟢 *Нет ордеров на покупку*"
            await update.message.reply_text(message, parse_mode='Markdown', reply_markup=self.get_order_management_keyboard())
        except Exception as e:
            logger.error(f"Error showing open orders: {e}")
            await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=self.get_order_management_keyboard())
    
    async def cancel_order_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.", reply_markup=self.get_order_management_keyboard())
            return ConversationHandler.END
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        try:
            orders_by_side = await self.bybit.get_open_orders_by_side(symbol)
            all_orders = []
            for order in orders_by_side.get('sell', []):
                all_orders.append(order)
            for order in orders_by_side.get('buy', []):
                all_orders.append(order)
            if not all_orders:
                await update.message.reply_text("📭 Нет открытых ордеров для удаления.", reply_markup=self.get_order_management_keyboard())
                return ConversationHandler.END
            context.user_data['cancel_orders'] = all_orders
            keyboard = []
            for idx, order in enumerate(all_orders[:20], 1):
                side_emoji = "🔴" if order.get('side') == 'Sell' else "🟢"
                price = float(order.get('price', 0))
                qty = float(order.get('qty', 0))
                btn_text = f"{idx}. {side_emoji} {format_quantity(qty, 5)} @ {format_price(price, 4)} USDT"
                keyboard.append([KeyboardButton(btn_text)])
            keyboard.append([KeyboardButton("❌ Отмена")])
            cancel_keyboard = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            message = f"🗑 *УДАЛЕНИЕ ОРДЕРА*\n\n🪙 Токен: `{symbol}`\n\nВыберите ордер для удаления (введите номер):"
            await update.message.reply_text(message, parse_mode='Markdown', reply_markup=cancel_keyboard)
            return WAITING_ORDER_ID_TO_CANCEL
        except Exception as e:
            logger.error(f"Error in cancel_order_start: {e}")
            await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=self.get_order_management_keyboard())
            return ConversationHandler.END
    
    async def cancel_order_execute(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Удаление отменено", reply_markup=self.get_order_management_keyboard())
            return ConversationHandler.END
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.", reply_markup=self.get_order_management_keyboard())
            return ConversationHandler.END
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        try:
            all_orders = context.user_data.get('cancel_orders', [])
            if not all_orders:
                await update.message.reply_text("❌ Список ордеров не найден.", reply_markup=self.get_order_management_keyboard())
                return ConversationHandler.END
            import re
            match = re.search(r'^(\d+)', text)
            if not match:
                await update.message.reply_text(f"❌ Введите номер ордера (1-{len(all_orders)})", reply_markup=self.get_order_management_keyboard())
                return ConversationHandler.END
            order_num = int(match.group(1))
            if order_num < 1 or order_num > len(all_orders):
                await update.message.reply_text(f"❌ Неверный номер. Введите число от 1 до {len(all_orders)}.", reply_markup=self.get_order_management_keyboard())
                return ConversationHandler.END
            order_to_cancel = all_orders[order_num - 1]
            order_id = order_to_cancel.get('orderId')
            result = await self.bybit.cancel_order(symbol, order_id)
            if result['success']:
                self.db.delete_sell_order(order_id)
                self.db.log_action('ORDER_CANCELLED', symbol, f"Ордер {order_id} отменен")
                side = order_to_cancel.get('side', 'Unknown')
                price = float(order_to_cancel.get('price', 0))
                qty = float(order_to_cancel.get('qty', 0))
                await update.message.reply_text(
                    f"✅ *Ордер успешно удален!*\n\n"
                    f"🪙 Токен: `{symbol}`\n"
                    f"📊 Сторона: `{side}`\n"
                    f"💰 Цена: `{format_price(price, 4)}` USDT\n"
                    f"📊 Количество: `{format_quantity(qty, 5)}`\n"
                    f"🆔 ID: `{order_id}`",
                    parse_mode='Markdown',
                    reply_markup=self.get_order_management_keyboard()
                )
                return ConversationHandler.END
            else:
                error_msg = result.get('error', 'Неизвестная ошибка')
                await update.message.reply_text(
                    f"❌ *Ошибка при удалении ордера*\n\n"
                    f"ID: `{order_id}`\n"
                    f"Ошибка: `{error_msg}`",
                    parse_mode='Markdown',
                    reply_markup=self.get_order_management_keyboard()
                )
                return ConversationHandler.END
        except Exception as e:
            logger.error(f"Error in cancel_order_execute: {e}")
            await update.message.reply_text(f"❌ Ошибка: {str(e)}", reply_markup=self.get_order_management_keyboard())
            return ConversationHandler.END
    
    async def show_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return
        try:
            symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
            coin = symbol.replace('USDT', '')
            coin_balance = await self.bybit.get_balance(coin)
            usdt_balance = await self.bybit.get_balance('USDT')
            current_price = await self.bybit.get_symbol_price(symbol)
            message = f"📊 *Мой Портфель*\n\n"
            if usdt_balance and 'equity' in usdt_balance:
                available_usdt = usdt_balance.get('available', usdt_balance.get('equity', 0))
                message += f"💵 USDT доступно: `{available_usdt:.2f}`\n\n"
            if coin_balance and 'equity' in coin_balance:
                equity = coin_balance['equity']
                available = coin_balance.get('available', 0)
                usd_value = coin_balance.get('usdValue', 0)
                if usd_value == 0 and current_price and equity > 0:
                    usd_value = equity * current_price
                dca_stats = self.db.get_dca_stats(symbol)
                avg_price = dca_stats['avg_price'] if dca_stats else 0
                if avg_price > 0 and current_price and equity > 0:
                    pnl_percent = ((current_price - avg_price) / avg_price * 100)
                    pnl_usd = (current_price - avg_price) * equity
                else:
                    pnl_percent = 0
                    pnl_usd = 0
                emoji = "🟢" if pnl_percent >= 0 else "🔴"
                message += f"🪙 *{coin}*\n"
                message += f"Всего: `{format_quantity(equity, 5)}`\n"
                message += f"Доступно: `{format_quantity(available, 5)}`\n"
                message += f"Стоимость: `{usd_value:.2f}` USDT\n"
                message += f"Текущая цена: `{format_price(current_price, 4)}` USDT\n"
                if avg_price > 0:
                    message += f"Средняя цена входа: `{format_price(avg_price, 4)}` USDT\n"
                    message += f"{emoji} PnL: `{pnl_percent:+.2f}%` ({pnl_usd:+.2f} USDT)\n\n"
            await update.message.reply_text(message, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error in show_portfolio: {e}")
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def show_dca_stats_detailed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return
        try:
            symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
            coin = symbol.replace('USDT', '')
            stats = self.db.get_dca_stats(symbol)
            current_price = await self.bybit.get_symbol_price(symbol)
            profit_percent = float(self.db.get_setting('profit_percent', str(PROFIT_PERCENT)))
            # manual_amount = self.db.get_manual_amount()  # Убираем эту переменную
            if not stats:
                await update.message.reply_text("📈 *Статистика DCA*\n\nПокупок пока нет.", parse_mode='Markdown')
                return
            total_amount = stats['total_quantity']
            total_cost = stats['total_usdt']
            avg_price = stats['avg_price']
            current_value = total_amount * current_price if current_price else 0
            pnl = current_value - total_cost
            pnl_percent = (pnl / total_cost * 100) if total_cost > 0 else 0
            target_info = self.strategy.calculate_target_info(stats, profit_percent)
            
            text = f"📊 *ДЕТАЛЬНАЯ СТАТИСТИКА DCA*\n\n"
            text += f"🪙 Токен: `{symbol}`\n"
            text += f"💰 Куплено: `{format_quantity(total_amount, 5)}` {coin}\n"
            text += f"💵 Инвестировано: `{total_cost:.2f}` USDT\n"
            text += f"📈 Средняя цена входа: `{format_price(avg_price, 4)}` USDT\n"
            if current_price:
                current_drop = calculate_current_drop(current_price, avg_price)
                text += f"\n📊 *ТЕКУЩАЯ СИТУАЦИЯ*\n"
                text += f"📉 Текущая цена: `{format_price(current_price, 4)}` USDT\n"
                text += f"📉 Падение от средней цены: `{current_drop:.1f}%`\n"
                text += f"💰 Текущая стоимость: `{current_value:.2f}` USDT\n"
                emoji = "📈" if pnl >= 0 else "📉"
                text += f"{emoji} Текущий PnL: `{pnl:.2f}` USDT ({pnl_percent:+.2f}%)\n"
            if target_info:
                tick_size = (await self.bybit.get_instrument_info(symbol))['tick_size']
                rounded_target = self.bybit._round_price_by_tick(target_info['target_price'], tick_size)
                text += f"\n🎯 *ЦЕЛЕВАЯ ПРИБЫЛЬ {profit_percent}%:*\n"
                text += f"Нужно продать: `{format_quantity(target_info['total_qty'], 5)}` {coin}\n"
                text += f"Цена продажи: `{format_price(target_info['target_price'], 4)}` USDT\n"
                text += f"Получите: `{target_info['target_value']:.2f}` USDT\n"
                text += f"Прибыль: `{target_info['target_profit']:.2f}` USDT\n"
                if current_price:
                    increase_needed = ((rounded_target - current_price) / current_price * 100)
                    text += f"Нужен рост: `{increase_needed:+.2f}%` от текущей цены"
            ladder_settings = self.db.get_ladder_settings(symbol)
            if ladder_settings:
                text += f"\n\n🪜 *ЛЕСТНИЦА МАРТИНГЕЙЛА*\n"
                text += f"Глубина просадки: `{ladder_settings['max_depth']}%`\n"
                text += f"Базовая сумма: `{ladder_settings['base_amount']}` USDT\n"
                text += f"Максимальная сумма: `{ladder_settings['max_amount']}` USDT\n\n"
            # Убираем строку с рекомендацией для ручного ордера
            # text += f"💡 *Для ручного ордера:* рекомендуемая сумма `{manual_amount:.2f}` USDT"
            await update.message.reply_text(text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error in show_dca_stats_detailed: {e}")
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    
    async def show_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        await self._reset_bot_state(context)
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        invest_amount = float(self.db.get_setting('invest_amount', str(INVEST_AMOUNT)))
        manual_amount = self.db.get_manual_amount()
        ladder_settings = self.db.get_ladder_settings(symbol)
        order_execution = self.db.get_order_execution_notify()
        sell_tracking = self.db.get_sell_tracking_enabled()
        purchase_notify = self.db.get_purchase_notify_enabled()
        purchase_notify_time = self.db.get_purchase_notify_time()
        order_interval = self.db.get_order_check_interval()
        last_full_check = self.db.get_last_full_check_time()
        first_order_date = self.db.get_first_order_date()
        current_time = get_moscow_time()
        next_purchase_str = self.db.get_setting('next_dca_purchase_time', '')
        mode = self.db.get_trading_mode()
        mode_text = "Демо-режим" if mode == 'demo' else "Обычный режим"
        
        # Получаем статус API
        api_status = self.db.get_api_status()
        api_error = self.db.get_api_error_message()
        
        # Проверяем API если прошло больше 6 часов с последней проверки
        last_api_check = self.db.get_last_api_check_time()
        if last_api_check is None or (get_moscow_time_naive() - last_api_check).total_seconds() > 21600:
            self._init_bybit()
            if self.bybit_initialized:
                health = await self.bybit.check_api_health()
                if health['success']:
                    api_status = 'working'
                    api_error = ''
                    self.db.set_api_status('working')
                else:
                    api_status = 'error'
                    api_error = health.get('user_message', 'Неизвестная ошибка')
                    self.db.set_api_status('error')
                    self.db.set_api_error_message(api_error)
                self.db.set_last_api_check_time(get_moscow_time_naive())
        
        api_status_text = "✅ Активен" if api_status == 'working' else f"❌ Неактивен ({api_error if api_error else 'Ошибка'})"
        
        message = f"📋 *Статус бота*\n\n"
        message += f"🤖 Версия: `{BOT_VERSION}`\n"
        message += f"🤖 Статус: {'✅ Активен' if is_active else '⏹ Остановлен'}\n"
        message += f"🔑 API Bybit: {api_status_text}\n"
        message += f"🌐 Режим: {mode_text}\n"
        if is_active and next_purchase_str:
            try:
                next_time = datetime.fromisoformat(next_purchase_str)
                message += f"⏰ Следующая покупка: `{next_time.strftime('%d.%m.%Y %H:%M')}` (МСК)\n"
            except:
                pass
        message += f"🪙 Токен: `{symbol}`\n"
        message += f"💵 Сумма для Авто DCA: `{invest_amount}` USDT\n"
        message += f"💵 Сумма для ручного ордера: `{manual_amount}` USDT\n"
        message += f"📈 Цель: `{self.db.get_setting('profit_percent', str(PROFIT_PERCENT))}%`\n"
        message += f"📋 Отслеживание ордеров: {'✅ Вкл' if order_execution else '⏹ Выкл'}\n"
        message += f"💰 Отслеживание продаж: {'✅ Вкл' if sell_tracking else '⏹ Выкл'}\n"
        message += f"🔔 Уведомления о покупке: {'✅ Вкл' if purchase_notify else '⏹ Выкл'} ({purchase_notify_time} МСК)\n"
        message += f"🕐 Текущее время (МСК): `{current_time.strftime('%H:%M')}`\n"
        message += f"🕐 Интервал проверки: `{order_interval}` мин\n"
        if first_order_date:
            message += f"📅 Первый ордер: `{first_order_date.strftime('%d.%m.%Y %H:%M')}`\n"
        if last_full_check:
            message += f"📅 Последняя полная проверка: `{last_full_check.strftime('%d.%m.%Y %H:%M')}`\n"
        message += f"\n🪜 *ЛЕСТНИЦА МАРТИНГЕЙЛА:*\n"
        message += f"Глубина просадки: `{ladder_settings['max_depth']}%`\n"
        message += f"Базовая сумма: `{ladder_settings['base_amount']}` USDT\n"
        message += f"Макс. сумма: `{ladder_settings['max_amount']}` USDT\n"
        stats = self.db.get_dca_stats(symbol)
        if stats:
            message += f"\n📊 Всего покупок: `{stats['total_purchases']}`\n💰 Вложено: `{stats['total_usdt']:.2f}` USDT"
        await safe_send_message(self.application.bot, update.effective_user.id, message, parse_mode='Markdown')
    
    async def toggle_dca(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return
        
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        
        if is_active:
            self.db.set_setting('dca_active', 'false')
            if self._sell_check_task and not self._sell_check_task.done():
                self._sell_check_task.cancel()
            await update.message.reply_text(
                "⏹ *DCA ОСТАНОВЛЕН*\n\n"
                "Бот больше не будет проверять ордера на продажу.\n"
                "Текущие ордера останутся активными на бирже.",
                parse_mode='Markdown',
                reply_markup=self.get_main_keyboard()
            )
            logger.info("DCA stopped by user")
        else:
            symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
            current_price = await self.bybit.get_symbol_price(symbol)
            if not current_price:
                await update.message.reply_text("❌ Не удалось получить цену")
                return
            
            self.db.set_setting('dca_active', 'true')
            
            await update.message.reply_text(
                f"🔍 *ПРОВЕРКА СТАТУСА*\n\n"
                f"🪙 Токен: `{symbol}`\n"
                f"💰 Текущая цена: `{format_price(current_price, 4)}` USDT\n\n"
                f"⏳ Проверяю баланс и наличие ордера на продажу...",
                parse_mode='Markdown'
            )
            
            stats = self.db.get_dca_stats(symbol)
            if not stats or stats['total_quantity'] <= 0:
                await update.message.reply_text(
                    f"✅ *DCA ЗАПУЩЕН!*\n\n"
                    f"🪙 Токен: `{symbol}`\n"
                    f"⚠️ *Нет покупок для создания ордера на продажу*\n\n"
                    f"🔄 Бот будет ждать выполнения покупок по расписанию.\n"
                    f"📈 Целевая прибыль: `{self.db.get_setting('profit_percent', str(PROFIT_PERCENT))}%`\n\n"
                    f"⏰ Следующая покупка по расписанию.",
                    parse_mode='Markdown',
                    reply_markup=self.get_main_keyboard()
                )
            else:
                result = await self.strategy.check_and_create_sell_order(symbol, self.application.bot, silent=False)
                
                if result.get('success'):
                    if result.get('message'):
                        await update.message.reply_text(
                            f"✅ *DCA ЗАПУЩЕН!*\n\n"
                            f"🪙 Токен: `{symbol}`\n"
                            f"📊 {result['message']}\n\n"
                            f"⏰ Проверка ордера будет выполняться каждый час.\n"
                            f"📈 Целевая прибыль: `{self.db.get_setting('profit_percent', str(PROFIT_PERCENT))}%`",
                            parse_mode='Markdown',
                            reply_markup=self.get_main_keyboard()
                        )
                    else:
                        await update.message.reply_text(
                            f"✅ *DCA ЗАПУЩЕН!*\n\n"
                            f"🪙 Токен: `{symbol}`\n"
                            f"💰 Создан ордер на продажу!\n"
                            f"📊 Количество: `{format_quantity(result['quantity'], 5)}`\n"
                            f"💰 Цена: `{format_price(result['price'], 4)}` USDT\n"
                            f"📈 Прибыль: `{result['profit_percent']}%`\n\n"
                            f"⏰ Проверка ордера будет выполняться каждый час.",
                            parse_mode='Markdown',
                            reply_markup=self.get_main_keyboard()
                        )
                else:
                    await update.message.reply_text(
                        f"⚠️ *DCA ЗАПУЩЕН, НО ОРДЕР НЕ СОЗДАН*\n\n"
                        f"🪙 Токен: `{symbol}`\n"
                        f"❗ Причина: {result.get('error', 'Неизвестная ошибка')}\n\n"
                        f"🔄 Бот будет проверять статус каждый час.",
                        parse_mode='Markdown',
                        reply_markup=self.get_main_keyboard()
                    )
            
            if self._sell_check_task is None or self._sell_check_task.done():
                self._sell_check_task = asyncio.create_task(
                    self.strategy.sell_order_check_loop(symbol, self.authorized_user_id, self.application.bot)
                )
                logger.info("Sell order check loop started")
            
            logger.info(f"DCA activated. Symbol: {symbol}")
    
    async def settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        await self._reset_bot_state(context)
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        profit_percent = self.db.get_setting('profit_percent', str(PROFIT_PERCENT))
        mode = self.db.get_trading_mode()
        mode_text = "Демо-режим" if mode == 'demo' else "Обычный режим"
        manual_amount = self.db.get_manual_amount()
        await update.message.reply_text(
            f"⚙️ *Настройки*\n\n"
            f"🪙 Токен: `{symbol}`\n"
            f"📈 Цель: `{profit_percent}%`\n"
            f"💵 Сумма для ручного ордера: `{manual_amount}` USDT\n"
            f"🌐 Режим: {mode_text}\n\n"
            f"Выберите раздел:",
            reply_markup=self.get_settings_keyboard(),
            parse_mode='Markdown'
        )
        return SELECTING_ACTION
    
    async def set_profit_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"📊 Введите процент прибыли (текущий: {self.db.get_setting('profit_percent', str(PROFIT_PERCENT))}%):", reply_markup=self.get_cancel_keyboard())
        return SET_PROFIT_PERCENT
    
    async def set_profit_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text in ["❌ ОТМЕНА", "❌ Отмена"]:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        try:
            percent = float(text)
            if percent < 0.1:
                raise ValueError
            self.db.set_setting('profit_percent', str(percent))
            await update.message.reply_text(f"✅ Процент изменен на {percent}%", reply_markup=self.get_settings_keyboard())
        except ValueError:
            await update.message.reply_text("❌ Некорректное значение", reply_markup=self.get_settings_keyboard())
        return SELECTING_ACTION
    
    async def set_symbol_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return SELECTING_ACTION
        await update.message.reply_text(f"🪙 Выберите токен или введите свой\nТекущий: {self.db.get_setting('symbol', DEFAULT_SYMBOL)}", reply_markup=self.get_symbol_selection_keyboard())
        return SELECTING_SYMBOL
    
    async def process_symbol_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        if text == "✏️ Ввести свой токен":
            await update.message.reply_text("✏️ Введите символ токена (например: TONUSDT):", reply_markup=self.get_cancel_keyboard())
            return SET_SYMBOL_MANUAL
        if text in POPULAR_SYMBOLS:
            return await self._validate_and_set_symbol(update, text)
        else:
            await update.message.reply_text("❌ Неверный выбор.", reply_markup=self.get_symbol_selection_keyboard())
            return SELECTING_SYMBOL
    
    async def set_symbol_manual(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        symbol = update.message.text.upper().strip()
        if symbol in ["❌ ОТМЕНА", "❌ Отмена"]:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        return await self._validate_and_set_symbol(update, symbol)
    
    async def _validate_and_set_symbol(self, update: Update, symbol: str) -> int:
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        
        price = await self.bybit.get_symbol_price(symbol)
        if not price:
            await update.message.reply_text(f"❌ Символ {symbol} не найден.", reply_markup=self.get_symbol_selection_keyboard())
            return SELECTING_SYMBOL
        
        instrument_info = await self.bybit.get_instrument_info(symbol)
        min_amt = instrument_info['min_amt']
        qty_decimals = instrument_info['qty_decimals']
        tick_size = instrument_info['tick_size']
        
        self.db.set_setting('symbol', symbol)
        self.db.set_setting('initial_reference_price', str(price))
        await update.message.reply_text(
            f"✅ Символ изменен на {symbol}\n"
            f"💰 Текущая цена: {format_price(price, 4)} USDT\n"
            f"⚠️ Минимальная сумма для Авто DCA: {min_amt} USDT\n"
            f"📊 Точность количества для покупки: {qty_decimals} знаков\n"
            f"📊 Точность количества для продажи: динамическая (из инструмента)",
            reply_markup=self.get_settings_keyboard()
        )
        return SELECTING_ACTION
    
    async def ladder_settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        await self._reset_bot_state(context)
        await update.message.reply_text(
            "🪜 *ЛЕСТНИЦА МАРТИНГЕЙЛА*\n\n"
            "Стратегия: при каждом падении цены на 1% от средней цены\n"
            "происходит докупка с линейным ростом суммы.\n\n"
            "Параметры:\n"
            "• Глубина просадки: максимальный процент падения\n"
            "• Рост суммы: от базовой до максимальной\n"
            "• Базовая сумма: сумма первого ордера",
            reply_markup=self.get_ladder_settings_keyboard(),
            parse_mode='Markdown'
        )
        return LADDER_MENU
    
    async def show_ladder_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return LADDER_MENU
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        ladder = self.db.get_ladder_settings(symbol)
        current_price = await self.bybit.get_symbol_price(symbol) if self.bybit_initialized else None
        summary = self.db.get_ladder_summary(symbol, current_price)
        text = f"🪜 *ТЕКУЩИЕ НАСТРОЙКИ*\n\n"
        text += f"🪙 Токен: `{symbol}`\n"
        if summary['avg_price'] > 0:
            text += f"💰 Средняя цена: `{format_price(summary['avg_price'], 4)}` USDT\n"
        text += f"📉 Глубина просадки: `{ladder['max_depth']}%`\n"
        text += f"💵 Базовая сумма: `{ladder['base_amount']}` USDT\n"
        text += f"💰 Максимальная сумма: `{ladder['max_amount']}` USDT\n"
        if current_price and summary['avg_price'] > 0:
            current_drop = calculate_current_drop(current_price, summary['avg_price'])
            text += f"📊 Текущее падение: `{current_drop:.1f}%`\n"
        text += f"\n*План покупок (от средней цены):*\n"
        for step in summary['steps'][:15]:
            status_emoji = "✅" if step['status'] == 'completed' else "⏳"
            target_price_str = format_price(step['price'], 4) if step['price'] > 0 else "—"
            text += f"{status_emoji} {step['drop_percent']}%: {step['amount']:.2f} USDT → {target_price_str}\n"
        if len(summary['steps']) > 15:
            text += f"_...и еще {len(summary['steps']) - 15} уровней_"
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=self.get_ladder_settings_keyboard())
        return LADDER_MENU
    
    async def set_ladder_max_depth_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return LADDER_MENU
        await update.message.reply_text("📉 Введите глубину просадки в процентах (30-95%):\n*Рекомендуется 80%*", reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
        return SET_LADDER_DEPTH
    
    async def set_ladder_max_depth_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_ladder_settings_keyboard())
            return LADDER_MENU
        try:
            max_depth = float(text.replace(',', '.'))
            if max_depth < 30 or max_depth > 95:
                raise ValueError
            symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
            ladder = self.db.get_ladder_settings(symbol)
            ladder['max_depth'] = max_depth
            self.db.save_ladder_settings(ladder)
            await update.message.reply_text(f"✅ Глубина просадки установлена: {max_depth}%", reply_markup=self.get_ladder_settings_keyboard())
            return LADDER_MENU
        except ValueError:
            await update.message.reply_text("❌ Некорректное значение (30-95).", reply_markup=self.get_cancel_keyboard())
            return SET_LADDER_DEPTH
    
    async def set_ladder_base_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return LADDER_MENU
        await update.message.reply_text("💵 Введите базовую сумму (мин 5 USDT):\n*Сумма первого ордера*", reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
        return SET_LADDER_BASE_AMOUNT
    
    async def set_ladder_base_amount_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_ladder_settings_keyboard())
            return LADDER_MENU
        try:
            base_amount = float(text.replace(',', '.'))
            if base_amount < 5:
                raise ValueError("Минимальная сумма 5 USDT")
            symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
            ladder = self.db.get_ladder_settings(symbol)
            ladder['base_amount'] = base_amount
            ladder['max_amount'] = base_amount * 3
            self.db.save_ladder_settings(ladder)
            await update.message.reply_text(f"✅ Базовая сумма: {base_amount} USDT\n💰 Максимальная сумма: {base_amount * 3} USDT", reply_markup=self.get_ladder_settings_keyboard())
            return LADDER_MENU
        except ValueError:
            await update.message.reply_text("❌ Некорректная сумма (мин 5).", reply_markup=self.get_cancel_keyboard())
            return SET_LADDER_BASE_AMOUNT
    
    async def reset_ladder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return LADDER_MENU
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        self.db.reset_ladder(symbol)
        await update.message.reply_text("🔄 Статистика DCA очищена! Лестница сброшена.\n⚠️ ID покупок будут начинаться с 1 при следующем добавлении.", reply_markup=self.get_ladder_settings_keyboard())
        return LADDER_MENU
    
    async def manual_buy_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return ConversationHandler.END
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        current_price = await self.bybit.get_symbol_price(symbol)
        if not current_price:
            await update.message.reply_text("❌ Не удалось получить цену", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        recommendation = await self.strategy.get_recommended_purchase(symbol)
        manual_amount = self.db.get_manual_amount()
        context.user_data['manual_buy_current_price'] = current_price
        context.user_data['manual_buy_symbol'] = symbol
        context.user_data['manual_buy_recommendation'] = recommendation
        msg = f"💰 Текущая цена {symbol}: `{format_price(current_price, 4)}` USDT\n\n"
        
        rec_manual = self.db.get_recommendation_for_current_drop(current_price, symbol, for_manual=True)
        if rec_manual['success']:
            if rec_manual['is_first']:
                msg += f"🟢 *ПЕРВАЯ ПОКУПКА*\n"
                msg += f"💰 Рекомендуемая сумма: `{rec_manual['amount_usdt']:.2f}` USDT\n\n"
            else:
                msg += f"🟢 *РЕКОМЕНДАЦИЯ ПО ПОКУПКЕ:*\n"
                msg += f"📉 Уровень падения: `{rec_manual['drop_percent']:.1f}%`\n"
                msg += f"💰 Рекомендуемая сумма: `{rec_manual['amount_usdt']:.2f}` USDT\n\n"
        
        msg += f"💵 *Сумма для ручного ордера в настройках*: `{manual_amount:.2f}` USDT\n"
        msg += f"Введите цену лимитного ордера:"
        await update.message.reply_text(msg, reply_markup=self.get_manual_buy_keyboard(), parse_mode='Markdown')
        return MANUAL_BUY_PRICE
    
    async def manual_buy_price_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        text = update.message.text.strip()
        if text in MAIN_MENU_BUTTONS:
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Действие отменено. Возврат в главное меню.", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        try:
            price = float(text.replace(',', '.'))
            if price <= 0:
                raise ValueError
            context.user_data['manual_buy_price'] = price
            manual_amount = self.db.get_manual_amount()
            await update.message.reply_text(f"💰 Введите сумму покупки в USDT\n*Рекомендуемая сумма:* {manual_amount:.2f} USDT\nМинимум: 1.1 USDT:", reply_markup=self.get_manual_buy_keyboard(), parse_mode='Markdown')
            return MANUAL_BUY_AMOUNT
        except ValueError:
            await update.message.reply_text("❌ Некорректная цена. Введите число больше 0.", reply_markup=self.get_manual_buy_keyboard())
            return MANUAL_BUY_PRICE
    
    async def manual_buy_amount_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        self._init_bybit()
        text = update.message.text.strip()
        if text in MAIN_MENU_BUTTONS:
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Действие отменено. Возврат в главное меню.", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        try:
            amount = float(text.replace(',', '.'))
            
            if amount < 1.1:
                raise ValueError("Минимальная сумма 1.1 USDT")
            price = context.user_data.get('manual_buy_price')
            symbol = context.user_data.get('manual_buy_symbol', DEFAULT_SYMBOL)
            recommendation = context.user_data.get('manual_buy_recommendation', {})
            if not price:
                await update.message.reply_text("❌ Ошибка", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            await update.message.reply_text("⏳ Создаю лимитный ордер...")
            result = await self.bybit.place_limit_buy(symbol, price, amount, is_auto=False)
            if result['success']:
                profit_percent = float(self.db.get_setting('profit_percent', str(PROFIT_PERCENT)))
                updated_stats = self.db.get_dca_stats(symbol)
                if updated_stats and updated_stats['total_quantity'] > 0:
                    target_price = updated_stats['avg_price'] * (1 + profit_percent / 100)
                else:
                    target_price = price * (1 + profit_percent / 100)
                current_date = get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")
                drop_percent = recommendation.get('drop_percent', 0) if recommendation.get('should_buy') else 0
                step_level = recommendation.get('step_level', 0) if recommendation.get('should_buy') else 0
                purchase_id = self.db.add_purchase(symbol=symbol, amount_usdt=amount, price=price, quantity=result['quantity'], multiplier=1.0, drop_percent=drop_percent, step_level=step_level, date=current_date, order_id=result.get('order_id'))
                if purchase_id:
                    # === ИСПРАВЛЕНИЕ 1: Ожидание зачисления монет ===
                    logger.info(f"Waiting for order {result['order_id']} to be filled...")
                    await self.bybit.wait_for_order_filled(symbol, result['order_id'], timeout=10, check_interval=0.5)
                    
                    coin = symbol.replace('USDT', '')
                    total_qty = 0
                    max_balance_retries = 5
                    
                    for attempt in range(max_balance_retries):
                        if attempt > 0:
                            await asyncio.sleep(1)
                        
                        balance_after = await self.bybit.get_balance(coin)
                        total_qty = balance_after.get('equity', 0) if balance_after else 0
                        
                        if total_qty > 0:
                            logger.info(f"Баланс {coin} обновился: {total_qty}")
                            break
                        else:
                            logger.warning(f"Попытка {attempt+1}/{max_balance_retries}: Баланс {coin} еще 0, ждем...")
                    # === КОНЕЦ ИСПРАВЛЕНИЯ 1 ===
                    
                    if total_qty > 0:
                        sell_result = await self.bybit.place_limit_sell(symbol, total_qty, target_price)
                        if sell_result['success']:
                            self.db.add_sell_order(symbol=symbol, order_id=sell_result['order_id'], quantity=total_qty, target_price=target_price, profit_percent=profit_percent)
                        elif sell_result.get('error') == 'insufficient_balance':
                            pending_id = self.db.add_pending_sell_order(symbol=symbol, quantity=total_qty, target_price=target_price, profit_percent=profit_percent)
                            await update.message.reply_text(f"⚠️ *ОРДЕР НА ПРОДАЖУ ОТЛОЖЕН*\n\nБаланс обновляется. Ордер будет автоматически создан позже.", parse_mode='Markdown')
                        elif sell_result.get('error') == 'min_amount_error':
                            pending_id = self.db.add_pending_sell_order(symbol=symbol, quantity=total_qty, target_price=target_price, profit_percent=profit_percent)
                            await update.message.reply_text(f"⚠️ *ОРДЕР НА ПРОДАЖУ ОТЛОЖЕН*\n\nСумма ордера меньше минимальной.\n✅ Ордер сохранен и будет автоматически выставлен при достижении нужной цены.", parse_mode='Markdown')
                        else:
                            await update.message.reply_text(f"⚠️ Не удалось создать ордер на продажу: {sell_result.get('error', 'Unknown')}")
                    else:
                        await update.message.reply_text(f"⚠️ Монеты не зачислены на баланс. Ордер на продажу не создан.")
                    
                    msg = f"✅ *Лимитный ордер создан!*\n\nЦена: `{format_price(price, 4)}` USDT\nСумма: `{amount:.2f}` USDT\nКоличество: `{format_quantity(result['quantity'], 5)}`\n"
                    if drop_percent > 0:
                        msg += f"📉 Падение: `{drop_percent:.1f}%` от средней цены\n"
                    msg += f"Цель продажи: `{format_price(target_price, 4)}` USDT ({profit_percent}%)"
                    await update.message.reply_text(msg, reply_markup=self.get_main_keyboard(), parse_mode='Markdown')
                else:
                    await update.message.reply_text("❌ Ошибка сохранения в базу данных", reply_markup=self.get_main_keyboard())
            else:
                await update.message.reply_text(f"❌ Ошибка: {result.get('error', 'Unknown')}", reply_markup=self.get_main_keyboard())
        except ValueError as e:
            await update.message.reply_text(f"❌ {str(e)}", reply_markup=self.get_manual_buy_keyboard())
            return MANUAL_BUY_AMOUNT
        return ConversationHandler.END
    
    async def manual_add_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return ConversationHandler.END
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        current_price = await self.bybit.get_symbol_price(symbol)
        stats = self.db.get_dca_stats(symbol)
        
        recommendation = self.db.get_recommendation_for_current_drop(current_price, symbol, for_manual=True)
        manual_amount = self.db.get_manual_amount()
        msg = f"➕ *Добавление покупки вручную*\n\n"
        msg += f"💰 Текущая цена {symbol}: `{format_price(current_price, 4)}` USDT\n\n"
        if stats and stats['avg_price'] > 0:
            current_drop = calculate_current_drop(current_price, stats['avg_price'])
            msg += f"📉 Средняя цена: `{format_price(stats['avg_price'], 4)}` USDT\n"
            msg += f"📉 Падение от средней цены: `{current_drop:.1f}%`\n\n"
        if recommendation['success']:
            if recommendation['is_first']:
                msg += f"🟢 *ПЕРВАЯ ПОКУПКА*\n"
                msg += f"💰 Рекомендуемая сумма: `{recommendation['amount_usdt']:.2f}` USDT\n\n"
            else:
                msg += f"🟢 *РЕКОМЕНДАЦИЯ:*\n"
                msg += f"📉 Уровень падения: `{recommendation['drop_percent']:.1f}%`\n"
                msg += f"💰 Рекомендуемая сумма: `{recommendation['amount_usdt']:.2f}` USDT\n\n"
        msg += f"💡 *Сумма для ручного ордера:* `{manual_amount:.2f}` USDT\n"
        msg += f"Введите цену покупки (USDT):"
        await update.message.reply_text(msg, reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
        return MANUAL_ADD_PRICE
    
    async def manual_add_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text in MAIN_MENU_BUTTONS:
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Действие отменено. Возврат в главное меню.", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        if text == "❌ Отмена":
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        try:
            price_str = text.replace(',', '.').strip()
            price = float(price_str)
            if price <= 0:
                raise ValueError("Цена должна быть положительной")
            context.user_data['manual_price'] = price
            symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
            stats = self.db.get_dca_stats(symbol)
            
            recommendation = self.db.get_recommendation_for_current_drop(price, symbol, for_manual=True)
            
            if stats and stats['avg_price'] > 0:
                drop_percent = calculate_current_drop(price, stats['avg_price'])
                await update.message.reply_text(
                    f"✅ Цена {format_price(price, 4)} USDT\n"
                    f"📉 Падение от средней цены ({format_price(stats['avg_price'], 4)}): `{drop_percent:.1f}%`\n\n"
                    f"💰 Введите количество монет (в {symbol.replace('USDT', '')}):\n"
                    f"*Рекомендуемая сумма:* {recommendation['amount_usdt']:.2f} USDT",
                    reply_markup=self.get_cancel_keyboard(),
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text(
                    f"✅ Цена {format_price(price, 4)} USDT\n\n"
                    f"💰 Введите количество монет (в {symbol.replace('USDT', '')}):\n"
                    f"*Рекомендуемая сумма:* {recommendation['amount_usdt']:.2f} USDT",
                    reply_markup=self.get_cancel_keyboard(),
                    parse_mode='Markdown'
                )
            return MANUAL_ADD_AMOUNT
        except ValueError as e:
            await update.message.reply_text(f"❌ Ошибка! Введите корректную цену.\nПример: 2.35 или 2,35\n\nОшибка: {str(e)}", reply_markup=self.get_cancel_keyboard())
            return MANUAL_ADD_PRICE
    
    async def manual_add_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text in MAIN_MENU_BUTTONS:
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Действие отменено. Возврат в главное меню.", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        if text == "❌ Отмена":
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        try:
            quantity_str = text.replace(',', '.').strip()
            quantity = float(quantity_str)
            if quantity <= 0:
                raise ValueError("Количество должно быть положительным")
            price = context.user_data.get('manual_price')
            if not price:
                await self._reset_bot_state(context)
                await update.message.reply_text("❌ Ошибка: цена не найдена. Попробуйте заново.", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
            amount_usdt = price * quantity
            stats = self.db.get_dca_stats(symbol)
            drop_percent = 0
            step_level = 0
            if stats and stats['avg_price'] > 0:
                drop_percent = calculate_current_drop(price, stats['avg_price'])
                step_level = int(drop_percent)
            purchase_id = self.db.add_purchase(symbol=symbol, amount_usdt=amount_usdt, price=price, quantity=quantity, multiplier=1.0, drop_percent=drop_percent, step_level=step_level, date=get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S"))
            if purchase_id:
                msg = f"✅ *Покупка добавлена!*\n\n🆔 ID: `{purchase_id}`\n💰 Цена: `{format_price(price, 4)}` USDT\n📊 Количество: `{format_quantity(quantity, 5)}`\n💵 Сумма: `{amount_usdt:.2f}` USDT"
                if drop_percent > 0:
                    msg += f"\n📉 Падение от средней цены: `{drop_percent:.1f}%`"
                await update.message.reply_text(msg, reply_markup=self.get_main_keyboard(), parse_mode='Markdown')
            else:
                await update.message.reply_text("❌ Ошибка сохранения в базу данных", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        except ValueError as e:
            await update.message.reply_text(f"❌ Ошибка! Введите корректное количество.\nПример: 10.5 или 10,5\n\nОшибка: {str(e)}", reply_markup=self.get_cancel_keyboard())
            return MANUAL_ADD_AMOUNT
    
    async def edit_purchases_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return ConversationHandler.END
        await self._reset_bot_state(context)
        context.user_data.pop('editing_purchase_id', None)
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        purchases = self.db.get_purchases(symbol)
        if not purchases:
            await update.message.reply_text("Нет покупок", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        await update.message.reply_text("✏️ Выберите покупку:", reply_markup=self.get_purchases_list_keyboard(purchases))
        return EDIT_PURCHASE_SELECT
    
    async def edit_purchase_selected(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if text == "🏠 Главное меню":
            await self.back_to_main(update, context)
            return ConversationHandler.END
        if text in ["💰 Изменить цену", "📊 Изменить количество", "📅 Изменить дату", "❌ Удалить покупку", "🔙 Назад к списку"]:
            if text == "🔙 Назад к списку":
                context.user_data.pop('editing_purchase_id', None)
                return await self.edit_purchases_list(update, context)
            return EDIT_PURCHASE_SELECT
        try:
            import re
            match = re.search(r'ID(\d+)', text)
            if not match:
                await update.message.reply_text("❌ Неверный формат.", reply_markup=self.get_purchases_list_keyboard(self.db.get_purchases(self.db.get_setting('symbol', DEFAULT_SYMBOL))))
                return EDIT_PURCHASE_SELECT
            purchase_id = int(match.group(1))
            purchase = self.db.get_purchase_by_id(purchase_id)
            if not purchase:
                await update.message.reply_text("❌ Покупка не найдена", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            context.user_data['editing_purchase_id'] = purchase_id
            try:
                date_display = datetime.strptime(purchase['date'], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
            except:
                date_display = purchase['date'][:10] if purchase['date'] else "N/A"
            await update.message.reply_text(f"✏️ *РЕДАКТИРОВАНИЕ ID: {purchase_id}*\n\n📅 Дата: `{date_display}`\n💰 Цена: `{format_price(purchase['price'], 4)}` USDT\n📊 Количество: `{format_quantity(purchase['quantity'], 5)}`", reply_markup=self.get_edit_purchases_keyboard(), parse_mode='Markdown')
            return EDIT_PURCHASE_SELECT
        except Exception as e:
            await update.message.reply_text("❌ Ошибка выбора", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
    
    async def edit_price_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("💰 Введите новую цену:", reply_markup=self.get_cancel_keyboard())
        return EDIT_PRICE
    
    async def edit_price_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await self.cancel_to_edit_menu(update, context)
            return EDIT_PURCHASE_SELECT
        try:
            new_price = float(text.replace(',', '.'))
            purchase_id = context.user_data.get('editing_purchase_id')
            if not purchase_id:
                await update.message.reply_text("❌ Ошибка", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            purchase = self.db.get_purchase_by_id(purchase_id)
            if not purchase:
                await update.message.reply_text("❌ Покупка не найдена", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            new_amount_usdt = new_price * purchase['quantity']
            symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
            stats = self.db.get_dca_stats(symbol)
            new_drop_percent = calculate_current_drop(new_price, stats['avg_price']) if stats else 0
            if self.db.update_purchase(purchase_id, price=new_price, amount_usdt=new_amount_usdt, drop_percent=new_drop_percent):
                await update.message.reply_text(f"✅ Цена обновлена: {format_price(new_price, 4)} USDT\n📉 Падение: {new_drop_percent:.1f}%")
            else:
                await update.message.reply_text("❌ Ошибка при обновлении")
            await self.show_purchase_after_edit(update, context, purchase_id)
            return EDIT_PURCHASE_SELECT
        except ValueError:
            await update.message.reply_text("❌ Ошибка! Введите число.", reply_markup=self.get_cancel_keyboard())
            return EDIT_PRICE
    
    async def edit_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("📊 Введите новое количество:", reply_markup=self.get_cancel_keyboard())
        return EDIT_AMOUNT
    
    async def edit_amount_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await self.cancel_to_edit_menu(update, context)
            return EDIT_PURCHASE_SELECT
        try:
            new_quantity = float(text.replace(',', '.'))
            purchase_id = context.user_data.get('editing_purchase_id')
            if not purchase_id:
                await update.message.reply_text("❌ Ошибка", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            purchase = self.db.get_purchase_by_id(purchase_id)
            if not purchase:
                await update.message.reply_text("❌ Покупка не найдена", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            new_amount_usdt = purchase['price'] * new_quantity
            if self.db.update_purchase(purchase_id, quantity=new_quantity, amount_usdt=new_amount_usdt):
                await update.message.reply_text(f"✅ Количество обновлено: {format_quantity(new_quantity, 5)}")
            else:
                await update.message.reply_text("❌ Ошибка при обновлении")
            await self.show_purchase_after_edit(update, context, purchase_id)
            return EDIT_PURCHASE_SELECT
        except ValueError:
            await update.message.reply_text("❌ Ошибка! Введите число.", reply_markup=self.get_cancel_keyboard())
            return EDIT_AMOUNT
    
    def parse_date(self, date_str: str) -> str:
        date_str = date_str.strip()
        patterns = [
            (r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', lambda m: (int(m.group(1)), int(m.group(2)), int(m.group(3)))),
            (r'^(\d{1,2})\.(\d{1,2})\.(\d{2})$', lambda m: (int(m.group(1)), int(m.group(2)), 2000 + int(m.group(3)))),
            (r'^(\d{1,2})\.(\d{1,2})$', lambda m: (int(m.group(1)), int(m.group(2)), get_moscow_time_naive().year)),
        ]
        for pattern, extractor in patterns:
            match = re.match(pattern, date_str)
            if match:
                day, month, year = extractor(match)
                try:
                    dt = datetime(year, month, day)
                    return dt.strftime("%Y-%m-%d")
                except ValueError:
                    raise ValueError("Некорректная дата")
        raise ValueError("Неподдерживаемый формат")
    
    async def edit_date_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        purchase_id = context.user_data.get('editing_purchase_id')
        purchase = self.db.get_purchase_by_id(purchase_id)
        try:
            current_date = datetime.strptime(purchase['date'], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y")
        except:
            current_date = purchase['date'][:10] if purchase['date'] else "неизвестно"
        await update.message.reply_text(f"📅 Текущая дата: {current_date}\n\nВведите новую дату (ДД.ММ.ГГГГ):", reply_markup=self.get_cancel_keyboard())
        return EDIT_DATE
    
    async def edit_date_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await self.cancel_to_edit_menu(update, context)
            return EDIT_PURCHASE_SELECT
        try:
            new_date = self.parse_date(text)
            purchase_id = context.user_data.get('editing_purchase_id')
            if not purchase_id:
                await update.message.reply_text("❌ Ошибка", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            purchase = self.db.get_purchase_by_id(purchase_id)
            if not purchase:
                await update.message.reply_text("❌ Покупка не найдена", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            old_time = purchase['date'][11:] if purchase['date'] and len(purchase['date']) > 10 else "00:00:00"
            new_date_with_time = f"{new_date} {old_time}"
            if self.db.update_purchase(purchase_id, date=new_date_with_time):
                await update.message.reply_text(f"✅ Дата обновлена: {new_date}")
            else:
                await update.message.reply_text("❌ Ошибка при обновлении")
            await self.show_purchase_after_edit(update, context, purchase_id)
            return EDIT_PURCHASE_SELECT
        except ValueError as e:
            await update.message.reply_text(f"❌ {str(e)}", reply_markup=self.get_cancel_keyboard())
            return EDIT_DATE
    
    async def delete_purchase_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⚠️ *Удалить эту покупку?*", reply_markup=self.get_confirm_delete_keyboard(), parse_mode='Markdown')
        return DELETE_CONFIRM
    
    async def delete_purchase_execute(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if text == "❌ Нет, отмена":
            purchase_id = context.user_data.get('editing_purchase_id')
            await self.show_purchase_after_edit(update, context, purchase_id)
            return EDIT_PURCHASE_SELECT
        if text == "✅ Да, удалить":
            purchase_id = context.user_data.get('editing_purchase_id')
            if purchase_id and self.db.delete_purchase(purchase_id):
                await update.message.reply_text("✅ Покупка удалена!", reply_markup=self.get_main_keyboard())
                context.user_data.pop('editing_purchase_id', None)
                await self._reset_bot_state(context)
                return ConversationHandler.END
            else:
                await update.message.reply_text("❌ Ошибка при удалении", reply_markup=self.get_main_keyboard())
                await self._reset_bot_state(context)
                return ConversationHandler.END
        return EDIT_PURCHASE_SELECT
    
    async def show_purchase_after_edit(self, update: Update, context: ContextTypes.DEFAULT_TYPE, purchase_id):
        purchase = self.db.get_purchase_by_id(purchase_id)
        if not purchase:
            await update.message.reply_text("❌ Покупка не найдена", reply_markup=self.get_main_keyboard())
            return
        try:
            date_display = datetime.strptime(purchase['date'], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
        except:
            date_display = purchase['date'][:10] if purchase['date'] else "N/A"
        await update.message.reply_text(f"✏️ *РЕДАКТИРОВАНИЕ ID: {purchase_id}*\n\n📅 Дата: `{date_display}`\n💰 Цена: `{format_price(purchase['price'], 4)}` USDT\n📊 Количество: `{format_quantity(purchase['quantity'], 5)}`", reply_markup=self.get_edit_purchases_keyboard(), parse_mode='Markdown')
    
    async def cancel_to_edit_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        purchase_id = context.user_data.get('editing_purchase_id')
        if purchase_id:
            await self.show_purchase_after_edit(update, context, purchase_id)
        else:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            await self._reset_bot_state(context)
            return ConversationHandler.END
    
    async def back_to_main(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        await update.message.reply_text("Главное меню:", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END
    
    async def cancel_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        await update.message.reply_text("Действие отменено", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END
    
    async def handle_unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user_fast(update):
            return
        await self._reset_bot_state(context)
        text = update.message.text
        if text == "⚙️ Настройки":
            await self.settings_menu(update, context)
        elif text == "🚀 Настройки Авто DCA":
            await self.auto_dca_settings_menu(update, context)
        elif text == "💵 Сумма для ручного ордера":
            await self.set_manual_amount_start(update, context)
        elif text in ["🏠 Главное меню", "🔙 Назад в меню", "🔙 Назад в настройки", "🔙 Назад к списку"]:
            await update.message.reply_text("Главное меню:", reply_markup=self.get_main_keyboard())
        else:
            await update.message.reply_text("Используйте кнопки меню", reply_markup=self.get_main_keyboard())
    
    async def dca_scheduler_loop(self):
        logger.info("DCA scheduler loop started")
        while self.scheduler_running:
            try:
                await asyncio.sleep(30)
                
                if self.db.get_setting('dca_active', 'false') != 'true':
                    continue
                
                if not self.bybit_initialized:
                    self._init_bybit()
                if not self.bybit_initialized:
                    continue
                
                now = get_moscow_time()
                next_purchase_str = self.db.get_setting('next_dca_purchase_time', '')
                
                if not next_purchase_str:
                    next_time = self._calculate_next_purchase_time()
                    self.db.set_setting('next_dca_purchase_time', next_time.isoformat())
                    continue
                
                try:
                    next_time = datetime.fromisoformat(next_purchase_str)
                except:
                    next_time = self._calculate_next_purchase_time()
                    self.db.set_setting('next_dca_purchase_time', next_time.isoformat())
                    continue
                
                if now >= next_time:
                    symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
                    profit_percent = float(self.db.get_setting('profit_percent', str(PROFIT_PERCENT)))
                    
                    logger.info(f"Scheduled purchase triggered at {now.isoformat()}")
                    
                    result = await self.strategy.execute_scheduled_purchase(symbol, profit_percent, self.application.bot)
                    
                    if result['success']:
                        if self.authorized_user_id:
                            msg = (f"🪜 *АВТО DCA — ПОКУПКА*\n\n"
                                   f"🪙 Токен: `{symbol}`\n"
                                   f"💰 Сумма: `{result['total_usdt']:.2f}` USDT\n"
                                   f"💵 Цена: `{format_price(result['price'], 4)}` USDT\n"
                                   f"📊 Количество: `{format_quantity(result['quantity'], 5)}`\n")
                            if result.get('drop_percent', 0) > 0:
                                msg += f"📉 Падение от средней: `{result['drop_percent']:.1f}%`\n"
                            if result.get('sell_quantity'):
                                msg += f"📊 Ордер на продажу: `{format_quantity(result['sell_quantity'], 5)}` {symbol.replace('USDT', '')}\n"
                            if result.get('sell_warning'):
                                msg += f"\n⚠️ {result['sell_warning']}"
                            await safe_send_message(self.application.bot, self.authorized_user_id, msg, parse_mode='Markdown')
                    elif result.get('error') == 'skip_price_above_avg':
                        pass
                    else:
                        if self.authorized_user_id:
                            await safe_send_message(
                                self.application.bot,
                                self.authorized_user_id,
                                f"❌ *Ошибка авто DCA*\n\n{result.get('error')}",
                                parse_mode='Markdown'
                            )
                    
                    frequency_hours = int(self.db.get_setting('frequency_hours', str(FREQUENCY_HOURS)))
                    next_time = next_time + timedelta(hours=frequency_hours)
                    
                    while next_time <= now:
                        next_time += timedelta(hours=frequency_hours)
                    
                    self.db.set_setting('next_dca_purchase_time', next_time.isoformat())
                    logger.info(f"Next purchase scheduled at {next_time.isoformat()}")
                
                current_symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
                await self.strategy.check_and_update_sell_orders(current_symbol)
                
                if self.authorized_user_id:
                    await self.strategy.auto_clear_expired_stats(current_symbol, self.authorized_user_id, self.application.bot)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"DCA scheduler error: {e}")
                await asyncio.sleep(60)
    
    async def order_checker_loop(self):
        logger.info("Order checker loop started")
        await asyncio.sleep(30)
        while self.scheduler_running:
            try:
                interval_minutes = self.db.get_order_check_interval()
                if not self.db.get_order_execution_notify():
                    await asyncio.sleep(interval_minutes * 60)
                    continue
                if not self.bybit_initialized:
                    self._init_bybit()
                if not self.bybit_initialized:
                    await asyncio.sleep(interval_minutes * 60)
                    continue
                symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
                if self.authorized_user_id:
                    result = await self.strategy.auto_check_and_notify(symbol, self.authorized_user_id, self.application.bot)
                    if result['count'] > 0:
                        logger.info(f"Auto check found {result['count']} orders ({result['type']})")
                await asyncio.sleep(interval_minutes * 60)
            except asyncio.CancelledError:
                logger.info("Order checker loop cancelled")
                break
            except Exception as e:
                logger.error(f"Order checker error: {e}")
                await asyncio.sleep(60)
    
    async def sell_checker_loop(self):
        logger.info("Sell checker loop started")
        await asyncio.sleep(60)
        while self.scheduler_running:
            try:
                if not self.db.get_sell_tracking_enabled():
                    await asyncio.sleep(3600)
                    continue
                if not self.bybit_initialized:
                    self._init_bybit()
                if not self.bybit_initialized:
                    await asyncio.sleep(3600)
                    continue
                symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
                if self.authorized_user_id:
                    completed_sells = await self.strategy.check_completed_sells(symbol, self.authorized_user_id, self.application.bot)
                    if completed_sells:
                        logger.info(f"Found {len(completed_sells)} completed sell orders")
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                logger.info("Sell checker loop cancelled")
                break
            except Exception as e:
                logger.error(f"Sell checker error: {e}")
                await asyncio.sleep(60)
    
    async def pending_sell_checker_loop(self):
        logger.info("Pending sell checker loop started")
        await asyncio.sleep(120)
        while self.scheduler_running:
            try:
                if not self.bybit_initialized:
                    self._init_bybit()
                if not self.bybit_initialized:
                    await asyncio.sleep(1800)
                    continue
                symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
                if self.authorized_user_id:
                    executed = await self.strategy.check_pending_sell_orders(symbol, self.authorized_user_id, self.application.bot)
                    if executed:
                        logger.info(f"Executed {len(executed)} pending sell orders")
                await asyncio.sleep(1800)
            except asyncio.CancelledError:
                logger.info("Pending sell checker loop cancelled")
                break
            except Exception as e:
                logger.error(f"Pending sell checker error: {e}")
                await asyncio.sleep(60)
    
    async def purchase_notify_loop(self):
        logger.info("Purchase notify loop started (Moscow timezone)")
        await asyncio.sleep(10)
        while self.scheduler_running:
            try:
                if not self.db.get_purchase_notify_enabled():
                    await asyncio.sleep(60)
                    continue
                if not self.bybit_initialized:
                    self._init_bybit()
                if not self.bybit_initialized:
                    await asyncio.sleep(60)
                    continue
                
                now = get_moscow_time()
                notify_time_str = self.db.get_purchase_notify_time()
                last_notify_date = self.db.get_last_purchase_notify_date()
                current_date_str = now.strftime("%Y-%m-%d")
                
                should_notify = False
                try:
                    notify_hour, notify_minute = map(int, notify_time_str.split(':'))
                except:
                    notify_hour, notify_minute = 6, 0
                
                if now.hour == notify_hour and now.minute >= notify_minute and now.minute < notify_minute + 5:
                    if last_notify_date != current_date_str:
                        should_notify = True
                
                if should_notify and self.authorized_user_id:
                    symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
                    current_price = await self.bybit.get_symbol_price(symbol)
                    if current_price:
                        stats = self.db.get_dca_stats(symbol)
                        manual_amount = self.db.get_manual_amount()
                        
                        recommendation = self.db.get_recommendation_for_current_drop(current_price, symbol, for_manual=True)
                        
                        msg = f"🔔 *ЕЖЕДНЕВНОЕ УВЕДОМЛЕНИЕ О ПОКУПКЕ*\n\n"
                        msg += f"💰 Текущая цена {symbol}: `{format_price(current_price, 4)}` USDT\n"
                        msg += f"🕐 Время (МСК): `{now.strftime('%H:%M')}`\n\n"
                        
                        if stats and stats['avg_price'] > 0:
                            current_drop = calculate_current_drop(current_price, stats['avg_price'])
                            msg += f"📉 Средняя цена: `{format_price(stats['avg_price'], 4)}` USDT\n"
                            msg += f"📉 Падение от средней цены: `{current_drop:.1f}%`\n\n"
                            
                            if recommendation['success']:
                                msg += f"🟢 *РЕКОМЕНДАЦИЯ ПО ПОКУПКЕ:*\n"
                                msg += f"📉 Уровень падения: `{recommendation['drop_percent']:.1f}%`\n"
                                msg += f"💰 Рекомендуемая сумма: `{recommendation['amount_usdt']:.2f}` USDT\n"
                                msg += f"📈 Рекомендуемая цена: `{format_price(current_price, 4)}` USDT\n\n"
                            else:
                                msg += f"🟢 *РЕКОМЕНДАЦИЯ:* Покупка не требуется\n"
                        else:
                            msg += f"📊 *Статистика DCA отсутствует*\n\n"
                            msg += f"🟢 *РЕКОМЕНДАЦИЯ ПО ПОКУПКЕ:*\n"
                            msg += f"💰 Рекомендуемая сумма: `{recommendation['amount_usdt']:.2f}` USDT\n"
                            msg += f"📈 Рекомендуемая цена: `{format_price(current_price, 4)}` USDT\n"
                        
                        msg += f"💡 *Сумма для ручного ордера:* `{manual_amount:.2f}` USDT"
                        
                        await safe_send_message(self.application.bot, self.authorized_user_id, msg, parse_mode='Markdown')
                        self.db.set_last_purchase_notify_date(current_date_str)
                        logger.info(f"Sent daily purchase notification at {notify_time_str} MSK")
                
            except asyncio.CancelledError:
                logger.info("Purchase notify loop cancelled")
                break
            except Exception as e:
                logger.error(f"Purchase notify loop error: {e}")
                await asyncio.sleep(60)
            
            await asyncio.sleep(60)
    
    async def send_sell_recommendation_from_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        profit_percent = float(self.db.get_setting('profit_percent', str(PROFIT_PERCENT)))
        stats = self.db.get_dca_stats(symbol)
        if not stats:
            await update.callback_query.message.reply_text("❌ Нет статистики покупок для расчета цены продажи.")
            return
        target_info = self.strategy.calculate_target_info(stats, profit_percent)
        if not target_info:
            await update.callback_query.message.reply_text("❌ Не удалось рассчитать целевую цену.")
            return
        raw_price = target_info['target_price']
        instrument_info = await self.bybit.get_instrument_info(symbol)
        tick_size = instrument_info['tick_size']
        rounded_price = self.bybit._round_price_by_tick(raw_price, tick_size)
        if rounded_price <= 0:
            rounded_price = tick_size
        coin = symbol.replace('USDT', '')
        
        balance_info = await self.bybit.get_balance(coin)
        total_quantity = balance_info.get('equity', 0) if balance_info else 0
        
        if total_quantity <= 0:
            await update.callback_query.message.reply_text(f"❌ Нет монет {coin} на балансе для продажи.")
            return
        
        min_qty = instrument_info['min_qty']
        qty_decimals = instrument_info.get('qty_decimals', SELL_DECIMALS_FALLBACK)
        display_quantity = self.bybit._round_quantity_for_sell(total_quantity, qty_decimals)
        if display_quantity < min_qty and total_quantity >= min_qty:
            for decimals in range(qty_decimals, 0, -1):
                factor = 10 ** decimals
                test_rounded = math.floor(total_quantity * factor) / factor
                if test_rounded >= min_qty:
                    display_quantity = test_rounded
                    break
        
        msg = (f"📊 *РЕКОМЕНДАЦИЯ ПО ПРОДАЖЕ*\n\n"
               f"🪙 Токен: `{symbol}`\n"
               f"💰 Количество для продажи: `{format_quantity(display_quantity, 5)}` {coin}\n"
               f"📈 Целевая прибыль: `{profit_percent}%`\n"
               f"💰 Цена продажи (расчетная): `{format_price(raw_price, 4)}` USDT\n"
               f"💰 Цена продажи (округленная): `{format_price(rounded_price, 4)}` USDT\n"
               f"📊 Прибыль: `{target_info['target_profit']:.2f}` USDT\n\n"
               f"✅ *Выставить ордер на продажу по цене {format_price(rounded_price, 4)} USDT?*")
        context.user_data['pending_sell_data'] = {
            'total_quantity': display_quantity, 
            'display_quantity': display_quantity, 
            'rounded_price': rounded_price, 
            'raw_price': raw_price, 
            'profit_percent': profit_percent, 
            'symbol': symbol
        }
        await update.callback_query.message.reply_text(msg, reply_markup=self.get_sell_confirmation_keyboard(), parse_mode='Markdown')
    
    async def handle_order_execution_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        if data.startswith("add_order_"):
            order_id = data.replace("add_order_", "")
            await self.add_executed_order_to_stats(update, context, order_id)
        elif data.startswith("skip_order_"):
            order_id = data.replace("skip_order_", "")
            await self.skip_executed_order(update, context, order_id)
        elif data.startswith("clear_stats_"):
            symbol = data.replace("clear_stats_", "")
            await self.confirm_clear_stats(update, context, symbol)
        elif data.startswith("skip_clear_"):
            symbol = data.replace("skip_clear_", "")
            await query.edit_message_text(f"⏭ Очистка статистики для {symbol} отложена.")
        elif data.startswith("do_clear_"):
            symbol = data.replace("do_clear_", "")
            await self.clear_stats(update, context, symbol)
        elif data.startswith("cancel_clear_"):
            symbol = data.replace("cancel_clear_", "")
            await query.edit_message_text(f"❌ Очистка статистики для {symbol} отменена.")
        elif data.startswith("confirm_clear_stats_"):
            parts = data.replace("confirm_clear_stats_", "").rsplit("_", 1)
            if len(parts) == 2:
                symbol = parts[0]
                try:
                    sell_id = int(parts[1])
                    await self.execute_clear_stats(update, context, symbol, sell_id)
                except ValueError:
                    logger.error(f"Invalid sell_id in callback: {parts[1]}")
                    await query.edit_message_text("❌ Ошибка: неверный идентификатор продажи.")
            else:
                logger.error(f"Invalid callback data format: {data}")
                await query.edit_message_text("❌ Ошибка: неверный формат данных.")
        elif data.startswith("skip_clear_stats_"):
            parts = data.replace("skip_clear_stats_", "").rsplit("_", 1)
            if len(parts) == 2:
                symbol = parts[0]
                try:
                    sell_id = int(parts[1])
                    await self.skip_clear_stats(update, context, symbol, sell_id)
                except ValueError:
                    logger.error(f"Invalid sell_id in callback: {parts[1]}")
                    await query.edit_message_text("❌ Ошибка: неверный идентификатор продажи.")
            else:
                logger.error(f"Invalid callback data format: {data}")
                await query.edit_message_text("❌ Ошибка: неверный формат данных.")
    
    async def execute_clear_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str, sell_id: int):
        query = update.callback_query
        deleted_count = self.db.clear_all_purchases(symbol)
        if deleted_count > 0:
            self.db.log_action('CONFIRMED_STATS_CLEARED', symbol, f"Подтвержденная очистка после продажи, удалено {deleted_count} покупок")
            self.db.mark_completed_sell_stats_cleared(sell_id)
            self.db.mark_completed_sell_notified(sell_id)
            ladder = self.db.get_ladder_settings(symbol)
            self.db.save_ladder_settings(ladder)
            await query.edit_message_text(f"✅ *Статистика DCA очищена!*\n\n🪙 Токен: `{symbol}`\n🗑 Удалено покупок: `{deleted_count}`\n\n📊 Начинаем новый цикл накопления.\n🪜 Расчет от новой средней цены.\n⚠️ ID покупок будут начинаться с 1 при следующем добавлении.", parse_mode='Markdown')
        else:
            await query.edit_message_text(f"❌ Ошибка при очистке статистики для {symbol}\nВозможно, статистика уже была очищена.", parse_mode='Markdown')
    
    async def skip_clear_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str, sell_id: int):
        query = update.callback_query
        self.db.mark_completed_sell_notified(sell_id)
        await query.edit_message_text(f"⏭ Очистка статистики для {symbol} отложена.\n\n📊 Статистика DCA сохранена.\n💡 Вы можете очистить её позже вручную через раздел '✏️ Редактировать покупки' или '🪜 Лестница Мартингейла' → 'Сбросить лестницу'.", parse_mode='Markdown')
    
    async def confirm_clear_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str):
        query = update.callback_query
        stats = self.db.get_dca_stats(symbol)
        if not stats:
            await query.edit_message_text(f"❌ Нет данных для очистки по {symbol}")
            return
        msg = (f"🗑 *ПОДТВЕРЖДЕНИЕ ОЧИСТКИ*\n\n"
               f"🪙 Токен: `{symbol}`\n"
               f"📊 Всего покупок: `{stats['total_purchases']}`\n"
               f"💰 Вложено: `{stats['total_usdt']:.2f}` USDT\n\n"
               f"❗ *ВНИМАНИЕ! Все покупки по {symbol} будут удалены из статистики!*\n"
               f"⚠️ ID покупок будут сброшены и начнутся с 1!\n\n"
               f"*Подтвердите действие:*")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Да, очистить всё", callback_data=f"do_clear_{symbol}"), InlineKeyboardButton("❌ Нет, отмена", callback_data=f"cancel_clear_{symbol}")]])
        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=keyboard)
    
    async def clear_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str):
        query = update.callback_query
        deleted_count = self.db.clear_all_purchases(symbol)
        if deleted_count > 0:
            self.db.log_action('STATS_CLEARED', symbol, f"Удалено {deleted_count} покупок")
            await query.edit_message_text(f"✅ *Статистика очищена!*\n\n🪙 Токен: `{symbol}`\n🗑 Удалено покупок: `{deleted_count}`\n\nСтатистика DCA по {symbol} очищена. Можно начинать новый цикл.\n⚠️ ID покупок будут начинаться с 1 при следующем добавлении.", parse_mode='Markdown', reply_markup=None)
            ladder = self.db.get_ladder_settings(symbol)
            self.db.save_ladder_settings(ladder)
            await self.application.bot.send_message(chat_id=self.authorized_user_id, text=f"🔄 Статистика для {symbol} очищена. Расчет от новой средней цены.", parse_mode='Markdown')
        else:
            await query.edit_message_text(f"❌ Ошибка при очистке статистики для {symbol}")
    
    async def add_executed_order_to_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str):
        conn = sqlite3.connect(self.db.db_file, timeout=5)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM executed_orders WHERE order_id = ?', (order_id,))
        order = cursor.fetchone()
        conn.close()
        if not order:
            await update.callback_query.edit_message_text("❌ Ордер не найден в базе.")
            return
        order_dict = dict(order)
        if order_dict.get('added_to_stats', 0) == 1:
            await update.callback_query.edit_message_text("ℹ️ Этот ордер уже был добавлен в статистику.")
            return
        
        if self.db.is_order_already_added(order_id):
            await update.callback_query.edit_message_text("ℹ️ Этот ордер уже есть в статистике покупок (по ID ордера).")
            self.db.mark_order_as_added(order_id)
            return
        
        executed_at = order_dict.get('executed_at')
        if executed_at:
            try:
                if isinstance(executed_at, str):
                    date_obj = datetime.strptime(executed_at, "%Y-%m-%d %H:%M:%S")
                else:
                    date_obj = executed_at
                purchase_date = date_obj.strftime("%Y-%m-%d %H:%M:%S")
            except:
                purchase_date = get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")
        else:
            purchase_date = get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")
        symbol = order_dict['symbol']
        price = order_dict['price']
        stats = self.db.get_dca_stats(symbol)
        drop_percent = 0
        step_level = 0
        if stats and stats['avg_price'] > 0:
            drop_percent = calculate_current_drop(price, stats['avg_price'])
            step_level = int(drop_percent)
        purchase_id = self.db.add_purchase(symbol=symbol, amount_usdt=order_dict['amount_usdt'], price=price, quantity=order_dict['quantity'], multiplier=1.0, drop_percent=drop_percent, step_level=step_level, date=purchase_date, order_id=order_id)
        if purchase_id:
            self.db.mark_order_as_added(order_id)
            self.db.reset_incremental_check_time()
            msg = f"✅ *Покупка добавлена в статистику!*\n\n🪙 Токен: `{symbol}`\n💰 Цена: `{format_price(price, 4)}` USDT\n📊 Количество: `{format_quantity(order_dict['quantity'], 5)}`\n💵 Сумма: `{order_dict['amount_usdt']:.2f}` USDT\n📅 Дата: `{purchase_date}`\n"
            if drop_percent > 0:
                msg += f"📉 Падение от средней цены: `{drop_percent:.1f}%`\n"
            msg += f"🆔 ID покупки: `{purchase_id}`"
            await update.callback_query.edit_message_text(msg, parse_mode='Markdown')
            self.db.log_action('EXECUTED_ORDER_ADDED', symbol, f"Ордер {order_id}: {order_dict['amount_usdt']:.2f} USDT по {price} от {purchase_date}")
            await self.send_sell_recommendation_from_callback(update, context)
        elif purchase_id is None:
            await update.callback_query.edit_message_text("ℹ️ Ордер уже был добавлен в статистику ранее.")
        else:
            await update.callback_query.edit_message_text("❌ Ошибка при добавлении покупки в статистику.")
    
    async def skip_executed_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str):
        self.db.mark_order_as_skipped(order_id)
        self.db.reset_incremental_check_time()
        await update.callback_query.edit_message_text("⏭ Пропущено. Ордер не будет добавлен в статистику.")
    
    async def post_init(self, application: Application):
        if self._is_running:
            logger.warning("Bot already running, skipping post_init")
            return
        self._is_running = True
        
        logger.info("Bot initialized, starting scheduler loops...")
        self.scheduler_running = True
        
        self._init_bybit()
        
        if self.bybit_initialized:
            await self.check_api_and_notify(is_startup=True)
        
        task1 = asyncio.create_task(self.dca_scheduler_loop())
        task2 = asyncio.create_task(self.order_checker_loop())
        task3 = asyncio.create_task(self.sell_checker_loop())
        task4 = asyncio.create_task(self.pending_sell_checker_loop())
        task5 = asyncio.create_task(self.purchase_notify_loop())
        task6 = asyncio.create_task(self.api_check_loop())
        
        self.background_tasks = [task1, task2, task3, task4, task5, task6]
        
        if self.db.get_setting('dca_active', 'false') == 'true':
            symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
            if self.bybit_initialized and self.authorized_user_id:
                stats = self.db.get_dca_stats(symbol)
                if stats and stats['total_quantity'] > 0:
                    await self.strategy.check_and_create_sell_order(symbol, self.application.bot, silent=False)
                else:
                    logger.info(f"No purchases for {symbol}, skipping sell order creation on init")
                self._sell_check_task = asyncio.create_task(
                    self.strategy.sell_order_check_loop(symbol, self.authorized_user_id, self.application.bot)
                )
                logger.info("Sell order check loop started on init")
    
    async def shutdown(self, application: Application):
        logger.info("Shutting down bot...")
        self.scheduler_running = False
        self._is_running = False
        
        if self._sell_check_task and not self._sell_check_task.done():
            self._sell_check_task.cancel()
        
        for task in self.background_tasks:
            if not task.done():
                task.cancel()
        
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        
        logger.info("Bot shutdown complete")
    
    async def api_check_loop(self):
        logger.info("API check loop started (every 6 hours)")
        
        await asyncio.sleep(60)
        
        while self.scheduler_running:
            try:
                self.refresh_api_session()
                
                if self.bybit_initialized:
                    await self.check_api_and_notify(is_startup=False)
                else:
                    self._init_bybit()
                    if self.bybit_initialized:
                        await self.check_api_and_notify(is_startup=False)
                
                await asyncio.sleep(6 * 3600)
                
            except asyncio.CancelledError:
                logger.info("API check loop cancelled")
                break
            except Exception as e:
                logger.error(f"API check loop error: {e}")
                await asyncio.sleep(300)
    
    def setup_handlers(self):
        logger.info("Setting up handlers...")
        self.application.add_handler(CommandHandler("start", self.cmd_start_fast))
        self.application.add_handler(CommandHandler("check_api", self.cmd_check_api))
        self.application.add_handler(CommandHandler("refresh_api", self.cmd_refresh_api))
        self.application.add_handler(CommandHandler("check_sells", self.cmd_check_sells))
        self.application.add_handler(CallbackQueryHandler(self.handle_order_execution_callback, pattern='^(add_order_|skip_order_|clear_stats_|skip_clear_|do_clear_|cancel_clear_|confirm_clear_stats_|skip_clear_stats_)'))
        self.application.add_handler(MessageHandler(filters.Regex('^(📤 Экспорт базы)$'), self.handle_export))
        self.application.add_handler(MessageHandler(filters.Regex('^(📥 Импорт базы)$'), self.handle_import_start))
        self.application.add_handler(MessageHandler(filters.Regex('^❌ Отмена$'), self.handle_import_cancel))
        self.application.add_handler(MessageHandler(filters.Document.ALL, self.handle_import_file))
        self.application.add_handler(MessageHandler(filters.Regex('^(✅ Да, выставить ордер на продажу|❌ Нет, отмена)$'), self.handle_sell_confirmation))
        
        purchase_notify_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(🔔 Уведомления о покупке)$'), self.purchase_notify_settings)],
            states={WAITING_PURCHASE_NOTIFY_TIME: [
                MessageHandler(filters.Regex('^(🔔 Уведомления Вкл|🔕 Уведомления Выкл)$'), self.toggle_purchase_notify),
                MessageHandler(filters.Regex('^(⏰ Время уведомления)'), self.set_purchase_notify_time_start),
                MessageHandler(filters.Regex('^(🔙 Назад в настройки)$'), self.back_to_settings_from_purchase),
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_purchase_notify_time_done)
            ]},
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="purchase_notify_conversation", persistent=False, conversation_timeout=CONVERSATION_TIMEOUT
        )
        self.application.add_handler(purchase_notify_conv)
        
        tracking_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(⚙️ Настройки отслеживания)$'), self.tracking_settings)],
            states={NOTIFICATION_SETTINGS_MENU: [
                MessageHandler(filters.Regex('^(✅ Отслеживание ордеров Вкл|❌ Отслеживание ордеров Выкл)$'), self.toggle_tracking),
                MessageHandler(filters.Regex('^(💰 Отслеживание продаж Вкл|⏳ Отслеживание продаж Выкл)$'), self.toggle_sell_tracking_in_settings),
                MessageHandler(filters.Regex('^(⏱ Интервал проверки Ордеров)'), self.set_tracking_interval_start),
                MessageHandler(filters.Regex('^(🔍 Тест отслеживания)$'), self.test_tracking),
                MessageHandler(filters.Regex('^(🔙 Назад в настройки)$'), self.back_to_settings)
            ], WAITING_ORDER_CHECK_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_tracking_interval_done)]},
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="tracking_conversation", persistent=False, conversation_timeout=CONVERSATION_TIMEOUT
        )
        self.application.add_handler(tracking_conv)
        
        edit_purchases_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(✏️ Редактировать покупки)$'), self.edit_purchases_list)],
            states={EDIT_PURCHASE_SELECT: [
                MessageHandler(filters.Regex('^(💰 Изменить цену)$'), self.edit_price_start),
                MessageHandler(filters.Regex('^(📊 Изменить количество)$'), self.edit_amount_start),
                MessageHandler(filters.Regex('^(📅 Изменить дату)$'), self.edit_date_start),
                MessageHandler(filters.Regex('^(❌ Удалить покупку)$'), self.delete_purchase_confirm),
                MessageHandler(filters.Regex('^(🔙 Назад к списку)$'), self.edit_purchases_list),
                MessageHandler(filters.Regex('^(🏠 Главное меню)$'), self.back_to_main),
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_purchase_selected)
            ], EDIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_price_save)], EDIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_amount_save)], EDIT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_date_save)], DELETE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.delete_purchase_execute)]},
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="edit_purchases_conversation", persistent=False, conversation_timeout=CONVERSATION_TIMEOUT
        )
        self.application.add_handler(edit_purchases_conv)
        
        main_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(⚙️ Настройки)$'), self.settings_menu)],
            states={
                SELECTING_ACTION: [
                    MessageHandler(filters.Regex('^(🪙 Выбор токена)$'), self.set_symbol_start),
                    MessageHandler(filters.Regex('^(🚀 Настройки Авто DCA)$'), self.auto_dca_settings_menu),
                    MessageHandler(filters.Regex('^(📊 Процент прибыли)$'), self.set_profit_start),
                    MessageHandler(filters.Regex('^(🪜 Лестница Мартингейла)$'), self.ladder_settings_menu),
                    MessageHandler(filters.Regex('^(💵 Сумма для ручного ордера)$'), self.set_manual_amount_start),
                    MessageHandler(filters.Regex('^(⚙️ Настройки отслеживания)$'), self.tracking_settings),
                    MessageHandler(filters.Regex('^(🔔 Уведомления о покупке)$'), self.purchase_notify_settings),
                    MessageHandler(filters.Regex('^🌐 Режим: (Обычный|Демо)$'), self.toggle_trading_mode),
                    MessageHandler(filters.Regex('^(📤 Экспорт базы)$'), self.handle_export),
                    MessageHandler(filters.Regex('^(📥 Импорт базы)$'), self.handle_import_start),
                    MessageHandler(filters.Regex('^(🔙 Назад в меню)$'), self.back_to_main),
                ],
                SELECTING_SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_symbol_selection)],
                SET_SYMBOL_MANUAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_symbol_manual)],
                SET_PROFIT_PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_profit_done)],
                SET_MANUAL_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_manual_amount_done)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="main_conversation", persistent=False, conversation_timeout=CONVERSATION_TIMEOUT
        )
        self.application.add_handler(main_conv)
        
        auto_dca_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(🚀 Настройки Авто DCA)$'), self.auto_dca_settings_menu)],
            states={
                AUTO_DCA_SETTINGS: [
                    MessageHandler(filters.Regex('^💵 Сумма покупки авто'), self.set_amount_start_auto),
                    MessageHandler(filters.Regex('^⏰ Время покупки'), self.set_time_start_auto),
                    MessageHandler(filters.Regex('^🔄 Частота покупки'), self.set_frequency_start_auto),
                    MessageHandler(filters.Regex('^(🔙 Назад в настройки)$'), self.back_to_settings),
                ],
                SET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_amount_done_auto)],
                SET_SCHEDULE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_time_done_auto)],
                SET_FREQUENCY_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_frequency_done_auto)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="auto_dca_conversation", persistent=False, conversation_timeout=CONVERSATION_TIMEOUT
        )
        self.application.add_handler(auto_dca_conv)
        
        ladder_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(🪜 Лестница Мартингейла)$'), self.ladder_settings_menu)],
            states={LADDER_MENU: [
                MessageHandler(filters.Regex('^(📉 Глубина просадки \(%\))$'), self.set_ladder_max_depth_start),
                MessageHandler(filters.Regex('^(💵 Базовая сумма)$'), self.set_ladder_base_amount_start),
                MessageHandler(filters.Regex('^(📋 Текущие настройки)$'), self.show_ladder_settings),
                MessageHandler(filters.Regex('^(🔄 Сбросить лестницу)$'), self.reset_ladder),
                MessageHandler(filters.Regex('^(🔙 Назад в настройки)$'), self.back_to_settings)
            ], SET_LADDER_DEPTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_ladder_max_depth_save)], SET_LADDER_BASE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_ladder_base_amount_save)]},
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="ladder_conversation", persistent=False, conversation_timeout=CONVERSATION_TIMEOUT
        )
        self.application.add_handler(ladder_conv)
        
        manual_limit_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(💰 Ручная покупка \(лимит\))$'), self.manual_buy_start)],
            states={MANUAL_BUY_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_buy_price_done)], MANUAL_BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_buy_amount_done)]},
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="manual_buy_conversation", persistent=False, conversation_timeout=CONVERSATION_TIMEOUT
        )
        self.application.add_handler(manual_limit_conv)
        
        manual_add_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(➕ Добавить покупку вручную)$'), self.manual_add_start)],
            states={MANUAL_ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_add_price)], MANUAL_ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_add_amount)]},
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="manual_add_conversation", persistent=False, conversation_timeout=CONVERSATION_TIMEOUT
        )
        self.application.add_handler(manual_add_conv)
        
        cancel_order_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(❌ Удалить ордер)$'), self.cancel_order_start)],
            states={WAITING_ORDER_ID_TO_CANCEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.cancel_order_execute)]},
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="cancel_order_conversation", persistent=False, conversation_timeout=CONVERSATION_TIMEOUT
        )
        self.application.add_handler(cancel_order_conv)
        
        self.application.add_handler(MessageHandler(filters.Regex('^(📊 Мой Портфель)$'), self.show_portfolio))
        self.application.add_handler(MessageHandler(filters.Regex('^(🚀 Запустить Авто DCA|⏹ Остановить Авто DCA)$'), self.toggle_dca))
        self.application.add_handler(MessageHandler(filters.Regex('^(📈 Статистика DCA)$'), self.show_dca_stats_detailed))
        self.application.add_handler(MessageHandler(filters.Regex('^(📋 Статус бота)$'), self.show_status))
        self.application.add_handler(MessageHandler(filters.Regex('^(📝 Управление ордерами)$'), self.orders_menu))
        self.application.add_handler(MessageHandler(filters.Regex('^(✅ Отслеживание ордеров Вкл|⏳ Отслеживание ордеров Выкл)$'), self.toggle_order_execution))
        self.application.add_handler(MessageHandler(filters.Regex('^(💰 Отслеживание продаж Вкл|⏳ Отслеживание продаж Выкл)$'), self.toggle_sell_tracking))
        self.application.add_handler(MessageHandler(filters.Regex('^(📋 Список открытых ордеров)$'), self.show_open_orders))
        self.application.add_handler(MessageHandler(filters.Regex('^(🔙 Назад в меню)$'), self.back_to_main))
        self.application.add_handler(MessageHandler(filters.Regex('^(⚙️ Настройки)$'), self.settings_menu))
        
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_unknown))
        logger.info("Handlers setup completed")
    
    def run(self):
        if self._is_running:
            logger.warning("Bot already running, ignoring duplicate run()")
            return
        
        print(f"\n{Fore.CYAN}{'='*60}")
        print(f"{Fore.CYAN}🚀 ЗАПУСК DCA BYBIT BOT (МАРТИНГЕЙЛ ЛЕСТНИЦОЙ)")
        print(f"{Fore.CYAN}Версия: {BOT_VERSION}")
        print(f"{Fore.CYAN}Часовой пояс: Москва (UTC+3)")
        print(f"{Fore.CYAN}{'='*60}")
        if not TELEGRAM_TOKEN:
            print(f"{Fore.RED}❌ TELEGRAM_BOT_TOKEN не найден!")
            return
        print(f"{Fore.GREEN}✅ Токен: {TELEGRAM_TOKEN[:10]}...{TELEGRAM_TOKEN[-5:]}")
        print(f"{Fore.WHITE}👤 Пользователь: {AUTHORIZED_USER}")
        print(f"{Fore.WHITE}🌐 Testnet (из .env): {'Да' if BYBIT_TESTNET_DEFAULT else 'Нет'}")
        print(f"{Fore.WHITE}💾 База данных: dca_bot.db (данные сохраняются)")
        print(f"{Fore.WHITE}🕐 Московское время: {get_moscow_time().strftime('%H:%M')}")
        print(f"{Fore.CYAN}{'='*60}\n")
        
        self.application.post_init = self.post_init
        self.application.shutdown = self.shutdown
        try:
            self.application.run_polling(allowed_updates=Update.ALL_TYPES, poll_interval=1.0, timeout=60)
        except Exception as e:
            logger.error(f"Failed to start bot: {e}")
            print(f"{Fore.RED}❌ Ошибка: {e}")


if __name__ == "__main__":
    try:
        import colorama
    except ImportError:
        print("Устанавливаю colorama...")
        os.system(f"{sys.executable} -m pip install colorama")
        import colorama
    bot = FastDCABot()
    bot.run()