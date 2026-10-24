"""Один раз сгенерировать key_points для всех вопросов, у которых их ещё нет.

Запуск:
  docker compose run --rm bot python -m scripts.generate_all_keypoints
"""
import asyncio
import logging
import os

import asyncpg

from app import llm

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch(
            """
            SELECT id, text FROM questions
            WHERE key_points IS NULL OR key_points = ''
            ORDER BY id
            """
        )
        log.info("Вопросов без тезисов: %s", len(rows))

        for i, row in enumerate(rows, start=1):
            qid, text = row["id"], row["text"]
            log.info("[%s/%s] id=%s | %s", i, len(rows), qid, text[:80])

            try:
                points = await llm.generate_key_points(text)
                await conn.execute(
                    "UPDATE questions SET key_points = $2 WHERE id = $1",
                    qid, "\n".join(points),
                )
                log.info("  → %s тезисов", len(points))
            except Exception:
                log.exception("  не удалось сгенерировать для id=%s", qid)
                # можно добавить sleep, если упрёшься в rate limit
                await asyncio.sleep(1)

    finally:
        await conn.close()

    log.info("Готово")


if __name__ == "__main__":
    asyncio.run(main())