import asyncio
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app import db, llm

log = logging.getLogger(__name__)
dp = Dispatcher()

TZ_NAME = os.getenv("BOT_TZ", "Europe/Moscow")
TZ = ZoneInfo(TZ_NAME)

PAGE_SIZE = 8
GOALS = [3, 5, 10, 15]
TIMES = ["09:00", "12:00", "15:00", "18:00", "20:00", "21:00"]
STATUS_ICON = {"covered": "✅", "partial": "🟡", "missing": "⬜", "contradicted": "❌"}

GREETING = (
    "Привет! Я твой помощник по подготовке к собесу. "
    "Ты можешь выбрать конкретную тему для повторения или включить случайный режим."
)


class Quiz(StatesGroup):
    waiting_answer = State()


# ======================= клавиатуры и тексты =======================

def markup(*rows) -> InlineKeyboardMarkup:
    """markup([(текст, data), (текст, data)], [(текст, data)]) - по строке на список."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def chunk(items: list, n: int) -> list[list]:
    return [items[i:i + n] for i in range(0, len(items), n)]


def conf_icon(confidence: int | None) -> str:
    if confidence is None:
        return "⬜"
    if confidence >= 4:
        return "🟢"
    if confidence >= 2:
        return "🟡"
    return "🔴"


def short(text: str, limit: int = 42) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def menu_markup() -> InlineKeyboardMarkup:
    return markup(
        [("📂 Выбрать тему", "m:topics")],
        [("🎲 Случайный режим", "m:random")],
        [("🔁 Повторение", "m:review")],
        [("⚙️ Настройки", "m:settings")],
    )


def goal_markup(prefix: str) -> InlineKeyboardMarkup:
    return markup([(str(n), f"{prefix}:{n}") for n in GOALS])


def time_markup(prefix: str) -> InlineKeyboardMarkup:
    rows = chunk([(t, f"{prefix}:{t}") for t in TIMES], 3)
    rows.append([("🔕 Без напоминаний", f"{prefix}:off")])
    return markup(*rows)


def list_markup(rows, page: int, total: int, topic_id: int,
                nav_prefix: str, pick_prefix: str, back: tuple[str, str]) -> InlineKeyboardMarkup:
    """Список вопросов постранично: кнопка на вопрос + стрелки + «назад»."""
    body = []
    for i, r in enumerate(rows, start=page * PAGE_SIZE + 1):
        mark = conf_icon(r["confidence"])
        if r.get("due"):
            mark += "🔔"
        body.append([(f"{mark} {i}. {short(r['text'])}", f"{pick_prefix}:{r['id']}")])
    pages = max(1, -(-total // PAGE_SIZE))
    nav = []
    if page > 0:
        nav.append(("◀", f"{nav_prefix}:{topic_id}:{page - 1}"))
    nav.append((f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(("▶", f"{nav_prefix}:{topic_id}:{page + 1}"))
    return markup(*body, nav, [back])


def result_markup() -> InlineKeyboardMarkup:
    return markup([("Следующий вопрос ➡️", "next")], [("🏠 В меню", "m:home")])


def format_result(result: dict, days: int, gain: int, xp: int, streak: int) -> str:
    lines = [
        f"Оценка: {result['score']:g}/10",
        "✅ раскрыто  🟡 частично  ⬜ нет  ❌ неверно",
        "",
    ]
    for it in result["items"]:
        lines.append(f"{STATUS_ICON[it['status']]} {it['thesis']}")
        if it["quote"]:
            lines.append(f"    «{it['quote']}»")
        if it["note"]:
            lines.append(f"    ⚠️ {it['note']}")
    lines += [
        "",
        f"+{gain} XP (всего {xp}), серия: {streak} дн.",
        f"Вернусь к этому вопросу через {days} дн.",
    ]
    return "\n".join(lines)[:3900]

async def progress_line(user_id: int) -> str:
    user = await db.get_user(user_id)
    if not user or not user["daily_goal"]:
        return ""
    done = await db.answered_today(user_id, TZ_NAME)
    goal = user["daily_goal"]
    line = f"📅 Сегодня ты ответила на {done} из {goal} запланированных вопросов."
    if done >= goal:
        line += " 🎉 Цель на сегодня выполнена!"
    return line

async def menu_text(user_id: int, prefix: str = "") -> str:
    parts = [prefix + GREETING if prefix else GREETING, await progress_line(user_id)]
    return "\n\n".join(p for p in parts if p)


async def show(message: Message, text: str, reply_markup=None) -> None:
    """Меняем текущее сообщение; если нельзя, отправляем новое."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await message.answer(text, reply_markup=reply_markup)


# ======================= вопросы =======================

async def start_question(message: Message, user_id: int, qid: int,
                         state: FSMContext, mode: str) -> None:
    q = await db.get_question(qid)
    topic = await db.get_topic_name(q["topic_id"])
    await state.set_state(Quiz.waiting_answer)
    await state.update_data(question_id=q["id"], topic_id=q["topic_id"], mode=mode)
    label = "🔄 Переответить" if await db.has_progress(user_id, q["id"]) else "🆕 Новый вопрос"
    await message.answer(
        f"{label} · {topic}\n\n{q['text']}\n\nНапиши ответ своими словами.",
        reply_markup=markup([("⏭ Пропустить", "next"), ("🏠 Меню", "m:home")]),
    )


async def ensure_key_points(q) -> list[str]:
    """Тезисы берём из базы; если их нет, генерируем один раз и сохраняем."""
    points = await db.get_key_points(q["id"])
    if points:
        return points
    topic = await db.get_topic_name(q["topic_id"])
    points = await llm.generate_key_points(q["text"], topic)
    await db.set_key_points(q["id"], points)
    return points


async def nothing_found(message: Message) -> None:
    await message.answer("Подходящих вопросов нет.", reply_markup=markup([("🏠 В меню", "m:home")]))


# ======================= онбординг и настройки =======================

class Setup(StatesGroup):
    waiting_goal = State()
    waiting_time = State()


def goal_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return markup(
        [("👀 Я просто посмотреть", f"{prefix}:skip")],
        [("∞ Без ограничений", f"{prefix}:unlimited")],
    )


def time_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return markup(
        [("🔕 Без напоминаний", f"{prefix}:off")],
    )


@dp.message(CommandStart())
@dp.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    uid = message.from_user.id
    await db.ensure_user(uid)
    user = await db.get_user(uid)

    if user["daily_goal"] is None:
        await state.set_state(Setup.waiting_goal)
        await message.answer(
            f"{GREETING}\n\n"
            "Сколько вопросов хочешь проходить в день?\n"
            "Просто напиши число (например 7) или выбери вариант:",
            reply_markup=goal_keyboard("ob_goal"),
        )
    else:
        await message.answer(await menu_text(uid), reply_markup=menu_markup())


@dp.message(Setup.waiting_goal, F.text)
async def setup_goal_text(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= 100):
        await message.answer("Напиши число от 1 до 100, или нажми кнопку.")
        return
    await db.set_goal(message.from_user.id, int(text))
    await state.set_state(Setup.waiting_time)
    await message.answer(
        "Во сколько напоминать о занятиях?\n"
        "Напиши время в формате 21:30 или нажми кнопку:",
        reply_markup=time_keyboard("ob_time"),
    )


@dp.callback_query(F.data.startswith("ob_goal:"))
async def ob_goal_btn(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    value = cb.data.split(":")[1]
    uid = cb.from_user.id

    if value == "skip":
        await db.set_goal(uid, 0)          # 0 = просто смотреть
        await db.set_notify_time(uid, None)
        await state.clear()
        await show(cb.message, await menu_text(uid, "Хорошо, просто смотри. "), menu_markup())
        return

    if value == "unlimited":
        await db.set_goal(uid, 0)
    else:
        await db.set_goal(uid, int(value))

    await state.set_state(Setup.waiting_time)
    await show(
        cb.message,
        "Во сколько напоминать о занятиях?\n"
        "Напиши время в формате 21:30 или нажми кнопку:",
        time_keyboard("ob_time"),
    )


@dp.message(Setup.waiting_time, F.text)
async def setup_time_text(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    try:
        t = datetime.strptime(text, "%H:%M").time()
    except ValueError:
        await message.answer("Напиши время в формате ЧЧ:ММ, например 21:30")
        return

    await db.set_notify_time(message.from_user.id, t)
    await state.clear()
    await message.answer(await menu_text(message.from_user.id, "Готово! "), reply_markup=menu_markup())


@dp.callback_query(F.data.startswith("ob_time:"))
async def ob_time_btn(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await db.set_notify_time(cb.from_user.id, None)
    await state.clear()
    await show(cb.message, await menu_text(cb.from_user.id, "Готово! "), menu_markup())


# ---------- настройки (можно менять в любой момент) ----------

@dp.callback_query(F.data == "m:settings")
@dp.message(Command("settings"))
async def open_settings(event: Message | CallbackQuery, state: FSMContext) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer()
        message = event.message
        uid = event.from_user.id
    else:
        message = event
        uid = event.from_user.id
        await state.clear()

    await db.ensure_user(uid)
    user = await db.get_user(uid)
    goal = user["daily_goal"]
    goal_text = "без ограничений / просто смотрю" if not goal else str(goal)
    when = user["notify_time"].strftime("%H:%M") if user["notify_time"] else "выключены"

    text = f"⚙️ Настройки\n\nВопросов в день: {goal_text}\nНапоминания: {when} ({TZ_NAME})"
    kb = markup(
        [("🎯 Изменить цель", "m:st_goal")],
        [("⏰ Изменить время", "m:st_time")],
        [("◀ В меню", "m:home")],
    )
    await show(message, text, kb)


@dp.callback_query(F.data == "m:st_goal")
async def st_goal_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(Setup.waiting_goal)
    await show(
        cb.message,
        "Сколько вопросов в день хочешь решать?\nНапиши число или выбери вариант:",
        goal_keyboard("st_goal"),
    )


@dp.callback_query(F.data.startswith("st_goal:"))
async def st_goal_btn(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    value = cb.data.split(":")[1]
    uid = cb.from_user.id

    if value in ("skip", "unlimited"):
        await db.set_goal(uid, 0)
    else:
        await db.set_goal(uid, int(value))

    await state.clear()
    await open_settings(cb, state)


@dp.callback_query(F.data == "m:st_time")
async def st_time_start(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(Setup.waiting_time)
    await show(
        cb.message,
        "Во сколько напоминать?\nНапиши время (21:30) или нажми кнопку:",
        time_keyboard("st_time"),
    )


@dp.callback_query(F.data.startswith("st_time:"))
async def st_time_btn(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await db.set_notify_time(cb.from_user.id, None)
    await state.clear()
    await open_settings(cb, state)


# ======================= выбрать тему =======================

@dp.callback_query(F.data == "m:topics")
async def cb_topics(cb: CallbackQuery) -> None:
    await cb.answer()
    stats = await db.topic_stats(cb.from_user.id)
    rows = chunk([(f"{t['name']} · {t['answered']}/{t['total']}", f"t:{t['id']}") for t in stats], 2)
    rows.append([("◀ В меню", "m:home")])
    await show(cb.message, "📂 Выбери тему (отвечено / всего):", markup(*rows))


@dp.callback_query(F.data.startswith("t:"))
async def cb_topic(cb: CallbackQuery) -> None:
    await cb.answer()
    topic_id = int(cb.data.split(":")[1])
    stat = next(t for t in await db.topic_stats(cb.from_user.id) if t["id"] == topic_id)
    await show(
        cb.message,
        f"📂 {stat['name']}\nОтвечено {stat['answered']} из {stat['total']}. Как будем заниматься?",
        markup(
            [("🎲 Случайный вопрос", f"tr:{topic_id}")],
            [("📋 По порядку", f"tl:{topic_id}:0")],
            [("◀ К темам", "m:topics")],
        ),
    )


@dp.callback_query(F.data.startswith("tr:"))
async def cb_topic_random(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    uid = cb.from_user.id
    qid = await db.random_study_question(uid, int(cb.data.split(":")[1]))
    if qid is None:
        return await nothing_found(cb.message)
    await start_question(cb.message, uid, qid, state, "rand_topic")


@dp.callback_query(F.data.startswith("tl:"))
async def cb_topic_list(cb: CallbackQuery) -> None:
    await cb.answer()
    _, topic_id, page = cb.data.split(":")
    topic_id, page = int(topic_id), int(page)
    uid = cb.from_user.id
    total = await db.count_questions(topic_id)
    rows = await db.list_questions(uid, topic_id, PAGE_SIZE, page * PAGE_SIZE)
    name = await db.get_topic_name(topic_id)
    await show(
        cb.message,
        f"📋 {name}: выбери вопрос\n🟢 хорошо  🟡 так себе  🔴 слабо  ⬜ ещё не отвечала",
        list_markup(rows, page, total, topic_id, "tl", "pk", ("◀ К теме", f"t:{topic_id}")),
    )


@dp.callback_query(F.data.startswith("pk:"))
async def cb_pick(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    qid = int(cb.data.split(":")[1])
    if await db.get_question(qid) is None:
        return await nothing_found(cb.message)
    await start_question(cb.message, cb.from_user.id, qid, state, "seq")


# ======================= случайный режим =======================

@dp.callback_query(F.data == "m:random")
async def cb_random(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    uid = cb.from_user.id
    topic_id = await db.random_topic()
    qid = await db.random_study_question(uid, topic_id) if topic_id else None
    if qid is None:
        return await nothing_found(cb.message)
    await start_question(cb.message, uid, qid, state, "rand_all")


# ======================= повторение =======================

@dp.callback_query(F.data == "m:review")
async def cb_review(cb: CallbackQuery) -> None:
    await cb.answer()
    stats = await db.topic_stats(cb.from_user.id)
    answered = sum(t["answered"] for t in stats)
    if answered == 0:
        await show(
            cb.message,
            "🔁 Повторение\n\nПока нечего повторять: ответь хотя бы на один вопрос.",
            markup([("◀ В меню", "m:home")]),
        )
        return
    due = sum(t["due"] for t in stats)
    rows = [[("🎲 Случайное повторение", "rr")]]
    rows += chunk(
        [(f"{t['name']} · 🔔{t['due']}/{t['answered']}", f"r:{t['id']}") for t in stats if t["answered"]],
        2,
    )
    rows.append([("◀ В меню", "m:home")])
    await show(
        cb.message,
        f"🔁 Повторение\nПора повторить сейчас: {due}\n🔔 пора повторить / всего отвечено",
        markup(*rows),
    )


@dp.callback_query(F.data == "rr")
async def cb_review_random(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    qid = await db.random_review_question(cb.from_user.id, None)
    if qid is None:
        return await nothing_found(cb.message)
    await start_question(cb.message, cb.from_user.id, qid, state, "rev_all")


@dp.callback_query(F.data.startswith("r:"))
async def cb_review_topic(cb: CallbackQuery) -> None:
    await cb.answer()
    topic_id = int(cb.data.split(":")[1])
    stat = next(t for t in await db.topic_stats(cb.from_user.id) if t["id"] == topic_id)
    await show(
        cb.message,
        f"🔁 {stat['name']}\nОтвечено {stat['answered']}, пора повторить {stat['due']}.",
        markup(
            [("🎲 Случайный из повторения", f"rtr:{topic_id}")],
            [("📋 По порядку", f"rl:{topic_id}:0")],
            [("◀ К повторению", "m:review")],
        ),
    )


@dp.callback_query(F.data.startswith("rtr:"))
async def cb_review_topic_random(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    uid = cb.from_user.id
    qid = await db.random_review_question(uid, int(cb.data.split(":")[1]))
    if qid is None:
        return await nothing_found(cb.message)
    await start_question(cb.message, uid, qid, state, "rev_topic")


@dp.callback_query(F.data.startswith("rl:"))
async def cb_review_list(cb: CallbackQuery) -> None:
    await cb.answer()
    _, topic_id, page = cb.data.split(":")
    topic_id, page = int(topic_id), int(page)
    uid = cb.from_user.id
    total = await db.count_review_questions(uid, topic_id)
    rows = await db.list_review_questions(uid, topic_id, PAGE_SIZE, page * PAGE_SIZE)
    name = await db.get_topic_name(topic_id)
    await show(
        cb.message,
        f"🔁 {name}: выбери вопрос\n🔔 пора повторить  🟢 хорошо  🟡 так себе  🔴 слабо",
        list_markup(rows, page, total, topic_id, "rl", "pr", ("◀ К теме", f"r:{topic_id}")),
    )


@dp.callback_query(F.data.startswith("pr:"))
async def cb_review_pick(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    qid = int(cb.data.split(":")[1])
    if await db.get_question(qid) is None:
        return await nothing_found(cb.message)
    await start_question(cb.message, cb.from_user.id, qid, state, "rev_seq")


# ======================= ответ и «следующий» =======================

@dp.message(Quiz.waiting_answer, F.text)
async def got_answer(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    uid = message.from_user.id
    q = await db.get_question(data["question_id"])
    wait = await message.answer("⏳ Проверяю ответ…")

    try:
        theses = await ensure_key_points(q)
        result = await llm.evaluate(q["text"], theses, message.text)
    except Exception:
        log.exception("ИИ-проверка не удалась")
        await db.save_attempt(uid, q["id"], message.text)
        await state.set_state(None)
        await wait.edit_text(
            "ИИ сейчас недоступен: ответ сохранён, но не оценён. Попробуй позже.",
            reply_markup=result_markup(),
        )
        return

    log.info("usage=%s score=%s", result.get("usage"), result["score"])
    await db.save_attempt(uid, q["id"], message.text, result, round(result["score"]))
    confidence = llm.confidence_from_score(result["score"])
    days, gain, xp, streak = await db.save_rating(uid, q["id"], confidence, q["priority"], TZ_NAME)
    await state.set_state(None)  # mode и topic_id остаются для кнопки «Следующий»
    text = format_result(result, days, gain, xp, streak)
    progress = await progress_line(uid)
    await wait.edit_text(f"{text}\n\n{progress}" if progress else text, reply_markup=result_markup())


@dp.callback_query(F.data == "next")
async def cb_next(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    uid = cb.from_user.id
    data = await state.get_data()
    mode, topic_id, current = data.get("mode"), data.get("topic_id"), data.get("question_id")
    if mode is None:
        await cb.message.answer("Открой меню: /start")
        return

    qid = None
    if mode == "rand_all":
        topic = await db.random_topic()
        qid = await db.random_study_question(uid, topic, current) if topic else None
    elif mode == "rand_topic":
        qid = await db.random_study_question(uid, topic_id, current)
    elif mode == "seq":
        qid = await db.next_in_order(topic_id, current)
    elif mode == "rev_all":
        qid = await db.random_review_question(uid, None, current)
    elif mode == "rev_topic":
        qid = await db.random_review_question(uid, topic_id, current)
    elif mode == "rev_seq":
        qid = await db.next_review_in_order(uid, topic_id, current)

    if qid is None:
        await cb.message.answer(
            "Дальше вопросов в этом режиме нет.", reply_markup=markup([("🏠 В меню", "m:home")])
        )
        return
    await start_question(cb.message, uid, qid, state, mode)


# ======================= напоминания =======================

async def send_due_reminders(bot: Bot) -> None:
    now = datetime.now(TZ)
    for r in await db.users_to_notify(now.time().replace(second=0, microsecond=0), now.date()):
        uid = r["telegram_id"]
        await db.mark_notified(uid, now.date())  # не больше одного раза в день
        scheduled = datetime.combine(now.date(), r["notify_time"], tzinfo=TZ)
        if now - scheduled > timedelta(hours=3):
            continue  # бот был выключен и проспал время: не беспокоим поздно вечером
        done = await db.answered_today(uid, TZ_NAME)
        goal = r["daily_goal"]
        if done >= goal:
            continue
        try:
            await bot.send_message(
                uid,
                f"⏰ Пора позаниматься!\n📅 Сегодня ты ответила на {done} из {goal} запланированных вопросов.",
                reply_markup=menu_markup(),
            )
        except Exception:
            log.warning("не удалось отправить напоминание %s", uid, exc_info=True)


async def reminder_loop(bot: Bot) -> None:
    while True:
        try:
            await send_due_reminders(bot)
        except Exception:
            log.exception("ошибка в цикле напоминаний")
        await asyncio.sleep(60)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await db.init_pool()
    session = AiohttpSession(proxy=os.getenv("TELEGRAM_PROXY") or None)
    bot = Bot(os.environ["BOT_TOKEN"], session=session)
    await bot.set_my_commands([
        BotCommand(command="start", description="Меню"),
        BotCommand(command="settings", description="Цель и напоминания"),
    ])
    reminders = asyncio.create_task(reminder_loop(bot))  # noqa: F841 (держим ссылку)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())