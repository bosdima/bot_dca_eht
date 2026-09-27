#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DCA Bybit Trading Bot - МАРТИНГЕЙЛ ЛЕСЕНКОЙ
Исправления:
- ИСПРАВЛЕН WebSocket: order_stream теперь запускается в отдельном потоке через asyncio.to_thread
- Улучшена надежность мониторинга ордеров на продажу (polling + WebSocket fallback)
- Исправлена обработка завершенных продаж и отправка уведомлений
- Исправлена автоматическая очистка статистики после продажи
- Улучшена обработка ошибок WebSocket
- Добавлены дополнительные проверки баланса перед созданием ордера
"""

BOT_VERSION = "5.43.0 (27.09.2026)"

import os
import sys
import asyncio
import logging
import json
import sqlite3
import re
import time
import math
import random
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any, Union
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
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters,
)
from telegram.request import HTTPXRequest
from pybit.unified_trading import HTTP
from pybit.unified_trading import WebSocket

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

init(autoreset=True)
load_dotenv()

# =============================================================================
#                       НАСТРОЙКИ БОТА (РЕДАКТИРУЙТЕ ЗДЕСЬ)
# =============================================================================
DEFAULT_SYMBOL = "ETHUSDT"
POPULAR_SYMBOLS = ["ETHUSDT", "GRAMUSDT", "XRPUSDT", "BTCUSDT"]

INVEST_AMOUNT = 5.0
SCHEDULE_TIME = "05:00"
FREQUENCY_HOURS = 24

LADDER_BASE_AMOUNT = 5.0
LADDER_MAX_AMOUNT = 15.0
LADDER_MAX_DEPTH = 80

PROFIT_PERCENT = 5
TRADING_MODE = "real"
MANUAL_AMOUNT = 1.1

PURCHASE_NOTIFY_ENABLED = False
PURCHASE_NOTIFY_TIME = "06:00"

ORDER_EXECUTION_NOTIFY = True
ORDER_CHECK_INTERVAL_MINUTES = 60

SELL_TRACKING_ENABLED = True

BYBIT_TESTNET_DEFAULT = False

# НАСТРОЙКИ МОНИТОРИНГА ПРОДАЖ
SELL_MONITOR_INTERVAL = 30
BALANCE_CHECK_THRESHOLD = 0.01
AUTO_CLEAR_DELAY_HOURS = 3
# =============================================================================

# ======================== БЕЗОПАСНАЯ ОТПРАВКА СООБЩЕНИЙ ============================
async def safe_send_message(bot, chat_id, text, parse_mode=None, reply_markup=None, **kwargs):
    try:
        if parse_mode:
            return await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode,
                                          reply_markup=reply_markup, **kwargs)
        else:
            return await bot.send_message(chat_id=chat_id, text=text,
                                          reply_markup=reply_markup, **kwargs)
    except Exception as e:
        if "Can't parse entities" in str(e) or "Bad Request" in str(e):
            logger.warning(f"Markdown parse error, sending without formatting: {e}")
            try:
                clean_text = text.replace('*', '').replace('`', '').replace('_', '').replace('~', '')
                return await bot.send_message(chat_id=chat_id, text=clean_text, reply_markup=reply_markup)
            except Exception as e2:
                logger.error(f"Telegram fallback send failed: {e2}")
        else:
            logger.error(f"Telegram send failed: {e}")
        return None

# =============================================================================

class CustomRotatingFileHandler(RotatingFileHandler):
    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        if self.backupCount > 0:
            for i in range(self.backupCount - 1, 0, -1):
                sfn = f"{self.baseFilename[:-4]}{i}.log"
                dfn = f"{self.baseFilename[:-4]}{i + 1}.log"
                if os.path.exists(sfn):
                    if os.path.exists(dfn):
                        os.remove(dfn)
                    os.rename(sfn, dfn)
            dfn = f"{self.baseFilename[:-4]}1.log"
            if os.path.exists(dfn):
                os.remove(dfn)
            self.rotate(self.baseFilename, dfn)
        if not self.delay:
            self.stream = self._open()

log_handler = CustomRotatingFileHandler("bot_errors.log", encoding='utf-8', maxBytes=200*1024, backupCount=2)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logging.basicConfig(
    level=logging.INFO,
    handlers=[log_handler, logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
AUTHORIZED_USER = os.getenv('AUTHORIZED_USER', '@bosdima')
BYBIT_TESTNET_DEFAULT = os.getenv('BYBIT_TESTNET', 'false').lower() == 'true'
CONVERSATION_TIMEOUT = 180
SELL_DECIMALS_FALLBACK = 5
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

def get_moscow_time() -> datetime:
    return datetime.now(MOSCOW_TZ)

def get_moscow_time_naive() -> datetime:
    return datetime.now(MOSCOW_TZ).replace(tzinfo=None)

def get_api_keys():
    load_dotenv()
    return os.getenv('BYBIT_API_KEY'), os.getenv('BYBIT_API_SECRET')

# --- Константы состояний ---
(
    SELECTING_ACTION, SET_SYMBOL, SET_SYMBOL_MANUAL, SET_AMOUNT, SET_PROFIT_PERCENT,
    SET_MAX_DROP, SET_SCHEDULE_TIME, SET_FREQUENCY_HOURS, MANAGE_ORDERS, EDIT_ORDER_PRICE,
    MANUAL_BUY_PRICE, MANUAL_BUY_AMOUNT, MANUAL_ADD_PRICE, MANUAL_ADD_AMOUNT,
    EDIT_PURCHASE_SELECT, EDIT_PRICE, EDIT_AMOUNT, EDIT_DATE, DELETE_CONFIRM,
    SETTINGS_MENU, NOTIFICATION_SETTINGS_MENU, WAITING_ALERT_PERCENT, WAITING_ALERT_INTERVAL,
    WAITING_IMPORT_FILE, SELECTING_SYMBOL, LADDER_MENU, SET_LADDER_DEPTH,
    SET_LADDER_BASE_AMOUNT, MANUAL_ADD_RECOMMENDATION, WAITING_ORDER_CHECK_INTERVAL,
    WAITING_ORDER_ID_TO_CANCEL, WAITING_SELL_CONFIRMATION, WAITING_CLEAR_STATS_CONFIRMATION,
    WAITING_PURCHASE_NOTIFY_TIME, AUTO_DCA_SETTINGS, SET_MANUAL_AMOUNT,
    MANUAL_ADD_DATE, MANUAL_ORDER_SIDE, MANUAL_SELL_PRICE, MANUAL_SELL_AMOUNT,
) = range(40)

DB_EXPORT_FILE = 'dca_data_export.json'
MAX_DROP_DEPTH = 80

MAIN_MENU_BUTTONS = [
    "📊 Мой Портфель", "🚀 Запустить Авто DCA", "⏹ Остановить Авто DCA",
    "💰 Ручная покупка (лимит)", "📈 Статистика DCA", "➕ Добавить покупку в Статистику DCA",
    "✏️ Редактировать покупки", "⚙️ Настройки", "📋 Статус бота",
    "📝 Управление ордерами", "✅ Отслеживание ордеров Вкл", "⏳ Отслеживание ордеров Выкл",
    "💰 Отслеживание продаж Вкл", "⏳ Отслеживание продаж Выкл", "🏠 Главное меню",
    "🔙 Назад в меню", "🔙 Назад в настройки", "🔙 Назад к списку", "❌ Отмена"
]

# ======================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============================
def format_price(price: float, decimals: int = 4) -> str:
    return f"{price:.{decimals}f}" if price is not None else "N/A"

def format_quantity(qty: float, decimals: int = 5) -> str:
    return f"{qty:.{decimals}f}" if qty is not None else "N/A"

def round_price_up(price: float) -> float:
    return math.ceil(price * 100) / 100

def get_ladder_levels(drop_percent: float, max_depth: float = MAX_DROP_DEPTH) -> Tuple[int, float]:
    if drop_percent <= 0:
        return 0, 0.0
    effective_drop = min(drop_percent, max_depth)
    ratio = (effective_drop / max_depth) * 3.0
    ratio = min(ratio, 3.0)
    return int(effective_drop), ratio

def get_amount_by_drop(drop_percent: float, base_amount: float, max_amount: float,
                       max_depth: float = MAX_DROP_DEPTH) -> float:
    if drop_percent <= 0:
        return base_amount
    effective_drop = min(drop_percent, max_depth)
    fraction = effective_drop / max_depth
    return min(base_amount + (max_amount - base_amount) * fraction, max_amount)

def calculate_current_drop(current_price: float, avg_price: float) -> float:
    if avg_price <= 0:
        return 0
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

# ============================= БАЗА ДАННЫХ ==================================
class Database:
    def __init__(self, db_file: str = "dca_bot.db"):
        self.db_file = db_file
        self.init_db()

    def init_db(self):
        try:
            conn = sqlite3.connect(self.db_file, timeout=10)
            cursor = conn.cursor()
            cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS dca_purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                amount_usdt REAL NOT NULL, price REAL NOT NULL, quantity REAL NOT NULL,
                multiplier REAL DEFAULT 1.0, drop_percent REAL DEFAULT 0,
                step_level INTEGER DEFAULT 0, date TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, order_id TEXT)''')
            cursor.execute("PRAGMA table_info(dca_purchases)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'order_id' not in columns:
                cursor.execute("ALTER TABLE dca_purchases ADD COLUMN order_id TEXT")

            cursor.execute('''CREATE TABLE IF NOT EXISTS sell_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                order_id TEXT NOT NULL UNIQUE, quantity REAL NOT NULL,
                target_price REAL NOT NULL, profit_percent REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, status TEXT DEFAULT 'active')''')

            cursor.execute('''CREATE TABLE IF NOT EXISTS pending_sell_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                quantity REAL NOT NULL, target_price REAL NOT NULL,
                profit_percent REAL NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending', retry_count INTEGER DEFAULT 0,
                last_retry TIMESTAMP, fail_reason TEXT)''')
            cursor.execute("PRAGMA table_info(pending_sell_orders)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'retry_count' not in columns:
                cursor.execute("ALTER TABLE pending_sell_orders ADD COLUMN retry_count INTEGER DEFAULT 0")
            if 'last_retry' not in columns:
                cursor.execute("ALTER TABLE pending_sell_orders ADD COLUMN last_retry TIMESTAMP")
            if 'fail_reason' not in columns:
                cursor.execute("ALTER TABLE pending_sell_orders ADD COLUMN fail_reason TEXT")

            cursor.execute('''CREATE TABLE IF NOT EXISTS completed_sells (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                order_id TEXT NOT NULL, quantity REAL NOT NULL, sell_price REAL NOT NULL,
                profit_percent REAL NOT NULL, profit_usdt REAL NOT NULL,
                sold_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, notified BOOLEAN DEFAULT 0,
                stats_cleared BOOLEAN DEFAULT 0, clear_deadline TIMESTAMP)''')
            cursor.execute("PRAGMA table_info(completed_sells)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'clear_deadline' not in columns:
                cursor.execute("ALTER TABLE completed_sells ADD COLUMN clear_deadline TIMESTAMP")

            cursor.execute('''CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
                symbol TEXT, details TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

            cursor.execute('''CREATE TABLE IF NOT EXISTS dca_start (
                id INTEGER PRIMARY KEY, start_date TIMESTAMP, symbol TEXT, initial_price REAL)''')

            cursor.execute('''CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT, enabled BOOLEAN DEFAULT 1,
                alert_percent REAL DEFAULT 10.0, alert_interval_minutes INTEGER DEFAULT 30,
                last_check TIMESTAMP)''')

            cursor.execute('''CREATE TABLE IF NOT EXISTS ladder_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                max_depth REAL NOT NULL, base_amount REAL NOT NULL, max_amount REAL NOT NULL,
                step_percent REAL DEFAULT 1.0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            cursor.execute("PRAGMA table_info(ladder_settings)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'step_percent' not in columns:
                cursor.execute("ALTER TABLE ladder_settings ADD COLUMN step_percent REAL DEFAULT 1.0")

            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='executed_orders'")
            if not cursor.fetchone():
                cursor.execute('''CREATE TABLE IF NOT EXISTS executed_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL, price REAL NOT NULL, quantity REAL NOT NULL,
                    amount_usdt REAL NOT NULL, executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    added_to_stats BOOLEAN DEFAULT 0, skipped BOOLEAN DEFAULT 0,
                    notified_at TIMESTAMP)''')
            else:
                cursor.execute("PRAGMA table_info(executed_orders)")
                columns = [col[1] for col in cursor.fetchall()]
                if 'skipped' not in columns:
                    cursor.execute("ALTER TABLE executed_orders ADD COLUMN skipped BOOLEAN DEFAULT 0")
                if 'notified_at' not in columns:
                    cursor.execute("ALTER TABLE executed_orders ADD COLUMN notified_at TIMESTAMP")
                if 'added_to_stats' not in columns:
                    cursor.execute("ALTER TABLE executed_orders ADD COLUMN added_to_stats BOOLEAN DEFAULT 0")

            cursor.execute('''CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)''')

            defaults = [
                ('symbol', DEFAULT_SYMBOL), ('invest_amount', str(INVEST_AMOUNT)),
                ('manual_amount', str(MANUAL_AMOUNT)), ('profit_percent', str(PROFIT_PERCENT)),
                ('max_drop_percent', str(LADDER_MAX_DEPTH)), ('max_multiplier', '3'),
                ('schedule_time', SCHEDULE_TIME), ('frequency_hours', str(FREQUENCY_HOURS)),
                ('price_alert_enabled', 'false'), ('dca_active', 'false'),
                ('last_purchase_price', '0'), ('initial_reference_price', '0'),
                ('last_purchase_time', '0'), ('ladder_base_amount', str(LADDER_BASE_AMOUNT)),
                ('ladder_max_depth', str(LADDER_MAX_DEPTH)), ('ladder_max_amount', str(LADDER_MAX_AMOUNT)),
                ('order_execution_notify', str(ORDER_EXECUTION_NOTIFY).lower()),
                ('order_check_interval_minutes', str(ORDER_CHECK_INTERVAL_MINUTES)),
                ('sell_tracking_enabled', str(SELL_TRACKING_ENABLED).lower()),
                ('purchase_notify_enabled', str(PURCHASE_NOTIFY_ENABLED).lower()),
                ('purchase_notify_time', PURCHASE_NOTIFY_TIME), ('last_order_check_time', ''),
                ('last_full_check_time', ''), ('last_sell_check_time', ''),
                ('last_purchase_notify_date', ''), ('first_order_date', ''),
                ('next_dca_purchase_time', ''), ('trading_mode', TRADING_MODE),
                ('last_api_check_time', ''), ('api_status', 'unknown'),
                ('api_error_message', ''), ('last_sell_order_date', ''),
                ('last_purchase_processed_time', ''),
            ]
            for key, value in defaults:
                cursor.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))

            cursor.execute('''INSERT OR IGNORE INTO notifications
                (id, enabled, alert_percent, alert_interval_minutes, last_check)
                VALUES (1, 1, 10.0, 30, CURRENT_TIMESTAMP)''')

            conn.commit()
            conn.close()
            logger.info("Database initialized")
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
            cursor.execute('INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)',
                           (key, value))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error setting {key}: {e}")

    def is_dca_active(self) -> bool:
        return self.get_setting('dca_active', 'false') == 'true'

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

    def get_last_sell_order_date(self) -> Optional[datetime]:
        date_str = self.get_setting('last_sell_order_date', '')
        if date_str:
            try:
                return datetime.fromisoformat(date_str)
            except:
                pass
        return None

    def set_last_sell_order_date(self, date: datetime):
        self.set_setting('last_sell_order_date', date.isoformat())

    def update_first_order_date(self):
        purchases = self.get_purchases()
        if purchases:
            first = min(purchases, key=lambda x: x['date'])
            try:
                first_date = datetime.strptime(first['date'], "%Y-%m-%d %H:%M:%S")
                self.set_first_order_date(first_date)
            except:
                pass
        else:
            self.set_setting('first_order_date', '')

    def get_last_completed_sell(self, symbol: str = None) -> Optional[Dict]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT * FROM completed_sells WHERE symbol = ? ORDER BY sold_at DESC LIMIT 1', (symbol,))
            else:
                cursor.execute('SELECT * FROM completed_sells ORDER BY sold_at DESC LIMIT 1')
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting last completed sell: {e}")
            return None

    def get_all_completed_sells(self, symbol: str = None) -> List[Dict]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT * FROM completed_sells WHERE symbol = ? ORDER BY sold_at DESC', (symbol,))
            else:
                cursor.execute('SELECT * FROM completed_sells ORDER BY sold_at DESC')
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting all completed sells: {e}")
            return []

    def get_check_start_date(self, symbol: str = None) -> Tuple[datetime, str]:
        purchases = self.get_purchases(symbol)
        if purchases:
            try:
                last_purchase = max(purchases, key=lambda x: x['date'])
                last_date = datetime.strptime(last_purchase['date'], "%Y-%m-%d %H:%M:%S")
                return last_date - timedelta(hours=1), f"последний ордер в статистике ({last_date.strftime('%d.%m.%Y %H:%M')})"
            except:
                pass
        last_sell = self.get_last_completed_sell(symbol)
        if last_sell and last_sell.get('sold_at'):
            try:
                sold_at = datetime.fromisoformat(last_sell['sold_at']) if isinstance(last_sell['sold_at'], str) else last_sell['sold_at']
                return sold_at - timedelta(seconds=1), f"последняя продажа ({sold_at.strftime('%d.%m.%Y %H:%M')})"
            except:
                pass
        return get_moscow_time_naive() - timedelta(days=30), "последние 30 дней (нет данных о продажах и ордерах)"

    def is_order_already_added(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT 1 FROM dca_purchases WHERE order_id = ?', (order_id,))
            exists = cursor.fetchone() is not None
            conn.close()
            return exists
        except:
            return False

    def _reassign_ids_by_date(self, symbol: str = None):
        try:
            conn = sqlite3.connect(self.db_file, timeout=10)
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT id, symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date, created_at, order_id FROM dca_purchases WHERE symbol = ? ORDER BY date ASC, created_at ASC', (symbol,))
            else:
                cursor.execute('SELECT id, symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date, created_at, order_id FROM dca_purchases ORDER BY date ASC, created_at ASC')
            rows = cursor.fetchall()
            if not rows:
                conn.close()
                return
            if symbol:
                cursor.execute('DELETE FROM dca_purchases WHERE symbol = ?', (symbol,))
            else:
                cursor.execute('DELETE FROM dca_purchases')
            cursor.execute("DELETE FROM sqlite_sequence WHERE name='dca_purchases'")
            for idx, row in enumerate(rows, 1):
                cursor.execute('''INSERT INTO dca_purchases 
                    (id, symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date, created_at, order_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (idx, row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], row[10]))
            conn.commit()
            conn.close()
            logger.info(f"Reassigned IDs by date for {symbol if symbol else 'all'} purchases. Total: {len(rows)}")
        except Exception as e:
            logger.error(f"Error reassigning IDs by date: {e}")

    def add_purchase(self, symbol: str, amount_usdt: float, price: float, quantity: float,
                     multiplier: float = 1.0, drop_percent: float = 0, step_level: int = 0,
                     date: str = None, order_id: str = None) -> Optional[int]:
        if date is None:
            date = get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")
        if order_id and self.is_order_already_added(order_id):
            logger.warning(f"Order {order_id} already added, skipping")
            return None
        try:
            conn = sqlite3.connect(self.db_file, timeout=10)
            cursor = conn.cursor()
            cursor.execute('''INSERT INTO dca_purchases
                (symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date, order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (symbol, amount_usdt, price, quantity, multiplier, drop_percent, step_level, date, order_id))
            purchase_id = cursor.lastrowid
            conn.commit()
            conn.close()
            self._reassign_ids_by_date(symbol)
            purchases = self.get_purchases(symbol)
            new_id = None
            for p in purchases:
                if (abs(p['price'] - price) < 0.0001 and 
                    abs(p['quantity'] - quantity) < 0.0001 and 
                    p['date'] == date):
                    new_id = p['id']
                    break
            self.update_first_order_date()
            logger.info(f"Purchase added: ID={new_id}, {quantity} {symbol} at {price}")
            return new_id
        except Exception as e:
            logger.error(f"Error adding purchase: {e}")
            return None

    def get_purchases(self, symbol: str = None) -> List[Dict]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=10)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT * FROM dca_purchases WHERE symbol = ? ORDER BY date ASC, id ASC', (symbol,))
            else:
                cursor.execute('SELECT * FROM dca_purchases ORDER BY date ASC, id ASC')
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
        except:
            return None

    def update_purchase(self, purchase_id: int, **kwargs) -> bool:
        allowed = ['symbol', 'amount_usdt', 'price', 'quantity', 'multiplier',
                   'drop_percent', 'step_level', 'date', 'order_id']
        updates = []
        values = []
        for k, v in kwargs.items():
            if k in allowed:
                updates.append(f"{k} = ?")
                values.append(v)
        if not updates:
            return False
        values.append(purchase_id)
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute(f"UPDATE dca_purchases SET {', '.join(updates)} WHERE id = ?", values)
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            if success:
                purchase = self.get_purchase_by_id(purchase_id)
                if purchase:
                    self._reassign_ids_by_date(purchase['symbol'])
                self.update_first_order_date()
            return success
        except:
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
                self.reset_executed_order_status(purchase['price'], purchase['quantity'],
                                                 purchase['symbol'], purchase.get('order_id'))
                self._reassign_ids_by_date(purchase['symbol'])
                self.update_first_order_date()
            return success
        except:
            return False

    def reset_executed_order_status(self, price: float, quantity: float,
                                     symbol: str, order_id: str = None) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            if order_id:
                cursor.execute('''UPDATE executed_orders
                    SET added_to_stats = 0, skipped = 0, notified_at = NULL WHERE order_id = ?''', (order_id,))
            else:
                cursor.execute('''UPDATE executed_orders
                    SET added_to_stats = 0, skipped = 0, notified_at = NULL
                    WHERE symbol = ? AND ABS(price - ?) < 0.0001 AND ABS(quantity - ?) < 0.0001''',
                    (symbol, price, quantity))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return success
        except:
            return False

    def get_dca_stats(self, symbol: str) -> Optional[Dict]:
        purchases = self.get_purchases(symbol)
        if not purchases:
            return None
        total_usdt = sum(p['amount_usdt'] for p in purchases)
        total_qty = sum(p['quantity'] for p in purchases)
        avg_price = total_usdt / total_qty if total_qty > 0 else 0
        return {'total_purchases': len(purchases), 'total_usdt': total_usdt,
                'total_quantity': total_qty, 'avg_price': avg_price}

    def add_sell_order(self, symbol: str, order_id: str, quantity: float,
                       target_price: float, profit_percent: float):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            try:
                cursor.execute('''INSERT INTO sell_orders
                    (symbol, order_id, quantity, target_price, profit_percent)
                    VALUES (?, ?, ?, ?, ?)''',
                    (symbol, order_id, quantity, target_price, profit_percent))
                conn.commit()
            except sqlite3.IntegrityError:
                cursor.execute('''UPDATE sell_orders
                    SET target_price = ?, profit_percent = ?, status = 'active' WHERE order_id = ?''',
                    (target_price, profit_percent, order_id))
                conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error adding sell order: {e}")

    def add_pending_sell_order(self, symbol: str, quantity: float, target_price: float,
                                profit_percent: float, fail_reason: str = None) -> int:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''INSERT INTO pending_sell_orders
                (symbol, quantity, target_price, profit_percent, status, retry_count, last_retry, fail_reason)
                VALUES (?, ?, ?, ?, 'pending', 0, CURRENT_TIMESTAMP, ?)''',
                (symbol, quantity, target_price, profit_percent, fail_reason))
            order_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return order_id
        except:
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
        except:
            return []

    def update_pending_sell_order_status(self, order_id: int, status: str):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE pending_sell_orders SET status = ? WHERE id = ?', (status, order_id))
            conn.commit()
            conn.close()
        except:
            pass

    def update_pending_sell_retry(self, order_id: int, fail_reason: str = None):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''UPDATE pending_sell_orders
                SET retry_count = retry_count + 1, last_retry = CURRENT_TIMESTAMP, fail_reason = ?
                WHERE id = ?''', (fail_reason, order_id))
            conn.commit()
            conn.close()
        except:
            pass

    def delete_pending_sell_order(self, order_id: int):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM pending_sell_orders WHERE id = ?', (order_id,))
            conn.commit()
            conn.close()
        except:
            pass

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
        except:
            return []

    def get_active_sell_order_by_id(self, order_id: str) -> Optional[Dict]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM sell_orders WHERE order_id = ? AND status = "active"', (order_id,))
            row = cursor.fetchone()
            conn.close()
            return dict(row) if row else None
        except:
            return None

    def update_sell_order_status(self, order_id: str, status: str):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE sell_orders SET status = ? WHERE order_id = ?', (status, order_id))
            conn.commit()
            conn.close()
        except:
            pass

    def delete_sell_order(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM sell_orders WHERE order_id = ?', (order_id,))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return success
        except:
            return False

    def add_completed_sell(self, symbol: str, order_id: str, quantity: float,
                            sell_price: float, profit_percent: float, profit_usdt: float) -> int:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('''INSERT INTO completed_sells
                (symbol, order_id, quantity, sell_price, profit_percent, profit_usdt, notified, stats_cleared)
                VALUES (?, ?, ?, ?, ?, ?, 0, 0)''',
                (symbol, order_id, quantity, sell_price, profit_percent, profit_usdt))
            sell_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return sell_id
        except:
            return 0

    def mark_completed_sell_notified(self, sell_id: int):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE completed_sells SET notified = 1 WHERE id = ?', (sell_id,))
            conn.commit()
            conn.close()
        except:
            pass

    def mark_completed_sell_stats_cleared(self, sell_id: int):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE completed_sells SET stats_cleared = 1 WHERE id = ?', (sell_id,))
            conn.commit()
            conn.close()
        except:
            pass

    def set_clear_deadline(self, sell_id: int, deadline: datetime):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE completed_sells SET clear_deadline = ? WHERE id = ?',
                           (deadline.isoformat(), sell_id))
            conn.commit()
            conn.close()
        except:
            pass

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
        except:
            return None

    def is_sell_notified(self, sell_id: int) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT notified FROM completed_sells WHERE id = ?', (sell_id,))
            row = cursor.fetchone()
            conn.close()
            return row and row[0] == 1
        except:
            return False

    def is_sell_notified_by_order_id(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT notified FROM completed_sells WHERE order_id = ? LIMIT 1', (order_id,))
            row = cursor.fetchone()
            conn.close()
            return row and row[0] == 1
        except:
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
        except:
            return []

    def clear_all_purchases(self, symbol: str) -> int:
        try:
            conn = sqlite3.connect(self.db_file, timeout=10)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM dca_purchases WHERE symbol = ?', (symbol,))
            deleted = cursor.rowcount
            cursor.execute("DELETE FROM sqlite_sequence WHERE name='dca_purchases'")
            conn.commit()
            conn.close()
            self.update_first_order_date()
            return deleted
        except:
            return 0

    def reset_autoincrement(self):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            for t in ['dca_purchases', 'sell_orders', 'pending_sell_orders',
                      'completed_sells', 'executed_orders', 'ladder_settings']:
                cursor.execute(f"DELETE FROM sqlite_sequence WHERE name='{t}'")
            conn.commit()
            conn.close()
        except:
            pass

    def get_sell_tracking_enabled(self) -> bool:
        return self.get_setting('sell_tracking_enabled', 'true') == 'true'

    def set_sell_tracking_enabled(self, enabled: bool):
        self.set_setting('sell_tracking_enabled', 'true' if enabled else 'false')

    def get_last_sell_check_time(self) -> Optional[datetime]:
        t = self.get_setting('last_sell_check_time', '')
        if t:
            try:
                return datetime.fromisoformat(t)
            except:
                pass
        return None

    def set_last_sell_check_time(self, dt: datetime):
        self.set_setting('last_sell_check_time', dt.isoformat())

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
        t = self.get_setting('last_api_check_time', '')
        if t:
            try:
                return datetime.fromisoformat(t)
            except:
                pass
        return None

    def set_last_api_check_time(self, dt: datetime):
        self.set_setting('last_api_check_time', dt.isoformat())

    def get_api_status(self) -> str:
        return self.get_setting('api_status', 'unknown')

    def set_api_status(self, status: str):
        self.set_setting('api_status', status)

    def get_api_error_message(self) -> str:
        return self.get_setting('api_error_message', '')

    def set_api_error_message(self, msg: str):
        self.set_setting('api_error_message', msg)

    def log_action(self, action: str, symbol: str = None, details: str = None):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('INSERT INTO history (action, symbol, details) VALUES (?, ?, ?)',
                           (action, symbol, details))
            conn.commit()
            conn.close()
        except:
            pass

    def set_dca_start(self, symbol: str, initial_price: float):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM dca_start')
            cursor.execute('INSERT INTO dca_start (id, start_date, symbol, initial_price) VALUES (1, CURRENT_TIMESTAMP, ?, ?)',
                           (symbol, initial_price))
            conn.commit()
            conn.close()
        except:
            pass

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
            return {
                'symbol': symbol,
                'max_depth': float(self.get_setting('ladder_max_depth', str(LADDER_MAX_DEPTH))),
                'base_amount': float(self.get_setting('invest_amount', str(LADDER_BASE_AMOUNT))),
                'max_amount': float(self.get_setting('invest_amount', str(LADDER_BASE_AMOUNT))) * 3,
                'step_percent': 1.0,
            }
        except:
            return {'symbol': symbol, 'max_depth': LADDER_MAX_DEPTH,
                    'base_amount': LADDER_BASE_AMOUNT, 'max_amount': LADDER_MAX_AMOUNT,
                    'step_percent': 1.0}

    def save_ladder_settings(self, settings: Dict):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM ladder_settings WHERE symbol = ?', (settings['symbol'],))
            cursor.execute('''INSERT INTO ladder_settings
                (symbol, max_depth, base_amount, max_amount, step_percent)
                VALUES (?, ?, ?, ?, ?)''',
                (settings['symbol'], settings['max_depth'], settings['base_amount'],
                 settings['max_amount'], settings.get('step_percent', 1.0)))
            conn.commit()
            conn.close()
            self.set_setting('ladder_max_depth', str(settings['max_depth']))
            self.set_setting('ladder_base_amount', str(settings['base_amount']))
            self.set_setting('ladder_max_amount', str(settings['max_amount']))
            self.set_setting('invest_amount', str(settings['base_amount']))
        except:
            pass

    def calculate_ladder_purchase(self, current_price: float, symbol: str = None) -> Dict:
        if symbol is None:
            symbol = self.get_setting('symbol', DEFAULT_SYMBOL)
        stats = self.get_dca_stats(symbol)
        if not stats or stats['total_quantity'] <= 0:
            return {
                'should_buy': True, 'step_level': 0,
                'amount_usdt': self.get_ladder_settings(symbol)['base_amount'],
                'target_price': current_price, 'drop_percent': 0, 'reason': 'Первая покупка'
            }
        settings = self.get_ladder_settings(symbol)
        avg_price = stats['avg_price']
        current_drop = calculate_current_drop(current_price, avg_price)
        purchases = self.get_purchases(symbol)
        max_purchased_drop = max([p.get('drop_percent', 0) for p in purchases], default=0)
        if current_drop > max_purchased_drop + 0.01:
            amount = get_amount_by_drop(current_drop, settings['base_amount'],
                                        settings['max_amount'], settings['max_depth'])
            if current_drop >= settings['max_depth']:
                return {'should_buy': False, 'step_level': int(current_drop),
                        'amount_usdt': amount, 'target_price': current_price,
                        'reason': f'Достигнута максимальная глубина ({settings["max_depth"]}%)'}
            return {
                'should_buy': True, 'step_level': int(current_drop),
                'amount_usdt': amount, 'target_price': current_price,
                'drop_percent': current_drop, 'current_drop': current_drop,
                'reason': f'Падение {current_drop:.1f}% от средней цены (превышает {max_purchased_drop:.1f}%)'
            }
        next_drop = max_purchased_drop + 1.0
        next_price = avg_price * (1 - next_drop / 100)
        return {
            'should_buy': False, 'step_level': 0, 'amount_usdt': 0,
            'target_price': next_price, 'current_drop': current_drop,
            'next_drop': next_drop,
            'reason': f'Ждем падения до {next_drop:.1f}% ({format_price(next_price)}) от средней цены {format_price(avg_price)}'
        }

    def get_recommendation_for_current_drop(self, current_price: float, symbol: str = None,
                                             for_manual: bool = False) -> Dict:
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
                'success': True, 'drop_percent': 0, 'ratio': 0,
                'amount_usdt': base_amount, 'level': 0, 'avg_price': 0, 'is_first': True,
                'base_amount': base_amount, 'max_amount': max_amount, 'max_depth': max_depth
            }
        avg_price = stats['avg_price']
        drop_percent = calculate_current_drop(current_price, avg_price)
        amount = get_amount_by_drop(drop_percent, base_amount, max_amount, max_depth)
        level, ratio = get_ladder_levels(drop_percent, max_depth)
        return {
            'success': True, 'drop_percent': drop_percent, 'ratio': ratio,
            'amount_usdt': amount, 'level': level, 'avg_price': avg_price,
            'current_drop': drop_percent, 'is_first': False,
            'base_amount': base_amount, 'max_amount': max_amount, 'max_depth': max_depth
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
            amount = get_amount_by_drop(drop_percent, settings['base_amount'],
                                        settings['max_amount'], settings['max_depth'])
            if drop_percent in levels:
                step_purchases = levels[drop_percent]
                total_amount = sum(p['amount_usdt'] for p in step_purchases)
                total_qty = sum(p['quantity'] for p in step_purchases)
                step_avg_price = total_amount / total_qty if total_qty > 0 else 0
                steps.append({
                    'step': drop_percent, 'drop_percent': drop_percent,
                    'ratio': ratio, 'price': step_avg_price,
                    'amount': amount, 'quantity': total_qty, 'status': 'completed'
                })
            else:
                target_price = avg_price * (1 - drop_percent / 100) if avg_price > 0 else 0
                steps.append({
                    'step': drop_percent, 'drop_percent': drop_percent,
                    'ratio': ratio, 'price': target_price,
                    'amount': amount, 'quantity': 0, 'status': 'pending'
                })
        max_purchase_drop = max([p.get('drop_percent', 0) for p in purchases], default=0)
        current_drop = calculate_current_drop(current_price, avg_price) if current_price and avg_price > 0 else 0
        return {
            'symbol': symbol, 'avg_price': avg_price, 'step_percent': 1,
            'max_depth': settings['max_depth'], 'base_amount': settings['base_amount'],
            'max_amount': settings['max_amount'], 'current_step': int(max_purchase_drop),
            'max_purchase_drop': max_purchase_drop, 'current_drop': current_drop, 'steps': steps
        }

    def reset_ladder(self, symbol: str = None):
        if symbol is None:
            symbol = self.get_setting('symbol', DEFAULT_SYMBOL)
        self.clear_all_purchases(symbol)

    def add_executed_order(self, order_id: str, symbol: str, price: float, quantity: float,
                            amount_usdt: float, executed_at: str = None) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            if executed_at:
                cursor.execute('''INSERT OR IGNORE INTO executed_orders
                    (order_id, symbol, price, quantity, amount_usdt, executed_at,
                     added_to_stats, skipped, notified_at)
                    VALUES (?, ?, ?, ?, ?, ?, 0, 0, NULL)''',
                    (order_id, symbol, price, quantity, amount_usdt, executed_at))
            else:
                cursor.execute('''INSERT OR IGNORE INTO executed_orders
                    (order_id, symbol, price, quantity, amount_usdt,
                     added_to_stats, skipped, notified_at)
                    VALUES (?, ?, ?, ?, ?, 0, 0, NULL)''',
                    (order_id, symbol, price, quantity, amount_usdt))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return success
        except:
            return False

    def mark_order_as_added(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE executed_orders SET added_to_stats = 1, notified_at = CURRENT_TIMESTAMP WHERE order_id = ?',
                           (order_id,))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return success
        except:
            return False

    def mark_order_as_skipped(self, order_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('UPDATE executed_orders SET skipped = 1, notified_at = CURRENT_TIMESTAMP WHERE order_id = ?',
                           (order_id,))
            success = cursor.rowcount > 0
            conn.commit()
            conn.close()
            return success
        except:
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
        t = self.get_setting('last_full_check_time', '')
        if t:
            try:
                return datetime.fromisoformat(t)
            except:
                pass
        return None

    def set_last_full_check_time(self, dt: datetime):
        self.set_setting('last_full_check_time', dt.isoformat())

    def get_last_incremental_check_time(self) -> Optional[datetime]:
        t = self.get_setting('last_order_check_time', '')
        if t:
            try:
                return datetime.fromisoformat(t)
            except:
                pass
        return None

    def set_last_incremental_check_time(self, dt: Optional[datetime]):
        self.set_setting('last_order_check_time', dt.isoformat() if dt else '')

    def reset_incremental_check_time(self):
        self.set_last_incremental_check_time(None)

    def get_authorized_user_id(self) -> Optional[int]:
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM bot_state WHERE key = "authorized_user_id"')
            row = cursor.fetchone()
            conn.close()
            return int(row[0]) if row else None
        except:
            return None

    def set_authorized_user_id(self, user_id: int):
        try:
            conn = sqlite3.connect(self.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)',
                           ('authorized_user_id', str(user_id)))
            conn.commit()
            conn.close()
        except:
            pass

    def get_last_purchase_processed_time(self) -> Optional[datetime]:
        t = self.get_setting('last_purchase_processed_time', '')
        if t:
            try:
                return datetime.fromisoformat(t)
            except:
                pass
        return None

    def set_last_purchase_processed_time(self, dt: datetime):
        self.set_setting('last_purchase_processed_time', dt.isoformat())

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
            row = cursor.fetchone()
            notifications = {
                'enabled': bool(row[0]) if row else True,
                'alert_percent': row[1] if row else 10.0,
                'alert_interval_minutes': row[2] if row else 30
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
            for r in ladder_rows:
                ladder_settings.append({
                    'id': r[0], 'symbol': r[1], 'max_depth': r[2],
                    'base_amount': r[3], 'max_amount': r[4],
                    'step_percent': r[5] if len(r) > 5 else 1.0,
                    'created_at': r[6] if len(r) > 6 else None
                })
            cursor.execute('SELECT * FROM executed_orders')
            exec_rows = cursor.fetchall()
            executed_orders = []
            for r in exec_rows:
                executed_orders.append({
                    'id': r[0], 'order_id': r[1], 'symbol': r[2],
                    'price': r[3], 'quantity': r[4], 'amount_usdt': r[5],
                    'executed_at': r[6],
                    'added_to_stats': r[7] if len(r) > 7 else 0,
                    'skipped': r[8] if len(r) > 8 else 0,
                    'notified_at': r[9] if len(r) > 9 else None
                })
            conn.close()
            export_data = {
                'export_date': get_moscow_time_naive().strftime('%Y-%m-%d %H:%M:%S'),
                'version': BOT_VERSION, 'purchases': purchases, 'sell_orders': sell_orders,
                'pending_sell_orders': pending_sells, 'completed_sells': completed_sells,
                'settings': settings, 'notifications': notifications,
                'dca_start': dca_start, 'ladder_settings': ladder_settings,
                'executed_orders': executed_orders
            }
            with open(DB_EXPORT_FILE, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, indent=2, ensure_ascii=False, default=str)
            return True, len(purchases), DB_EXPORT_FILE
        except Exception as e:
            logger.error(f"Export error: {e}")
            return False, 0, str(e)

    def import_database(self, file_path: str) -> Tuple[bool, str]:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            conn = sqlite3.connect(self.db_file, timeout=10)
            cursor = conn.cursor()
            cursor.execute("PRAGMA foreign_keys = OFF")
            for t in ['dca_purchases', 'sell_orders', 'pending_sell_orders', 'completed_sells',
                      'settings', 'dca_start', 'ladder_settings', 'executed_orders',
                      'history', 'notifications']:
                cursor.execute(f"DELETE FROM {t}")
            self.reset_autoincrement()
            purchases_imported = 0
            for p in data.get('purchases', []):
                try:
                    cursor.execute('''INSERT INTO dca_purchases
                        (id, symbol, amount_usdt, price, quantity, multiplier, drop_percent,
                         step_level, date, created_at, order_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (p.get('id'), p.get('symbol', DEFAULT_SYMBOL),
                         p.get('amount_usdt', 0), p.get('price', 0),
                         p.get('quantity', 0), p.get('multiplier', 1.0),
                         p.get('drop_percent', 0), p.get('step_level', 0),
                         p.get('date', get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")),
                         p.get('created_at', get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")),
                         p.get('order_id')))
                    purchases_imported += 1
                except:
                    pass
            orders_imported = 0
            for o in data.get('sell_orders', []):
                try:
                    cursor.execute('''INSERT OR IGNORE INTO sell_orders
                        (id, symbol, order_id, quantity, target_price, profit_percent, created_at, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                        (o.get('id'), o.get('symbol', DEFAULT_SYMBOL),
                         o.get('order_id', f"imported_{o.get('id', 0)}"),
                         o.get('quantity', 0), o.get('target_price', 0),
                         o.get('profit_percent', 5),
                         o.get('created_at', get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")),
                         o.get('status', 'active')))
                    orders_imported += 1
                except:
                    pass
            for p in data.get('pending_sell_orders', []):
                try:
                    cursor.execute('''INSERT OR IGNORE INTO pending_sell_orders
                        (id, symbol, quantity, target_price, profit_percent, created_at,
                         status, retry_count, fail_reason)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (p.get('id'), p.get('symbol', DEFAULT_SYMBOL),
                         p.get('quantity', 0), p.get('target_price', 0),
                         p.get('profit_percent', 5),
                         p.get('created_at', get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")),
                         p.get('status', 'pending'), p.get('retry_count', 0),
                         p.get('fail_reason')))
                except:
                    pass
            for s in data.get('completed_sells', []):
                try:
                    cursor.execute('''INSERT INTO completed_sells
                        (id, symbol, order_id, quantity, sell_price, profit_percent, profit_usdt,
                         sold_at, notified, stats_cleared)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (s.get('id'), s.get('symbol', DEFAULT_SYMBOL), s.get('order_id'),
                         s.get('quantity', 0), s.get('sell_price', 0),
                         s.get('profit_percent', 0), s.get('profit_usdt', 0),
                         s.get('sold_at', get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")),
                         s.get('notified', 0), s.get('stats_cleared', 0)))
                except:
                    pass
            for k, v in data.get('settings', {}).items():
                try:
                    cursor.execute('INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)',
                                   (k, v))
                except:
                    pass
            dca_start = data.get('dca_start')
            if dca_start and dca_start.get('start_date'):
                try:
                    cursor.execute('INSERT OR REPLACE INTO dca_start (id, start_date, symbol, initial_price) VALUES (1, ?, ?, ?)',
                                   (dca_start['start_date'], dca_start.get('symbol', DEFAULT_SYMBOL),
                                    dca_start.get('initial_price', 0)))
                except:
                    pass
            notifications = data.get('notifications', {})
            if notifications:
                try:
                    cursor.execute('''INSERT OR REPLACE INTO notifications
                        (id, enabled, alert_percent, alert_interval_minutes, last_check)
                        VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP)''',
                        (1 if notifications.get('enabled', True) else 0,
                         notifications.get('alert_percent', 10.0),
                         notifications.get('alert_interval_minutes', 30)))
                except:
                    pass
            else:
                cursor.execute('''INSERT OR IGNORE INTO notifications
                    (id, enabled, alert_percent, alert_interval_minutes, last_check)
                    VALUES (1, 1, 10.0, 30, CURRENT_TIMESTAMP)''')
            for l in data.get('ladder_settings', []):
                try:
                    cursor.execute('''INSERT OR REPLACE INTO ladder_settings
                        (id, symbol, max_depth, base_amount, max_amount, step_percent, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)''',
                        (l.get('id'), l.get('symbol', DEFAULT_SYMBOL),
                         l.get('max_depth', 80), l.get('base_amount', 1.1),
                         l.get('max_amount', 3.3), l.get('step_percent', 1.0),
                         l.get('created_at', get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S"))))
                except:
                    pass
            for e in data.get('executed_orders', []):
                try:
                    cursor.execute('''INSERT OR IGNORE INTO executed_orders
                        (id, order_id, symbol, price, quantity, amount_usdt, executed_at,
                         added_to_stats, skipped, notified_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (e.get('id'), e.get('order_id'),
                         e.get('symbol', DEFAULT_SYMBOL), e.get('price', 0),
                         e.get('quantity', 0), e.get('amount_usdt', 0),
                         e.get('executed_at', get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")),
                         e.get('added_to_stats', 0), e.get('skipped', 0),
                         e.get('notified_at')))
                except:
                    pass
            cursor.execute("PRAGMA foreign_keys = ON")
            conn.commit()
            conn.close()
            self.update_first_order_date()
            return True, f"Импортировано: {purchases_imported} покупок, {orders_imported} ордеров"
        except Exception as e:
            logger.error(f"Import error: {e}")
            return False, str(e)

# ============================= BYBIT CLIENT =================================
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
        self._cache_stale_ttl = 300
        self._instrument_cache = {}
        self._instrument_cache_time = {}
        self._instrument_cache_ttl = 3600
        self._init_session()
        self.ws = None
        self.ws_running = False
        self._order_update_callback = None
        self._ws_thread = None
        self._ws_stop_event = threading.Event()

    def _init_session(self):
        try:
            api_key, api_secret = get_api_keys()
            if api_key and api_secret:
                self.api_key = api_key
                self.api_secret = api_secret
                self.session = HTTP(testnet=self.testnet, api_key=self.api_key,
                                    api_secret=self.api_secret, recv_window=10000)
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

    @staticmethod
    def _is_retriable_exc(e: Exception) -> bool:
        err = str(e).lower()
        return ('timeout' in err or 'connection' in err or 'econn' in err
                or 'reset' in err or 'read' in err or 'rate' in err
                or '10016' in err or '10018' in err)

    @staticmethod
    def _is_rate_limit(ret_code: int) -> bool:
        return ret_code in (10016, 10018)

    async def _place_order_with_retry(self, symbol: str, side: str, qty: str,
                                      price: str, context: str = "") -> Dict:
        for attempt in range(3):
            try:
                if not self.session:
                    self._init_session()
                response = self.session.place_order(
                    category="spot", symbol=symbol, side=side, orderType="Limit",
                    qty=qty, price=price, timeInForce="GTC"
                )
                if response['retCode'] in (0, 170131):
                    return response
                if BybitClient._is_rate_limit(int(response['retCode'])):
                    if attempt == 1:
                        logger.warning(f"Bybit rate limit ({response['retCode']}), повторяю: {context}")
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                return response
            except Exception as e:
                if attempt < 2 and BybitClient._is_retriable_exc(e):
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                logger.error(f"Ошибка размещения ордера ({context}): {e}")
                return {'retCode': -1, 'retMsg': str(e), 'result': {}}
        return {'retCode': -1, 'retMsg': 'Ретраи исчерпаны', 'result': {}}

    # =====================================================================
    #   ИСПРАВЛЕННЫЙ МЕТОД ЗАПУСКА WEBSOCKET
    #   Проблема: pybit.order_stream() — синхронный блокирующий метод.
    #   Решение: запускаем его в отдельном потоке через asyncio.to_thread().
    # =====================================================================
    async def start_websocket(self, callback, symbol: str):
        """
        Запускает WebSocket для отслеживания обновлений ордеров.

        ВАЖНО: pybit использует синхронную библиотеку websocket-client,
        поэтому order_stream() — блокирующий вызов. Запускаем его в
        отдельном потоке через asyncio.to_thread(), чтобы не блокировать
        event loop бота.
        """
        if self.ws_running:
            logger.info("WebSocket already running")
            return

        self._order_update_callback = callback
        self._ws_stop_event.clear()

        fail_count = 0
        last_error_log = 0.0
        loop = asyncio.get_running_loop()

        while not self._ws_stop_event.is_set():
            # Перед созданием нового сокета закрываем старый
            if self.ws:
                try:
                    self.ws.exit()
                except Exception:
                    pass
                self.ws = None

            try:
                logger.info(f"Starting WebSocket for {symbol}...")

                self.ws = WebSocket(
                    testnet=self.testnet,
                    channel_type="private",
                    api_key=self.api_key,
                    api_secret=self.api_secret
                )

                self.ws_running = True
                logger.info(f"WebSocket created, starting order_stream in thread...")
                fail_count = 0

                # ---- КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ ----
                # order_stream() блокирует поток. Запускаем его в отдельном
                # потоке через asyncio.to_thread(), чтобы не блокировать loop.
                # Внутри потока pybit сам обрабатывает callback-и в event loop
                # через asyncio.run_coroutine_threadsafe (мы должны это учесть).
                await asyncio.to_thread(self._run_order_stream_sync, symbol)

                logger.info("order_stream thread exited normally")

            except asyncio.CancelledError:
                logger.info("WebSocket task cancelled")
                break
            except Exception as e:
                fail_count += 1
                now = time.time()
                if now - last_error_log > 600 or fail_count <= 3:
                    logger.error(f"WebSocket error: {e} (сбой #{fail_count})")
                    last_error_log = now
                else:
                    logger.debug(f"WebSocket retry... (сбой #{fail_count}): {e}")
            finally:
                self.ws_running = False
                if self.ws:
                    try:
                        self.ws.exit()
                    except Exception:
                        pass
                    self.ws = None

            if self._ws_stop_event.is_set():
                break

            # Реконнект с плавным ростом паузы
            delay = min(60 * (2 ** min(max(fail_count - 1, 0), 2)), 300) + random.uniform(0, 30)
            logger.info(f"WebSocket reconnect in {delay:.0f}s...")
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    def _run_order_stream_sync(self, symbol: str):
        """
        Синхронный метод, который запускается в отдельном потоке.
        Здесь вызывается блокирующий order_stream() из pybit.

        Callback-и из pybit будут вызываться в этом же потоке. Чтобы
        передать их в asyncio event loop, используем
        asyncio.run_coroutine_threadsafe.
        """
        try:
            loop = self._get_main_loop()
            if loop is None:
                logger.error("Main event loop not found, WebSocket callback will not work")
                return

            def sync_callback(message):
                # pybit вызывает этот callback в потоке order_stream.
                # Нам нужно передать управление в основной asyncio loop.
                try:
                    if self._order_update_callback and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._handle_order_update_async(message),
                            loop
                        )
                except Exception as e:
                    logger.error(f"Error scheduling WS callback: {e}")

            self.ws.order_stream(callback=sync_callback)

        except Exception as e:
            logger.error(f"_run_order_stream_sync error: {e}")
            raise

    def _get_main_loop(self):
        """Возвращает основной asyncio event loop."""
        try:
            return asyncio.get_event_loop()
        except RuntimeError:
            return None

    async def _handle_order_update_async(self, message):
        """Асинхронная обёртка для обработки сообщения."""
        try:
            if 'data' not in message:
                return

            order_data = message['data']
            order_id = order_data.get('orderId')
            order_status = order_data.get('orderStatus')
            symbol = order_data.get('symbol')
            side = order_data.get('side')

            if not order_id or not order_status:
                return

            logger.info(f"WebSocket order update: {order_id} -> {order_status}")

            if order_status == 'Filled' and side == 'Sell' and self._order_update_callback:
                order_info = {
                    'order_id': order_id,
                    'symbol': symbol,
                    'side': side,
                    'status': order_status,
                    'price': float(order_data.get('price', 0) or 0),
                    'qty': float(order_data.get('qty', 0) or 0),
                    'cumExecQty': float(order_data.get('cumExecQty', 0) or 0),
                    'cumExecValue': float(order_data.get('cumExecValue', 0) or 0),
                    'avgPrice': float(order_data.get('avgPrice', 0) or 0)
                }
                await self._order_update_callback(order_info)

        except Exception as e:
            logger.error(f"Error handling WebSocket message: {e}")

    async def stop_websocket(self):
        logger.info("Stopping WebSocket...")
        self._ws_stop_event.set()
        self.ws_running = False
        if self.ws:
            try:
                self.ws.exit()
            except Exception:
                pass
        self.ws = None
        # Даём потоку время завершиться
        await asyncio.sleep(1)
        logger.info("WebSocket stopped")

    # --- Остальные методы ---
    async def check_api_health(self) -> Dict:
        self._refresh_session()
        if not self._is_api_available():
            return {'success': False, 'error': 'API keys not set',
                    'user_message': 'API ключи не настроены в .env файле', 'is_api_error': True}
        try:
            if not self.session:
                self._init_session()
            if not self.session:
                return {'success': False, 'error': 'Failed to init session',
                        'user_message': 'Ошибка инициализации API', 'is_api_error': True}
            response = self.session.get_wallet_balance(accountType="UNIFIED")
            if response['retCode'] == 0:
                return {'success': True, 'message': 'API key works'}
            else:
                error_code = response.get('retCode', 0)
                error_msg = response.get('retMsg', 'Unknown error')
                error_descriptions = {
                    10003: 'API ключ не найден',
                    10004: 'API ключ истек или неверный',
                    10005: 'Неверный API ключ или секрет',
                    10006: 'Недостаточно прав',
                    10010: 'IP не в белом списке',
                    10016: 'Превышен лимит запросов',
                }
                user_message = error_descriptions.get(error_code, error_msg)
                return {
                    'success': False, 'error': error_msg, 'error_code': error_code,
                    'user_message': user_message,
                    'is_api_error': error_code in [10003, 10004, 10005, 10006, 10010, 10016]
                }
        except Exception as e:
            logger.error(f"API health check error: {e}")
            return {'success': False, 'error': str(e),
                    'user_message': f'Ошибка соединения: {str(e)[:100]}', 'is_api_error': True}

    async def get_symbol_price(self, symbol: str) -> Optional[float]:
        now = time.time()
        if symbol in self._cache_time and now - self._cache_time.get(symbol, 0) < self._cache_ttl:
            return self._price_cache.get(symbol)
        if not self._is_api_available():
            return self._stale_price_fallback(symbol, now)
        try:
            if not self.session:
                self._init_session()
            response = self.session.get_tickers(category="spot", symbol=symbol)
            if response['retCode'] == 0 and response['result']['list']:
                price = float(response['result']['list'][0]['lastPrice'])
                self._price_cache[symbol] = price
                self._cache_time[symbol] = now
                return price
            return self._stale_price_fallback(symbol, now)
        except Exception as e:
            logger.error(f"Error getting price for {symbol}: {e}")
            return self._stale_price_fallback(symbol, now)

    def _stale_price_fallback(self, symbol: str, now: float) -> Optional[float]:
        cached = self._price_cache.get(symbol)
        if cached is not None:
            cached_age = now - self._cache_time.get(symbol, 0)
            if cached_age < self._cache_stale_ttl:
                return cached
            logger.warning(f"Цена {symbol} в кэше устарела ({cached_age:.0f}s), не использую")
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
            return len(cancelled_ids), cancelled_ids
        except Exception as e:
            logger.error(f"Error cancelling sell orders: {e}")
            return 0, []

    async def get_balance(self, coin: str = None) -> Dict:
        if not self._is_api_available():
            return {'error': 'API not available'}
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
                                    return {'coin': coin, 'equity': equity, 'available': available, 'usdValue': usd_value}
                            return {'coin': coin, 'equity': 0, 'available': 0, 'usdValue': 0}
                        else:
                            total_equity = float(account_data.get('totalEquity', 0) or 0)
                            return {'total_equity': total_equity, 'coins': coins}
            except Exception as e:
                logger.warning(f"Error getting balance with UNIFIED: {e}")
            return {'error': 'Failed to get balance'}
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

    async def get_order_status(self, symbol: str, order_id: str) -> Optional[str]:
        if not self._is_api_available():
            return None
        try:
            if not self.session:
                self._init_session()
            response = self.session.get_open_orders(category="spot", symbol=symbol, orderId=order_id)
            if response['retCode'] == 0:
                orders = response['result']['list']
                if orders:
                    return orders[0].get('orderStatus')
            history = await self.get_order_history(symbol, limit=100)
            for order in history:
                if order.get('orderId') == order_id:
                    return order.get('orderStatus')
            return None
        except Exception as e:
            logger.error(f"Error getting order status for {order_id}: {e}")
            return None

    async def get_instrument_info(self, symbol: str) -> Dict:
        if not self._is_api_available():
            return {'min_qty': 0.01, 'min_amt': 5, 'qty_step': 0.01, 'qty_decimals': 2,
                    'tick_size': 0.0001, 'price_decimals': 4}
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
                qty_step = float(lot_size_filter.get('qtyStep', '0.01'))
                qty_decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 2
                min_qty = float(lot_size_filter.get('minOrderQty', 0.01))
                min_amt = float(lot_size_filter.get('minOrderAmt', 5))
                tick_size = float(price_filter.get('tickSize', '0.0001'))
                price_decimals = len(str(tick_size).split('.')[-1]) if '.' in str(tick_size) else 4
                result = {
                    'min_qty': min_qty, 'min_amt': min_amt, 'qty_step': qty_step,
                    'qty_decimals': qty_decimals, 'tick_size': tick_size,
                    'price_decimals': price_decimals,
                }
                self._instrument_cache[symbol] = result
                self._instrument_cache_time[symbol] = now
                return result
            return {'min_qty': 0.01, 'min_amt': 5, 'qty_step': 0.01, 'qty_decimals': 2,
                    'tick_size': 0.0001, 'price_decimals': 4}
        except Exception as e:
            logger.error(f"Error getting instrument info: {e}")
            return {'min_qty': 0.01, 'min_amt': 5, 'qty_step': 0.01, 'qty_decimals': 2,
                    'tick_size': 0.0001, 'price_decimals': 4}

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
        decimals = len(qty_str.split('.')[-1]) if '.' in qty_str else 0
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

    async def wait_for_order_filled(self, symbol: str, order_id: str,
                                     timeout: int = 3600, check_interval: float = 8.0) -> bool:
        try:
            start_time = time.time()
            while time.time() - start_time < timeout:
                status = await self.get_order_status(symbol, order_id)
                if status:
                    if status == 'Filled':
                        return True
                    elif status in ['Cancelled', 'Rejected']:
                        return False
                await asyncio.sleep(check_interval)
            return False
        except Exception as e:
            logger.error(f"Error waiting for order fill: {e}")
            return False

    async def wait_for_balance_credit(self, coin: str, expected_quantity: float,
                                       timeout: int = 120, check_interval: float = 3.0,
                                       initial_balance: float = 0) -> Tuple[bool, float, float]:
        logger.info(f"[BALANCE CREDIT] Waiting for {coin} balance credit. Expected: {expected_quantity:.8f}")
        target_balance = initial_balance + expected_quantity
        start_time = time.time()
        last_balance = initial_balance
        balance_stable_count = 0
        while time.time() - start_time < timeout:
            balance = await self.get_balance(coin)
            if balance and 'equity' in balance:
                current = balance['equity']
                if current >= target_balance * 0.99:
                    actual_qty = current - initial_balance
                    return True, actual_qty, current
                if current == last_balance:
                    balance_stable_count += 1
                else:
                    balance_stable_count = 0
                if balance_stable_count >= 3:
                    history = await self.get_order_history(symbol=f"{coin}USDT", limit=20)
                    for order in history:
                        if order.get('orderStatus') == 'Filled' and order.get('side') == 'Buy':
                            exec_qty = float(order.get('cumExecQty', 0))
                            if exec_qty >= expected_quantity * 0.95:
                                return True, exec_qty, current
                last_balance = current
            await asyncio.sleep(check_interval)
        final = await self.get_balance(coin)
        if final and 'equity' in final:
            current = final['equity']
            actual_qty = current - initial_balance
            if actual_qty >= expected_quantity * 0.9:
                return True, actual_qty, current
        return False, expected_quantity, initial_balance

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
                                avg_price = float(order.get('avgPrice', 0)) or float(order.get('price', 0))
                                qty = float(order.get('cumExecQty', 0)) or float(order.get('qty', 0))
                                amount_usdt = float(order.get('cumExecValue', 0))
                                if amount_usdt == 0 and avg_price > 0:
                                    amount_usdt = avg_price * qty
                                if qty > 0 and avg_price > 0:
                                    executed.append({
                                        'order_id': order.get('orderId'),
                                        'symbol': order.get('symbol'),
                                        'price': avg_price, 'quantity': qty,
                                        'amount_usdt': amount_usdt,
                                        'executed_at': created_time,
                                        'order_status': order_status
                                    })
                        except:
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
                                avg_price = float(order.get('avgPrice', 0)) or float(order.get('price', 0))
                                qty = float(order.get('cumExecQty', 0)) or float(order.get('qty', 0))
                                amount_usdt = float(order.get('cumExecValue', 0))
                                if amount_usdt == 0 and avg_price > 0:
                                    amount_usdt = avg_price * qty
                                if qty > 0 and avg_price > 0:
                                    completed.append({
                                        'order_id': order.get('orderId'),
                                        'symbol': order.get('symbol'),
                                        'sell_price': avg_price, 'quantity': qty,
                                        'amount_usdt': amount_usdt,
                                        'executed_at': created_time,
                                    })
                        except:
                            continue
            return completed
        except Exception as e:
            logger.error(f"Error getting completed sell orders: {e}")
            return []

    async def get_last_completed_sell_from_api(self, symbol: str) -> Optional[Dict]:
        try:
            orders = await self.get_completed_sell_orders(symbol, from_date=get_moscow_time_naive() - timedelta(days=90))
            if not orders:
                return None
            return max(orders, key=lambda x: x.get('executed_at', datetime.min))
        except Exception as e:
            logger.error(f"Error getting last completed sell from API: {e}")
            return None

    async def cancel_order(self, symbol: str, order_id: str) -> Dict:
        if not self._is_api_available():
            return {'success': False, 'error': 'API not available'}
        for attempt in range(3):
            try:
                if not self.session:
                    self._init_session()
                response = self.session.cancel_order(category="spot", symbol=symbol, orderId=order_id)
                if response['retCode'] == 0:
                    return {'success': True}
                if BybitClient._is_rate_limit(int(response['retCode'])):
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                return {'success': False, 'error': response['retMsg']}
            except Exception as e:
                if attempt < 2 and BybitClient._is_retriable_exc(e):
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                return {'success': False, 'error': str(e)}
        return {'success': False, 'error': 'Не удалось отменить ордер: ретраи исчерпаны'}

    async def place_limit_sell(self, symbol: str, quantity: float, price: float) -> Dict:
        if not self._is_api_available():
            return {'success': False, 'error': 'API not available'}
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
                for dec in range(qty_decimals, 0, -1):
                    factor = 10 ** dec
                    test = math.floor(quantity * factor) / factor
                    if test >= min_qty:
                        rounded_quantity = test
                        break
            if rounded_quantity < min_qty:
                return {'success': False, 'error': f'Минимальное количество: {min_qty} {symbol.replace("USDT","")}'}
            if rounded_quantity <= 0:
                return {'success': False, 'error': 'Недостаточно средств'}
            order_value = rounded_quantity * rounded_price
            if order_value < min_amt:
                return {'success': False, 'error': 'min_amount_error', 'min_amt': min_amt,
                        'order_value': order_value, 'quantity': rounded_quantity, 'price': rounded_price}
            response = await self._place_order_with_retry(
                symbol=symbol, side="Sell", qty=str(rounded_quantity),
                price=str(rounded_price),
                context=f"Продажа {symbol} {rounded_quantity}"
            )
            if response['retCode'] == 0:
                return {'success': True, 'order_id': response['result']['orderId'],
                        'quantity': rounded_quantity, 'price': rounded_price}
            if response['retCode'] == 170131:
                return {'success': False, 'error': 'insufficient_balance', 'message': response['retMsg']}
            return {'success': False, 'error': f"{response['retMsg']} (Код: {response['retCode']})"}
        except Exception as e:
            logger.error(f"Error placing sell order: {e}")
            return {'success': False, 'error': str(e)}

    async def place_limit_buy(self, symbol: str, price: float, amount_usdt: float, is_auto: bool = True) -> Dict:
        if not self._is_api_available():
            return {'success': False, 'error': 'API not available'}
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
            if is_auto and amount_usdt < min_amt:
                amount_usdt = min_amt
            quantity = amount_usdt / rounded_price
            rounded_quantity = self._round_quantity_for_buy(quantity, qty_step, min_qty)
            if rounded_quantity > 0:
                rounded_quantity = round(rounded_quantity, qty_decimals)
            order_value = rounded_quantity * rounded_price
            if order_value < min_amt:
                needed_qty = min_amt / rounded_price
                rounded_needed = self._round_quantity_for_buy(needed_qty, qty_step, min_qty)
                if rounded_needed * rounded_price >= min_amt:
                    rounded_quantity = rounded_needed
                else:
                    rounded_quantity += qty_step
                    order_value = rounded_quantity * rounded_price
                    if order_value < min_amt:
                        return {'success': False, 'error': f'Минимальная сумма ордера: {min_amt} USDT'}
            response = self.session.place_order(
                category="spot", symbol=symbol, side="Buy", orderType="Limit",
                qty=str(rounded_quantity), price=str(rounded_price), timeInForce="GTC"
            )
            if response['retCode'] == 0:
                return {'success': True, 'order_id': response['result']['orderId'],
                        'quantity': rounded_quantity, 'price': rounded_price,
                        'total_usdt': order_value}
            if response['retCode'] == 170131:
                return {'success': False, 'error': 'insufficient_balance', 'message': response['retMsg']}
            return {'success': False, 'error': response['retMsg'], 'code': response['retCode']}
        except Exception as e:
            logger.error(f"Error placing buy order: {e}")
            return {'success': False, 'error': str(e)}

# ============================= DCA STRATEGY ================================
class DCAStrategy:
    def __init__(self, db: Database, bybit: BybitClient):
        self.db = db
        self.bybit = bybit
        self._pending_sell_retry_interval = 300
        self._sell_check_loop_running = False
        self._sell_monitor_active = False
        self.bot = None
        self._monitor_task = None
        self._last_order_status = {}

    def set_bot(self, bot):
        self.bot = bot

    async def _send_sell_order_notification(self, symbol: str, quantity: float, price: float,
                                             profit_percent: float, avg_price: float, order_id: str = None):
        user_id = self.db.get_authorized_user_id()
        if not user_id or not self.bot:
            return
        coin = symbol.replace('USDT', '')
        total_receive = quantity * price
        profit_amount = (price - avg_price) * quantity
        text = (f"✅ *ОРДЕР НА ПРОДАЖУ УСПЕШНО ВЫСТАВЛЕН!*\n"
                f"🪙 Пара: `{symbol}`\n"
                f"📊 Количество: `{format_quantity(quantity, 5)}` {coin}\n"
                f"💰 Цена продажи: `{format_price(price, 4)}` USDT\n"
                f"📈 Прибыль: `{profit_percent}%` от средней цены\n"
                f"📊 *ДЕТАЛИ СДЕЛКИ:*\n"
                f"📉 Средняя цена входа: `{format_price(avg_price, 4)}` USDT\n"
                f"💵 Получу при продаже: `{total_receive:.2f}` USDT\n"
                f"📈 Прибыль: `{profit_amount:.2f}` USDT\n"
                f"✅ Ордер активен!")
        if order_id:
            text += f"\n🆔 ID ордера: `{order_id}`"
        await safe_send_message(self.bot, user_id, text, parse_mode='Markdown')

    async def _send_no_sell_order_notification(self, symbol: str, reason: str):
        user_id = self.db.get_authorized_user_id()
        if not user_id or not self.bot:
            return
        if "Нет монет" in reason or "Количество (0.0)" in reason or "нет покупок" in reason:
            logger.info(f"Skipping no sell order notification for {symbol}: {reason}")
            return
        text = (f"ℹ️ *ОРДЕР НА ПРОДАЖУ НЕ СОЗДАН*\n"
                f"🪙 Пара: `{symbol}`\n"
                f"❗ *Причина:*\n`{reason}`\n"
                f"🔄 Проверка будет выполнена через 1 час.")
        await safe_send_message(self.bot, user_id, text, parse_mode='Markdown')

    async def _send_sell_order_removed_notification(self, symbol: str):
        user_id = self.db.get_authorized_user_id()
        if not user_id or not self.bot:
            return
        text = (f"⚠️ *ОРДЕР НА ПРОДАЖУ БЫЛ УДАЛЕН!*\n"
                f"🪙 Пара: `{symbol}`\n"
                f"❗ Ордер на продажу был удален вручную.\n"
                f"🔄 Бот восстановит ордер автоматически.\n"
                f"✅ Новый ордер будет создан с {self.db.get_setting('profit_percent', str(PROFIT_PERCENT))}% прибыли.")
        await safe_send_message(self.bot, user_id, text, parse_mode='Markdown')

    async def _send_purchase_skipped_notification(self, symbol: str, reason: str,
                                                   current_price: float, avg_price: float):
        user_id = self.db.get_authorized_user_id()
        if not user_id or not self.bot:
            return
        text = (f"⏭ *ПОКУПКА ПРОПУЩЕНА*\n"
                f"🪙 Пара: `{symbol}`\n"
                f"💰 Текущая цена: `{format_price(current_price, 4)}` USDT\n"
                f"📊 Средняя цена: `{format_price(avg_price, 4)}` USDT\n"
                f"❗ *Причина:* {reason}\n"
                f"🔄 Следующая проверка по расписанию.")
        await safe_send_message(self.bot, user_id, text, parse_mode='Markdown')

    async def _send_sell_completed_notification(self, sell_data: Dict, symbol: str):
        user_id = self.db.get_authorized_user_id()
        if not user_id or not self.bot:
            return

        profit_emoji = "🟢" if sell_data['profit_usdt'] >= 0 else "🔴"
        profit_color = "+" if sell_data['profit_usdt'] >= 0 else ""
        days = sell_data.get('days_invested', 1)
        apy = sell_data.get('apy', 0)

        msg = (f"💰 <b>СДЕЛКА ПРОДАНА!</b>\n"
               f"🪙 Токен: <code>{symbol}</code>\n"
               f"📊 Количество: <code>{format_quantity(sell_data['quantity'], 5)}</code>\n"
               f"💰 Цена продажи: <code>{format_price(sell_data['sell_price'], 4)}</code> USDT\n"
               f"💵 Сумма продажи: <code>{sell_data['amount_usdt']:.2f}</code> USDT\n"
               f"📈 <b>СТАТИСТИКА СДЕЛКИ:</b>\n"
               f"💰 Всего инвестировано: <code>{sell_data['total_invested']:.2f}</code> USDT\n"
               f"💵 Получено: <code>{sell_data['amount_usdt']:.2f}</code> USDT\n"
               f"{profit_emoji} Прибыль: <code>{profit_color}{sell_data['profit_usdt']:.2f}</code> USDT\n"
               f"📊 Процент прибыли: <code>{profit_color}{sell_data['profit_percent']:.2f}%</code>\n"
               f"📅 Период инвестиций: <code>{days}</code> дн.\n"
               f"📈 Годовая ставка (APY): <code>{profit_color}{apy:.2f}%</code>\n"
               f"❗ <b>Очистить статистику DCA по этому токену?</b>\n"
               f"После очистки начнется новый цикл накопления.\n"
               f"⚠️ <b>ВНИМАНИЕ: ID покупок будут сброшены и начнутся с 1!</b>\n"
               f"⏰ Если вы не нажмете кнопку, статистика будет очищена автоматически через {AUTO_CLEAR_DELAY_HOURS} часа.")

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, очистить статистику сейчас", callback_data=f"confirm_clear_stats_{symbol}_{sell_data['id']}"),
             InlineKeyboardButton("❌ Нет, оставить", callback_data=f"skip_clear_stats_{symbol}_{sell_data['id']}")]
        ])
        await safe_send_message(self.bot, user_id, msg, parse_mode='HTML', reply_markup=keyboard)

    async def check_and_create_sell_order(self, symbol: str, silent: bool = False) -> Dict:
        try:
            coin = symbol.replace('USDT', '')
            stats = self.db.get_dca_stats(symbol)
            if not stats or stats['total_quantity'] <= 0:
                error = 'Нет статистики DCA для расчета цены (нет покупок)'
                if not silent:
                    await self._send_no_sell_order_notification(symbol, error)
                return {'success': False, 'error': error, 'no_purchases': True}

            avg_price = stats['avg_price']
            profit_percent = float(self.db.get_setting('profit_percent', str(PROFIT_PERCENT)))
            target_price = avg_price * (1 + profit_percent / 100)

            balance_info = await self.bybit.get_balance(coin)
            if not balance_info or 'equity' not in balance_info:
                error = 'Не удалось получить баланс монеты'
                if not silent:
                    await self._send_no_sell_order_notification(symbol, error)
                return {'success': False, 'error': error}

            actual_balance = balance_info.get('equity', 0)

            instrument_info = await self.bybit.get_instrument_info(symbol)
            min_qty = instrument_info['min_qty']

            if actual_balance < min_qty:
                error = f'Недостаточно монет для продажи (баланс: {actual_balance:.8f}, минимум: {min_qty})'
                logger.info(f"No sell order created for {symbol}: {error}")
                return {'success': False, 'error': error, 'insufficient_balance': True}

            open_orders = await self.bybit.get_open_orders(symbol)
            existing_sell = [o for o in open_orders if o.get('side') == 'Sell']
            if existing_sell:
                for order in existing_sell:
                    order_id = order.get('orderId')
                    if self.db.get_active_sell_order_by_id(order_id):
                        return {'success': True, 'message': f'Уже есть ордер на продажу', 'order_id': order_id}
                return {'success': True, 'message': 'Есть ручной ордер на продажу'}

            min_amt = instrument_info['min_amt']
            tick_size = instrument_info['tick_size']
            qty_decimals = instrument_info.get('qty_decimals', SELL_DECIMALS_FALLBACK)
            rounded_price = self.bybit._round_price_by_tick(target_price, tick_size)
            sell_qty = self.bybit._round_quantity_for_sell(actual_balance, qty_decimals)

            if sell_qty < min_qty and actual_balance >= min_qty:
                for dec in range(qty_decimals, 0, -1):
                    factor = 10 ** dec
                    test = math.floor(actual_balance * factor) / factor
                    if test >= min_qty:
                        sell_qty = test
                        break

            if sell_qty < min_qty:
                error = f'Количество ({sell_qty}) меньше минимального ({min_qty})'
                if not silent:
                    await self._send_no_sell_order_notification(symbol, error)
                return {'success': False, 'error': error}

            if sell_qty <= 0:
                error = f'Недостаточно средств. Доступно: {actual_balance} {coin}'
                if not silent:
                    await self._send_no_sell_order_notification(symbol, error)
                return {'success': False, 'error': error}

            order_value = sell_qty * rounded_price
            if order_value < min_amt:
                needed_qty = min_amt / rounded_price
                needed_qty = self.bybit._round_quantity_for_sell(needed_qty, qty_decimals)
                if needed_qty <= actual_balance and needed_qty > 0:
                    sell_qty = needed_qty
                else:
                    error = f'Сумма ({order_value:.2f} USDT) меньше минимальной ({min_amt} USDT)'
                    if not silent:
                        await self._send_no_sell_order_notification(symbol, error)
                    return {'success': False, 'error': error}

            result = await self.bybit.place_limit_sell(symbol, sell_qty, rounded_price)
            if result['success']:
                self.db.add_sell_order(symbol, result['order_id'], result['quantity'],
                                       result['price'], profit_percent)
                await self._send_sell_order_notification(symbol, result['quantity'], result['price'],
                                                         profit_percent, avg_price, result['order_id'])
                return {'success': True, 'order_id': result['order_id'],
                        'quantity': result['quantity'], 'price': result['price'],
                        'profit_percent': profit_percent}
            else:
                error = result.get('error', 'Неизвестная ошибка')
                if result.get('error') == 'insufficient_balance':
                    pending_id = self.db.add_pending_sell_order(symbol, sell_qty, rounded_price,
                                                                profit_percent, 'Недостаточно средств на балансе')
                    if not silent:
                        await self._send_no_sell_order_notification(symbol, 'Недостаточно средств. Ордер отложен.')
                    return {'success': False, 'pending': True, 'pending_id': pending_id, 'error': error}
                if not silent:
                    await self._send_no_sell_order_notification(symbol, error)
                return {'success': False, 'error': error}
        except Exception as e:
            logger.error(f"Error in check_and_create_sell_order: {e}")
            return {'success': False, 'error': str(e)}

    async def sell_order_monitor_loop(self, symbol: str, user_id: int, bot):
        logger.info(f"Sell order monitor loop started for {symbol}")
        self._sell_monitor_active = True
        check_interval = SELL_MONITOR_INTERVAL

        while self._sell_monitor_active and self.db.is_dca_active():
            try:
                active_orders = self.db.get_active_sell_orders(symbol)
                if not active_orders:
                    stats = self.db.get_dca_stats(symbol)
                    if stats and stats['total_quantity'] > 0:
                        await self.check_and_create_sell_order(symbol, silent=False)
                    await asyncio.sleep(check_interval)
                    continue

                order = active_orders[0]
                order_id = order['order_id']
                target_price = order['target_price']

                status = await self.bybit.get_order_status(symbol, order_id)

                if status == 'Filled':
                    logger.info(f"Sell order {order_id} is FILLED!")

                    completed_orders = await self.bybit.get_completed_sell_orders(
                        symbol, from_date=get_moscow_time_naive() - timedelta(hours=1)
                    )
                    completed = None
                    for co in completed_orders:
                        if co['order_id'] == order_id:
                            completed = co
                            break

                    coin = symbol.replace('USDT', '')
                    balance_info = await self.bybit.get_balance(coin)
                    remaining = balance_info.get('equity', 0) if balance_info else 0

                    if not completed:
                        completed = {
                            'order_id': order_id,
                            'symbol': symbol,
                            'sell_price': order.get('target_price', 0),
                            'quantity': order.get('quantity', 0),
                            'amount_usdt': order.get('quantity', 0) * order.get('target_price', 0),
                            'executed_at': get_moscow_time_naive()
                        }

                    if completed:
                        stats = self.db.get_dca_stats(symbol)
                        avg_entry = stats['avg_price'] if stats else 0

                        sell_price = completed.get('sell_price', 0)
                        if sell_price == 0:
                            sell_price = order.get('target_price', 0)

                        qty = completed.get('quantity', 0)
                        if qty == 0:
                            qty = order.get('quantity', 0)

                        profit_percent = ((sell_price - avg_entry) / avg_entry * 100) if avg_entry > 0 else 0
                        profit_usdt = (sell_price - avg_entry) * qty if avg_entry > 0 else 0
                        total_invested = stats['total_usdt'] if stats else 0
                        first_order_date = self.db.get_first_order_date()
                        days_invested = max(1, (get_moscow_time_naive() - first_order_date).days) if first_order_date else 1
                        apy = calculate_apy(profit_usdt, total_invested, days_invested) if total_invested > 0 else 0

                        sell_id = self.db.add_completed_sell(
                            symbol=symbol,
                            order_id=order_id,
                            quantity=qty,
                            sell_price=sell_price,
                            profit_percent=profit_percent,
                            profit_usdt=profit_usdt
                        )

                        self.db.update_sell_order_status(order_id, 'completed')
                        self.db.set_last_sell_order_date(get_moscow_time_naive())

                        sell_data = {
                            'id': sell_id,
                            'order_id': order_id,
                            'quantity': qty,
                            'sell_price': sell_price,
                            'amount_usdt': qty * sell_price,
                            'profit_percent': profit_percent,
                            'profit_usdt': profit_usdt,
                            'total_invested': total_invested,
                            'days_invested': days_invested,
                            'apy': apy
                        }

                        await self._send_sell_completed_notification(sell_data, symbol)
                        self.db.mark_completed_sell_notified(sell_id)

                        deadline = get_moscow_time_naive() + timedelta(hours=AUTO_CLEAR_DELAY_HOURS)
                        self.db.set_clear_deadline(sell_id, deadline)

                        await asyncio.sleep(3)
                        stats_after = self.db.get_dca_stats(symbol)
                        if stats_after and stats_after['total_quantity'] > 0:
                            await self.check_and_create_sell_order(symbol, silent=False)
                        else:
                            if user_id and self.bot:
                                await safe_send_message(
                                    self.bot, user_id,
                                    f"📊 Статистика DCA для {symbol} будет очищена через {AUTO_CLEAR_DELAY_HOURS} часа, если вы не нажмете кнопку очистки.",
                                    parse_mode='Markdown'
                                )

                elif status in ['Cancelled', 'Rejected']:
                    logger.info(f"Sell order {order_id} was {status}")
                    self.db.update_sell_order_status(order_id, status.lower())
                    stats = self.db.get_dca_stats(symbol)
                    if stats and stats['total_quantity'] > 0:
                        await self.check_and_create_sell_order(symbol, silent=False)

                elif status is None:
                    completed_orders = await self.bybit.get_completed_sell_orders(
                        symbol, from_date=get_moscow_time_naive() - timedelta(hours=1)
                    )
                    found = False
                    for co in completed_orders:
                        if co['order_id'] == order_id:
                            found = True
                            status = 'Filled'
                            break

                    if found:
                        continue
                    else:
                        self.db.update_sell_order_status(order_id, 'unknown')
                        stats = self.db.get_dca_stats(symbol)
                        if stats and stats['total_quantity'] > 0:
                            await self.check_and_create_sell_order(symbol, silent=False)

                await asyncio.sleep(check_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in sell_order_monitor_loop: {e}")
                await asyncio.sleep(check_interval)

        logger.info(f"Sell order monitor loop stopped for {symbol}")

    async def sell_order_check_loop(self, symbol: str):
        logger.info(f"Sell order check loop started for {symbol}")
        self._sell_check_loop_running = True
        stats = self.db.get_dca_stats(symbol)
        if stats and stats['total_quantity'] > 0:
            await self.check_and_create_sell_order(symbol, silent=False)
        while self._sell_check_loop_running and self.db.is_dca_active():
            await asyncio.sleep(3600)
            if not self.db.is_dca_active():
                break
            stats = self.db.get_dca_stats(symbol)
            if not stats or stats['total_quantity'] <= 0:
                await self.check_completed_sells(symbol, force=True)
                break
            open_orders = await self.bybit.get_open_orders(symbol)
            sell_orders = [o for o in open_orders if o.get('side') == 'Sell']
            if not sell_orders:
                await self._send_sell_order_removed_notification(symbol)
                await self.check_and_create_sell_order(symbol, silent=False)

    def stop_sell_check_loop(self):
        self._sell_check_loop_running = False

    async def cancel_old_sell_orders(self, symbol: str) -> int:
        try:
            open_orders = await self.bybit.get_open_orders(symbol)
            sell_orders = [o for o in open_orders if o.get('side') == 'Sell']
            if not sell_orders:
                return 0
            cancelled_count, cancelled_ids = await self.bybit.cancel_all_sell_orders(symbol)
            for order_id in cancelled_ids:
                self.db.update_sell_order_status(order_id, 'cancelled')
            if cancelled_count > 0:
                await asyncio.sleep(3)
            return cancelled_count
        except Exception as e:
            logger.error(f"Error cancelling old sell orders: {e}")
            return 0

    async def handle_order_update(self, order_info: Dict):
        """Обрабатывает обновление статуса ордера через WebSocket."""
        try:
            order_id = order_info['order_id']
            order_status = order_info['status']
            symbol = order_info['symbol']

            active_order = self.db.get_active_sell_order_by_id(order_id)
            if not active_order:
                logger.info(f"Order {order_id} not found in active orders, ignoring")
                return

            if order_status == 'Filled':
                logger.info(f"Sell order {order_id} was FILLED via WebSocket!")

                avg_price = order_info.get('avgPrice', 0)
                qty = order_info.get('cumExecQty', 0) or order_info.get('qty', 0)
                amount_usdt = order_info.get('cumExecValue', 0)

                if avg_price == 0:
                    orders = await self.bybit.get_completed_sell_orders(symbol, from_date=get_moscow_time_naive() - timedelta(hours=1))
                    for o in orders:
                        if o['order_id'] == order_id:
                            avg_price = o['sell_price']
                            qty = o['quantity']
                            amount_usdt = o['amount_usdt']
                            break

                if avg_price == 0 or qty == 0:
                    avg_price = active_order['target_price']
                    qty = active_order['quantity']
                    amount_usdt = qty * avg_price

                stats = self.db.get_dca_stats(symbol)
                avg_entry = stats['avg_price'] if stats else 0
                profit_percent = ((avg_price - avg_entry) / avg_entry * 100) if avg_entry > 0 else 0
                profit_usdt = (avg_price - avg_entry) * qty if avg_entry > 0 else 0
                total_invested = stats['total_usdt'] if stats else 0
                first_order_date = self.db.get_first_order_date()
                days_invested = max(1, (get_moscow_time_naive() - first_order_date).days) if first_order_date else 1
                apy = calculate_apy(profit_usdt, total_invested, days_invested) if total_invested > 0 else 0

                sell_id = self.db.add_completed_sell(
                    symbol=symbol,
                    order_id=order_id,
                    quantity=qty,
                    sell_price=avg_price,
                    profit_percent=profit_percent,
                    profit_usdt=profit_usdt
                )

                self.db.update_sell_order_status(order_id, 'completed')
                self.db.set_last_sell_order_date(get_moscow_time_naive())

                sell_data = {
                    'id': sell_id,
                    'order_id': order_id,
                    'quantity': qty,
                    'sell_price': avg_price,
                    'amount_usdt': amount_usdt,
                    'profit_percent': profit_percent,
                    'profit_usdt': profit_usdt,
                    'total_invested': total_invested,
                    'days_invested': days_invested,
                    'apy': apy
                }

                await self._send_sell_completed_notification(sell_data, symbol)
                self.db.mark_completed_sell_notified(sell_id)

                deadline = get_moscow_time_naive() + timedelta(hours=AUTO_CLEAR_DELAY_HOURS)
                self.db.set_clear_deadline(sell_id, deadline)

                await asyncio.sleep(3)
                stats_after = self.db.get_dca_stats(symbol)
                if stats_after and stats_after['total_quantity'] > 0:
                    await self.check_and_create_sell_order(symbol, silent=False)
                else:
                    user_id = self.db.get_authorized_user_id()
                    if user_id and self.bot:
                        await safe_send_message(
                            self.bot, user_id,
                            f"📊 Статистика DCA для {symbol} будет очищена через {AUTO_CLEAR_DELAY_HOURS} часа, если вы не нажмете кнопку очистки.",
                            parse_mode='Markdown'
                        )

            elif order_status in ['Cancelled', 'Rejected']:
                logger.info(f"Sell order {order_id} was {order_status}")
                self.db.update_sell_order_status(order_id, order_status.lower())
                stats = self.db.get_dca_stats(symbol)
                if stats and stats['total_quantity'] > 0:
                    await self.check_and_create_sell_order(symbol, silent=False)

        except Exception as e:
            logger.error(f"Error handling order update: {e}")

    async def _place_buy_order_and_wait(self, symbol: str, price: float, amount: float, is_auto: bool) -> Dict:
        result = await self.bybit.place_limit_buy(symbol, price, amount, is_auto)
        if not result['success']:
            return result
        order_id = result['order_id']
        coin = symbol.replace('USDT', '')
        initial_balance = 0
        bal = await self.bybit.get_balance(coin)
        if bal and 'equity' in bal:
            initial_balance = bal['equity']
        filled = await self.bybit.wait_for_order_filled(symbol, order_id, timeout=3600)
        if not filled:
            logger.warning(f"Order {order_id} not filled, cancelling...")
            await self.bybit.cancel_order(symbol, order_id)
            return {'success': False, 'error': 'order_not_filled'}
        credited, actual_qty, final_bal = await self.bybit.wait_for_balance_credit(
            coin, result['quantity'], timeout=120, initial_balance=initial_balance)
        if credited:
            actual_qty = actual_qty
        else:
            actual_qty = result['quantity']
        instrument_info = await self.bybit.get_instrument_info(symbol)
        qty_decimals = instrument_info.get('qty_decimals', 5)
        actual_qty_rounded = round(actual_qty, qty_decimals)
        if actual_qty_rounded <= 0:
            actual_qty_rounded = round(result['quantity'], qty_decimals)
        actual_amount_usdt = actual_qty_rounded * result['price']
        return {
            'success': True,
            'order_id': order_id,
            'price': result['price'],
            'quantity': result['quantity'],
            'actual_quantity': actual_qty_rounded,
            'actual_amount_usdt': actual_amount_usdt,
            'initial_balance': initial_balance,
            'final_balance': final_bal if credited else None
        }

    async def _place_sell_order(self, symbol: str, quantity: float, target_price: float,
                                 profit_percent: float) -> Dict:
        instrument_info = await self.bybit.get_instrument_info(symbol)
        min_qty = instrument_info['min_qty']
        min_amt = instrument_info['min_amt']
        tick_size = instrument_info['tick_size']
        qty_decimals = instrument_info.get('qty_decimals', SELL_DECIMALS_FALLBACK)
        rounded_qty = self.bybit._round_quantity_for_sell(quantity, qty_decimals)
        if rounded_qty <= 0:
            return {'success': False, 'error': f'Недостаточно средств для продажи'}
        if rounded_qty < min_qty and quantity >= min_qty:
            for dec in range(qty_decimals, 0, -1):
                factor = 10 ** dec
                test = math.floor(quantity * factor) / factor
                if test >= min_qty:
                    rounded_qty = test
                    break
        if rounded_qty < min_qty and quantity >= min_qty * 0.99:
            rounded_qty = min_qty
        if rounded_qty < min_qty:
            return {'success': False, 'error': f'Минимальное количество: {min_qty}'}
        rounded_price = self.bybit._round_price_by_tick(target_price, tick_size)
        order_value = rounded_qty * rounded_price
        if order_value < min_amt:
            pending_id = self.db.add_pending_sell_order(
                symbol, rounded_qty, rounded_price, profit_percent,
                f'Сумма ордера ({order_value:.2f} USDT) меньше минимальной ({min_amt} USDT)')
            return {'success': False, 'pending': True, 'pending_id': pending_id,
                    'reason': f'Сумма ордера ({order_value:.2f}) < {min_amt}'}
        result = await self.bybit.place_limit_sell(symbol, rounded_qty, rounded_price)
        if result['success']:
            self.db.add_sell_order(symbol, result['order_id'], result['quantity'],
                                   result['price'], profit_percent)
            return {'success': True, 'order_id': result['order_id'],
                    'quantity': result['quantity'], 'price': result['price']}
        elif result.get('error') == 'insufficient_balance':
            pending_id = self.db.add_pending_sell_order(symbol, rounded_qty, rounded_price,
                                                        profit_percent, 'Недостаточно средств')
            return {'success': False, 'pending': True, 'pending_id': pending_id,
                    'reason': 'Недостаточно средств на балансе'}
        elif result.get('error') == 'min_amount_error':
            pending_id = self.db.add_pending_sell_order(symbol, rounded_qty, rounded_price,
                                                        profit_percent, f'Минимальная сумма: {min_amt} USDT')
            return {'success': False, 'pending': True, 'pending_id': pending_id,
                    'reason': f'Минимальная сумма: {min_amt} USDT'}
        else:
            error = result.get('error', 'Неизвестная ошибка')
            pending_id = self.db.add_pending_sell_order(symbol, rounded_qty, rounded_price,
                                                        profit_percent, error)
            return {'success': False, 'pending': True, 'pending_id': pending_id, 'reason': error}

    async def execute_scheduled_purchase(self, symbol: str, profit_percent: float) -> Dict:
        if not self.bybit._is_api_available():
            return {'success': False, 'error': 'API Bybit не доступен'}

        current_price = await self.bybit.get_symbol_price(symbol)
        if not current_price:
            return {'success': False, 'error': 'Не удалось получить цену'}

        stats = self.db.get_dca_stats(symbol)
        settings = self.db.get_ladder_settings(symbol)
        base_amount = settings['base_amount']
        instrument_info = await self.bybit.get_instrument_info(symbol)
        min_amt = instrument_info['min_amt']
        tick_size = instrument_info['tick_size']
        qty_decimals = instrument_info.get('qty_decimals', 5)

        if stats and stats['total_quantity'] > 0:
            avg_price = stats['avg_price']
            if current_price > avg_price:
                reason = f'Текущая цена ({format_price(current_price, 4)}) ВЫШЕ средней цены ({format_price(avg_price, 4)})'
                await self._send_purchase_skipped_notification(symbol, reason, current_price, avg_price)
                return {'success': False, 'error': 'skip_price_above_avg'}

        if not stats or stats['total_quantity'] <= 0:
            amount_usdt = max(base_amount, min_amt)
            drop_percent = 0
            step_level = 0
        else:
            avg_price = stats['avg_price']
            current_drop = calculate_current_drop(current_price, avg_price)
            if current_price < avg_price:
                amount_usdt = get_amount_by_drop(current_drop, base_amount,
                                                 settings['max_amount'], settings['max_depth'])
                drop_percent = current_drop
                step_level = int(current_drop)
            else:
                amount_usdt = base_amount
                drop_percent = 0
                step_level = 0

        if amount_usdt < min_amt:
            amount_usdt = min_amt

        usdt_balance = await self.bybit.get_balance('USDT')
        available_usdt = usdt_balance.get('available', 0) if usdt_balance else 0
        if available_usdt < amount_usdt:
            return {'success': False, 'error': f'Недостаточно средств. Нужно {amount_usdt:.2f} USDT, доступно {available_usdt:.2f} USDT'}

        last_processed = self.db.get_last_purchase_processed_time()
        if last_processed and (get_moscow_time_naive() - last_processed).total_seconds() < 60:
            return {'success': False, 'error': 'purchase_already_processed'}

        max_retries = 2
        retry_count = 0
        order_result = None
        while retry_count < max_retries:
            if retry_count > 0:
                current_price = await self.bybit.get_symbol_price(symbol)
                if not current_price:
                    return {'success': False, 'error': 'Не удалось обновить цену'}
            limit_price = self.bybit._round_price_by_tick(current_price, tick_size)
            order_result = await self._place_buy_order_and_wait(symbol, limit_price, amount_usdt, is_auto=True)
            if order_result['success']:
                break
            retry_count += 1
            if retry_count < max_retries:
                await asyncio.sleep(5)

        if not order_result or not order_result['success']:
            return {'success': False, 'error': order_result.get('error', 'Не удалось разместить ордер')}

        current_date = get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")
        purchase_id = self.db.add_purchase(
            symbol=symbol,
            amount_usdt=order_result['actual_amount_usdt'],
            price=order_result['price'],
            quantity=order_result['actual_quantity'],
            multiplier=1.0,
            drop_percent=drop_percent,
            step_level=step_level,
            date=current_date,
            order_id=order_result['order_id']
        )
        if purchase_id is None:
            return {'success': False, 'error': 'Ордер уже существует в базе данных'}

        self.db.set_setting('last_purchase_price', str(order_result['price']))
        self.db.set_setting('last_purchase_time', str(get_moscow_time_naive().timestamp()))
        self.db.set_last_purchase_processed_time(get_moscow_time_naive())

        await self.cancel_old_sell_orders(symbol)
        await asyncio.sleep(2)

        updated_stats = self.db.get_dca_stats(symbol)
        if updated_stats and updated_stats['total_quantity'] > 0:
            avg_price = updated_stats['avg_price']
        else:
            avg_price = order_result['price']
        target_price_sell = avg_price * (1 + profit_percent / 100)

        coin = symbol.replace('USDT', '')
        balance_after = await self.bybit.get_balance(coin)
        sell_qty = balance_after.get('equity', 0) if balance_after else 0
        if sell_qty <= 0:
            sell_qty = order_result['actual_quantity']

        sell_result = await self._place_sell_order(symbol, sell_qty, target_price_sell, profit_percent)

        result = {
            'success': True,
            'amount_usdt': amount_usdt,
            'price': order_result['price'],
            'quantity': order_result['quantity'],
            'actual_quantity': order_result['actual_quantity'],
            'actual_amount_usdt': order_result['actual_amount_usdt'],
            'drop_percent': drop_percent,
            'initial_balance': order_result.get('initial_balance', 0),
            'final_balance': order_result.get('final_balance', 0),
        }
        if sell_result['success']:
            result['sell_order_id'] = sell_result['order_id']
            result['sell_quantity'] = sell_result['quantity']
            result['target_price'] = sell_result['price']
            result['sell_order_placed'] = True
            await self._send_sell_order_notification(symbol, sell_result['quantity'], sell_result['price'],
                                                     profit_percent, avg_price, sell_result['order_id'])
        elif sell_result.get('pending'):
            result['pending_order_id'] = sell_result['pending_id']
            result['sell_warning'] = "⚠️ Ордер на продажу отложен"
            result['sell_order_placed'] = False
        else:
            result['sell_warning'] = sell_result.get('reason', 'Не удалось создать ордер на продажу')
            result['sell_order_placed'] = False
        return result

    async def check_pending_sell_orders(self, symbol: str) -> List[Dict]:
        pending_orders = self.db.get_pending_sell_orders(symbol)
        executed = []
        if not pending_orders:
            return []
        current_price = await self.bybit.get_symbol_price(symbol)
        if not current_price:
            return []
        instrument_info = await self.bybit.get_instrument_info(symbol)
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
                    if (get_moscow_time_naive() - last_retry_time).total_seconds() < self._pending_sell_retry_interval:
                        continue
                except:
                    pass
            open_orders = await self.bybit.get_open_orders(symbol)
            if any(o.get('side') == 'Sell' for o in open_orders):
                self.db.delete_pending_sell_order(order['id'])
                continue
            if current_price >= order['target_price']:
                new_target = current_price * (1 + order['profit_percent'] / 100)
                rounded_price = self.bybit._round_price_by_tick(new_target, tick_size)
                rounded_qty = self.bybit._round_quantity_for_sell(order['quantity'], qty_decimals)
                sell_result = await self._place_sell_order(symbol, rounded_qty, rounded_price,
                                                           order['profit_percent'])
                if sell_result['success']:
                    self.db.delete_pending_sell_order(order['id'])
                    executed.append(order)
                    stats = self.db.get_dca_stats(symbol)
                    avg_price = stats['avg_price'] if stats and stats['avg_price'] > 0 else rounded_price / (1 + order['profit_percent']/100)
                    await self._send_sell_order_notification(symbol, rounded_qty, rounded_price,
                                                             order['profit_percent'], avg_price, sell_result['order_id'])
                else:
                    self.db.update_pending_sell_retry(order['id'], sell_result.get('reason', 'Неизвестная причина'))
            else:
                self.db.update_pending_sell_retry(order['id'], f'Цена {format_price(current_price, 4)} < {format_price(order["target_price"], 4)}')
        return executed

    async def check_and_update_sell_orders(self, symbol: str):
        active_orders = self.db.get_active_sell_orders(symbol)
        open_orders = await self.bybit.get_open_orders(symbol)
        open_ids = {o['orderId'] for o in open_orders}
        for order in active_orders:
            if order['order_id'] not in open_ids:
                self.db.update_sell_order_status(order['order_id'], 'completed')

    async def check_completed_sells(self, symbol: str, force: bool = False) -> List[Dict]:
        check_date, reason = self.db.get_check_start_date(symbol)
        logger.info(f"Checking completed sells from {reason}: {check_date}")
        all_completed = await self.bybit.get_completed_sell_orders(symbol, from_date=check_date)
        if not all_completed:
            return []

        processed_sells = self.db.get_all_completed_sells(symbol)
        processed_ids = {s['order_id'] for s in processed_sells}
        our_completed = []
        first_order_date = self.db.get_first_order_date()
        stats = self.db.get_dca_stats(symbol)

        for sell in all_completed:
            if sell['order_id'] in processed_ids:
                continue
            conn = sqlite3.connect(self.db.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT 1 FROM sell_orders WHERE order_id = ?', (sell['order_id'],))
            is_our = cursor.fetchone() is not None
            conn.close()
            if not is_our and stats and stats['total_quantity'] > 0:
                if abs(sell['quantity'] - stats['total_quantity']) < 0.0001:
                    is_our = True
            if not is_our:
                continue

            if stats and stats['total_quantity'] > 0:
                avg_price = stats['avg_price']
                profit_percent = ((sell['sell_price'] - avg_price) / avg_price) * 100
                profit_usdt = (sell['sell_price'] - avg_price) * sell['quantity']
                total_invested = stats['total_usdt']
            else:
                profit_percent = 0
                profit_usdt = 0
                total_invested = 0

            days_invested = 1
            if first_order_date:
                days_invested = max(1, (get_moscow_time_naive() - first_order_date).days)
            apy = calculate_apy(profit_usdt, total_invested, days_invested) if total_invested > 0 else 0.0

            sell_id = self.db.add_completed_sell(
                symbol=symbol, order_id=sell['order_id'], quantity=sell['quantity'],
                sell_price=sell['sell_price'], profit_percent=profit_percent, profit_usdt=profit_usdt
            )
            now = get_moscow_time_naive()
            deadline = now + timedelta(hours=AUTO_CLEAR_DELAY_HOURS)
            self.db.set_clear_deadline(sell_id, deadline)
            if sell.get('executed_at'):
                self.db.set_last_sell_order_date(sell['executed_at'])

            sell_data = {
                'id': sell_id, 'order_id': sell['order_id'],
                'quantity': sell['quantity'], 'sell_price': sell['sell_price'],
                'amount_usdt': sell['amount_usdt'], 'executed_at': sell['executed_at'],
                'profit_percent': profit_percent, 'profit_usdt': profit_usdt,
                'total_invested': total_invested, 'apy': apy, 'days_invested': days_invested
            }
            our_completed.append(sell_data)

            if is_our:
                self.db.update_sell_order_status(sell['order_id'], 'completed')

            if not self.db.is_sell_notified(sell_id):
                await self._send_sell_completed_notification(sell_data, symbol)
                self.db.mark_completed_sell_notified(sell_id)

        return our_completed

    async def auto_clear_expired_stats(self, symbol: str):
        """Автоматическая очистка статистики по истечении дедлайна."""
        conn = sqlite3.connect(self.db.db_file, timeout=5)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        now = get_moscow_time_naive()

        cursor.execute('''SELECT id, symbol FROM completed_sells
            WHERE notified = 1 AND stats_cleared = 0 AND clear_deadline IS NOT NULL AND clear_deadline <= ?''',
            (now.isoformat(),))
        expired = cursor.fetchall()
        conn.close()

        user_id = self.db.get_authorized_user_id()
        for sell in expired:
            sell_id = sell['id']
            sym = sell['symbol']
            deleted = self.db.clear_all_purchases(sym)
            if deleted > 0:
                self.db.mark_completed_sell_stats_cleared(sell_id)
                if user_id and self.bot:
                    msg = (f"🔄 *Автоматическая очистка статистики*\n"
                           f"🪙 Токен: `{sym}`\n"
                           f"🗑 Удалено покупок: `{deleted}`\n"
                           f"⏰ Прошло более {AUTO_CLEAR_DELAY_HOURS} часов с момента продажи\n"
                           f"📊 Начинаем новый цикл накопления.")
                    await safe_send_message(self.bot, user_id, msg, parse_mode='Markdown')

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

    def calculate_target_info(self, stats: Dict, profit_percent: float) -> Optional[Dict]:
        if not stats or stats['total_quantity'] <= 0:
            return None
        total_qty = stats['total_quantity']
        avg_price = stats['avg_price']
        target_price = avg_price * (1 + profit_percent / 100)
        target_value = total_qty * target_price
        total_cost = stats['total_usdt']
        target_profit = target_value - total_cost
        return {
            'target_price': target_price, 'target_value': target_value,
            'target_profit': target_profit, 'total_qty': total_qty,
            'avg_price': avg_price, 'profit_percent': profit_percent
        }

    async def _process_new_orders(self, symbol: str, bot, orders: List[Dict]) -> List[Dict]:
        new_orders = []
        user_id = self.db.get_authorized_user_id()
        for order in orders:
            if self.db.is_order_already_added(order['order_id']):
                self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'],
                                           order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                self.db.mark_order_as_added(order['order_id'])
                continue
            purchases = self.db.get_purchases(symbol)
            added = set()
            for p in purchases:
                added.add(f"{round(p['price'], 4)}_{round(p['quantity'], 8)}")
            if f"{round(order['price'], 4)}_{round(order['quantity'], 8)}" in added:
                self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'],
                                           order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                self.db.mark_order_as_added(order['order_id'])
                continue
            self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'],
                                       order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
            new_orders.append(order)
        for order in new_orders:
            msg = (f"✅ *ОРДЕР ИСПОЛНЕН!*\n"
                   f"🪙 Токен: `{symbol}`\n"
                   f"💰 Цена: `{format_price(order['price'], 4)}` USDT\n"
                   f"📊 Количество: `{format_quantity(order['quantity'], 5)}`\n"
                   f"💵 Сумма: `{order['amount_usdt']:.2f}` USDT\n"
                   f"🕐 Время: `{order['executed_at'].strftime('%Y-%m-%d %H:%M:%S')}`\n"
                   f"❗ *Добавить в статистику покупок?*")
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Добавить", callback_data=f"add_order_{order['order_id']}"),
                InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_order_{order['order_id']}")
            ]])
            if user_id:
                await safe_send_message(bot, user_id, msg, parse_mode='Markdown', reply_markup=keyboard)
        return new_orders

    async def check_new_orders_incremental(self, symbol: str, bot) -> List[Dict]:
        if not self.db.is_dca_active():
            return []
        last_check = self.db.get_last_incremental_check_time()
        if last_check is not None:
            check_date = last_check
        else:
            check_date, _ = self.db.get_check_start_date(symbol)
        all_orders = await self.bybit.get_all_executed_orders(symbol, from_date=check_date)
        self.db.set_last_incremental_check_time(get_moscow_time_naive())
        conn = sqlite3.connect(self.db.db_file, timeout=5)
        cursor = conn.cursor()
        cursor.execute('SELECT order_id, added_to_stats, skipped FROM executed_orders WHERE symbol = ?', (symbol,))
        records = cursor.fetchall()
        conn.close()
        processed_ids = set()
        for rec in records:
            added = rec[1] if len(rec) > 1 else 0
            skipped = rec[2] if len(rec) > 2 else 0
            if added == 1 or skipped == 1:
                processed_ids.add(rec[0])
        new_orders = [o for o in all_orders if o['order_id'] not in processed_ids]
        return await self._process_new_orders(symbol, bot, new_orders)

    async def full_check_missing_orders(self, symbol: str, bot) -> List[Dict]:
        if not self.db.is_dca_active():
            return []
        check_date, _ = self.db.get_check_start_date(symbol)
        all_orders = await self.bybit.get_all_executed_orders(symbol, from_date=check_date)
        conn = sqlite3.connect(self.db.db_file, timeout=5)
        cursor = conn.cursor()
        cursor.execute('SELECT order_id, added_to_stats, skipped FROM executed_orders WHERE symbol = ?', (symbol,))
        records = cursor.fetchall()
        conn.close()
        processed_ids = set()
        for rec in records:
            added = rec[1] if len(rec) > 1 else 0
            skipped = rec[2] if len(rec) > 2 else 0
            if added == 1 or skipped == 1:
                processed_ids.add(rec[0])
        missing = [o for o in all_orders if o['order_id'] not in processed_ids]
        result = await self._process_new_orders(symbol, bot, missing)
        self.db.set_last_full_check_time(get_moscow_time_naive())
        return result

    async def auto_check_and_notify(self, symbol: str, bot) -> Dict:
        if not self.db.is_dca_active():
            return {'type': 'skipped', 'count': 0, 'orders': [], 'reason': 'dca_not_active'}
        last_full = self.db.get_last_full_check_time()
        now = get_moscow_time_naive()
        need_full = False
        if last_full is None:
            need_full = True
        else:
            if now.date() > last_full.date() and now.hour >= 19:
                need_full = True
            elif now.date() == last_full.date() and last_full.hour < 19 and now.hour >= 19:
                need_full = True
        if need_full:
            orders = await self.full_check_missing_orders(symbol, bot)
            return {'type': 'full', 'count': len(orders), 'orders': orders}
        else:
            orders = await self.check_new_orders_incremental(symbol, bot)
            return {'type': 'incremental', 'count': len(orders), 'orders': orders}

    async def find_and_show_orders_after_last_sell(self, symbol: str, bot) -> Dict:
        result = {
            'last_sell_order': None,
            'buy_orders_after_sell': [],
            'check_date': None,
            'check_reason': ''
        }
        user_id = self.db.get_authorized_user_id()
        last_sell = await self.bybit.get_last_completed_sell_from_api(symbol)
        if last_sell:
            result['last_sell_order'] = last_sell
            result['check_date'] = last_sell['executed_at']
            result['check_reason'] = f"последний ордер на продажу через API ({last_sell['executed_at'].strftime('%d.%m.%Y %H:%M')})"
            sell_msg = (f"🔍 *ПОСЛЕДНИЙ ОРДЕР НА ПРОДАЖУ (через API биржи):*\n"
                        f"🪙 Токен: `{symbol}`\n"
                        f"📊 Количество: `{format_quantity(last_sell['quantity'], 5)}`\n"
                        f"💰 Цена продажи: `{format_price(last_sell['sell_price'], 4)}` USDT\n"
                        f"💵 Сумма: `{last_sell['amount_usdt']:.2f}` USDT\n"
                        f"🕐 Время: `{last_sell['executed_at'].strftime('%d.%m.%Y %H:%M:%S')}`\n"
                        f"🆔 ID ордера: `{last_sell['order_id']}`\n\n"
                        f"📋 *Ищу ордера на покупку ПОСЛЕ этой даты...*")
            if user_id:
                await safe_send_message(bot, user_id, sell_msg, parse_mode='Markdown')
            check_date = last_sell['executed_at']
            all_orders = await self.bybit.get_all_executed_orders(symbol, from_date=check_date)
            conn = sqlite3.connect(self.db.db_file, timeout=5)
            cursor = conn.cursor()
            cursor.execute('SELECT order_id, added_to_stats, skipped FROM executed_orders WHERE symbol = ?', (symbol,))
            records = cursor.fetchall()
            conn.close()
            processed_ids = set()
            for rec in records:
                added = rec[1] if len(rec) > 1 else 0
                skipped = rec[2] if len(rec) > 2 else 0
                if added == 1 or skipped == 1:
                    processed_ids.add(rec[0])
            new_orders = [o for o in all_orders if o['order_id'] not in processed_ids]
            purchases = self.db.get_purchases(symbol)
            added_set = set()
            for p in purchases:
                added_set.add(f"{round(p['price'], 4)}_{round(p['quantity'], 8)}")
            for order in new_orders[:]:
                if f"{round(order['price'], 4)}_{round(order['quantity'], 8)}" in added_set:
                    self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'],
                                               order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                    self.db.mark_order_as_added(order['order_id'])
                    new_orders.remove(order)
                elif self.db.is_order_already_added(order['order_id']):
                    self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'],
                                               order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                    self.db.mark_order_as_added(order['order_id'])
                    new_orders.remove(order)
            result['buy_orders_after_sell'] = new_orders
            if new_orders:
                buy_msg = f"✅ *Найдено {len(new_orders)} ордеров на покупку ПОСЛЕ последнего ордера продажи:*\nДобавьте их вручную в статистику:"
                if user_id:
                    await safe_send_message(bot, user_id, buy_msg, parse_mode='Markdown')
                for order in new_orders:
                    msg = (f"✅ *ОРДЕР ИСПОЛНЕН!*\n"
                           f"🪙 Токен: `{symbol}`\n"
                           f"💰 Цена: `{format_price(order['price'], 4)}` USDT\n"
                           f"📊 Количество: `{format_quantity(order['quantity'], 5)}`\n"
                           f"💵 Сумма: `{order['amount_usdt']:.2f}` USDT\n"
                           f"🕐 Время: `{order['executed_at'].strftime('%Y-%m-%d %H:%M:%S')}`\n"
                           f"❗ *Добавить в статистику покупок?*")
                    keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Добавить", callback_data=f"add_order_{order['order_id']}"),
                        InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_order_{order['order_id']}")
                    ]])
                    if user_id:
                        await safe_send_message(bot, user_id, msg, parse_mode='Markdown', reply_markup=keyboard)
            else:
                no_orders_msg = f"✨ *Новых ордеров на покупку после последнего ордера продажи не найдено.*"
                if user_id:
                    await safe_send_message(bot, user_id, no_orders_msg, parse_mode='Markdown')
        else:
            result['check_reason'] = "ордеров на продажу не найдено через API"
            no_sell_msg = (f"ℹ️ *Ордеров на продажу не найдено через API биржи.*\n"
                           f"Использую fallback: поиск за последние 30 дней.")
            if user_id:
                await safe_send_message(bot, user_id, no_sell_msg, parse_mode='Markdown')
        return result

    async def force_check_executed_orders(self, symbol: str, bot) -> Dict:
        if not self.db.is_dca_active():
            return {'total_found': 0, 'already_added': 0, 'missing': [],
                    'check_date': None, 'check_reason': 'Авто DCA не запущен'}
        check_date, reason = self.db.get_check_start_date(symbol)
        all_orders = await self.bybit.get_all_executed_orders(symbol, from_date=check_date)
        conn = sqlite3.connect(self.db.db_file, timeout=5)
        cursor = conn.cursor()
        cursor.execute('SELECT order_id, added_to_stats, skipped FROM executed_orders WHERE symbol = ?', (symbol,))
        records = cursor.fetchall()
        conn.close()
        processed_ids = set()
        for rec in records:
            added = rec[1] if len(rec) > 1 else 0
            skipped = rec[2] if len(rec) > 2 else 0
            if added == 1 or skipped == 1:
                processed_ids.add(rec[0])
        missing = [o for o in all_orders if o['order_id'] not in processed_ids]
        purchases = self.db.get_purchases(symbol)
        added_set = set()
        for p in purchases:
            added_set.add(f"{round(p['price'], 4)}_{round(p['quantity'], 8)}")
        already = []
        for order in missing[:]:
            if f"{round(order['price'], 4)}_{round(order['quantity'], 8)}" in added_set:
                self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'],
                                           order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                self.db.mark_order_as_added(order['order_id'])
                already.append(order)
                missing.remove(order)
            elif self.db.is_order_already_added(order['order_id']):
                self.db.add_executed_order(order['order_id'], symbol, order['price'], order['quantity'],
                                           order['amount_usdt'], order['executed_at'].strftime("%Y-%m-%d %H:%M:%S"))
                self.db.mark_order_as_added(order['order_id'])
                already.append(order)
                missing.remove(order)
        return {
            'total_found': len(all_orders),
            'already_added': len(already),
            'missing': missing,
            'check_date': check_date,
            'check_reason': reason
        }

    async def force_check_completed_sells(self, symbol: str, bot) -> Dict:
        check_date, reason = self.db.get_check_start_date(symbol)
        logger.info(f"Force checking completed sells from {reason}: {check_date}")
        result = await self.check_completed_sells(symbol, force=True)
        return {
            'total_found': len(result) if result else 0,
            'already_processed': 0,
            'missing': result if result else [],
            'check_date': check_date,
            'check_reason': reason
        }

    async def place_full_sell_order(self, update, symbol: str, profit_percent: float,
                                     auto_cancel_old: bool = True) -> Dict:
        try:
            stats = self.db.get_dca_stats(symbol)
            if not stats or stats['total_quantity'] <= 0:
                return {'success': False, 'error': 'Нет купленных активов для продажи'}
            coin = symbol.replace('USDT', '')
            if auto_cancel_old:
                open_orders = await self.bybit.get_open_orders(symbol)
                old_sell = [o for o in open_orders if o.get('side') == 'Sell']
                if old_sell:
                    if update and hasattr(update, 'message'):
                        await update.message.reply_text(f"🔄 Обнаружено {len(old_sell)} старых ордеров на продажу. Отменяю их...")
                    cancelled_count, cancelled_ids = await self.bybit.cancel_all_sell_orders(symbol)
                    if cancelled_count > 0:
                        for order_id in cancelled_ids:
                            self.db.update_sell_order_status(order_id, 'cancelled')
                        if update and hasattr(update, 'message'):
                            await update.message.reply_text(f"✅ Отменено {cancelled_count} старых ордеров.")
                        await asyncio.sleep(2)
            balance_info = await self.bybit.get_balance(coin)
            if not balance_info or 'equity' not in balance_info:
                return {'success': False, 'error': 'Не удалось получить баланс монеты'}
            actual_balance = balance_info.get('equity', 0)
            if actual_balance <= 0:
                return {'success': False, 'error': f'Доступный баланс {coin} равен 0.'}
            avg_price = stats['avg_price']
            raw_target = avg_price * (1 + profit_percent / 100)
            instrument_info = await self.bybit.get_instrument_info(symbol)
            tick_size = instrument_info['tick_size']
            qty_decimals = instrument_info.get('qty_decimals', SELL_DECIMALS_FALLBACK)
            rounded_price = self.bybit._round_price_by_tick(raw_target, tick_size)
            min_qty = instrument_info['min_qty']
            min_amt = instrument_info['min_amt']
            sell_qty = self.bybit._round_quantity_for_sell(actual_balance, qty_decimals)
            if sell_qty < min_qty and actual_balance >= min_qty:
                for dec in range(qty_decimals, 0, -1):
                    factor = 10 ** dec
                    test = math.floor(actual_balance * factor) / factor
                    if test >= min_qty:
                        sell_qty = test
                        break
            if sell_qty < min_qty and actual_balance >= min_qty * 0.99:
                sell_qty = min_qty
            if sell_qty <= 0 or sell_qty < min_qty:
                return {'success': False, 'error': f'Недостаточно средств. Доступно: {actual_balance:.8f} {coin}'}
            order_value = sell_qty * rounded_price
            if order_value < min_amt:
                pending_id = self.db.add_pending_sell_order(symbol, sell_qty, rounded_price,
                                                            profit_percent, f'Сумма {order_value:.2f} < {min_amt}')
                msg = (f"⏳ *ОРДЕР ОТЛОЖЕН*\n"
                       f"🪙 Токен: `{symbol}`\n"
                       f"📊 Количество: `{format_quantity(sell_qty, 5)}` {coin}\n"
                       f"💰 Целевая цена: `{format_price(rounded_price, 4)}` USDT\n"
                       f"📈 Целевая прибыль: `{profit_percent}%`\n"
                       f"⚠️ *Сумма ордера ({order_value:.2f} USDT) меньше минимальной ({min_amt} USDT)*\n"
                       f"🔄 Ордер будет автоматически выставлен при достижении целевой цены.\n"
                       f"🔄 Повторная попытка через 5 минут.")
                if update and hasattr(update, 'message'):
                    await update.message.reply_text(msg, parse_mode='Markdown')
                return {'success': False, 'pending': True, 'pending_id': pending_id, 'error': 'min_amount_error', 'message': msg}
            if update and hasattr(update, 'message'):
                await update.message.reply_text(f"📤 Выставляю ордер на продажу {format_quantity(sell_qty, 5)} {coin} по {format_price(rounded_price, 4)} USDT...")
            result = await self.bybit.place_limit_sell(symbol, sell_qty, rounded_price)
            if result['success']:
                self.db.add_sell_order(symbol, result['order_id'], sell_qty, rounded_price, profit_percent)
                warning = ""
                if sell_qty < stats['total_quantity']:
                    warning = f"\n⚠️ Продано только {format_quantity(sell_qty, 5)} из {format_quantity(stats['total_quantity'], 5)} {coin}."
                return {
                    'success': True, 'order_id': result['order_id'], 'quantity': sell_qty,
                    'price': rounded_price, 'raw_price': raw_target,
                    'profit_percent': profit_percent, 'warning': warning
                }
            else:
                return {'success': False, 'error': result.get('error', 'Ошибка создания ордера')}
        except Exception as e:
            logger.error(f"Error placing full sell order: {e}")
            return {'success': False, 'error': str(e)}

# ============================= ОСНОВНОЙ БОТ ================================
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
        self._sell_monitor_task = None
        self._api_was_working = False
        self._api_error_count = 0
        self._is_running = False
        self._websocket_task = None
        request_kwargs = {'connect_timeout': 60.0, 'read_timeout': 60.0,
                          'write_timeout': 60.0, 'pool_timeout': 60.0}
        self.application = Application.builder().token(TELEGRAM_TOKEN).request(HTTPXRequest(**request_kwargs)).build()
        self.authorized_user_id = self.db.get_authorized_user_id()
        self.setup_handlers()

    def _init_bybit(self, force_reload: bool = False):
        if self.bybit and self.bybit_initialized and not force_reload:
            return
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
            self.strategy.set_bot(self.application.bot)
            self.bybit_initialized = True
        except Exception as e:
            logger.error(f"Bybit init error: {e}")
            self.bybit_initialized = False

    def refresh_api_session(self):
        if not self.bybit:
            self._init_bybit()
            return self.bybit_initialized
        self.bybit._refresh_session()
        self.bybit_initialized = self.bybit._is_api_available()
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
                    msg = (f"✅ *API Bybit восстановлен!*\n"
                           f"🔑 Ключи работают корректно.\n"
                           f"🕐 Время проверки: `{get_moscow_time().strftime('%H:%M:%S')}`")
                    await safe_send_message(self.application.bot, user_id, msg, parse_mode='Markdown')
            return True
        else:
            self._api_was_working = False
            self._api_error_count += 1
            self.db.set_api_status('error')
            self.db.set_api_error_message(health.get('user_message', 'Неизвестная ошибка'))
            if user_id and (is_startup or self._api_error_count % 3 == 0):
                error_code = health.get('error_code', 'N/A')
                user_msg = health.get('user_message', 'Неизвестная ошибка')
                msg = (f"🚨 *ОШИБКА API BYBIT!*\n"
                       f"❌ Статус: НЕ РАБОТАЕТ\n"
                       f"📝 Ошибка: {user_msg}\n"
                       f"🔢 Код: {error_code}\n"
                       f"⚠️ *Что делать:*\n"
                       f"1️⃣ Проверьте API ключ в файле `.env`\n"
                       f"2️⃣ Убедитесь, что ключ активен\n"
                       f"3️⃣ Проверьте права доступа\n"
                       f"4️⃣ Проверьте IP в белом списке Bybit\n"
                       f"🔄 Бот будет проверять доступ каждые 6 часов.")
                await safe_send_message(self.application.bot, user_id, msg, parse_mode='Markdown')
            return False

    def authorized_only(func):
        async def wrapper(self, update, context, *args, **kwargs):
            if not await self._check_user_fast(update):
                return
            return await func(self, update, context, *args, **kwargs)
        return wrapper

    async def _check_user_fast(self, update: Update) -> bool:
        user = update.effective_user
        username = f"@{user.username}" if user.username else f"ID:{user.id}"
        if self.authorized_user_id is None:
            if username == AUTHORIZED_USER:
                self.authorized_user_id = user.id
                self.db.set_authorized_user_id(user.id)
                return True
            elif user.id == self.authorized_user_id:
                return True
            await update.message.reply_text("⛔ Доступ запрещен")
            return False
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
            [KeyboardButton("➕ Добавить покупку в Статистику DCA"), KeyboardButton("✏️ Редактировать покупки")],
            [KeyboardButton("⚙️ Настройки"), KeyboardButton("📝 Управление ордерами")],
            [KeyboardButton("📋 Статус бота")],
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    def get_order_management_keyboard(self):
        return ReplyKeyboardMarkup([
            [KeyboardButton("📋 Список открытых ордеров"), KeyboardButton("❌ Удалить ордер")],
            [KeyboardButton("🔙 Назад в меню")]
        ], resize_keyboard=True)

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

    def get_order_side_keyboard(self):
        return ReplyKeyboardMarkup([
            [KeyboardButton("🟢 Купить"), KeyboardButton("🔴 Продать")],
            [KeyboardButton("❌ Отмена")]
        ], resize_keyboard=True)

    def get_full_amount_keyboard(self, full_amount: float):
        return ReplyKeyboardMarkup([
            [KeyboardButton(f"✅ Выставить всё ({format_quantity(full_amount, 5)})")],
            [KeyboardButton("❌ Отмена")]
        ], resize_keyboard=True)

    def get_settings_keyboard(self):
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        profit_percent = self.db.get_setting('profit_percent', str(PROFIT_PERCENT))
        keyboard = [
            [KeyboardButton("🪙 Выбор токена"), KeyboardButton("🚀 Настройки Авто DCA")],
            [KeyboardButton("📊 Процент прибыли"), KeyboardButton("🪜 Лестница Мартингейла")],
            [KeyboardButton("⚙️ Настройки отслеживания"), KeyboardButton("🔔 Уведомления о покупке")],
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

    @authorized_only
    async def cmd_start_fast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        next_purchase_str = self.db.get_setting('next_dca_purchase_time', '')
        if next_purchase_str:
            try:
                next_time = datetime.fromisoformat(next_purchase_str)
                if get_moscow_time() >= next_time:
                    self.db.set_setting('next_dca_purchase_time', '')
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
            f"👋 Привет, {update.effective_user.first_name}!\n"
            f"🤖 DCA Bybit Bot (Мартингейл лесенкой)\n"
            f"📌 Версия: {BOT_VERSION}\n"
            f"🌐 Режим: {mode_text}\n"
            f"🕐 Московское время: {current_time.strftime('%H:%M')}\n"
            f"🔑 *Статус API Bybit:* {api_status_text}\n"
            f"✅ Бот запущен и готов к работе!\n"
            f"🌐 Доступ к бирже Bybit по API ключу {status_emoji}\n"
            f"📋 Уведомления об исполненных ордерах будут приходить сюда.\n"
            f"🔄 WebSocket для мгновенных уведомлений о продажах активен (с fallback на polling).\n"
            f"🔄 Проверка API выполняется каждые 6 часов."
        )
        await safe_send_message(self.application.bot, update.effective_user.id, start_message,
                                parse_mode='Markdown', reply_markup=self.get_main_keyboard())
        if self.bybit_initialized and health and not health['success']:
            await self.check_api_and_notify(is_startup=True)
        if self.authorized_user_id:
            try:
                await self.application.bot.send_message(
                    chat_id=self.authorized_user_id,
                    text="✅ Бот запущен и готов к работе!\nУведомления об исполненных ордерах будут приходить сюда.",
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Failed to send test notification: {e}")

    @authorized_only
    async def cmd_check_api(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            msg = (f"✅ *API Bybit работает корректно!*\n"
                   f"🔑 Ключ активен и имеет необходимые права.\n"
                   f"🕐 Время проверки: `{get_moscow_time().strftime('%H:%M:%S')}`")
            await safe_send_message(self.application.bot, update.effective_user.id, msg, parse_mode='Markdown')
        else:
            error_code = health.get('error_code', 'N/A')
            user_msg = health.get('user_message', 'Неизвестная ошибка')
            msg = (f"🚨 *КРИТИЧЕСКАЯ ОШИБКА API BYBIT!*\n"
                   f"❌ Статус: `НЕ РАБОТАЕТ`\n"
                   f"📝 Ошибка: `{user_msg}`\n"
                   f"🔢 Код: `{error_code}`\n"
                   f"⚠️ *Что делать:*\n"
                   f"1️⃣ Проверьте API ключ в файле `.env`\n"
                   f"2️⃣ Убедитесь, что ключ активен\n"
                   f"3️⃣ Проверьте права доступа\n"
                   f"4️⃣ Проверьте IP в белом списке Bybit")
            await safe_send_message(self.application.bot, update.effective_user.id, msg, parse_mode='Markdown')

    @authorized_only
    async def cmd_refresh_api(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                "✅ *API Bybit работает корректно!*\n"
                "🔑 Ключи актуальны и имеют необходимые права.",
                parse_mode='Markdown'
            )
        else:
            error_code = health.get('error_code', 'N/A')
            user_msg = health.get('user_message', 'Неизвестная ошибка')
            msg = (f"❌ *Ошибка API*\n"
                   f"📝 {user_msg}\n"
                   f"🔢 Код: {error_code}\n"
                   f"Проверьте ключи в .env файле.")
            await safe_send_message(self.application.bot, update.effective_user.id, msg, parse_mode='Markdown')

    @authorized_only
    async def cmd_check_sells(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🔍 *Запускаю проверку продаж...*", parse_mode='Markdown')
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        try:
            check_date, reason = self.db.get_check_start_date(symbol)
            await update.message.reply_text(
                f"📅 Поиск от: *{reason}*\n🕐 Дата: `{check_date.strftime('%d.%m.%Y %H:%M')}`",
                parse_mode='Markdown'
            )
            result = await self.strategy.force_check_completed_sells(symbol, self.application.bot)
            if result['missing']:
                await update.message.reply_text(f"✅ *Найдено {len(result['missing'])} новых продаж!*\nУведомления отправлены.", parse_mode='Markdown')
            else:
                await update.message.reply_text("✅ *Новых продаж не найдено.*", parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error checking sells: {e}")
            await update.message.reply_text(f"❌ Ошибка при проверке продаж: {str(e)}")

    @authorized_only
    async def purchase_notify_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        enabled = self.db.get_purchase_notify_enabled()
        notify_time = self.db.get_purchase_notify_time()
        status_text = "🔔 Включены" if enabled else "🔕 Выключены"
        await update.message.reply_text(
            f"🔔 *Уведомления о покупке*\n"
            f"📋 Статус: {status_text}\n"
            f"⏰ Время уведомления: `{notify_time}` (МСК)\n"
            f"🕐 Текущее московское время: `{get_moscow_time().strftime('%H:%M')}`\n"
            f"Выберите действие:",
            reply_markup=self.get_purchase_notify_settings_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_PURCHASE_NOTIFY_TIME

    @authorized_only
    async def toggle_purchase_notify(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current = self.db.get_purchase_notify_enabled()
        new = not current
        self.db.set_purchase_notify_enabled(new)
        status_text = "🔔 Включены" if new else "🔕 Выключены"
        await update.message.reply_text(f"🔔 Уведомления о покупке: {status_text}",
                                        reply_markup=self.get_purchase_notify_settings_keyboard())
        return WAITING_PURCHASE_NOTIFY_TIME

    @authorized_only
    async def set_purchase_notify_time_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current = self.db.get_purchase_notify_time()
        await update.message.reply_text(
            f"⏰ Введите время уведомления (формат ЧЧ:ММ):\n"
            f"*Текущее время:* `{current}` (МСК)\n"
            f"*Текущее московское время:* `{get_moscow_time().strftime('%H:%M')}`\n"
            f"Пример: 06:00 или 18:30",
            reply_markup=self.get_cancel_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_PURCHASE_NOTIFY_TIME

    @authorized_only
    async def set_purchase_notify_time_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_purchase_notify_settings_keyboard())
            return WAITING_PURCHASE_NOTIFY_TIME
        try:
            datetime.strptime(text, "%H:%M")
            self.db.set_purchase_notify_time(text)
            await update.message.reply_text(f"✅ Время уведомления установлено: {text} (МСК)",
                                            reply_markup=self.get_purchase_notify_settings_keyboard())
            return WAITING_PURCHASE_NOTIFY_TIME
        except ValueError:
            await update.message.reply_text("❌ Некорректный формат. Используйте ЧЧ:ММ (например: 06:00)",
                                            reply_markup=self.get_cancel_keyboard())
            return WAITING_PURCHASE_NOTIFY_TIME

    @authorized_only
    async def back_to_settings_from_purchase(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⚙️ *Настройки*", reply_markup=self.get_settings_keyboard(), parse_mode='Markdown')
        return ConversationHandler.END

    @authorized_only
    async def auto_dca_settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        schedule_time = self.db.get_setting('schedule_time', SCHEDULE_TIME)
        frequency_hours = self.db.get_setting('frequency_hours', str(FREQUENCY_HOURS))
        invest_amount = self.db.get_setting('invest_amount', str(INVEST_AMOUNT))
        await update.message.reply_text(
            f"🚀 *Настройки Авто DCA*\n"
            f"💵 Сумма покупки авто: `{invest_amount}` USDT\n"
            f"⏰ Время покупки: `{schedule_time}` (МСК)\n"
            f"🔄 Частота покупки: `{frequency_hours}` часов\n"
            f"Выберите параметр:",
            reply_markup=self.get_auto_dca_keyboard(),
            parse_mode='Markdown'
        )
        return AUTO_DCA_SETTINGS

    @authorized_only
    async def set_amount_start_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"💵 Введите сумму для Авто DCA (текущая: {self.db.get_setting('invest_amount', str(INVEST_AMOUNT))}):\n*Минимальная сумма: 5 USDT*",
            reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown'
        )
        return SET_AMOUNT

    @authorized_only
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

    @authorized_only
    async def set_time_start_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"⏰ Введите время (текущее: {self.db.get_setting('schedule_time', SCHEDULE_TIME)}, формат ЧЧ:ММ):",
            reply_markup=self.get_cancel_keyboard()
        )
        return SET_SCHEDULE_TIME

    @authorized_only
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

    @authorized_only
    async def set_frequency_start_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"🔄 Введите частоту в часах (текущая: {self.db.get_setting('frequency_hours', str(FREQUENCY_HOURS))}):",
            reply_markup=self.get_cancel_keyboard()
        )
        return SET_FREQUENCY_HOURS

    @authorized_only
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

    @authorized_only
    async def handle_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⏳ Экспортирую базу данных...")
        success, count, file_path = self.db.export_database()
        if success:
            await update.message.reply_text(f"✅ Экспортировано! Записей: {count}")
            try:
                with open(file_path, 'rb') as f:
                    await update.message.reply_document(
                        document=InputFile(f, filename=DB_EXPORT_FILE),
                        caption=f"💾 Файл базы данных от {get_moscow_time_naive().strftime('%d.%m.%Y %H:%M')}"
                    )
            except Exception as e:
                await update.message.reply_text(f"❌ Ошибка отправки файла: {e}")
        else:
            await update.message.reply_text(f"❌ Ошибка экспорта: {file_path}")

    @authorized_only
    async def handle_import_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.import_waiting = True
        await update.message.reply_text(
            "📥 *ИМПОРТ БАЗЫ ДАННЫХ*\n"
            "Отправьте файл .json\n"
            "⚠️ *ВНИМАНИЕ! Все текущие данные будут заменены!*\n"
            "Или нажмите ❌ Отмена для отмены",
            reply_markup=self.get_cancel_keyboard(),
            parse_mode='Markdown'
        )

    @authorized_only
    async def handle_import_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    @authorized_only
    async def handle_import_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if self.import_waiting:
            self.import_waiting = False
            await update.message.reply_text("❌ Импорт отменен", reply_markup=self.get_main_keyboard())
        else:
            await self._reset_bot_state(context)
            await update.message.reply_text("Главное меню:", reply_markup=self.get_main_keyboard())

    @authorized_only
    async def handle_sell_confirmation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                actual_qty = self.bybit._round_quantity_for_sell(balance_info['equity'], qty_decimals)
                if actual_qty < min_qty and balance_info['equity'] >= min_qty:
                    for dec in range(qty_decimals, 0, -1):
                        factor = 10 ** dec
                        test = math.floor(balance_info['equity'] * factor) / factor
                        if test >= min_qty:
                            actual_qty = test
                            break
                if actual_qty > 0:
                    sell_data['total_quantity'] = actual_qty
                    sell_data['display_quantity'] = actual_qty
                await update.message.reply_text("⏳ Выставляю ордер на продажу...")
                self._init_bybit()
                if not self.bybit_initialized:
                    await update.message.reply_text("❌ Bybit API не инициализирован.", reply_markup=self.get_main_keyboard())
                    return
                result = await self.strategy.place_full_sell_order(update, sell_data['symbol'],
                                                                    sell_data['profit_percent'], auto_cancel_old=True)
                if result['success']:
                    msg = (f"✅ *Ордер на продажу успешно создан!*\n"
                           f"🪙 Токен: `{sell_data['symbol']}`\n"
                           f"📊 Количество: `{format_quantity(result['quantity'], 5)}`\n"
                           f"💰 Цена: `{format_price(result['price'], 4)}` USDT\n"
                           f"📈 Целевая прибыль: `{result['profit_percent']}%`\n"
                           f"🆔 ID ордера: `{result['order_id']}`\n"
                           f"{result.get('warning', '')}\n"
                           f"✅ Ордер успешно выставлен!")
                    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=self.get_main_keyboard())
                else:
                    await update.message.reply_text(f"❌ *Ошибка при создании ордера*\n{result['error']}",
                                                    parse_mode='Markdown', reply_markup=self.get_main_keyboard())
            context.user_data.pop('pending_sell_data', None)
            await self._reset_bot_state(context)

    @authorized_only
    async def toggle_order_execution(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current = self.db.get_order_execution_notify()
        new = not current
        self.db.set_order_execution_notify(new)
        status_text = "✅ Включено" if new else "⏹ Выключено"
        interval = self.db.get_order_check_interval()
        await update.message.reply_text(
            f"📋 *Отслеживание исполненных ордеров*: {status_text}\n"
            f"🕐 Интервал проверки: {interval} минут\n"
            f"При включенной настройке бот каждые {interval} минут проверяет новые исполненные ордера",
            parse_mode='Markdown',
            reply_markup=self.get_tracking_settings_keyboard()
        )

    @authorized_only
    async def toggle_sell_tracking(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current = self.db.get_sell_tracking_enabled()
        new = not current
        self.db.set_sell_tracking_enabled(new)
        status_text = "✅ Включено" if new else "⏹ Выключено"
        await update.message.reply_text(
            f"💰 *Отслеживание выполненных продаж*: {status_text}",
            parse_mode='Markdown',
            reply_markup=self.get_tracking_settings_keyboard()
        )

    @authorized_only
    async def tracking_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        current_status = self.db.get_order_execution_notify()
        sell_tracking = self.db.get_sell_tracking_enabled()
        current_interval = self.db.get_order_check_interval()
        status_text = "✅ Включено" if current_status else "⏹ Выключено"
        sell_tracking_text = "💰 Включено" if sell_tracking else "⏹ Выключено"
        await update.message.reply_text(
            f"⚙️ *Настройки отслеживания*\n"
            f"📋 Отслеживание ордеров: {status_text}\n"
            f"💰 Отслеживание продаж: {sell_tracking_text}\n"
            f"🕐 Интервал проверки: `{current_interval}` минут\n"
            f"Выберите действие:",
            reply_markup=self.get_tracking_settings_keyboard(),
            parse_mode='Markdown'
        )
        return NOTIFICATION_SETTINGS_MENU

    @authorized_only
    async def toggle_tracking(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current = self.db.get_order_execution_notify()
        new = not current
        self.db.set_order_execution_notify(new)
        status_text = "✅ Включено" if new else "⏹ Выключено"
        await update.message.reply_text(f"📋 Отслеживание ордеров: {status_text}",
                                        reply_markup=self.get_tracking_settings_keyboard())
        return NOTIFICATION_SETTINGS_MENU

    @authorized_only
    async def toggle_sell_tracking_in_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current = self.db.get_sell_tracking_enabled()
        new = not current
        self.db.set_sell_tracking_enabled(new)
        status_text = "💰 Включено" if new else "⏹ Выключено"
        await update.message.reply_text(f"💰 Отслеживание продаж: {status_text}",
                                        reply_markup=self.get_tracking_settings_keyboard())
        return NOTIFICATION_SETTINGS_MENU

    @authorized_only
    async def set_tracking_interval_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"⏱ Введите интервал проверки в минутах (от 5 до 1440):\n"
            f"*Текущий интервал: {self.db.get_order_check_interval()} минут*",
            reply_markup=self.get_cancel_keyboard(),
            parse_mode='Markdown'
        )
        return WAITING_ORDER_CHECK_INTERVAL

    @authorized_only
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
            await update.message.reply_text(f"✅ Интервал проверки изменен на {minutes} минут",
                                            reply_markup=self.get_tracking_settings_keyboard())
            return NOTIFICATION_SETTINGS_MENU
        except ValueError:
            await update.message.reply_text("❌ Некорректное значение. Введите число от 5 до 1440.",
                                            reply_markup=self.get_cancel_keyboard())
            return WAITING_ORDER_CHECK_INTERVAL

    @authorized_only
    async def test_tracking(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = await update.message.reply_text("🔍 *Запускаю полную проверку...*", parse_mode='Markdown')
        self._init_bybit()
        if not self.bybit_initialized:
            await msg.edit_text("❌ Bybit API не инициализирован.")
            return NOTIFICATION_SETTINGS_MENU

        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)

        purchases = self.db.get_purchases(symbol)
        last_purchase_info = ""
        if purchases:
            try:
                last = max(purchases, key=lambda x: x['date'])
                last_date = datetime.strptime(last['date'], "%Y-%m-%d %H:%M:%S")
                last_purchase_info = (f"📋 *ПОСЛЕДНИЙ ОРДЕР В СТАТИСТИКЕ:*\n"
                                      f"📅 Дата: `{last_date.strftime('%d.%m.%Y %H:%M')}`\n"
                                      f"🆔 ID: `{last['id']}`\n"
                                      f"📊 Количество: `{format_quantity(last['quantity'], 5)}`\n"
                                      f"💰 Цена: `{format_price(last['price'], 4)}` USDT\n")
            except:
                last_purchase_info = "📋 *ПОСЛЕДНИЙ ОРДЕР В СТАТИСТИКЕ:* Не удалось получить информацию\n"
        else:
            last_purchase_info = "📋 *ПОСЛЕДНИЙ ОРДЕР В СТАТИСТИКЕ:* В статистике нет ордеров\n"

        buy_result = await self.strategy.force_check_executed_orders(symbol, self.application.bot)
        sell_result = await self.strategy.force_check_completed_sells(symbol, self.application.bot)

        check_reason = buy_result.get('check_reason', 'N/A')

        summary = (f"📊 *РЕЗУЛЬТАТ ПРОВЕРКИ*\n"
                   f"🪙 `{symbol}`\n"
                   f"🔎 *Основание поиска:* {check_reason}\n"
                   f"{last_purchase_info}\n"
                   f"🟢 *Покупки:* найдено `{buy_result['total_found']}`, новых `{len(buy_result['missing'])}`\n"
                   f"🔴 *Продажи:* найдено `{sell_result['total_found']}`, новых `{len(sell_result['missing'])}`")

        if buy_result['missing'] or sell_result['missing']:
            summary += f"\n⚠️ *Найдены новые ордера!* Сейчас пришлю уведомления..."
            await msg.edit_text(summary, parse_mode='Markdown')

            notified = 0
            user_id = self.authorized_user_id
            for order in buy_result['missing']:
                if notified >= 10:
                    break
                msg_text = (f"✅ *НОВЫЙ ОРДЕР НА ПОКУПКУ!*\n"
                            f"🪙 Токен: `{symbol}`\n"
                            f"💰 Цена: `{format_price(order['price'], 4)}` USDT\n"
                            f"📊 Количество: `{format_quantity(order['quantity'], 5)}`\n"
                            f"💵 Сумма: `{order['amount_usdt']:.2f}` USDT\n"
                            f"🕐 Время: `{order['executed_at'].strftime('%Y-%m-%d %H:%M:%S')}`\n"
                            f"❗ *Добавить в статистику покупок?*")
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Добавить", callback_data=f"add_order_{order['order_id']}"),
                    InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_order_{order['order_id']}")
                ]])
                if user_id:
                    await safe_send_message(self.application.bot, user_id, msg_text,
                                            parse_mode='Markdown', reply_markup=keyboard)
                notified += 1
                await asyncio.sleep(0.3)

            for sell in sell_result['missing']:
                profit_emoji = "🟢" if sell['profit_usdt'] >= 0 else "🔴"
                profit_color = "+" if sell['profit_usdt'] >= 0 else ""
                msg_text = (f"💰 *НОВАЯ СДЕЛКА ПРОДАНА!*\n"
                            f"🪙 Токен: `{symbol}`\n"
                            f"📊 Количество: `{format_quantity(sell['quantity'], 5)}`\n"
                            f"💰 Цена продажи: `{format_price(sell['sell_price'], 4)}` USDT\n"
                            f"💵 Сумма: `{sell['amount_usdt']:.2f}` USDT\n"
                            f"{profit_emoji} Прибыль: `{profit_color}{sell['profit_usdt']:.2f}` USDT\n"
                            f"📈 Процент: `{profit_color}{sell['profit_percent']:.2f}%`\n"
                            f"📅 Период: `{sell['days_invested']}` дн.\n"
                            f"📈 APY: `{profit_color}{sell['apy']:.2f}%`\n"
                            f"🕐 Время: `{sell['executed_at'].strftime('%Y-%m-%d %H:%M:%S')}`\n"
                            f"❗ *Очистить статистику DCA по этому токену?*")
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Да, очистить", callback_data=f"confirm_clear_stats_{symbol}_{sell['id']}"),
                    InlineKeyboardButton("❌ Нет, оставить", callback_data=f"skip_clear_stats_{symbol}_{sell['id']}")
                ]])
                if user_id:
                    await safe_send_message(self.application.bot, user_id, msg_text,
                                            parse_mode='Markdown', reply_markup=keyboard)
                await asyncio.sleep(0.3)

            if notified == 0 and not sell_result['missing']:
                if user_id:
                    await self.application.bot.send_message(chat_id=user_id,
                                                            text="✨ *Отлично!* Все ордера синхронизированы.",
                                                            parse_mode='Markdown')
        else:
            summary += f"\n✨ *Отлично!* Новых ордеров не найдено."
            await msg.edit_text(summary, parse_mode='Markdown')

        return NOTIFICATION_SETTINGS_MENU

    @authorized_only
    async def back_to_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⚙️ *Настройки*", reply_markup=self.get_settings_keyboard(), parse_mode='Markdown')
        return ConversationHandler.END

    @authorized_only
    async def orders_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                f"📝 *Управление ордерами*\n"
                f"🪙 Токен: `{symbol}`\n"
                f"🔴 Ордера на продажу: `{sell_count}`\n"
                f"🟢 Ордера на покупку: `{buy_count}`\n"
                f"Выберите действие:",
                reply_markup=self.get_order_management_keyboard(),
                parse_mode='Markdown'
            )
            return MANAGE_ORDERS
        except Exception as e:
            logger.error(f"Error in orders_menu: {e}")
            await update.message.reply_text(f"❌ Ошибка: {e}")
            return ConversationHandler.END

    @authorized_only
    async def show_open_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.", reply_markup=self.get_order_management_keyboard())
            return
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        coin = symbol.replace('USDT', '')
        try:
            orders_by_side = await self.bybit.get_open_orders_by_side(symbol)
            message = f"📋 *ОТКРЫТЫЕ ОРДЕРА*\n🪙 {symbol}\n"
            sell_orders = orders_by_side.get('sell', [])
            if sell_orders:
                message += f"🔴 *ОРДЕРА НА ПРОДАЖУ ({len(sell_orders)})*\n"
                for i, order in enumerate(sell_orders[:20], 1):
                    price = float(order.get('price', 0))
                    qty = float(order.get('qty', 0))
                    message += f"{i}. {format_quantity(qty, 5)} {coin} @ {format_price(price, 4)} USDT\n"
                if len(sell_orders) > 20:
                    message += f"_...и еще {len(sell_orders) - 20}_\n"
                message += "\n"
            else:
                message += f"🔴 *Нет ордеров на продажу*\n"
            buy_orders = orders_by_side.get('buy', [])
            if buy_orders:
                message += f"🟢 *ОРДЕРА НА ПОКУПКУ ({len(buy_orders)})*\n"
                for i, order in enumerate(buy_orders[:20], 1):
                    price = float(order.get('price', 0))
                    qty = float(order.get('qty', 0))
                    message += f"{i}. {format_quantity(qty, 5)} {coin} @ {format_price(price, 4)} USDT\n"
                if len(buy_orders) > 20:
                    message += f"_...и еще {len(buy_orders) - 20}_\n"
            else:
                message += f"🟢 *Нет ордеров на покупку*"
            await update.message.reply_text(message, parse_mode='Markdown', reply_markup=self.get_order_management_keyboard())
        except Exception as e:
            logger.error(f"Error showing open orders: {e}")
            await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=self.get_order_management_keyboard())

    @authorized_only
    async def cancel_order_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.", reply_markup=self.get_order_management_keyboard())
            return ConversationHandler.END
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        try:
            orders_by_side = await self.bybit.get_open_orders_by_side(symbol)
            all_orders = orders_by_side.get('sell', []) + orders_by_side.get('buy', [])
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
            message = f"🗑 *УДАЛЕНИЕ ОРДЕРА*\n🪙 Токен: `{symbol}`\nВыберите ордер для удаления (введите номер):"
            await update.message.reply_text(message, parse_mode='Markdown', reply_markup=cancel_keyboard)
            return WAITING_ORDER_ID_TO_CANCEL
        except Exception as e:
            logger.error(f"Error in cancel_order_start: {e}")
            await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=self.get_order_management_keyboard())
            return ConversationHandler.END

    @authorized_only
    async def cancel_order_execute(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                side = order_to_cancel.get('side', 'Unknown')
                price = float(order_to_cancel.get('price', 0))
                qty = float(order_to_cancel.get('qty', 0))
                await update.message.reply_text(
                    f"✅ *Ордер успешно удален!*\n"
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
                    f"❌ *Ошибка при удалении ордера*\n"
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

    @authorized_only
    async def manual_order_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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

        context.user_data['manual_order_symbol'] = symbol
        context.user_data['manual_order_price'] = current_price

        msg = (f"💰 Текущая цена {symbol}: `{format_price(current_price, 4)}` USDT\n\n"
               f"Выберите сторону ордера:")

        await update.message.reply_text(msg, reply_markup=self.get_order_side_keyboard(), parse_mode='Markdown')
        return MANUAL_ORDER_SIDE

    @authorized_only
    async def manual_order_side(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()

        if text == "❌ Отмена":
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Операция отменена", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END

        if text == "🟢 Купить":
            context.user_data['manual_order_side'] = 'buy'
            symbol = context.user_data.get('manual_order_symbol', DEFAULT_SYMBOL)
            current_price = context.user_data.get('manual_order_price', 0)
            manual_amount = self.db.get_manual_amount()

            msg = (f"🟢 *ПОКУПКА*\n"
                   f"🪙 Токен: `{symbol}`\n"
                   f"💰 Текущая цена: `{format_price(current_price, 4)}` USDT\n"
                   f"💵 *Сумма для ручного ордера:* `{manual_amount:.2f}` USDT\n\n"
                   f"Введите цену покупки (USDT):")

            await update.message.reply_text(msg, reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
            return MANUAL_BUY_PRICE

        elif text == "🔴 Продать":
            context.user_data['manual_order_side'] = 'sell'
            symbol = context.user_data.get('manual_order_symbol', DEFAULT_SYMBOL)
            current_price = context.user_data.get('manual_order_price', 0)
            coin = symbol.replace('USDT', '')

            balance_info = await self.bybit.get_balance(coin)
            if not balance_info or 'equity' not in balance_info:
                await update.message.reply_text("❌ Не удалось получить баланс монеты", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END

            total_balance = balance_info.get('equity', 0)
            instrument_info = await self.bybit.get_instrument_info(symbol)
            qty_decimals = instrument_info.get('qty_decimals', SELL_DECIMALS_FALLBACK)
            min_qty = instrument_info['min_qty']

            context.user_data['manual_order_balance'] = total_balance
            context.user_data['manual_order_min_qty'] = min_qty

            if total_balance <= 0:
                await update.message.reply_text(f"❌ Нет монет {coin} на балансе для продажи", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END

            rounded_balance = self.bybit._round_quantity_for_sell(total_balance, qty_decimals)
            if rounded_balance < min_qty and total_balance >= min_qty:
                for dec in range(qty_decimals, 0, -1):
                    factor = 10 ** dec
                    test = math.floor(total_balance * factor) / factor
                    if test >= min_qty:
                        rounded_balance = test
                        break

            msg = (f"🔴 *ПРОДАЖА*\n"
                   f"🪙 Токен: `{symbol}`\n"
                   f"💰 Текущая цена: `{format_price(current_price, 4)}` USDT\n"
                   f"📊 Доступно: `{format_quantity(total_balance, 5)}` {coin}\n"
                   f"✅ Можно выставить от `{format_quantity(min_qty, 5)}` до `{format_quantity(rounded_balance, 5)}` {coin}\n\n"
                   f"Введите цену продажи (USDT):")

            await update.message.reply_text(msg, reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
            return MANUAL_SELL_PRICE

        else:
            await update.message.reply_text("❌ Пожалуйста, выберите 'Купить' или 'Продать'", reply_markup=self.get_order_side_keyboard())
            return MANUAL_ORDER_SIDE

    @authorized_only
    async def manual_sell_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text in MAIN_MENU_BUTTONS:
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Действие отменено.", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        if text == "❌ Отмена":
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END

        try:
            price = float(text.replace(',', '.'))
            if price <= 0:
                raise ValueError("Цена должна быть положительной")

            context.user_data['manual_sell_price'] = price
            symbol = context.user_data.get('manual_order_symbol', DEFAULT_SYMBOL)
            coin = symbol.replace('USDT', '')
            total_balance = context.user_data.get('manual_order_balance', 0)
            min_qty = context.user_data.get('manual_order_min_qty', 0.01)

            instrument_info = await self.bybit.get_instrument_info(symbol)
            qty_decimals = instrument_info.get('qty_decimals', SELL_DECIMALS_FALLBACK)
            rounded_balance = self.bybit._round_quantity_for_sell(total_balance, qty_decimals)

            if rounded_balance < min_qty and total_balance >= min_qty:
                for dec in range(qty_decimals, 0, -1):
                    factor = 10 ** dec
                    test = math.floor(total_balance * factor) / factor
                    if test >= min_qty:
                        rounded_balance = test
                        break

            msg = (f"✅ Цена продажи: `{format_price(price, 4)}` USDT\n"
                   f"📊 Доступно: `{format_quantity(total_balance, 5)}` {coin}\n"
                   f"✅ Можно выставить от `{format_quantity(min_qty, 5)}` до `{format_quantity(rounded_balance, 5)}` {coin}\n\n"
                   f"Введите количество монет для продажи:")

            if total_balance > 0:
                keyboard = self.get_full_amount_keyboard(rounded_balance)
            else:
                keyboard = self.get_cancel_keyboard()

            await update.message.reply_text(msg, reply_markup=keyboard, parse_mode='Markdown')
            return MANUAL_SELL_AMOUNT

        except ValueError as e:
            await update.message.reply_text(f"❌ Ошибка! Введите корректную цену.\nПример: 2.35\nОшибка: {str(e)}", reply_markup=self.get_cancel_keyboard())
            return MANUAL_SELL_PRICE

    @authorized_only
    async def manual_sell_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text in MAIN_MENU_BUTTONS:
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Действие отменено.", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        if text == "❌ Отмена":
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END

        symbol = context.user_data.get('manual_order_symbol', DEFAULT_SYMBOL)
        price = context.user_data.get('manual_sell_price', 0)
        coin = symbol.replace('USDT', '')
        total_balance = context.user_data.get('manual_order_balance', 0)
        min_qty = context.user_data.get('manual_order_min_qty', 0.01)

        instrument_info = await self.bybit.get_instrument_info(symbol)
        qty_decimals = instrument_info.get('qty_decimals', SELL_DECIMALS_FALLBACK)
        rounded_balance = self.bybit._round_quantity_for_sell(total_balance, qty_decimals)

        if rounded_balance < min_qty and total_balance >= min_qty:
            for dec in range(qty_decimals, 0, -1):
                factor = 10 ** dec
                test = math.floor(total_balance * factor) / factor
                if test >= min_qty:
                    rounded_balance = test
                    break

        if text.startswith("✅ Выставить всё"):
            quantity = rounded_balance
        else:
            try:
                quantity = float(text.replace(',', '.'))
                if quantity <= 0:
                    raise ValueError("Количество должно быть положительным")
                quantity = self.bybit._round_quantity_for_sell(quantity, qty_decimals)
            except ValueError as e:
                await update.message.reply_text(f"❌ Ошибка! Введите корректное количество.\nПример: 10.5\nОшибка: {str(e)}", reply_markup=self.get_cancel_keyboard())
                return MANUAL_SELL_AMOUNT

        if quantity < min_qty:
            await update.message.reply_text(f"❌ Минимальное количество для продажи: {format_quantity(min_qty, 5)}", reply_markup=self.get_cancel_keyboard())
            return MANUAL_SELL_AMOUNT

        if quantity > rounded_balance:
            await update.message.reply_text(
                f"❌ Недостаточно средств. Доступно: {format_quantity(rounded_balance, 5)} {coin}",
                reply_markup=self.get_full_amount_keyboard(rounded_balance)
            )
            return MANUAL_SELL_AMOUNT

        await update.message.reply_text(f"⏳ Выставляю ордер на продажу {format_quantity(quantity, 5)} {coin} по {format_price(price, 4)} USDT...")

        profit_percent = float(self.db.get_setting('profit_percent', str(PROFIT_PERCENT)))
        result = await self.strategy._place_sell_order(symbol, quantity, price, profit_percent)

        if result['success']:
            msg = (f"✅ *Ордер на продажу успешно создан!*\n"
                   f"🪙 Токен: `{symbol}`\n"
                   f"📊 Количество: `{format_quantity(result['quantity'], 5)}` {coin}\n"
                   f"💰 Цена: `{format_price(result['price'], 4)}` USDT\n"
                   f"📈 Целевая прибыль: `{profit_percent}%`\n"
                   f"🆔 ID ордера: `{result['order_id']}`")
            await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=self.get_main_keyboard())
        elif result.get('pending'):
            await update.message.reply_text(
                f"⚠️ *ОРДЕР НА ПРОДАЖУ ОТЛОЖЕН*\n"
                f"🪙 Токен: `{symbol}`\n"
                f"📊 Количество: `{format_quantity(quantity, 5)}` {coin}\n"
                f"💰 Цена: `{format_price(price, 4)}` USDT\n"
                f"🔄 Причина: {result.get('reason', 'Недостаточно средств')}",
                parse_mode='Markdown', reply_markup=self.get_main_keyboard()
            )
        else:
            await update.message.reply_text(
                f"❌ *Ошибка при создании ордера*\n{result.get('reason', 'Неизвестная ошибка')}",
                parse_mode='Markdown', reply_markup=self.get_main_keyboard()
            )

        await self._reset_bot_state(context)
        return ConversationHandler.END

    @authorized_only
    async def manual_add_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return ConversationHandler.END

        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        current_price = await self.bybit.get_symbol_price(symbol)
        stats = self.db.get_dca_stats(symbol)

        msg = f"➕ *Добавление покупки в статистику DCA*\n"
        msg += f"🪙 Токен: `{symbol}`\n"
        msg += f"💰 Текущая цена: `{format_price(current_price, 4)}` USDT\n"

        if stats and stats['avg_price'] > 0:
            current_drop = calculate_current_drop(current_price, stats['avg_price'])
            msg += f"📉 Средняя цена: `{format_price(stats['avg_price'], 4)}` USDT\n"
            msg += f"📉 Падение от средней цены: `{current_drop:.1f}%`\n"
        else:
            msg += f"🟢 *ПЕРВАЯ ПОКУПКА*\n"

        msg += f"\nВведите цену покупки (USDT):"

        await update.message.reply_text(msg, reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
        return MANUAL_ADD_PRICE

    @authorized_only
    async def manual_add_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text in MAIN_MENU_BUTTONS:
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Действие отменено.", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        if text == "❌ Отмена":
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END

        try:
            price = float(text.replace(',', '.'))
            if price <= 0:
                raise ValueError("Цена должна быть положительной")

            context.user_data['manual_price'] = price
            symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)

            msg = f"✅ Цена: `{format_price(price, 4)}` USDT\n"
            msg += f"💰 Введите количество монет (в {symbol.replace('USDT', '')}):"

            await update.message.reply_text(msg, reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
            return MANUAL_ADD_AMOUNT

        except ValueError as e:
            await update.message.reply_text(f"❌ Ошибка! Введите корректную цену.\nПример: 2.35 или 2,35\nОшибка: {str(e)}", reply_markup=self.get_cancel_keyboard())
            return MANUAL_ADD_PRICE

    @authorized_only
    async def manual_add_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text in MAIN_MENU_BUTTONS:
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Действие отменено.", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        if text == "❌ Отмена":
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END

        try:
            quantity = float(text.replace(',', '.'))
            if quantity <= 0:
                raise ValueError("Количество должно быть положительным")

            context.user_data['manual_quantity'] = quantity
            symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
            price = context.user_data.get('manual_price')

            if not price:
                await self._reset_bot_state(context)
                await update.message.reply_text("❌ Ошибка: цена не найдена.", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END

            current_date = get_moscow_time_naive().strftime("%d.%m.%Y")
            msg = (f"✅ *Добавление в статистику DCA*\n"
                   f"🪙 Токен: `{symbol}`\n"
                   f"📊 Количество: `{format_quantity(quantity, 5)}`\n"
                   f"✅ по Цене: `{format_price(price, 4)}` USDT\n"
                   f"📅 Введите дату покупки (формат `{current_date}` или `{get_moscow_time_naive().strftime('%d.%m.%y')}` или `{get_moscow_time_naive().strftime('%d.%m')}`):\n"
                   f"Текущая дата: `{current_date}`")

            await update.message.reply_text(msg, reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown')
            return MANUAL_ADD_DATE

        except ValueError as e:
            await update.message.reply_text(f"❌ Ошибка! Введите корректное количество.\nПример: 10.5 или 10,5\nОшибка: {str(e)}", reply_markup=self.get_cancel_keyboard())
            return MANUAL_ADD_AMOUNT

    @authorized_only
    async def manual_add_date(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text in MAIN_MENU_BUTTONS:
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Действие отменено.", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        if text == "❌ Отмена":
            await self._reset_bot_state(context)
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END

        try:
            date_patterns = [
                (r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', lambda m: (int(m.group(1)), int(m.group(2)), int(m.group(3)))),
                (r'^(\d{1,2})\.(\d{1,2})\.(\d{2})$', lambda m: (int(m.group(1)), int(m.group(2)), 2000 + int(m.group(3)))),
                (r'^(\d{1,2})\.(\d{1,2})$', lambda m: (int(m.group(1)), int(m.group(2)), get_moscow_time_naive().year)),
            ]

            date_obj = None
            for pattern, extractor in date_patterns:
                match = re.match(pattern, text)
                if match:
                    day, month, year = extractor(match)
                    try:
                        date_obj = datetime(year, month, day)
                        break
                    except ValueError:
                        raise ValueError("Некорректная дата")

            if not date_obj:
                raise ValueError("Неподдерживаемый формат даты")

            symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
            price = context.user_data.get('manual_price')
            quantity = context.user_data.get('manual_quantity')

            if not price or not quantity:
                await self._reset_bot_state(context)
                await update.message.reply_text("❌ Ошибка: данные не найдены.", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END

            amount_usdt = price * quantity
            date_str = date_obj.strftime("%Y-%m-%d %H:%M:%S")

            stats = self.db.get_dca_stats(symbol)
            drop_percent = 0
            step_level = 0
            if stats and stats['avg_price'] > 0:
                drop_percent = calculate_current_drop(price, stats['avg_price'])
                step_level = int(drop_percent)

            purchase_id = self.db.add_purchase(
                symbol=symbol,
                amount_usdt=amount_usdt,
                price=price,
                quantity=quantity,
                multiplier=1.0,
                drop_percent=drop_percent,
                step_level=step_level,
                date=date_str
            )

            if purchase_id:
                msg = (f"✅ *Покупка добавлена!*\n"
                       f"🆔 ID: `{purchase_id}`\n"
                       f"🪙 Токен: `{symbol}`\n"
                       f"💰 Цена: `{format_price(price, 4)}` USDT\n"
                       f"📊 Количество: `{format_quantity(quantity, 5)}`\n"
                       f"💵 Сумма: `{amount_usdt:.2f}` USDT\n"
                       f"📅 Дата: `{date_obj.strftime('%d.%m.%Y %H:%M:%S')}`")
                if drop_percent > 0:
                    msg += f"\n📉 Падение от средней цены: `{drop_percent:.1f}%`"
                await update.message.reply_text(msg, reply_markup=self.get_main_keyboard(), parse_mode='Markdown')
            else:
                await update.message.reply_text("❌ Ошибка сохранения в базу данных", reply_markup=self.get_main_keyboard())

            await self._reset_bot_state(context)
            return ConversationHandler.END

        except ValueError as e:
            await update.message.reply_text(f"❌ Ошибка! {str(e)}\nИспользуйте формат: ДД.ММ.ГГГГ или ДД.ММ.ГГ (например: 15.08.2026)", reply_markup=self.get_cancel_keyboard())
            return MANUAL_ADD_DATE
        except Exception as e:
            logger.error(f"Error in manual_add_date: {e}")
            await update.message.reply_text(f"❌ Ошибка: {str(e)}", reply_markup=self.get_cancel_keyboard())
            return MANUAL_ADD_DATE

    @authorized_only
    async def show_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        coin = symbol.replace('USDT', '')
        coin_balance = await self.bybit.get_balance(coin)
        usdt_balance = await self.bybit.get_balance('USDT')
        current_price = await self.bybit.get_symbol_price(symbol)
        message = f"📊 *Мой Портфель*\n"
        if usdt_balance and 'equity' in usdt_balance:
            available_usdt = usdt_balance.get('available', usdt_balance.get('equity', 0))
            message += f"💵 USDT доступно: `{available_usdt:.2f}`\n"
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
                message += f"{emoji} PnL: `{pnl_percent:+.2f}%` ({pnl_usd:+.2f} USDT)\n"
        await update.message.reply_text(message, parse_mode='Markdown')

    @authorized_only
    async def show_dca_stats_detailed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        coin = symbol.replace('USDT', '')
        stats = self.db.get_dca_stats(symbol)
        current_price = await self.bybit.get_symbol_price(symbol)
        profit_percent = float(self.db.get_setting('profit_percent', str(PROFIT_PERCENT)))

        if not stats:
            await update.message.reply_text("📈 *Статистика DCA*\nПокупок пока нет.", parse_mode='Markdown')
            return

        total_amount = stats['total_quantity']
        total_cost = stats['total_usdt']
        avg_price = stats['avg_price']
        total_purchases = stats['total_purchases']
        current_value = total_amount * current_price if current_price else 0
        pnl = current_value - total_cost
        pnl_percent = (pnl / total_cost * 100) if total_cost > 0 else 0
        target_info = self.strategy.calculate_target_info(stats, profit_percent)

        open_orders = await self.bybit.get_open_orders(symbol)
        sell_orders = [o for o in open_orders if o.get('side') == 'Sell']
        buy_orders = [o for o in open_orders if o.get('side') == 'Buy']

        next_purchase_str = self.db.get_setting('next_dca_purchase_time', '')
        next_purchase_text = "Не запланирована"
        if next_purchase_str:
            try:
                next_time = datetime.fromisoformat(next_purchase_str)
                next_purchase_text = next_time.strftime('%d.%m.%Y %H:%M')
            except:
                pass

        text = f"📊 *ДЕТАЛЬНАЯ СТАТИСТИКА DCA*\n"
        text += f"🪙 Токен: `{symbol}`\n"
        text += f"📊 Всего покупок: `{total_purchases}`\n"
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

        if sell_orders:
            text += f"\n🔴 *ОРДЕРА НА ПРОДАЖУ ({len(sell_orders)})*\n"
            for i, order in enumerate(sell_orders[:5], 1):
                price = float(order.get('price', 0))
                qty = float(order.get('qty', 0))
                text += f"{i}. {format_quantity(qty, 5)} {coin} @ {format_price(price, 4)} USDT\n"
            if len(sell_orders) > 5:
                text += f"_...и еще {len(sell_orders) - 5}_\n"
        else:
            text += f"\n🔴 *Нет ордеров на продажу*\n"

        if buy_orders:
            text += f"\n🟢 *ОРДЕРА НА ПОКУПКУ ({len(buy_orders)})*\n"
            for i, order in enumerate(buy_orders[:5], 1):
                price = float(order.get('price', 0))
                qty = float(order.get('qty', 0))
                text += f"{i}. {format_quantity(qty, 5)} {coin} @ {format_price(price, 4)} USDT\n"
            if len(buy_orders) > 5:
                text += f"_...и еще {len(buy_orders) - 5}_\n"
        else:
            text += f"🟢 *Нет ордеров на покупку*\n"

        text += f"\n⏰ *Следующая покупка:* `{next_purchase_text}` (МСК)\n"

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

        await update.message.reply_text(text, parse_mode='Markdown')

    @authorized_only
    async def show_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        is_active = self.db.is_dca_active()
        invest_amount = float(self.db.get_setting('invest_amount', str(INVEST_AMOUNT)))
        ladder_settings = self.db.get_ladder_settings(symbol)
        order_execution = self.db.get_order_execution_notify()
        sell_tracking = self.db.get_sell_tracking_enabled()
        purchase_notify = self.db.get_purchase_notify_enabled()
        purchase_notify_time = self.db.get_purchase_notify_time()
        order_interval = self.db.get_order_check_interval()
        last_full_check = self.db.get_last_full_check_time()
        first_order_date = self.db.get_first_order_date()
        last_sell_date = self.db.get_last_sell_order_date()
        current_time = get_moscow_time()
        next_purchase_str = self.db.get_setting('next_dca_purchase_time', '')
        mode = self.db.get_trading_mode()
        mode_text = "Демо-режим" if mode == 'demo' else "Обычный режим"
        api_status = self.db.get_api_status()
        api_error = self.db.get_api_error_message()
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
        message = f"📋 *Статус бота*\n"
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
        message += f"📈 Цель: `{self.db.get_setting('profit_percent', str(PROFIT_PERCENT))}%`\n"
        message += f"📋 Отслеживание ордеров: {'✅ Вкл' if order_execution else '⏹ Выкл'}\n"
        message += f"💰 Отслеживание продаж: {'✅ Вкл' if sell_tracking else '⏹ Выкл'}\n"
        message += f"🔔 Уведомления о покупке: {'✅ Вкл' if purchase_notify else '⏹ Выкл'} ({purchase_notify_time} МСК)\n"
        message += f"🕐 Текущее время (МСК): `{current_time.strftime('%H:%M')}`\n"
        message += f"🕐 Интервал проверки: `{order_interval}` мин\n"
        if first_order_date:
            message += f"📅 Первый ордер: `{first_order_date.strftime('%d.%m.%Y %H:%M')}`\n"
        if last_sell_date:
            message += f"📅 Последняя продажа: `{last_sell_date.strftime('%d.%m.%Y %H:%M')}`\n"
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

    @authorized_only
    async def toggle_dca(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self._init_bybit()
        if not self.bybit_initialized:
            await update.message.reply_text("❌ Bybit API не инициализирован.")
            return
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        if is_active:
            self.db.set_setting('dca_active', 'false')
            if self.strategy:
                self.strategy.stop_sell_check_loop()
                self.strategy._sell_monitor_active = False
            if self._sell_check_task and not self._sell_check_task.done():
                self._sell_check_task.cancel()
            if self._sell_monitor_task and not self._sell_monitor_task.done():
                self._sell_monitor_task.cancel()
            if self._websocket_task and not self._websocket_task.done():
                self._websocket_task.cancel()
                await self.bybit.stop_websocket()
            await update.message.reply_text(
                "⏹ *DCA ОСТАНОВЛЕН*\n"
                "Бот больше не будет проверять ордера на продажу.\n"
                "Текущие ордера останутся активными на бирже.",
                parse_mode='Markdown',
                reply_markup=self.get_main_keyboard()
            )
        else:
            current_price = await self.bybit.get_symbol_price(symbol)
            if not current_price:
                await update.message.reply_text("❌ Не удалось получить цену")
                return
            self.db.set_setting('dca_active', 'true')
            await update.message.reply_text(
                f"🔍 *ПРОВЕРКА СТАТУСА*\n"
                f"🪙 Токен: `{symbol}`\n"
                f"💰 Текущая цена: `{format_price(current_price, 4)}` USDT\n"
                f"⏳ Проверяю наличие покупок в статистике...",
                parse_mode='Markdown'
            )
            stats = self.db.get_dca_stats(symbol)
            if not stats or stats['total_quantity'] <= 0:
                await update.message.reply_text(
                    f"📊 *В статистике нет ордеров!*\n"
                    f"🔍 Ищу последний ордер на продажу через API биржи...",
                    parse_mode='Markdown'
                )
                result = await self.strategy.find_and_show_orders_after_last_sell(
                    symbol, self.application.bot
                )
                if result['last_sell_order']:
                    await update.message.reply_text(
                        f"✅ *DCA ЗАПУЩЕН!*\n"
                        f"🪙 Токен: `{symbol}`\n"
                        f"📊 Покупок в статистике: `0`\n"
                        f"🔍 Найден последний ордер на продажу через API\n"
                        f"📋 Найдено ордеров на покупку после продажи: `{len(result['buy_orders_after_sell'])}`\n"
                        f"📈 Целевая прибыль: `{self.db.get_setting('profit_percent', str(PROFIT_PERCENT))}%`\n"
                        f"💡 Добавьте ордера на покупку вручную через кнопки выше.",
                        parse_mode='Markdown',
                        reply_markup=self.get_main_keyboard()
                    )
                else:
                    await update.message.reply_text(
                        f"✅ *DCA ЗАПУЩЕН!*\n"
                        f"🪙 Токен: `{symbol}`\n"
                        f"📊 Покупок в статистике: `0`\n"
                        f"🔍 Ордеров на продажу не найдено через API\n"
                        f"📈 Целевая прибыль: `{self.db.get_setting('profit_percent', str(PROFIT_PERCENT))}%`\n"
                        f"⏰ Следующая покупка будет выполнена по расписанию.\n"
                        f"💡 Как только появится первая покупка, бот автоматически создаст ордер на продажу.",
                        parse_mode='Markdown',
                        reply_markup=self.get_main_keyboard()
                    )
            else:
                await update.message.reply_text(
                    f"🔍 *ОБНАРУЖЕНЫ ПОКУПКИ!*\n"
                    f"📊 Всего покупок: `{stats['total_purchases']}`\n"
                    f"💰 Вложено: `{stats['total_usdt']:.2f}` USDT\n"
                    f"📈 Средняя цена: `{format_price(stats['avg_price'], 4)}` USDT\n"
                    f"⏳ Создаю ордер на продажу...",
                    parse_mode='Markdown'
                )
                result = await self.strategy.check_and_create_sell_order(symbol, silent=False)
                if result.get('success'):
                    if result.get('message'):
                        await update.message.reply_text(
                            f"✅ *DCA ЗАПУЩЕН!*\n"
                            f"🪙 Токен: `{symbol}`\n"
                            f"📊 {result['message']}\n"
                            f"📈 Целевая прибыль: `{self.db.get_setting('profit_percent', str(PROFIT_PERCENT))}%`\n"
                            f"🔄 Активный мониторинг ордера запущен (интервал {SELL_MONITOR_INTERVAL} сек).",
                            parse_mode='Markdown',
                            reply_markup=self.get_main_keyboard()
                        )
                    else:
                        await update.message.reply_text(
                            f"✅ *DCA ЗАПУЩЕН!*\n"
                            f"🪙 Токен: `{symbol}`\n"
                            f"💰 Создан ордер на продажу!\n"
                            f"📊 Количество: `{format_quantity(result['quantity'], 5)}`\n"
                            f"💰 Цена: `{format_price(result['price'], 4)}` USDT\n"
                            f"📈 Прибыль: `{result['profit_percent']}%`\n"
                            f"🔄 Активный мониторинг ордера запущен (интервал {SELL_MONITOR_INTERVAL} сек).",
                            parse_mode='Markdown',
                            reply_markup=self.get_main_keyboard()
                        )
                else:
                    await update.message.reply_text(
                        f"⚠️ *DCA ЗАПУЩЕН, НО ОРДЕР НЕ СОЗДАН*\n"
                        f"🪙 Токен: `{symbol}`\n"
                        f"❗ Причина: {result.get('error', 'Неизвестная ошибка')}\n"
                        f"🔄 Бот будет проверять статус каждые {SELL_MONITOR_INTERVAL} сек.",
                        parse_mode='Markdown',
                        reply_markup=self.get_main_keyboard()
                    )

            if self._sell_monitor_task is None or self._sell_monitor_task.done():
                self._sell_monitor_task = asyncio.create_task(
                    self.strategy.sell_order_monitor_loop(symbol, self.authorized_user_id, self.application.bot)
                )

            if self._websocket_task is None or self._websocket_task.done():
                self._websocket_task = asyncio.create_task(
                    self.bybit.start_websocket(
                        self.strategy.handle_order_update,
                        symbol
                    )
                )

            if self._sell_check_task is None or self._sell_check_task.done():
                self._sell_check_task = asyncio.create_task(
                    self.strategy.sell_order_check_loop(symbol)
                )

    @authorized_only
    async def settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        profit_percent = self.db.get_setting('profit_percent', str(PROFIT_PERCENT))
        await update.message.reply_text(
            f"⚙️ *Настройки*\n"
            f"🪙 Токен: `{symbol}`\n"
            f"📈 Цель: `{profit_percent}%`\n"
            f"Выберите раздел:",
            reply_markup=self.get_settings_keyboard(),
            parse_mode='Markdown'
        )
        return SELECTING_ACTION

    @authorized_only
    async def set_profit_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"📊 Введите процент прибыли (текущий: {self.db.get_setting('profit_percent', str(PROFIT_PERCENT))}%):",
            reply_markup=self.get_cancel_keyboard()
        )
        return SET_PROFIT_PERCENT

    @authorized_only
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

    @authorized_only
    async def set_symbol_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"🪙 Выберите токен или введите свой\nТекущий: {self.db.get_setting('symbol', DEFAULT_SYMBOL)}",
            reply_markup=self.get_symbol_selection_keyboard()
        )
        return SELECTING_SYMBOL

    @authorized_only
    async def process_symbol_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_settings_keyboard())
            return SELECTING_ACTION
        if text == "✏️ Ввести свой токен":
            await update.message.reply_text("✏️ Введите символ токена (например: TONUSDT):", reply_markup=self.get_cancel_keyboard())
            return SET_SYMBOL_MANUAL
        return await self._validate_and_set_symbol(update, text)

    @authorized_only
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
            await update.message.reply_text(
                f"❌ Символ {symbol} не найден на Bybit.\nПроверьте правильность написания.",
                reply_markup=self.get_symbol_selection_keyboard()
            )
            return SELECTING_SYMBOL
        instrument_info = await self.bybit.get_instrument_info(symbol)
        min_amt = instrument_info['min_amt']
        qty_decimals = instrument_info['qty_decimals']
        self.db.set_setting('symbol', symbol)
        self.db.set_setting('initial_reference_price', str(price))
        await update.message.reply_text(
            f"✅ Символ изменен на {symbol}\n"
            f"💰 Текущая цена: {format_price(price, 4)} USDT\n"
            f"⚠️ Минимальная сумма для Авто DCA: {min_amt} USDT\n"
            f"📊 Точность количества для покупки: {qty_decimals} знаков\n"
            f"📊 Точность количества для продажи: динамическая",
            reply_markup=self.get_settings_keyboard()
        )
        return SELECTING_ACTION

    @authorized_only
    async def ladder_settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        await update.message.reply_text(
            "🪜 *ЛЕСТНИЦА МАРТИНГЕЙЛА*\n"
            "Стратегия: при каждом падении цены на 1% от средней цены\n"
            "происходит докупка с линейным ростом суммы.\n"
            "Параметры:\n"
            "• Глубина просадки: максимальный процент падения\n"
            "• Рост суммы: от базовой до максимальной\n"
            "• Базовая сумма: сумма первого ордера",
            reply_markup=self.get_ladder_settings_keyboard(),
            parse_mode='Markdown'
        )
        return LADDER_MENU

    @authorized_only
    async def show_ladder_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        ladder = self.db.get_ladder_settings(symbol)
        current_price = await self.bybit.get_symbol_price(symbol) if self.bybit_initialized else None
        summary = self.db.get_ladder_summary(symbol, current_price)
        text = f"🪜 *ТЕКУЩИЕ НАСТРОЙКИ*\n"
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

    @authorized_only
    async def set_ladder_max_depth_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📉 Введите глубину просадки в процентах (30-95%):\n*Рекомендуется 80%*",
            reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown'
        )
        return SET_LADDER_DEPTH

    @authorized_only
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

    @authorized_only
    async def set_ladder_base_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "💵 Введите базовую сумму (мин 5 USDT):\n*Сумма первого ордера*",
            reply_markup=self.get_cancel_keyboard(), parse_mode='Markdown'
        )
        return SET_LADDER_BASE_AMOUNT

    @authorized_only
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
            await update.message.reply_text(
                f"✅ Базовая сумма: {base_amount} USDT\n💰 Максимальная сумма: {base_amount * 3} USDT",
                reply_markup=self.get_ladder_settings_keyboard()
            )
            return LADDER_MENU
        except ValueError:
            await update.message.reply_text("❌ Некорректная сумма (мин 5).", reply_markup=self.get_cancel_keyboard())
            return SET_LADDER_BASE_AMOUNT

    @authorized_only
    async def reset_ladder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        self.db.reset_ladder(symbol)
        await update.message.reply_text(
            "🔄 Статистика DCA очищена! Лестница сброшена.\n⚠️ ID покупок будут начинаться с 1 при следующем добавлении.",
            reply_markup=self.get_ladder_settings_keyboard()
        )
        return LADDER_MENU

    @authorized_only
    async def manual_buy_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        return await self.manual_order_start(update, context)

    @authorized_only
    async def manual_buy_price_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        return await self.manual_order_side(update, context)

    @authorized_only
    async def manual_buy_amount_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        return await self.manual_order_side(update, context)

    @authorized_only
    async def edit_purchases_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        context.user_data.pop('editing_purchase_id', None)
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        purchases = self.db.get_purchases(symbol)
        if not purchases:
            await update.message.reply_text("Нет покупок", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END
        await update.message.reply_text("✏️ Выберите покупку:", reply_markup=self.get_purchases_list_keyboard(purchases))
        return EDIT_PURCHASE_SELECT

    @authorized_only
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
            match = re.search(r'ID(\d+)', text)
            if not match:
                await update.message.reply_text("❌ Неверный формат.",
                                                reply_markup=self.get_purchases_list_keyboard(self.db.get_purchases(self.db.get_setting('symbol', DEFAULT_SYMBOL))))
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
            await update.message.reply_text(
                f"✏️ *РЕДАКТИРОВАНИЕ ID: {purchase_id}*\n📅 Дата: `{date_display}`\n💰 Цена: `{format_price(purchase['price'], 4)}` USDT\n📊 Количество: `{format_quantity(purchase['quantity'], 5)}`",
                reply_markup=self.get_edit_purchases_keyboard(), parse_mode='Markdown'
            )
            return EDIT_PURCHASE_SELECT
        except Exception as e:
            await update.message.reply_text("❌ Ошибка выбора", reply_markup=self.get_main_keyboard())
            return ConversationHandler.END

    @authorized_only
    async def edit_price_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("💰 Введите новую цену:", reply_markup=self.get_cancel_keyboard())
        return EDIT_PRICE

    @authorized_only
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

    @authorized_only
    async def edit_amount_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("📊 Введите новое количество:", reply_markup=self.get_cancel_keyboard())
        return EDIT_AMOUNT

    @authorized_only
    async def edit_amount_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            await self.cancel_to_edit_menu(update, context)
            return EDIT_PURCHASE_SELECT
        try:
            new_qty = float(text.replace(',', '.'))
            purchase_id = context.user_data.get('editing_purchase_id')
            if not purchase_id:
                await update.message.reply_text("❌ Ошибка", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            purchase = self.db.get_purchase_by_id(purchase_id)
            if not purchase:
                await update.message.reply_text("❌ Покупка не найдена", reply_markup=self.get_main_keyboard())
                return ConversationHandler.END
            new_amount_usdt = purchase['price'] * new_qty
            if self.db.update_purchase(purchase_id, quantity=new_qty, amount_usdt=new_amount_usdt):
                await update.message.reply_text(f"✅ Количество обновлено: {format_quantity(new_qty, 5)}")
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

    @authorized_only
    async def edit_date_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        purchase_id = context.user_data.get('editing_purchase_id')
        purchase = self.db.get_purchase_by_id(purchase_id)
        try:
            current_date = datetime.strptime(purchase['date'], "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y")
        except:
            current_date = purchase['date'][:10] if purchase['date'] else "неизвестно"
        await update.message.reply_text(
            f"📅 Текущая дата: {current_date}\nВведите новую дату (ДД.ММ.ГГГГ):",
            reply_markup=self.get_cancel_keyboard()
        )
        return EDIT_DATE

    @authorized_only
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

    @authorized_only
    async def delete_purchase_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⚠️ *Удалить эту покупку?*", reply_markup=self.get_confirm_delete_keyboard(), parse_mode='Markdown')
        return DELETE_CONFIRM

    @authorized_only
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
        await update.message.reply_text(
            f"✏️ *РЕДАКТИРОВАНИЕ ID: {purchase_id}*\n📅 Дата: `{date_display}`\n💰 Цена: `{format_price(purchase['price'], 4)}` USDT\n📊 Количество: `{format_quantity(purchase['quantity'], 5)}`",
            reply_markup=self.get_edit_purchases_keyboard(), parse_mode='Markdown'
        )

    async def cancel_to_edit_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        purchase_id = context.user_data.get('editing_purchase_id')
        if purchase_id:
            await self.show_purchase_after_edit(update, context, purchase_id)
        else:
            await update.message.reply_text("❌ Отменено", reply_markup=self.get_main_keyboard())
            await self._reset_bot_state(context)

    @authorized_only
    async def back_to_main(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        await update.message.reply_text("Главное меню:", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END

    @authorized_only
    async def cancel_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        await update.message.reply_text("Действие отменено", reply_markup=self.get_main_keyboard())
        return ConversationHandler.END

    @authorized_only
    async def handle_unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_bot_state(context)
        text = update.message.text
        if text == "⚙️ Настройки":
            await self.settings_menu(update, context)
        elif text == "🚀 Настройки Авто DCA":
            await self.auto_dca_settings_menu(update, context)
        elif text in POPULAR_SYMBOLS:
            await self._validate_and_set_symbol(update, text)
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

                    logger.info(f"[SCHEDULER] Executing scheduled purchase for {symbol} at {now.strftime('%H:%M')}")
                    result = await self.strategy.execute_scheduled_purchase(symbol, profit_percent)

                    freq = int(self.db.get_setting('frequency_hours', str(FREQUENCY_HOURS)))
                    next_time_calc = next_time + timedelta(hours=freq)
                    while next_time_calc <= now:
                        next_time_calc += timedelta(hours=freq)
                    self.db.set_setting('next_dca_purchase_time', next_time_calc.isoformat())

                    if result.get('success'):
                        if self.authorized_user_id:
                            msg = (f"🪜 *АВТО DCA — ПОКУПКА*\n"
                                   f"🪙 Токен: `{symbol}`\n"
                                   f"💰 Сумма (запрошенная): `{result['amount_usdt']:.2f}` USDT\n"
                                   f"💰 Сумма (фактическая): `{result.get('actual_amount_usdt', result['amount_usdt']):.2f}` USDT\n"
                                   f"💵 Цена: `{format_price(result['price'], 4)}` USDT\n"
                                   f"📊 Количество (фактическое): `{format_quantity(result.get('actual_quantity', result['quantity']), 5)}`\n")
                            if result.get('drop_percent', 0) > 0:
                                msg += f"📉 Падение от средней: `{result['drop_percent']:.1f}%`\n"
                            if result.get('sell_quantity'):
                                msg += f"📊 Ордер на продажу: `{format_quantity(result['sell_quantity'], 5)}` {symbol.replace('USDT', '')}\n"
                            if result.get('sell_warning'):
                                msg += f"\n⚠️ {result['sell_warning']}"
                            await safe_send_message(self.application.bot, self.authorized_user_id, msg, parse_mode='Markdown')
                    elif result.get('error') != 'skip_price_above_avg':
                        if self.authorized_user_id:
                            await safe_send_message(
                                self.application.bot,
                                self.authorized_user_id,
                                f"❌ *Ошибка авто DCA*\n{result.get('error', 'Неизвестная ошибка')}",
                                parse_mode='Markdown'
                            )

                    current_symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
                    await self.strategy.check_and_update_sell_orders(current_symbol)
                    if self.authorized_user_id:
                        await self.strategy.auto_clear_expired_stats(current_symbol)

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
                if not self.db.get_order_execution_notify() or not self.db.is_dca_active():
                    await asyncio.sleep(interval_minutes * 60)
                    continue
                if not self.bybit_initialized:
                    self._init_bybit()
                if not self.bybit_initialized:
                    await asyncio.sleep(interval_minutes * 60)
                    continue
                symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
                if self.authorized_user_id:
                    result = await self.strategy.auto_check_and_notify(symbol, self.application.bot)
                    if result['count'] > 0:
                        logger.info(f"Auto check found {result['count']} orders ({result['type']})")
                await asyncio.sleep(interval_minutes * 60)
            except asyncio.CancelledError:
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
                    completed = await self.strategy.check_completed_sells(symbol)
                    if completed:
                        logger.info(f"Found {len(completed)} completed sell orders")
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
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
                    executed = await self.strategy.check_pending_sell_orders(symbol)
                    if executed:
                        logger.info(f"Executed {len(executed)} pending sell orders")
                await asyncio.sleep(1800)
            except asyncio.CancelledError:
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
                        msg = f"🔔 *ЕЖЕДНЕВНОЕ УВЕДОМЛЕНИЕ О ПОКУПКЕ*\n"
                        msg += f"💰 Текущая цена {symbol}: `{format_price(current_price, 4)}` USDT\n"
                        msg += f"🕐 Время (МСК): `{now.strftime('%H:%M')}`\n"
                        if stats and stats['avg_price'] > 0:
                            current_drop = calculate_current_drop(current_price, stats['avg_price'])
                            msg += f"📉 Средняя цена: `{format_price(stats['avg_price'], 4)}` USDT\n"
                            msg += f"📉 Падение от средней цены: `{current_drop:.1f}%`\n"
                        if recommendation['success']:
                            msg += f"🟢 *РЕКОМЕНДАЦИЯ ПО ПОКУПКЕ:*\n"
                            msg += f"📉 Уровень падения: `{recommendation['drop_percent']:.1f}%`\n"
                            msg += f"💰 Рекомендуемая сумма: `{recommendation['amount_usdt']:.2f}` USDT\n"
                            msg += f"📈 Рекомендуемая цена: `{format_price(current_price, 4)}` USDT\n"
                        else:
                            msg += f"🟢 *РЕКОМЕНДАЦИЯ:* Покупка не требуется\n"
                        msg += f"💡 *Сумма для ручного ордера:* `{manual_amount:.2f}` USDT"
                        await safe_send_message(self.application.bot, self.authorized_user_id, msg, parse_mode='Markdown')
                        self.db.set_last_purchase_notify_date(current_date_str)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Purchase notify loop error: {e}")
                await asyncio.sleep(60)
            await asyncio.sleep(60)

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
                break
            except Exception as e:
                logger.error(f"API check loop error: {e}")
                await asyncio.sleep(300)

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
        elif data.startswith("confirm_clear_stats_"):
            parts = data.replace("confirm_clear_stats_", "").rsplit("_", 1)
            if len(parts) == 2:
                symbol = parts[0]
                try:
                    sell_id = int(parts[1])
                    await self.execute_clear_stats(update, context, symbol, sell_id)
                except:
                    await query.edit_message_text("❌ Ошибка: неверный идентификатор продажи.")
            else:
                await query.edit_message_text("❌ Ошибка: неверный формат данных.")
        elif data.startswith("skip_clear_stats_"):
            parts = data.replace("skip_clear_stats_", "").rsplit("_", 1)
            if len(parts) == 2:
                symbol = parts[0]
                try:
                    sell_id = int(parts[1])
                    await self.skip_clear_stats(update, context, symbol, sell_id)
                except:
                    await query.edit_message_text("❌ Ошибка: неверный идентификатор продажи.")
            else:
                await query.edit_message_text("❌ Ошибка: неверный формат данных.")

    async def execute_clear_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str, sell_id: int):
        query = update.callback_query
        deleted = self.db.clear_all_purchases(symbol)
        if deleted > 0:
            self.db.mark_completed_sell_stats_cleared(sell_id)
            self.db.mark_completed_sell_notified(sell_id)
            ladder = self.db.get_ladder_settings(symbol)
            self.db.save_ladder_settings(ladder)
            await query.edit_message_text(
                f"✅ *Статистика DCA очищена!*\n🪙 Токен: `{symbol}`\n🗑 Удалено покупок: `{deleted}`\n📊 Начинаем новый цикл накопления.\n🪜 Расчет от новой средней цены.\n⚠️ ID покупок будут начинаться с 1 при следующем добавлении.",
                parse_mode='Markdown'
            )

            stats = self.db.get_dca_stats(symbol)
            if stats and stats['total_quantity'] > 0:
                await self.strategy.check_and_create_sell_order(symbol, silent=False)
        else:
            await query.edit_message_text(f"❌ Ошибка при очистке статистики для {symbol}", parse_mode='Markdown')

    async def skip_clear_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str, sell_id: int):
        query = update.callback_query
        self.db.mark_completed_sell_notified(sell_id)
        await query.edit_message_text(
            f"⏭ Очистка статистики для {symbol} отложена.\n"
            f"📊 Статистика DCA сохранена.\n"
            f"⏰ Статистика будет автоматически очищена через {AUTO_CLEAR_DELAY_HOURS} часа.\n"
            f"💡 Вы можете очистить её позже вручную через раздел '✏️ Редактировать покупки' или '🪜 Лестница Мартингейла' → 'Сбросить лестницу'.",
            parse_mode='Markdown'
        )

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
            await update.callback_query.edit_message_text("ℹ️ Этот ордер уже есть в статистике покупок.")
            self.db.mark_order_as_added(order_id)
            return
        executed_at = order_dict.get('executed_at')
        if executed_at:
            try:
                date_obj = datetime.strptime(executed_at, "%Y-%m-%d %H:%M:%S") if isinstance(executed_at, str) else executed_at
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
        purchase_id = self.db.add_purchase(symbol=symbol, amount_usdt=order_dict['amount_usdt'], price=price,
                                           quantity=order_dict['quantity'], multiplier=1.0,
                                           drop_percent=drop_percent, step_level=step_level,
                                           date=purchase_date, order_id=order_id)
        if purchase_id:
            self.db.mark_order_as_added(order_id)
            self.db.reset_incremental_check_time()
            msg = f"✅ *Покупка добавлена в статистику!*\n🪙 Токен: `{symbol}`\n💰 Цена: `{format_price(price, 4)}` USDT\n📊 Количество: `{format_quantity(order_dict['quantity'], 5)}`\n💵 Сумма: `{order_dict['amount_usdt']:.2f}` USDT\n📅 Дата: `{purchase_date}`\n"
            if drop_percent > 0:
                msg += f"📉 Падение от средней цены: `{drop_percent:.1f}%`\n"
            msg += f"🆔 ID покупки: `{purchase_id}`"
            await update.callback_query.edit_message_text(msg, parse_mode='Markdown')
        elif purchase_id is None:
            await update.callback_query.edit_message_text("ℹ️ Ордер уже был добавлен в статистику ранее.")
        else:
            await update.callback_query.edit_message_text("❌ Ошибка при добавлении покупки в статистику.")

    async def skip_executed_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str):
        self.db.mark_order_as_skipped(order_id)
        self.db.reset_incremental_check_time()
        await update.callback_query.edit_message_text("⏭ Пропущено. Ордер не будет добавлен в статистику.")

    async def post_init(self, application=None):
        if self._is_running:
            return
        self._is_running = True
        logger.info("Bot initialized, starting scheduler loops...")
        first_order_date = self.db.get_first_order_date()
        if first_order_date is None:
            purchases = self.db.get_purchases()
            if purchases:
                self.db.update_first_order_date()
        self.scheduler_running = True
        self._init_bybit()
        if self.bybit_initialized:
            await self.check_api_and_notify(is_startup=True)
        tasks = [
            asyncio.create_task(self.dca_scheduler_loop()),
            asyncio.create_task(self.order_checker_loop()),
            asyncio.create_task(self.sell_checker_loop()),
            asyncio.create_task(self.pending_sell_checker_loop()),
            asyncio.create_task(self.purchase_notify_loop()),
            asyncio.create_task(self.api_check_loop())
        ]
        self.background_tasks = tasks
        if self.db.get_setting('dca_active', 'false') == 'true':
            symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
            if self.bybit_initialized and self.authorized_user_id:
                stats = self.db.get_dca_stats(symbol)
                if stats and stats['total_quantity'] > 0:
                    await self.strategy.check_and_create_sell_order(symbol, silent=False)
                    self._sell_monitor_task = asyncio.create_task(
                        self.strategy.sell_order_monitor_loop(symbol, self.authorized_user_id, self.application.bot)
                    )
                    self._websocket_task = asyncio.create_task(
                        self.bybit.start_websocket(
                            self.strategy.handle_order_update,
                            symbol
                        )
                    )
                    self._sell_check_task = asyncio.create_task(
                        self.strategy.sell_order_check_loop(symbol)
                    )

    async def shutdown(self, application=None):
        logger.info("Shutting down bot...")
        self.scheduler_running = False
        self._is_running = False
        if self.strategy:
            self.strategy.stop_sell_check_loop()
            self.strategy._sell_monitor_active = False
        if self._sell_check_task and not self._sell_check_task.done():
            self._sell_check_task.cancel()
        if self._sell_monitor_task and not self._sell_monitor_task.done():
            self._sell_monitor_task.cancel()
        if self._websocket_task and not self._websocket_task.done():
            self._websocket_task.cancel()
            if self.bybit:
                await self.bybit.stop_websocket()
        if self.background_tasks:
            for task in self.background_tasks:
                if task and not task.done():
                    task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(*self.background_tasks, return_exceptions=True), timeout=5.0)
            except:
                pass
        logger.info("Bot shutdown complete")

    def setup_handlers(self):
        logger.info("Setting up handlers...")
        self.application.add_handler(CommandHandler("start", self.cmd_start_fast))
        self.application.add_handler(CommandHandler("check_api", self.cmd_check_api))
        self.application.add_handler(CommandHandler("refresh_api", self.cmd_refresh_api))
        self.application.add_handler(CommandHandler("check_sells", self.cmd_check_sells))
        self.application.add_handler(CallbackQueryHandler(
            self.handle_order_execution_callback,
            pattern='^(add_order_|skip_order_|confirm_clear_stats_|skip_clear_stats_)'
        ))

        manual_order_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(💰 Ручная покупка \(лимит\))$'), self.manual_order_start)],
            states={
                MANUAL_ORDER_SIDE: [
                    MessageHandler(filters.Regex('^(🟢 Купить|🔴 Продать|❌ Отмена)$'), self.manual_order_side),
                ],
                MANUAL_BUY_PRICE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_order_side),
                ],
                MANUAL_SELL_PRICE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_sell_price),
                ],
                MANUAL_SELL_AMOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_sell_amount),
                ],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="manual_order_conversation", persistent=False, conversation_timeout=CONVERSATION_TIMEOUT
        )
        self.application.add_handler(manual_order_conv)

        manual_add_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex('^(➕ Добавить покупку в Статистику DCA)$'), self.manual_add_start)],
            states={
                MANUAL_ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_add_price)],
                MANUAL_ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_add_amount)],
                MANUAL_ADD_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_add_date)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="manual_add_conversation", persistent=False, conversation_timeout=CONVERSATION_TIMEOUT
        )
        self.application.add_handler(manual_add_conv)

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
            ], EDIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_price_save)],
            EDIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_amount_save)],
            EDIT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_date_save)],
            DELETE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.delete_purchase_execute)]},
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
                    MessageHandler(filters.Regex('^(⚙️ Настройки отслеживания)$'), self.tracking_settings),
                    MessageHandler(filters.Regex('^(🔔 Уведомления о покупке)$'), self.purchase_notify_settings),
                    MessageHandler(filters.Regex('^(📤 Экспорт базы)$'), self.handle_export),
                    MessageHandler(filters.Regex('^(📥 Импорт базы)$'), self.handle_import_start),
                    MessageHandler(filters.Regex('^(🔙 Назад в меню)$'), self.back_to_main),
                ],
                SELECTING_SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.process_symbol_selection)],
                SET_SYMBOL_MANUAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_symbol_manual)],
                SET_PROFIT_PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_profit_done)],
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
            ], SET_LADDER_DEPTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_ladder_max_depth_save)],
            SET_LADDER_BASE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_ladder_base_amount_save)]},
            fallbacks=[CommandHandler("cancel", self.cancel_conversation)],
            name="ladder_conversation", persistent=False, conversation_timeout=CONVERSATION_TIMEOUT
        )
        self.application.add_handler(ladder_conv)

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
        print(f"{Fore.CYAN}🔌 WebSocket: ВКЛЮЧЕН (исправлен — через отдельный поток)")
        print(f"{Fore.CYAN}⏱ Интервал мониторинга ордера: {SELL_MONITOR_INTERVAL} сек")
        print(f"{Fore.CYAN}📊 Порог остатка монет для очистки: {BALANCE_CHECK_THRESHOLD}")
        print(f"{Fore.CYAN}⏰ Задержка автоматической очистки: {AUTO_CLEAR_DELAY_HOURS} часа")
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
        os.system(f"{sys.executable} -m pip install colorama")
        import colorama
    bot = FastDCABot()
    bot.run()