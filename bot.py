import asyncio
import logging
import os
import re
import sqlite3
from pathlib import Path
from html import escape
from datetime import datetime, timezone

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)

# Загружаем .env строго рядом с bot.py
_ENV_FILE = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_FILE)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DATABASE_PATH", "data/quadroprofile.sqlite3")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Скопируй .env.example в .env и укажи токен.")

router = Router()
INTERESTS = ["Квадробика", "Рисование", "Монтаж", "Косплей", "Фурри", "Фотография", "Общение", "Другое"]
REACTIONS = {"heart": "❤️", "fire": "🔥", "laugh": "😂", "sad": "😢", "dislike": "👎"}

# Умный рандом: { user_id: set(просмотренные id) }
RANDOM_SEEN = {}


class ProfileForm(StatesGroup):
    nickname = State()
    avatar = State()
    interests = State()
    bio = State()
    channel = State()
    city = State()


class EditProfileForm(StatesGroup):
    nickname = State()
    avatar = State()
    interests = State()
    bio = State()
    channel = State()
    city = State()


class ReportForm(StatesGroup):
    reason = State()


class SearchForm(StatesGroup):
    nickname = State()


class NomineeForm(StatesGroup):
    telegram_id = State()


class BroadcastForm(StatesGroup):
    text = State()
    confirm = State()


class ThoughtForm(StatesGroup):
    text = State()
    confirm = State()


def connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          telegram_id INTEGER PRIMARY KEY, 
          username TEXT DEFAULT '',
          status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS profiles(
          telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
          nickname TEXT NOT NULL DEFAULT '', avatar_file_id TEXT NOT NULL DEFAULT '',
          interests TEXT NOT NULL DEFAULT '', bio TEXT NOT NULL DEFAULT '',
          channel_url TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '',
          visibility TEXT NOT NULL DEFAULT 'draft', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS contests(
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'draft', opens_at TEXT, closes_at TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS nominations(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
          nominee_id INTEGER NOT NULL REFERENCES users(telegram_id),
          UNIQUE(contest_id, nominee_id)
        );
        CREATE TABLE IF NOT EXISTS votes(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
          voter_id INTEGER NOT NULL REFERENCES users(telegram_id),
          nominee_id INTEGER NOT NULL REFERENCES users(telegram_id),
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE(contest_id, voter_id)
        );
        CREATE TABLE IF NOT EXISTS reports(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          reporter_id INTEGER NOT NULL REFERENCES users(telegram_id),
          target_id INTEGER NOT NULL REFERENCES users(telegram_id),
          reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS blocks(
          blocker_id INTEGER NOT NULL REFERENCES users(telegram_id),
          blocked_id INTEGER NOT NULL REFERENCES users(telegram_id),
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(blocker_id, blocked_id), CHECK(blocker_id != blocked_id)
        );
        CREATE TABLE IF NOT EXISTS posts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          author_id INTEGER NOT NULL REFERENCES users(telegram_id),
          text TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'published',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_posts_feed ON posts(status, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_id, status);

        CREATE TABLE IF NOT EXISTS reactions(
          post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
          user_id INTEGER NOT NULL REFERENCES users(telegram_id),
          reaction TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (post_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_reactions_post ON reactions(post_id);
        """)
        try:
            con.execute("ALTER TABLE users ADD COLUMN username TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass


def ensure_user(user_id: int, username: str = ""):
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES(?)", (user_id,))
        con.execute("INSERT OR IGNORE INTO profiles(telegram_id) VALUES(?)", (user_id,))
        if username:
            con.execute("UPDATE users SET username=? WHERE telegram_id=?", (username, user_id))


def get_profile(uid):
    with connect() as con:
        r = con.execute("SELECT * FROM profiles WHERE telegram_id=?", (uid,)).fetchone()
        return dict(r) if r else None


def complete(p):
    return bool(p and p["nickname"].strip() and p["avatar_file_id"] and p["interests"].strip() and p["bio"].strip())


def is_admin(uid):
    return uid in ADMIN_IDS


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def main_menu(uid):
    rows = [
        [("👤 Моя анкета", "profile:home"), ("🔎 Каталог", "directory:home")],
        [("💭 Мысли", "thoughts:home")],
        [("🏆 Топ-10 авторов", "contest:home")],
        [("⚙️ Настройки", "settings:home"), ("ℹ️ Помощь", "help")],
    ]
    if is_admin(uid):
        rows.append([("🛡 Админ-панель", "admin:home")])
    return keyboard(rows)


def bottom_menu(uid):
    kb = [
        [KeyboardButton(text="👤 Моя анкета"), KeyboardButton(text="🔎 Каталог")],
        [KeyboardButton(text="💭 Мысли"), KeyboardButton(text="🏆 Топ-10")],
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="ℹ️ Помощь")],
    ]
    if is_admin(uid):
        kb.append([KeyboardButton(text="🛡 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, input_field_placeholder="Выбери раздел...")


def profile_menu(p):
    if not p or not p["nickname"]:
        return keyboard([[("📝 Создать анкету", "profile:create")], [("⬅️ Главное меню", "home")]])
    rows = [[("👁 Предпросмотр", "profile:preview"), ("✏️ Изменить", "profile:edit_menu")]]
    rows.append([("🙈 Скрыть анкету", "profile:hide")] if p["visibility"] == "published"
                else [("📢 Опубликовать анкету", "profile:publish")])
    rows += [
        [("🗑 Удалить анкету", "profile:delete_confirm")],
        [("⬅️ Главное меню", "home")],
    ]
    return keyboard(rows)


def back(target="home"):
    return keyboard([[("⬅️ Назад", target)]])


def cancel_kb():
    return keyboard([[("✖️ Отмена", "cancel")]])


async def edit_message(call: CallbackQuery, text: str, reply_markup=None):
    """Удаляет старое сообщение и отправляет новое. Работает и для фото, и для текста."""
    try:
        await call.message.delete()
    except Exception:
        pass
    try:
        return await call.message.answer(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        logging.warning("edit_message fallback: %s", e)


def profile_text(p, preview=False):
    interests = ", ".join(x for x in (p.get("interests") or "").split("|") if x)
    with connect() as con:
        u = con.execute("SELECT username FROM users WHERE telegram_id=?", (p['telegram_id'],)).fetchone()
        username = u["username"] if u else ""
    lines = [f"🐾 <b>{escape(p.get('nickname') or 'Без никнейма')}</b>"]
    if username:
        lines.append(f"🔗 <b>Связь:</b> @{username}")
    lines += [
        f"🆔 <b>ID:</b> <code>{p['telegram_id']}</code>",  # NEW: ID в анкете
        f"✨ <b>Интересы:</b> {escape(interests or 'не указаны')}",
        f"💬 <b>О себе:</b> {escape(p.get('bio') or 'не заполнено')}",
    ]
    if p.get("channel_url"):
        lines.append(f"📣 <b>Канал:</b> {escape(p['channel_url'])}")
    if p.get("city"):
        lines.append(f"📍 <b>Город:</b> {escape(p['city'])}")
    if preview:
        lines.append("\n<i>Предпросмотр анкеты. Проверь данные перед публикацией.</i>")
    return "\n".join(lines)


def valid_channel(url):
    return bool(re.fullmatch(r"https://t\.me/[A-Za-z0-9_]{4,}/?", url.strip()))


def time_ago(dt_str):
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        diff = datetime.now(timezone.utc).replace(tzinfo=None) - dt
        mins = int(diff.total_seconds() // 60)
        if mins < 1:
            return "только что"
        if mins < 60:
            return f"{mins} мин. назад"
        hours = mins // 60
        if hours < 24:
            return f"{hours} ч. назад"
        return f"{hours // 24} дн. назад"
    except Exception:
        return dt_str


from aiogram.types import InputMediaPhoto

async def show_public_profile(call: CallbackQuery, target_id: int, back_target="directory:home"):
    uid = call.from_user.id
    with connect() as con:
        row = con.execute("""SELECT p.* FROM profiles p JOIN users u ON u.telegram_id=p.telegram_id
          WHERE p.telegram_id=? AND p.visibility='published' AND u.status='active'""", (target_id,)).fetchone()
        blocked = con.execute("SELECT 1 FROM blocks WHERE blocker_id=? AND blocked_id=?", (uid, target_id)).fetchone()
        
        if not row or blocked:
            return await edit_message(call, "Анкета недоступна.", reply_markup=back(back_target))
            
        # Ищем следующую и предыдущую анкеты для листания
        next_row = con.execute("""SELECT telegram_id FROM profiles 
            WHERE visibility='published' AND telegram_id > ? 
            AND telegram_id NOT IN (SELECT blocked_id FROM blocks WHERE blocker_id=?)
            ORDER BY telegram_id ASC LIMIT 1""", (target_id, uid)).fetchone()
            
        prev_row = con.execute("""SELECT telegram_id FROM profiles 
            WHERE visibility='published' AND telegram_id < ? 
            AND telegram_id NOT IN (SELECT blocked_id FROM blocks WHERE blocker_id=?)
            ORDER BY telegram_id DESC LIMIT 1""", (target_id, uid)).fetchone()

    p = dict(row)
    
    # Собираем кнопки
    rows = []
    nav_row = []
    if prev_row:
        nav_row.append(("◀️ Назад", f"directory:show:{prev_row['telegram_id']}"))
    if next_row:
        nav_row.append(("Далее ▶️", f"directory:show:{next_row['telegram_id']}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([("🚩 Пожаловаться", f"report:{target_id}"), ("🚫 Заблокировать", f"block:{target_id}")])
    rows.append([("⬅️ В каталог", back_target)])
    
    markup = keyboard(rows)
    caption = profile_text(p)
    
    # Пытаемся отредактировать сообщение, чтобы оно не мигало
    try:
        if getattr(call.message, "photo", None):
            # Если текущее сообщение уже с фото — редактируем фото
            await call.message.edit_media(
                media=InputMediaPhoto(media=p["avatar_file_id"], caption=caption, parse_mode="HTML"),
                reply_markup=markup
            )
        else:
            # Если было текстовое сообщение (например, нажали из поиска) — удаляем и отправляем фото
            await call.message.delete()
            await call.message.answer_photo(p["avatar_file_id"], caption=caption, reply_markup=markup)
    except TelegramBadRequest as e:
        # Если фото не изменилось (нажали на ту же анкету) или возникла ошибка
        if "message is not modified" not in str(e).lower():
            try:
                await call.message.delete()
            except Exception:
                pass
            await call.message.answer_photo(p["avatar_file_id"], caption=caption, reply_markup=markup)


# ---------------- START / HELP / ADMIN CMDS ----------------

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    ensure_user(message.from_user.id, message.from_user.username or "")
    with connect() as con:
        status = con.execute("SELECT status FROM users WHERE telegram_id=?", (message.from_user.id,)).fetchone()["status"]
    if status == "banned":
        return await message.answer("Доступ к боту ограничен администрацией.")
    await message.answer(
        "🐾 <b>QuadroProfileBot</b>\n"
        "Социальная сеть квадрофандома: анкеты, новые знакомства и «Топ-10 авторов».\n\n"
        "Выбери раздел:",
        reply_markup=bottom_menu(message.from_user.id),
    )


# --- Нативные кнопки ---

@router.message(F.text == "👤 Моя анкета")
async def msg_profile_home(message: Message, state: FSMContext):
    await state.clear()
    p = get_profile(message.from_user.id)
    label = {"draft": "Черновик", "hidden": "Скрыта", "published": "Опубликована"}.get(p["visibility"], p["visibility"])
    await message.answer(f"👤 <b>Моя анкета</b>\nСтатус: <b>{label}</b>", reply_markup=profile_menu(p))


@router.message(F.text == "🔎 Каталог")
async def msg_directory_home(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🔎 <b>Каталог участников</b>\nВыбери способ поиска:", reply_markup=keyboard([
        [("🔤 По нику", "directory:search"), ("✨ По интересам", "directory:interests")],
        [("🎲 Случайный участник", "directory:random")],
        [("⬅️ Главное меню", "home")],
    ]))


@router.message(F.text == "💭 Мысли")
async def msg_thoughts_home(message: Message, state: FSMContext):
    await state.clear()
    with connect() as con:
        latest = con.execute("SELECT id FROM posts WHERE status='published' ORDER BY id DESC LIMIT 1").fetchone()
    if not latest:
        return await message.answer("💭 <b>Мысли</b>\n\nПока нет мыслей. Будь первым!", reply_markup=keyboard([
            [("✍️ Написать мысль", "thoughts:create")],
            [("⬅️ В меню", "home")],
        ]))
    text, markup = get_post_card(latest["id"], message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.message(F.text == "🏆 Топ-10")
async def msg_contest_home(message: Message, state: FSMContext):
    await state.clear()
    with connect() as con:
        rows = con.execute("SELECT * FROM contests WHERE status IN ('open','closed') ORDER BY id DESC LIMIT 10").fetchall()
    if not rows:
        return await message.answer("🏆 Пока нет активных или завершённых выпусков. Загляни позже!", reply_markup=back())
    buttons = [[(f"{'🟢' if r['status'] == 'open' else '🏁'} {r['title']}", f"contest:view:{r['id']}")] for r in rows]
    buttons.append([("⬅️ Главное меню", "home")])
    await message.answer("🏆 <b>Топ-10 авторов</b>\nВыбери выпуск:", reply_markup=keyboard(buttons))


@router.message(F.text == "⚙️ Настройки")
async def msg_settings(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("⚙️ <b>Настройки</b>\nУправляй видимостью анкеты в разделе «Моя анкета».", reply_markup=keyboard([
        [("👤 Моя анкета", "profile:home")],
        [("⬅️ Главное меню", "home")],
    ]))


@router.message(F.text == "ℹ️ Помощь")
async def msg_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "ℹ️ <b>Помощь</b>\n\n"
        "• Анкета появляется в каталоге только после подтверждения.\n"
        "• Город и ссылка на канал необязательны.\n"
        "• Анкету можно скрыть или удалить.\n"
        "• Во вкладке «Мысли» можно написать сообщение или поставить реакцию.\n"
        "• В одном выпуске «Топ-10» доступен один голос.\n"
        "• В карточке можно пожаловаться или заблокировать участника.",
        reply_markup=back(),
    )


@router.message(F.text == "🛡 Админ-панель")
async def msg_admin_home(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return await message.answer("Нет доступа.")
    await state.clear()
    await message.answer("🛡 <b>Админ-панель</b>", reply_markup=keyboard([
        [("➕ Создать выпуск", "admin:create")],
        [("📋 Выпуски", "admin:contests")],
        [("👥 Пользователи", "admin:users:0")],  # NEW
        [("🚩 Жалобы", "admin:reports")],
        [("📢 Рассылка", "admin:broadcast")],
        [("💭 Мысли", "admin:thoughts")],
    ]))

@router.callback_query(F.data.startswith("admin:users:"))
async def admin_users_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа.", show_alert=True)
    try:
        page = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        page = 0
    limit = 10
    offset = page * limit
    
    with connect() as con:
        total = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        rows = con.execute("""
            SELECT u.telegram_id, u.status, p.nickname 
            FROM users u 
            LEFT JOIN profiles p ON p.telegram_id = u.telegram_id 
            ORDER BY u.created_at DESC 
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
        
    if not rows:
        await edit_message(call, "На этой странице нет пользователей.", reply_markup=back("admin:home"))
        return await call.answer()
        
    total_pages = max(1, (total + limit - 1) // limit)
    text = f"👥 <b>Пользователи</b> (Стр. {page + 1}/{total_pages})\nВсего: {total}\n\n"
    buttons = []
    
    for r in rows:
        icon = "🔴" if r["status"] == "banned" else "🟢"
        name = r["nickname"] or "Без анкеты"
        action = "unban" if r["status"] == "banned" else "ban"
        buttons.append([(f"{icon} {name} (ID: {r['telegram_id']})", f"admin:user_toggle:{action}:{r['telegram_id']}")])
        
    nav = []
    if page > 0:
        nav.append(("⬅️ Стр.", f"admin:users:{page - 1}"))
    if offset + limit < total:
        nav.append(("Стр. ➡️", f"admin:users:{page + 1}"))
    if nav:
        buttons.append(nav)
        
    buttons.append([("⬅️ Админ-панель", "admin:home")])
    await edit_message(call, text, reply_markup=keyboard(buttons))
    await call.answer()


@router.callback_query(F.data.startswith("admin:user_toggle:"))
async def admin_user_toggle(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа.", show_alert=True)
    parts = call.data.split(":")
    if len(parts) != 4:
        return await call.answer("Некорректный запрос.", show_alert=True)
    action, target_id = parts[2], int(parts[3])
    
    if target_id == call.from_user.id:
        return await call.answer("Себя банить нельзя.", show_alert=True)
        
    new_status = "banned" if action == "ban" else "active"
    with connect() as con:
        con.execute("UPDATE users SET status=? WHERE telegram_id=?", (new_status, target_id))
        
    await call.answer("Готово" if new_status == "active" else "Забанен")
    
    # Обновляем список
    call.data = "admin:users:0"
    await admin_users_list(call)

@router.message(Command("ban"))
async def admin_ban(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ У тебя нет прав для этой команды.")
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        return await message.answer("⚠️ Использование: <code>/ban ID</code>\nНапример: <code>/ban 123456789</code>")
    target_id = int(args[1])
    if target_id == message.from_user.id:
        return await message.answer("Ты не можешь забанить самого себя.")
    with connect() as con:
        user = con.execute("SELECT status FROM users WHERE telegram_id=?", (target_id,)).fetchone()
        if not user:
            return await message.answer(f"❌ Пользователь с ID <code>{target_id}</code> не найден в базе.")
        con.execute("UPDATE users SET status='banned' WHERE telegram_id=?", (target_id,))
    await message.answer(f"✅ Пользователь <code>{target_id}</code> заблокирован.")



@router.message(Command("unban"))
async def admin_unban(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ У тебя нет прав для этой команды.")
    args = message.text.split()
    if len(args) != 2 or not args[1].isdigit():
        return await message.answer("⚠️ Использование: <code>/unban ID</code>\nНапример: <code>/unban 123456789</code>")
    target_id = int(args[1])
    with connect() as con:
        user = con.execute("SELECT status FROM users WHERE telegram_id=?", (target_id,)).fetchone()
        if not user:
            return await message.answer(f"❌ Пользователь с ID <code>{target_id}</code> не найден в базе.")
        con.execute("UPDATE users SET status='active' WHERE telegram_id=?", (target_id,))
    await message.answer(f"✅ Пользователь <code>{target_id}</code> разблокирован.")


@router.message(Command("id"))
async def show_id(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) == 1:
        username = message.from_user.username or "нет"
        return await message.answer(
            f"👤 <b>Твои данные:</b>\n"
            f"🆔 ID: <code>{message.from_user.id}</code>\n"
            f"🔗 Юзернейм: @{username}\n\n"
            f"<i>Не публикуй ID в открытом доступе.</i>"
        )
    query = args[1].strip()
    with connect() as con:
        rows = con.execute(
            "SELECT telegram_id, nickname FROM profiles WHERE lower(nickname) LIKE lower(?) LIMIT 5",
            (f"%{query}%",)
        ).fetchall()
    if not rows:
        return await message.answer(f"❌ Никого с ником «{escape(query)}» не найдено.")
    lines = [f"🔎 <b>Результаты поиска по нику «{escape(query)}»:</b>\n"]
    for r in rows:
        lines.append(f"🐾 <b>{escape(r['nickname'])}</b> — ID: <code>{r['telegram_id']}</code>")
    await message.answer("\n".join(lines))


# ---------------- CALLBACK: HOME / HELP ----------------

@router.callback_query(F.data == "home")
async def home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    ensure_user(call.from_user.id, call.from_user.username or "")
    await edit_message(call, "🐾 <b>QuadroProfileBot</b>\nВыбери раздел:", reply_markup=main_menu(call.from_user.id))
    await call.answer()


@router.callback_query(F.data == "cancel")
async def cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_message(call, "Действие отменено.", reply_markup=back())
    await call.answer()


@router.callback_query(F.data == "help")
async def help_page(call: CallbackQuery):
    await edit_message(
        call,
        "ℹ️ <b>Помощь</b>\n\n"
        "• Анкета появляется в каталоге только после подтверждения.\n"
        "• Город и ссылка на канал необязательны.\n"
        "• Анкету можно скрыть или удалить.\n"
        "• Во вкладке «Мысли» можно написать сообщение или поставить реакцию.\n"
        "• В одном выпуске «Топ-10» доступен один голос.\n"
        "• В карточке можно пожаловаться или заблокировать участника.",
        reply_markup=back(),
    )
    await call.answer()


# ---------------- PROFILE ----------------

@router.callback_query(F.data == "profile:home")
async def profile_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    ensure_user(call.from_user.id, call.from_user.username or "")
    p = get_profile(call.from_user.id)
    label = {"draft": "Черновик", "hidden": "Скрыта", "published": "Опубликована"}.get(p["visibility"], p["visibility"])
    await edit_message(call, f"👤 <b>Моя анкета</b>\nСтатус: <b>{label}</b>", reply_markup=profile_menu(p))
    await call.answer()


@router.callback_query(F.data == "profile:create")
async def profile_create(call: CallbackQuery, state: FSMContext):
    ensure_user(call.from_user.id, call.from_user.username or "")
    await state.clear()
    await state.set_state(ProfileForm.nickname)
    await edit_message(call, "📝 <b>Анкета · 1/6</b>\nОтправь никнейм (2–32 символа).", reply_markup=cancel_kb())
    await call.answer()


# --- Выборочное редактирование ---

@router.callback_query(F.data == "profile:edit_menu")
async def profile_edit_menu(call: CallbackQuery):
    await edit_message(call, "✏️ <b>Что именно изменить?</b>", reply_markup=keyboard([
        [("Никнейм", "profile:edit:nickname"), ("Аватар", "profile:edit:avatar")],
        [("Интересы", "profile:edit:interests"), ("О себе", "profile:edit:bio")],
        [("Канал", "profile:edit:channel"), ("Город", "profile:edit:city")],
        [("⬅️ Назад", "profile:home")],
    ]))
    await call.answer()


@router.callback_query(F.data.startswith("profile:edit:"))
async def profile_edit_field(call: CallbackQuery, state: FSMContext):
    field = call.data.split(":")[2]
    await state.clear()
    if field == "nickname":
        await state.set_state(EditProfileForm.nickname)
        await edit_message(call, "Введи новый никнейм (2–32 символа):", reply_markup=cancel_kb())
    elif field == "avatar":
        await state.set_state(EditProfileForm.avatar)
        await edit_message(call, "Отправь новое фото для аватара:", reply_markup=cancel_kb())
    elif field == "interests":
        p = get_profile(call.from_user.id)
        selected = [x for x in (p.get("interests") or "").split("|") if x]
        await state.update_data(interests=selected)
        await state.set_state(EditProfileForm.interests)
        rows = [[(("☑️ " if x in selected else "") + x, f"interest:{x}")] for x in INTERESTS]
        rows.append([("Готово ✓", "interest:done"), ("✖️ Отмена", "cancel")])
        await edit_message(call, "Выбери интересы заново:", reply_markup=keyboard(rows))
    elif field == "bio":
        await state.set_state(EditProfileForm.bio)
        await edit_message(call, "Напиши новое описание (до 500 символов):", reply_markup=cancel_kb())
    elif field == "channel":
        await state.set_state(EditProfileForm.channel)
        await edit_message(call, "Ссылка на Telegram-канал или <code>-</code>, чтобы удалить:", reply_markup=cancel_kb())
    elif field == "city":
        await state.set_state(EditProfileForm.city)
        await edit_message(call, "Новый город или <code>-</code>, чтобы удалить:", reply_markup=cancel_kb())
    await call.answer()


@router.message(EditProfileForm.nickname)
async def edit_nick(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not 2 <= len(val) <= 32:
        return await message.answer("Никнейм должен содержать 2–32 символа.", reply_markup=cancel_kb())
    with connect() as con:
        con.execute("UPDATE profiles SET nickname=?, updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?", (val, message.from_user.id))
    await state.clear()
    await profile_home_msg(message)


@router.message(EditProfileForm.avatar)
async def edit_avatar(message: Message, state: FSMContext):
    if not message.photo:
        return await message.answer("Отправь изображение как фото Telegram.", reply_markup=cancel_kb())
    with connect() as con:
        con.execute("UPDATE profiles SET avatar_file_id=?, updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?", (message.photo[-1].file_id, message.from_user.id))
    await state.clear()
    await profile_home_msg(message)


@router.message(EditProfileForm.bio)
async def edit_bio(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not val or len(val) > 500:
        return await message.answer("Описание должно содержать 1–500 символов.", reply_markup=cancel_kb())
    with connect() as con:
        con.execute("UPDATE profiles SET bio=?, updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?", (val, message.from_user.id))
    await state.clear()
    await profile_home_msg(message)


@router.message(EditProfileForm.channel)
async def edit_channel(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if val == "-":
        val = ""
    elif val and not valid_channel(val):
        return await message.answer("Укажи ссылку формата https://t.me/имя или «-».", reply_markup=cancel_kb())
    with connect() as con:
        con.execute("UPDATE profiles SET channel_url=?, updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?", (val, message.from_user.id))
    await state.clear()
    await profile_home_msg(message)


@router.message(EditProfileForm.city)
async def edit_city(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if val == "-":
        val = ""
    if len(val) > 80:
        return await message.answer("Слишком длинный город.", reply_markup=cancel_kb())
    with connect() as con:
        con.execute("UPDATE profiles SET city=?, updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?", (val, message.from_user.id))
    await state.clear()
    await profile_home_msg(message)


@router.callback_query(F.data.startswith("interest:"), EditProfileForm.interests)
async def edit_interest(call: CallbackQuery, state: FSMContext):
    value = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected = data.get("interests", [])
    if value == "done":
        if not selected:
            return await call.answer("Выбери хотя бы один интерес.", show_alert=True)
        with connect() as con:
            con.execute("UPDATE profiles SET interests=?, updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?", ("|".join(selected), call.from_user.id))
        await state.clear()
        await edit_message(call, "✅ Интересы обновлены!", reply_markup=back("profile:home"))
        return await call.answer()
    if value in selected:
        selected.remove(value)
    elif value in INTERESTS:
        selected.append(value)
    await state.update_data(interests=selected)
    rows = [[(("☑️ " if x in selected else "") + x, f"interest:{x}")] for x in INTERESTS]
    rows.append([("Готово ✓", "interest:done"), ("✖️ Отмена", "cancel")])
    try:
        await call.message.edit_reply_markup(reply_markup=keyboard(rows))
    except TelegramBadRequest:
        pass
    await call.answer("Выбор обновлён")


async def profile_home_msg(message: Message):
    p = get_profile(message.from_user.id)
    label = {"draft": "Черновик", "hidden": "Скрыта", "published": "Опубликована"}.get(p["visibility"], p["visibility"])
    await message.answer(f"👤 <b>Моя анкета</b>\nСтатус: <b>{label}</b>", reply_markup=profile_menu(p))


# --- Полное создание анкеты ---

@router.message(ProfileForm.nickname)
async def profile_nickname(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not 2 <= len(value) <= 32:
        return await message.answer("Никнейм должен содержать 2–32 символа.", reply_markup=cancel_kb())
    await state.update_data(nickname=value)
    await state.set_state(ProfileForm.avatar)
    await message.answer("🖼 <b>Анкета · 2/6</b>\nОтправь изображение для аватара как фото.", reply_markup=cancel_kb())


@router.message(ProfileForm.avatar)
async def profile_avatar(message: Message, state: FSMContext):
    if not message.photo:
        return await message.answer("Отправь изображение как фото Telegram.", reply_markup=cancel_kb())
    await state.update_data(avatar_file_id=message.photo[-1].file_id, interests=[])
    await state.set_state(ProfileForm.interests)
    data = await state.get_data()
    rows = [[(("☑️ " if x in data["interests"] else "") + x, f"interest:{x}")] for x in INTERESTS]
    rows.append([("Готово ✓", "interest:done"), ("✖️ Отмена", "cancel")])
    await message.answer("✨ <b>Анкета · 3/6</b>\nВыбери несколько интересов, затем нажми «Готово».", reply_markup=keyboard(rows))


@router.callback_query(F.data.startswith("interest:"), ProfileForm.interests)
async def profile_interest(call: CallbackQuery, state: FSMContext):
    value = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected = data.get("interests", [])
    if value == "done":
        if not selected:
            return await call.answer("Выбери хотя бы один интерес.", show_alert=True)
        await state.set_state(ProfileForm.bio)
        await edit_message(call, "💬 <b>Анкета · 4/6</b>\nРасскажи о себе (до 500 символов).", reply_markup=cancel_kb())
        return await call.answer()
    if value in selected:
        selected.remove(value)
    elif value in INTERESTS:
        selected.append(value)
    await state.update_data(interests=selected)
    rows = [[(("☑️ " if x in selected else "") + x, f"interest:{x}")] for x in INTERESTS]
    rows.append([("Готово ✓", "interest:done"), ("✖️ Отмена", "cancel")])
    try:
        await call.message.edit_reply_markup(reply_markup=keyboard(rows))
    except TelegramBadRequest:
        pass
    await call.answer("Выбор обновлён")


@router.message(ProfileForm.bio)
async def profile_bio(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if not value or len(value) > 500:
        return await message.answer("Описание должно содержать 1–500 символов.", reply_markup=cancel_kb())
    await state.update_data(bio=value)
    await state.set_state(ProfileForm.channel)
    await message.answer("📣 <b>Анкета · 5/6</b>\nСсылка на Telegram-канал или <code>-</code>, чтобы пропустить.", reply_markup=cancel_kb())


@router.message(ProfileForm.channel)
async def profile_channel(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if value == "-":
        value = ""
    elif value and not valid_channel(value):
        return await message.answer("Укажи ссылку формата https://t.me/имя_канала или «-».", reply_markup=cancel_kb())
    await state.update_data(channel_url=value)
    await state.set_state(ProfileForm.city)
    await message.answer("📍 <b>Анкета · 6/6</b>\nГород или <code>-</code>, чтобы пропустить.", reply_markup=cancel_kb())


@router.message(ProfileForm.city)
async def profile_city(message: Message, state: FSMContext):
    city = (message.text or "").strip()
    if city == "-":
        city = ""
    if len(city) > 80:
        return await message.answer("Название города слишком длинное.", reply_markup=cancel_kb())
    data = await state.get_data()
    uid = message.from_user.id
    ensure_user(uid, message.from_user.username or "")
    with connect() as con:
        con.execute(
            """UPDATE profiles SET nickname=?, avatar_file_id=?, interests=?, bio=?, channel_url=?, city=?,
               visibility='draft', updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?""",
            (data["nickname"], data["avatar_file_id"], "|".join(data["interests"]), data["bio"], data["channel_url"], city, uid),
        )
    await state.clear()
    p = get_profile(uid)
    await message.answer_photo(
        p["avatar_file_id"], caption=profile_text(p, True),
        reply_markup=keyboard([
            [("📢 Подтвердить публикацию", "profile:publish")],
            [("✏️ Заполнить заново", "profile:create")],
            [("⬅️ В меню анкеты", "profile:home")],
        ]),
    )


@router.callback_query(F.data == "profile:preview")
async def profile_preview(call: CallbackQuery):
    p = get_profile(call.from_user.id)
    if not p or not p["nickname"]:
        return await call.answer("Сначала создай анкету.", show_alert=True)
    await edit_message(call, profile_text(p, True), reply_markup=profile_menu(p))
    await call.answer()


@router.callback_query(F.data == "profile:publish")
async def publish_prompt(call: CallbackQuery):
    p = get_profile(call.from_user.id)
    if not complete(p):
        return await call.answer("Сначала заполни все обязательные поля.", show_alert=True)
    await edit_message(call, "📢 <b>Подтверждение публикации</b>\n\nАнкета станет видна участникам каталога. Опубликовать?",
        reply_markup=keyboard([
            [("✅ Да, опубликовать", "profile:publish_yes")],
            [("⬅️ Отмена", "profile:home")],
        ]))
    await call.answer()


@router.callback_query(F.data == "profile:publish_yes")
async def publish_yes(call: CallbackQuery):
    p = get_profile(call.from_user.id)
    if not complete(p):
        return await call.answer("Анкета неполная.", show_alert=True)
    with connect() as con:
        con.execute("UPDATE profiles SET visibility='published', updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?", (call.from_user.id,))
    await edit_message(call, "✅ Анкета опубликована и доступна в каталоге.", reply_markup=profile_menu(get_profile(call.from_user.id)))
    await call.answer("Опубликовано!")


@router.callback_query(F.data == "profile:hide")
async def profile_hide(call: CallbackQuery):
    with connect() as con:
        con.execute("UPDATE profiles SET visibility='hidden', updated_at=CURRENT_TIMESTAMP WHERE telegram_id=?", (call.from_user.id,))
    await edit_message(call, "🙈 Анкета скрыта из каталога.", reply_markup=profile_menu(get_profile(call.from_user.id)))
    await call.answer()


@router.callback_query(F.data == "profile:delete_confirm")
async def delete_confirm(call: CallbackQuery):
    await edit_message(call, "🗑 <b>Удалить анкету?</b>\nПрофиль будет удалён. Исторические голоса сохраняются.",
        reply_markup=keyboard([
            [("🗑 Да, удалить", "profile:delete_yes")],
            [("⬅️ Отмена", "profile:home")],
        ]))
    await call.answer()


@router.callback_query(F.data == "profile:delete_yes")
async def delete_yes(call: CallbackQuery):
    with connect() as con:
        con.execute("DELETE FROM profiles WHERE telegram_id=?", (call.from_user.id,))
    await edit_message(call, "Анкета удалена. При желании можно создать новую.", reply_markup=keyboard([
        [("📝 Создать анкету", "profile:create")],
        [("⬅️ Главное меню", "home")],
    ]))
    await call.answer()


# ---------------- DIRECTORY ----------------

@router.callback_query(F.data == "directory:home")
async def directory_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_message(call, "🔎 <b>Каталог участников</b>\nВыбери способ поиска:", reply_markup=keyboard([
        [("🔤 По нику", "directory:search"), ("✨ По интересам", "directory:interests")],
        [("🎲 Случайный участник", "directory:random")],
        [("⬅️ Главное меню", "home")],
    ]))
    await call.answer()


@router.callback_query(F.data == "directory:search")
async def search_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(SearchForm.nickname)
    await edit_message(call, "Введи никнейм или часть никнейма:", reply_markup=cancel_kb())
    await call.answer()


@router.message(SearchForm.nickname)
async def search_result(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    await state.clear()
    uid = message.from_user.id
    with connect() as con:
        rows = con.execute(
            """SELECT p.telegram_id, p.nickname FROM profiles p JOIN users u ON u.telegram_id=p.telegram_id
               WHERE p.visibility='published' AND u.status='active' AND lower(p.nickname) LIKE lower(?)
                 AND p.telegram_id != ?
                 AND NOT EXISTS (SELECT 1 FROM blocks b WHERE b.blocker_id=? AND b.blocked_id=p.telegram_id)
               ORDER BY p.nickname LIMIT 20""",
            (f"%{query}%", uid, uid),
        ).fetchall()
    if not rows:
        return await message.answer("Ничего не найдено.", reply_markup=back("directory:home"))
    buttons = [[(f"🐾 {r['nickname']}", f"directory:show:{r['telegram_id']}")] for r in rows]
    buttons.append([("⬅️ В каталог", "directory:home")])
    await message.answer(f"🔎 Найдено анкет: {len(rows)}", reply_markup=keyboard(buttons))


@router.callback_query(F.data == "directory:interests")
async def interest_menu(call: CallbackQuery):
    rows = [[(x, f"directory:interest:{x}")] for x in INTERESTS]
    rows.append([("⬅️ В каталог", "directory:home")])
    await edit_message(call, "Выбери интерес:", reply_markup=keyboard(rows))
    await call.answer()


@router.callback_query(F.data.startswith("directory:interest:"))
async def directory_interest(call: CallbackQuery):
    interest = call.data.split(":", 2)[2]
    uid = call.from_user.id
    with connect() as con:
        all_rows = con.execute(
            """SELECT p.telegram_id, p.nickname, p.interests FROM profiles p
               JOIN users u ON u.telegram_id=p.telegram_id
               WHERE p.visibility='published' AND u.status='active' AND p.telegram_id != ?
               ORDER BY p.nickname""", (uid,)).fetchall()
        blocked = {r["blocked_id"] for r in con.execute("SELECT blocked_id FROM blocks WHERE blocker_id=?", (uid,)).fetchall()}
    rows = [r for r in all_rows if interest in (r["interests"] or "").split("|") and r["telegram_id"] not in blocked][:20]
    if not rows:
        await edit_message(call, "Пока нет опубликованных анкет с этим интересом.", reply_markup=back("directory:home"))
    else:
        await edit_message(call, f"✨ Участники с интересом «{escape(interest)}»:",
            reply_markup=keyboard([[(f"🐾 {r['nickname']}", f"directory:show:{r['telegram_id']}")] for r in rows] + [[("⬅️ В каталог", "directory:home")]]))
    await call.answer()


@router.callback_query(F.data == "directory:random")
async def directory_random(call: CallbackQuery):
    # FIX: сначала отвечаем, чтобы не сработал query is too old
    await call.answer()
    uid = call.from_user.id
    with connect() as con:
        rows = con.execute("""SELECT p.telegram_id FROM profiles p JOIN users u ON u.telegram_id=p.telegram_id
          WHERE p.visibility='published' AND u.status='active' AND p.telegram_id != ?
          AND NOT EXISTS (SELECT 1 FROM blocks b WHERE b.blocker_id=? AND b.blocked_id=p.telegram_id)""", (uid, uid)).fetchall()
    all_ids = {r["telegram_id"] for r in rows}
    if not all_ids:
        return await edit_message(call, "Пока нет доступных анкет.", reply_markup=back("directory:home"))
    seen = RANDOM_SEEN.get(uid, set()).intersection(all_ids)
    unseen = list(all_ids - seen)
    if not unseen:
        seen.clear()
        unseen = list(all_ids)
    import random
    chosen = random.choice(unseen)
    seen.add(chosen)
    RANDOM_SEEN[uid] = seen
    await show_public_profile(call, chosen, back_target="directory:home")


@router.callback_query(F.data.startswith("directory:show:"))
async def directory_show(call: CallbackQuery):
    try:
        target = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        return await call.answer("Некорректная анкета.", show_alert=True)
    await call.answer()
    await show_public_profile(call, target)


# --- Жалобы (Анкеты) ---

@router.callback_query(F.data.startswith("report:"))
async def report_start(call: CallbackQuery, state: FSMContext):
    target_id = int(call.data.split(":")[1])
    if target_id == call.from_user.id:
        return await call.answer("Нельзя пожаловаться на себя.", show_alert=True)
    await state.update_data(target_id=target_id, report_type="Анкета")
    await state.set_state(ReportForm.reason)
    await edit_message(call, "🚩 <b>Жалоба на анкету</b>\n\nВыбери причину жалобы:", reply_markup=keyboard([
        [("🤬 Оскорбления / мат", "rep_reason:Оскорбления / мат")],
        [("🔞 18+ контент", "rep_reason:18+ контент")],
        [("🛑 Спам / реклама", "rep_reason:Спам / реклама")],
        [("🤡 Фейк / обман", "rep_reason:Фейк / обман")],
        [("✖️ Отмена", "cancel")],
    ]))
    await call.answer()


@router.callback_query(F.data.startswith("rep_reason:"), ReportForm.reason)
async def report_reason(call: CallbackQuery, state: FSMContext):
    reason_text = call.data.split(":", 1)[1]
    data = await state.get_data()
    target_id = data.get("target_id")
    report_type = data.get("report_type")
    await state.clear()
    with connect() as con:
        con.execute("INSERT INTO reports(reporter_id, target_id, reason) VALUES (?, ?, ?)",
                    (call.from_user.id, target_id, f"[{report_type}] {reason_text}"))
    await edit_message(call, "✅ <b>Жалоба отправлена</b>\n\nМодерация рассмотрит её в ближайшее время. Спасибо за помощь!",
                       reply_markup=back("home"))
    await call.answer("Жалоба принята")


@router.callback_query(F.data.startswith("block:"))
async def block_user(call: CallbackQuery):
    try:
        target = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return await call.answer("Некорректный запрос.", show_alert=True)
    if target == call.from_user.id:
        return await call.answer("Нельзя заблокировать себя.", show_alert=True)
    ensure_user(target, "")
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO blocks(blocker_id, blocked_id) VALUES (?, ?)", (call.from_user.id, target))
    await edit_message(call, "🚫 Пользователь заблокирован и будет скрыт от тебя в каталоге.", reply_markup=back("directory:home"))
    await call.answer()


# ---------------- SETTINGS ----------------

@router.callback_query(F.data == "settings:home")
async def settings(call: CallbackQuery):
    await edit_message(call, "⚙️ <b>Настройки</b>\nУправляй видимостью анкеты в разделе «Моя анкета».",
        reply_markup=keyboard([
            [("👤 Моя анкета", "profile:home")],
            [("⬅️ Главное меню", "home")],
        ]))
    await call.answer()


# ---------------- CONTESTS ----------------

@router.callback_query(F.data == "contest:home")
async def contest_home(call: CallbackQuery):
    with connect() as con:
        rows = con.execute("SELECT * FROM contests WHERE status IN ('open','closed') ORDER BY id DESC LIMIT 10").fetchall()
    if not rows:
        await edit_message(call, "🏆 Пока нет активных или завершённых выпусков. Загляни позже!", reply_markup=back())
    else:
        buttons = [[(f"{'🟢' if r['status'] == 'open' else '🏁'} {r['title']}", f"contest:view:{r['id']}")] for r in rows]
        buttons.append([("⬅️ Главное меню", "home")])
        await edit_message(call, "🏆 <b>Топ-10 авторов</b>\nВыбери выпуск:", reply_markup=keyboard(buttons))
    await call.answer()


@router.callback_query(F.data.startswith("contest:view:"))
async def contest_view(call: CallbackQuery):
    try:
        cid = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        return await call.answer("Некорректный выпуск.", show_alert=True)
    uid = call.from_user.id
    with connect() as con:
        contest = con.execute("SELECT * FROM contests WHERE id=?", (cid,)).fetchone()
        nominees = con.execute("""SELECT n.nominee_id, p.nickname FROM nominations n
          JOIN profiles p ON p.telegram_id=n.nominee_id
          WHERE n.contest_id=? AND p.visibility='published' ORDER BY p.nickname""", (cid,)).fetchall()
        existing = con.execute("SELECT nominee_id FROM votes WHERE contest_id=? AND voter_id=?", (cid, uid)).fetchone()
        counts = con.execute("SELECT nominee_id, COUNT(*) total FROM votes WHERE contest_id=? GROUP BY nominee_id", (cid,)).fetchall()
    if not contest:
        return await call.answer("Выпуск не найден.", show_alert=True)
    totals = {r["nominee_id"]: r["total"] for r in counts}
    if contest["status"] == "closed":
        ordered = sorted(nominees, key=lambda n: (-totals.get(n["nominee_id"], 0), n["nickname"].lower()))
        lines = [f"{i}. {escape(n['nickname'])} — {totals.get(n['nominee_id'], 0)} голосов" for i, n in enumerate(ordered[:10], 1)]
        await edit_message(call, f"🏁 <b>{escape(contest['title'])}</b>\n\n" + ("\n".join(lines) if lines else "В выпуске нет номинантов."), reply_markup=back("contest:home"))
    elif contest["status"] == "open":
        if existing:
            await edit_message(call, f"🏆 <b>{escape(contest['title'])}</b>\n\nТы уже проголосовал.", reply_markup=back("contest:home"))
        else:
            rows = [[(f"🐾 {n['nickname']}", f"vote:{cid}:{n['nominee_id']}")] for n in nominees if n["nominee_id"] != uid]
            rows.append([("⬅️ К выпускам", "contest:home")])
            await edit_message(call, f"🏆 <b>{escape(contest['title'])}</b>\nВыбери одного автора. Голос нельзя изменить.", reply_markup=keyboard(rows))
    else:
        await edit_message(call, "Этот выпуск ещё не открыт.", reply_markup=back("contest:home"))
    await call.answer()


@router.callback_query(F.data.startswith("vote:"))
async def vote(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        return await call.answer("Некорректный голос.", show_alert=True)
    cid, nominee = int(parts[1]), int(parts[2])
    uid = call.from_user.id
    if nominee == uid:
        return await call.answer("За себя голосовать нельзя.", show_alert=True)
    ensure_user(uid, call.from_user.username or "")
    with connect() as con:
        contest = con.execute("SELECT status FROM contests WHERE id=?", (cid,)).fetchone()
        nominated = con.execute("SELECT 1 FROM nominations WHERE contest_id=? AND nominee_id=?", (cid, nominee)).fetchone()
        if not contest or contest["status"] != "open" or not nominated:
            return await call.answer("Голосование закрыто или автор не участвует.", show_alert=True)
        try:
            con.execute("INSERT INTO votes(contest_id, voter_id, nominee_id) VALUES(?,?,?)", (cid, uid, nominee))
        except sqlite3.IntegrityError:
            return await call.answer("В этом выпуске у тебя уже есть голос.", show_alert=True)
        row = con.execute("SELECT nickname FROM profiles WHERE telegram_id=?", (nominee,)).fetchone()
    await edit_message(call, f"✅ Твой голос учтён за «{escape(row['nickname'] if row else 'автора')}».", reply_markup=back("contest:home"))
    await call.answer("Голос учтён!")


# ---------------- ADMIN ----------------

@router.callback_query(F.data == "admin:home")
async def admin_home(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа.", show_alert=True)
    await edit_message(call, "🛡 <b>Админ-панель</b>", reply_markup=keyboard([
        [("➕ Создать выпуск", "admin:create")],
        [("📋 Выпуски", "admin:contests")],
        [("👥 Пользователи", "admin:users:0")],  # NEW
        [("🚩 Жалобы", "admin:reports")],
        [("📢 Рассылка", "admin:broadcast")],
        [("💭 Мысли", "admin:thoughts")],
        [("⬅️ Главное меню", "home")],
    ]))
    await call.answer()


# --- NEW: список пользователей с баном ---

@router.callback_query(F.data.startswith("admin:users:"))
async def admin_users_list(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа.", show_alert=True)
    try:
        page = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        page = 0
    limit = 10
    offset = page * limit
    with connect() as con:
        total = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        rows = con.execute("""
            SELECT u.telegram_id, u.status, p.nickname 
            FROM users u 
            LEFT JOIN profiles p ON p.telegram_id = u.telegram_id 
            ORDER BY u.created_at DESC 
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
    if not rows:
        await edit_message(call, "На этой странице нет пользователей.", reply_markup=back("admin:home"))
        return await call.answer()
    total_pages = max(1, (total + limit - 1) // limit)
    text = f"👥 <b>Пользователи</b> (Стр. {page + 1}/{total_pages})\nВсего: {total}\n\n"
    buttons = []
    for r in rows:
        icon = "🔴" if r["status"] == "banned" else "🟢"
        name = r["nickname"] or "Без анкеты"
        buttons.append([(f"{icon} {name} (ID: {r['telegram_id']})", f"admin:user_toggle:{'unban' if r['status']=='banned' else 'ban'}:{r['telegram_id']}")])
    nav = []
    if page > 0:
        nav.append(("⬅️ Стр.", f"admin:users:{page - 1}"))
    if offset + limit < total:
        nav.append(("Стр. ➡️", f"admin:users:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([("⬅️ Админ-панель", "admin:home")])
    await edit_message(call, text, reply_markup=keyboard(buttons))
    await call.answer()


@router.callback_query(F.data.startswith("admin:user_toggle:"))
async def admin_user_toggle(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа.", show_alert=True)
    parts = call.data.split(":")
    if len(parts) != 4:
        return await call.answer("Некорректный запрос.", show_alert=True)
    action, target_id = parts[2], int(parts[3])
    if target_id == call.from_user.id:
        return await call.answer("Себя банить нельзя.", show_alert=True)
    new_status = "banned" if action == "ban" else "active"
    with connect() as con:
        con.execute("UPDATE users SET status=? WHERE telegram_id=?", (new_status, target_id))
    await call.answer("Готово" if new_status == "active" else "Забанен")
    # Обновляем список на 0-й странице
    call.data = "admin:users:0"
    await admin_users_list(call)


@router.callback_query(F.data == "admin:create")
async def admin_create(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа.", show_alert=True)
    with connect() as con:
        cur = con.execute("INSERT INTO contests(title,status) VALUES(?, 'draft')", ("Топ-10 авторов · выпуск",))
        cid = cur.lastrowid
    await edit_message(call, f"Создан черновик выпуска #{cid}. Добавь номинантов через меню выпуска.",
        reply_markup=keyboard([
            [("📋 Выпуски", "admin:contests")],
            [("⬅️ Админ-панель", "admin:home")],
        ]))
    await call.answer()


@router.callback_query(F.data == "admin:contests")
async def admin_contests(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа.", show_alert=True)
    with connect() as con:
        rows = con.execute("SELECT * FROM contests ORDER BY id DESC LIMIT 20").fetchall()
    buttons = [[(f"#{r['id']} · {r['title']} · {r['status']}", f"admin:contest:{r['id']}")] for r in rows]
    buttons.append([("⬅️ Админ-панель", "admin:home")])
    await edit_message(call, "📋 <b>Выпуски</b>", reply_markup=keyboard(buttons))
    await call.answer()


@router.callback_query(F.data.startswith("admin:contest:"))
async def admin_contest(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа.", show_alert=True)
    try:
        cid = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        return await call.answer("Некорректный выпуск.", show_alert=True)
    with connect() as con:
        contest = con.execute("SELECT * FROM contests WHERE id=?", (cid,)).fetchone()
        nominees = con.execute("""SELECT n.nominee_id, p.nickname FROM nominations n
          LEFT JOIN profiles p ON p.telegram_id=n.nominee_id
          WHERE n.contest_id=? ORDER BY p.nickname""", (cid,)).fetchall()
    if not contest:
        return await call.answer("Выпуск не найден.", show_alert=True)
    lines = [f"• {escape(n['nickname'] or str(n['nominee_id']))} (ID: <code>{n['nominee_id']}</code>)" for n in nominees]
    body = f"🏆 <b>Выпуск #{cid}</b>\nСтатус: {contest['status']}\nНоминанты:\n" + ("\n".join(lines) if lines else "пока нет")
    await edit_message(call, body, reply_markup=keyboard([
        [("➕ Добавить номинанта по ID", f"admin:add:{cid}")],
        [("🟢 Открыть голосование", f"admin:open:{cid}"), ("🏁 Закрыть", f"admin:close:{cid}")],
        [("📊 Результаты", f"admin:results:{cid}")],
        [("⬅️ К выпускам", "admin:contests")],
    ]))
    await call.answer()


@router.callback_query(F.data.startswith("admin:add:"))
async def admin_add_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа.", show_alert=True)
    try:
        cid = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        return await call.answer("Некорректный выпуск.", show_alert=True)
    await state.set_state(NomineeForm.telegram_id)
    await state.update_data(contest_id=cid)
    await edit_message(call, "Отправь Telegram ID участника. У него должна быть опубликованная анкета.", reply_markup=cancel_kb())
    await call.answer()


@router.message(NomineeForm.telegram_id)
async def admin_add_id(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        return await message.answer("Нужен числовой Telegram ID.", reply_markup=cancel_kb())
    uid = int(raw)
    data = await state.get_data()
    contest_id = data.get("contest_id")
    with connect() as con:
        p = con.execute("SELECT nickname, visibility FROM profiles WHERE telegram_id=?", (uid,)).fetchone()
        c = con.execute("SELECT status FROM contests WHERE id=?", (contest_id,)).fetchone()
        if not p or p["visibility"] != "published":
            return await message.answer("У участника нет опубликованной анкеты.")
        if not c or c["status"] != "draft":
            return await message.answer("Добавлять номинантов можно только в черновик.")
        try:
            con.execute("INSERT INTO nominations(contest_id,nominee_id) VALUES(?,?)", (contest_id, uid))
        except sqlite3.IntegrityError:
            return await message.answer("Этот участник уже добавлен.")
    await state.clear()
    await message.answer(f"Номинант «{escape(p['nickname'])}» добавлен.", reply_markup=keyboard([
        [("📋 Выпуски", "admin:contests")],
        [("🛡 Админ-панель", "admin:home")],
    ]))


@router.callback_query(F.data.startswith("admin:open:"))
async def admin_open(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа.", show_alert=True)
    try:
        cid = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        return await call.answer("Некорректный выпуск.", show_alert=True)
    with connect() as con:
        count = con.execute("SELECT COUNT(*) n FROM nominations WHERE contest_id=?", (cid,)).fetchone()["n"]
        if count < 2:
            return await call.answer("Добавь минимум двух номинантов.", show_alert=True)
        con.execute("UPDATE contests SET status='open', opens_at=CURRENT_TIMESTAMP WHERE id=? AND status='draft'", (cid,))
    await edit_message(call, "🟢 Голосование открыто.", reply_markup=keyboard([
        [("📋 Выпуски", "admin:contests")],
        [("🛡 Админ-панель", "admin:home")],
    ]))
    await call.answer()


@router.callback_query(F.data.startswith("admin:close:"))
async def admin_close(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа.", show_alert=True)
    try:
        cid = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        return await call.answer("Некорректный выпуск.", show_alert=True)
    with connect() as con:
        con.execute("UPDATE contests SET status='closed', closes_at=CURRENT_TIMESTAMP WHERE id=? AND status='open'", (cid,))
    await edit_message(call, "🏁 Голосование закрыто.", reply_markup=keyboard([
        [("📊 Результаты", f"admin:results:{cid}")],
        [("📋 Выпуски", "admin:contests")],
    ]))
    await call.answer()


@router.callback_query(F.data.startswith("admin:results:"))
async def admin_results(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа.", show_alert=True)
    try:
        cid = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        return await call.answer("Некорректный выпуск.", show_alert=True)
    with connect() as con:
        contest = con.execute("SELECT * FROM contests WHERE id=?", (cid,)).fetchone()
        rows = con.execute("""SELECT p.nickname, COUNT(v.id) total FROM nominations n
          JOIN profiles p ON p.telegram_id=n.nominee_id
          LEFT JOIN votes v ON v.contest_id=n.contest_id AND v.nominee_id=n.nominee_id
          WHERE n.contest_id=? GROUP BY n.nominee_id ORDER BY total DESC, p.nickname LIMIT 10""", (cid,)).fetchall()
    if not contest:
        return await call.answer("Выпуск не найден.", show_alert=True)
    lines = [f"{i}. {escape(r['nickname'])} — {r['total']} голосов" for i, r in enumerate(rows, 1)]
    await edit_message(call, f"📊 <b>Результаты: {escape(contest['title'])}</b>\n\n" + ("\n".join(lines) if lines else "Нет номинантов."),
        reply_markup=back(f"admin:contest:{cid}"))
    await call.answer()


async def _render_reports(call: CallbackQuery):
    with connect() as con:
        rows = con.execute("""
            SELECT r.*, 
                   p1.nickname AS reporter_nick, u1.username AS reporter_user,
                   p2.nickname AS target_nick, u2.username AS target_user
            FROM reports r
            LEFT JOIN profiles p1 ON p1.telegram_id = r.reporter_id
            LEFT JOIN users u1 ON u1.telegram_id = r.reporter_id
            LEFT JOIN profiles p2 ON p2.telegram_id = r.target_id
            LEFT JOIN users u2 ON u2.telegram_id = r.target_id
            WHERE r.status='open' 
            ORDER BY r.id DESC LIMIT 15
        """).fetchall()
    if not rows:
        return await edit_message(call, "🚩 Открытых жалоб нет.", reply_markup=back("admin:home"))
    lines = []
    buttons = []
    for r in rows:
        rep_nick = r['reporter_nick'] or "Без анкеты"
        rep_user = f"@{r['reporter_user']}" if r['reporter_user'] else "нет @"
        rep_id = r['reporter_id']
        tgt_nick = r['target_nick'] or "Без анкеты"
        tgt_user = f"@{r['target_user']}" if r['target_user'] else "нет @"
        tgt_id = r['target_id']
        lines.append(
            f"<b>#{r['id']}</b> · {r['reason']}\n"
            f"👤 <b>От:</b> {escape(rep_nick)} ({escape(rep_user)}, <code>{rep_id}</code>)\n"
            f"🎯 <b>На:</b> {escape(tgt_nick)} ({escape(tgt_user)}, <code>{tgt_id}</code>)"
        )
        buttons.append([(f"✅ Закрыть #{r['id']}", f"admin:report_close:{r['id']}")])
    buttons.append([("⬅️ Админ-панель", "admin:home")])
    text = "🚩 <b>Открытые жалобы</b>\n\n" + "\n\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n<i>...и другие (список обрезан)</i>"
    await edit_message(call, text, reply_markup=keyboard(buttons))


@router.callback_query(F.data == "admin:reports")
async def admin_reports(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа.", show_alert=True)
    await _render_reports(call)
    await call.answer()


@router.callback_query(F.data.startswith("admin:report_close:"))
async def admin_report_close(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа.", show_alert=True)
    try:
        rid = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        return await call.answer("Некорректная жалоба.", show_alert=True)
    with connect() as con:
        con.execute("UPDATE reports SET status='closed' WHERE id=?", (rid,))
    await call.answer("Жалоба закрыта.")
    await _render_reports(call)


# ---------------- THOUGHTS ----------------

def get_post_card(post_id: int, current_user_id: int):
    with connect() as con:
        post = con.execute("SELECT p.*, pr.nickname FROM posts p LEFT JOIN profiles pr ON p.author_id = pr.telegram_id WHERE p.id = ? AND p.status = 'published'", (post_id,)).fetchone()
        if not post:
            return None, None
        r_counts = {k: 0 for k in REACTIONS}
        for r in con.execute("SELECT reaction, COUNT(*) c FROM reactions WHERE post_id = ? GROUP BY reaction", (post_id,)):
            if r["reaction"] in r_counts:
                r_counts[r["reaction"]] = r["c"]
        my_react = con.execute("SELECT reaction FROM reactions WHERE post_id = ? AND user_id = ?", (post_id, current_user_id)).fetchone()
        my_react = my_react["reaction"] if my_react else None
    author = post["nickname"] or "Аноним"
    time_str = time_ago(post["created_at"])
    text = f"💭 <b>Мысли</b>\n\n<b>{escape(author)}</b> · {time_str}\n\n{escape(post['text'])}\n\n"
    react_str = "   ".join([f"{REACTIONS[k]} {r_counts[k]}" for k in REACTIONS])
    text += react_str
    react_btns = []
    for k, emoji in REACTIONS.items():
        prefix = "✅ " if my_react == k else ""
        react_btns.append((f"{prefix}{emoji}", f"th_react:{post_id}:{k}"))
    rows = [react_btns]
    with connect() as con:
        prev_post = con.execute("SELECT id FROM posts WHERE status='published' AND id > ? ORDER BY id ASC LIMIT 1", (post_id,)).fetchone()
        next_post = con.execute("SELECT id FROM posts WHERE status='published' AND id < ? ORDER BY id DESC LIMIT 1", (post_id,)).fetchone()
    nav_row = []
    if prev_post:
        nav_row.append(("◀️ Предыдущая", f"thoughts:view:{prev_post['id']}"))
    if next_post:
        nav_row.append(("Следующая ▶️", f"thoughts:view:{next_post['id']}"))
    if nav_row:
        rows.append(nav_row)
    rows.append([("👤 Профиль автора", f"directory:show:{post['author_id']}"), ("✍️ Написать", "thoughts:create")])
    if post["author_id"] == current_user_id or is_admin(current_user_id):
        rows.append([("🗑 Удалить", f"th_delete:{post_id}")])
    else:
        rows.append([("🚩 Пожаловаться", f"th_report:{post_id}")])
    rows.append([("⬅️ В меню", "home")])
    return text, keyboard(rows)


@router.callback_query(F.data == "thoughts:home")
async def thoughts_home(call: CallbackQuery):
    with connect() as con:
        latest = con.execute("SELECT id FROM posts WHERE status='published' ORDER BY id DESC LIMIT 1").fetchone()
    if not latest:
        await edit_message(call, "💭 <b>Мысли</b>\n\nПока нет мыслей. Будь первым!", reply_markup=keyboard([
            [("✍️ Написать мысль", "thoughts:create")],
            [("⬅️ В меню", "home")],
        ]))
    else:
        await thoughts_view(call, latest["id"])
    await call.answer()


@router.callback_query(F.data.startswith("thoughts:view:"))
async def thoughts_view_handler(call: CallbackQuery):
    post_id = int(call.data.split(":")[2])
    await thoughts_view(call, post_id)
    await call.answer()


async def thoughts_view(call: CallbackQuery, post_id: int):
    text, markup = get_post_card(post_id, call.from_user.id)
    if not text:
        with connect() as con:
            fallback = con.execute("SELECT id FROM posts WHERE status='published' ORDER BY id DESC LIMIT 1").fetchone()
        if fallback:
            text, markup = get_post_card(fallback["id"], call.from_user.id)
            if text:
                return await edit_message(call, "Эта мысль была удалена. Показываю актуальную ленту.", reply_markup=markup)
        return await edit_message(call, "💭 <b>Мысли</b>\n\nПока нет мыслей. Будь первым!", reply_markup=keyboard([
            [("✍️ Написать мысль", "thoughts:create")],
            [("⬅️ В меню", "home")],
        ]))
    await edit_message(call, text, reply_markup=markup)


@router.callback_query(F.data.startswith("th_react:"))
async def thought_react(call: CallbackQuery):
    _, post_id, reaction = call.data.split(":")
    post_id = int(post_id)
    user_id = call.from_user.id
    with connect() as con:
        existing = con.execute("SELECT reaction FROM reactions WHERE post_id=? AND user_id=?", (post_id, user_id)).fetchone()
        if existing:
            if existing["reaction"] == reaction:
                con.execute("DELETE FROM reactions WHERE post_id=? AND user_id=?", (post_id, user_id))
            else:
                con.execute("UPDATE reactions SET reaction=? WHERE post_id=? AND user_id=?", (reaction, post_id, user_id))
        else:
            con.execute("INSERT INTO reactions(post_id, user_id, reaction) VALUES(?,?,?)", (post_id, user_id, reaction))
    await thoughts_view(call, post_id)
    await call.answer("Реакция обновлена")


@router.callback_query(F.data.startswith("th_delete:"))
async def thought_delete(call: CallbackQuery):
    post_id = int(call.data.split(":")[1])
    with connect() as con:
        post = con.execute("SELECT author_id FROM posts WHERE id=?", (post_id,)).fetchone()
    if not post:
        return await call.answer("Уже удалено", show_alert=True)
    if post["author_id"] != call.from_user.id and not is_admin(call.from_user.id):
        return await call.answer("Ты не можешь удалить чужую мысль.", show_alert=True)
    await edit_message(call, "🗑 Удалить эту мысль?", reply_markup=keyboard([
        [("✅ Да, удалить", f"th_delete_yes:{post_id}")],
        [("⬅️ Отмена", f"thoughts:view:{post_id}")],
    ]))
    await call.answer()


@router.callback_query(F.data.startswith("th_delete_yes:"))
async def thought_delete_yes(call: CallbackQuery):
    post_id = int(call.data.split(":")[1])
    with connect() as con:
        post = con.execute("SELECT author_id FROM posts WHERE id=?", (post_id,)).fetchone()
        if not post:
            return await call.answer("Уже удалено", show_alert=True)
        if post["author_id"] != call.from_user.id and not is_admin(call.from_user.id):
            return await call.answer("Нет прав.", show_alert=True)
        con.execute("UPDATE posts SET status='deleted' WHERE id=?", (post_id,))
    with connect() as con:
        latest = con.execute("SELECT id FROM posts WHERE status='published' ORDER BY id DESC LIMIT 1").fetchone()
    if latest:
        await thoughts_view(call, latest["id"])
    else:
        await edit_message(call, "💭 <b>Мысли</b>\n\nПока нет мыслей. Будь первым!", reply_markup=keyboard([
            [("✍️ Написать мысль", "thoughts:create")],
            [("⬅️ В меню", "home")],
        ]))
    await call.answer("Удалено")


@router.callback_query(F.data == "thoughts:create")
async def thought_create(call: CallbackQuery, state: FSMContext):
    await state.set_state(ThoughtForm.text)
    await edit_message(call, "✍️ <b>Новая мысль</b>\n\nОтправь текст (до 500 символов):", reply_markup=cancel_kb())
    await call.answer()


@router.message(ThoughtForm.text)
async def thought_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        return await message.answer("Текст не может быть пустым. Попробуй снова:", reply_markup=cancel_kb())
    if len(text) > 500:
        return await message.answer(f"Слишком длинно ({len(text)} символов). Максимум 500.", reply_markup=cancel_kb())
    await state.update_data(text=text)
    await state.set_state(ThoughtForm.confirm)
    await message.answer(f"💭 <b>Предпросмотр</b>\n\n{escape(text)}\n\nВсё верно?", reply_markup=keyboard([
        [("✅ Опубликовать", "thoughts:publish")],
        [("✖️ Отмена", "cancel")],
    ]))


@router.callback_query(F.data == "thoughts:publish", ThoughtForm.confirm)
async def thought_publish(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = data.get("text")
    if not text:
        await state.clear()
        return await call.answer("Текст потерян. Начни заново.", show_alert=True)
    await state.clear()
    with connect() as con:
        cur = con.execute("INSERT INTO posts(author_id, text) VALUES(?,?)", (call.from_user.id, text))
        post_id = cur.lastrowid
    await call.answer("Опубликовано!")
    await thoughts_view(call, post_id)


# FIX: единственный обработчик жалоб на мысли (дубликат удалён)
@router.callback_query(F.data.startswith("th_report:"))
async def thought_report(call: CallbackQuery, state: FSMContext):
    post_id = int(call.data.split(":")[1])
    with connect() as con:
        post = con.execute("SELECT author_id FROM posts WHERE id=?", (post_id,)).fetchone()
        if not post:
            return await call.answer("Пост не найден", show_alert=True)
    await state.update_data(target_id=post["author_id"], report_type=f"Мысль #{post_id}")
    await state.set_state(ReportForm.reason)
    await edit_message(call, "🚩 <b>Жалоба на мысль</b>\n\nВыбери причину жалобы:", reply_markup=keyboard([
        [("🤬 Оскорбления / мат", "rep_reason:Оскорбления / мат")],
        [("🔞 18+ контент", "rep_reason:18+ контент")],
        [("🛑 Спам / реклама", "rep_reason:Спам / реклама")],
        [("🤡 Фейк / обман", "rep_reason:Фейк / обман")],
        [("✖️ Отмена", "cancel")],
    ]))
    await call.answer()


@router.callback_query(F.data == "admin:thoughts")
async def admin_thoughts(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    with connect() as con:
        posts = con.execute("SELECT id, text FROM posts WHERE status='published' ORDER BY id DESC LIMIT 10").fetchall()
    if not posts:
        return await edit_message(call, "Опубликованных мыслей нет.", reply_markup=back("admin:home"))
    rows = []
    for p in posts:
        short_text = p["text"][:25] + "..." if len(p["text"]) > 25 else p["text"]
        rows.append([(f"❌ Скрыть #{p['id']}: {short_text}", f"admin:th_hide:{p['id']}")])
    rows.append([("⬅️ Админ-панель", "admin:home")])
    await edit_message(call, "💭 <b>Управление мыслями</b>\nНажми, чтобы скрыть нарушение:", reply_markup=keyboard(rows))
    await call.answer()


@router.callback_query(F.data.startswith("admin:th_hide:"))
async def admin_thought_hide(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    post_id = int(call.data.split(":")[2])
    with connect() as con:
        con.execute("UPDATE posts SET status='hidden' WHERE id=?", (post_id,))
    await call.answer("Мысль скрыта")
    await admin_thoughts(call)


# ---------------- BROADCAST ----------------

@router.callback_query(F.data == "admin:broadcast")
async def broadcast_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа.", show_alert=True)
    with connect() as con:
        total = con.execute("SELECT COUNT(*) n FROM users WHERE status='active'").fetchone()["n"]
    await state.set_state(BroadcastForm.text)
    await edit_message(call, f"📢 <b>Рассылка от имени бота</b>\n\nПолучателей (активных): <b>{total}</b>\n\nОтправь текст рассылки (HTML поддерживается).\n<i>Важно: получатели увидят, что пишет бот.</i>", reply_markup=cancel_kb())
    await call.answer()


@router.message(BroadcastForm.text)
async def broadcast_preview(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or message.html_text or "").strip()
    if not text:
        return await message.answer("Пустой текст. Отправь текст сообщения или нажми «Отмена».", reply_markup=cancel_kb())
    if len(text) > 4096:
        return await message.answer("Слишком длинный текст (лимит 4096). Сократи.", reply_markup=cancel_kb())
    await state.update_data(broadcast_text=text)
    await state.set_state(BroadcastForm.confirm)
    await message.answer(f"📢 <b>Предпросмотр рассылки</b>\n\n{text}\n\n<i>Это увидят получатели.</i>", reply_markup=keyboard([
        [("✅ Отправить всем", "admin:broadcast_go")],
        [("✏️ Изменить текст", "admin:broadcast")],
        [("✖️ Отмена", "cancel")],
    ]))


@router.callback_query(F.data == "admin:broadcast_go")
async def broadcast_go(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer("Нет доступа.", show_alert=True)
    data = await state.get_data()
    text = data.get("broadcast_text")
    if not text:
        await state.clear()
        return await call.answer("Текст потерян. Начни заново.", show_alert=True)
    await state.clear()
    await call.answer("Рассылка запущена…")
    with connect() as con:
        targets = [r["telegram_id"] for r in con.execute("SELECT telegram_id FROM users WHERE status='active'").fetchall()]
    bot = call.bot
    sent = blocked = failed = 0
    for uid in targets:
        try:
            await bot.send_message(uid, text)
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(uid, text)
                sent += 1
            except Exception as ex:
                failed += 1
                logging.warning("broadcast retry failed for %s: %s", uid, ex)
        except Exception as ex:
            failed += 1
            logging.warning("broadcast failed for %s: %s", uid, ex)
        await asyncio.sleep(0.05)
    await call.message.answer(
        f"📢 <b>Рассылка завершена</b>\n\n"
        f"Всего получателей: {len(targets)}\n"
        f"✅ Доставлено: {sent}\n"
        f"🚫 Заблокировали бота: {blocked}\n"
        f"⚠️ Ошибок: {failed}",
        reply_markup=keyboard([[("⬅️ Админ-панель", "admin:home")]]),
    )


# ---------------- ENTRYPOINT ----------------

async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

