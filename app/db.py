import json
import os
from datetime import date, time

import asyncpg

pool: asyncpg.Pool | None = None

# Уверенность (1-5) -> через сколько дней повторить вопрос
INTERVALS = {1: 1, 2: 1, 3: 3, 4: 7, 5: 14}
PRIORITY_XP = {1: 10, 2: 15, 3: 20}

# Мини-миграции: выполняются при каждом старте, повторный запуск безопасен.
MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_goal SMALLINT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_time TIME",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_notified DATE",
]


async def init_pool() -> None:
    global pool
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    async with pool.acquire() as conn:
        for sql in MIGRATIONS:
            await conn.execute(sql)


# ---------- пользователи и настройки ----------

async def ensure_user(user_id: int) -> None:
    await pool.execute(
        "INSERT INTO users (telegram_id) VALUES ($1) ON CONFLICT DO NOTHING",
        user_id,
    )


async def get_user(user_id: int):
    return await pool.fetchrow("SELECT * FROM users WHERE telegram_id = $1", user_id)


async def set_goal(user_id: int, goal: int) -> None:
    await pool.execute("UPDATE users SET daily_goal = $2 WHERE telegram_id = $1", user_id, goal)


async def set_notify_time(user_id: int, t: time | None) -> None:
    await pool.execute(
        "UPDATE users SET notify_time = $2 WHERE telegram_id = $1", user_id, t
    )


async def users_to_notify(now_time: time, today: date):
    """Кому пора отправить напоминание (время наступило, сегодня ещё не слали)."""
    return await pool.fetch(
        """
        SELECT telegram_id, daily_goal, notify_time
        FROM users
        WHERE notify_time IS NOT NULL AND daily_goal IS NOT NULL
          AND notify_time <= $1
          AND (last_notified IS NULL OR last_notified < $2)
        """,
        now_time, today,
    )


async def mark_notified(user_id: int, today: date) -> None:
    await pool.execute(
        "UPDATE users SET last_notified = $2 WHERE telegram_id = $1", user_id, today
    )


async def answered_today(user_id: int, tz: str) -> int:
    """Сколько разных вопросов проверено сегодня (по часовому поясу tz)."""
    return await pool.fetchval(
        """
        SELECT count(DISTINCT question_id) FROM attempts
        WHERE user_id = $1 AND score IS NOT NULL
          AND (created_at AT TIME ZONE $2::text)::date = (now() AT TIME ZONE $2::text)::date
        """,
        user_id, tz,
    )


# ---------- темы и вопросы ----------

async def topic_stats(user_id: int):
    """По каждой теме: всего вопросов, отвечено, пора повторить."""
    return await pool.fetch(
        """
        SELECT t.id, t.name,
               count(q.id)                                              AS total,
               count(p.question_id)                                     AS answered,
               count(p.question_id) FILTER (WHERE p.next_review_at <= now()) AS due
        FROM topics t
        LEFT JOIN questions q ON q.topic_id = t.id
        LEFT JOIN user_progress p ON p.question_id = q.id AND p.user_id = $1
        GROUP BY t.id, t.name
        ORDER BY t.name
        """,
        user_id,
    )


async def get_topic_name(topic_id: int) -> str:
    return await pool.fetchval("SELECT name FROM topics WHERE id = $1", topic_id)


async def get_question(question_id: int):
    return await pool.fetchrow(
        "SELECT id, topic_id, text, priority FROM questions WHERE id = $1",
        question_id,
    )


async def has_progress(user_id: int, question_id: int) -> bool:
    return await pool.fetchval(
        "SELECT EXISTS (SELECT 1 FROM user_progress WHERE user_id = $1 AND question_id = $2)",
        user_id, question_id,
    )


async def count_questions(topic_id: int) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM questions WHERE topic_id = $1", topic_id
    )


async def list_questions(user_id: int, topic_id: int, limit: int, offset: int):
    """Вопросы темы в порядке из базы; confidence NULL = ещё не отвечала."""
    return await pool.fetch(
        """
        SELECT q.id, q.text, p.confidence
        FROM questions q
        LEFT JOIN user_progress p
               ON p.question_id = q.id AND p.user_id = $1
        WHERE q.topic_id = $2
        ORDER BY q.id
        LIMIT $3 OFFSET $4
        """,
        user_id, topic_id, limit, offset,
    )


async def count_review_questions(user_id: int, topic_id: int) -> int:
    return await pool.fetchval(
        """
        SELECT count(*) FROM questions q
        JOIN user_progress p ON p.question_id = q.id AND p.user_id = $1
        WHERE q.topic_id = $2
        """,
        user_id, topic_id,
    )


async def list_review_questions(user_id: int, topic_id: int, limit: int, offset: int):
    """Уже отвечённые вопросы темы; due = пора повторить."""
    return await pool.fetch(
        """
        SELECT q.id, q.text, p.confidence, (p.next_review_at <= now()) AS due
        FROM questions q
        JOIN user_progress p ON p.question_id = q.id AND p.user_id = $1
        WHERE q.topic_id = $2
        ORDER BY q.id
        LIMIT $3 OFFSET $4
        """,
        user_id, topic_id, limit, offset,
    )


# ---------- выбор следующего вопроса ----------

async def random_topic() -> int | None:
    return await pool.fetchval(
        """
        SELECT t.id FROM topics t
        WHERE EXISTS (SELECT 1 FROM questions q WHERE q.topic_id = t.id)
        ORDER BY random() LIMIT 1
        """
    )


async def random_study_question(user_id: int, topic_id: int, exclude: int | None = None):
    """Случайный вопрос темы. Сначала среди ещё не отвеченных, потом среди всех."""
    sql = """
        SELECT q.id FROM questions q
        LEFT JOIN user_progress p ON p.question_id = q.id AND p.user_id = $1
        WHERE q.topic_id = $2
          AND ($3::int IS NULL OR q.id <> $3)
          AND (NOT $4::bool OR p.question_id IS NULL)
        ORDER BY random() LIMIT 1
    """
    for new_only, ex in [(True, exclude), (False, exclude), (False, None)]:
        qid = await pool.fetchval(sql, user_id, topic_id, ex, new_only)
        if qid is not None:
            return qid
    return None


async def random_review_question(user_id: int, topic_id: int | None, exclude: int | None = None):
    """Случайный из уже отвеченных. Сначала те, которым пора повториться."""
    sql = """
        SELECT q.id FROM questions q
        JOIN user_progress p ON p.question_id = q.id AND p.user_id = $1
        WHERE ($2::int IS NULL OR q.topic_id = $2)
          AND ($3::int IS NULL OR q.id <> $3)
          AND (NOT $4::bool OR p.next_review_at <= now())
        ORDER BY random() LIMIT 1
    """
    for due_only, ex in [(True, exclude), (False, exclude), (False, None)]:
        qid = await pool.fetchval(sql, user_id, topic_id, ex, due_only)
        if qid is not None:
            return qid
    return None


async def next_in_order(topic_id: int, after_id: int):
    return await pool.fetchval(
        "SELECT id FROM questions WHERE topic_id = $1 AND id > $2 ORDER BY id LIMIT 1",
        topic_id, after_id,
    )


async def next_review_in_order(user_id: int, topic_id: int, after_id: int):
    return await pool.fetchval(
        """
        SELECT q.id FROM questions q
        JOIN user_progress p ON p.question_id = q.id AND p.user_id = $1
        WHERE q.topic_id = $2 AND q.id > $3
        ORDER BY q.id LIMIT 1
        """,
        user_id, topic_id, after_id,
    )


# ---------- тезисы, ответы, прогресс ----------

async def get_key_points(question_id: int) -> list[str] | None:
    text = await pool.fetchval(
        "SELECT key_points FROM questions WHERE id = $1", question_id
    )
    points = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return points or None


async def set_key_points(question_id: int, points: list[str]) -> None:
    await pool.execute(
        "UPDATE questions SET key_points = $2 WHERE id = $1",
        question_id, "\n".join(points),
    )


async def save_attempt(
    user_id: int, question_id: int, answer: str,
    ai_feedback: dict | None = None, score: int | None = None,
) -> None:
    await pool.execute(
        """
        INSERT INTO attempts (user_id, question_id, answer_text, ai_feedback, score)
        VALUES ($1, $2, $3, $4::jsonb, $5)
        """,
        user_id, question_id, answer,
        json.dumps(ai_feedback, ensure_ascii=False) if ai_feedback else None,
        score,
    )


async def save_rating(user_id: int, question_id: int, confidence: int, priority: int, tz: str):
    days = INTERVALS[confidence]
    await pool.execute(
        """
        INSERT INTO user_progress
            (user_id, question_id, confidence, interval_days, next_review_at)
        VALUES ($1, $2, $3, $4, now() + make_interval(days => $4))
        ON CONFLICT (user_id, question_id) DO UPDATE SET
            confidence     = EXCLUDED.confidence,
            interval_days  = EXCLUDED.interval_days,
            next_review_at = EXCLUDED.next_review_at,
            attempts_count = user_progress.attempts_count + 1
        """,
        user_id, question_id, confidence, days,
    )
    xp_gain = PRIORITY_XP.get(priority, 10) * confidence // 3 or 1
    row = await pool.fetchrow(
        """
        UPDATE users SET
            xp = xp + $2,
            streak = CASE
                WHEN last_active = (now() AT TIME ZONE $3::text)::date     THEN streak
                WHEN last_active = (now() AT TIME ZONE $3::text)::date - 1 THEN streak + 1
                ELSE 1
            END,
            last_active = (now() AT TIME ZONE $3::text)::date
        WHERE telegram_id = $1
        RETURNING xp, streak
        """,
        user_id, xp_gain, tz,
    )
    return days, xp_gain, row["xp"], row["streak"]