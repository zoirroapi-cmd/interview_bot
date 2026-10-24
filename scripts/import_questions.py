"""Импорт вопросов из CSV (экспорт листа Google Sheets).

Запуск:
  docker compose run --rm bot python -m scripts.import_questions /data/questions.csv
"""
import asyncio
import csv
import os
import re
import sys

import asyncpg

PRIORITY_RE = re.compile(r"Приоритет\s*(\d)")


def parse_priority(row: dict) -> int:
    # "Приоритет 1 — база" лежит в одной из колонок строки
    for value in row.values():
        m = PRIORITY_RE.search(value or "")
        if m:
            return int(m.group(1))
    return 2


def read_rows(path: str) -> list[tuple[str, str, int]]:
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            topic = (row.get("Тема") or "").strip()
            question = (row.get("Вопрос") or "").strip()
            if topic and question:
                rows.append((topic, question, parse_priority(row)))
    return rows


async def main(path: str) -> None:
    rows = read_rows(path)
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        for topic, question, priority in rows:
            topic_id = await conn.fetchval(
                """INSERT INTO topics (name) VALUES ($1)
                   ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name
                   RETURNING id""",
                topic,
            )
            await conn.execute(
                """INSERT INTO questions (topic_id, text, priority)
                   VALUES ($1, $2, $3)
                   ON CONFLICT (topic_id, text)
                   DO UPDATE SET priority = EXCLUDED.priority""",
                topic_id, question, priority,
            )
    finally:
        await conn.close()
    print(f"Импортировано вопросов: {len(rows)}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))