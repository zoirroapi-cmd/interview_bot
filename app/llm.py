"""Проверка ответов через GigaChat.

Модель ставит статус каждому эталонному тезису и приводит цитату из ответа.
Оценку считает код (calc_score), а не модель.
"""
import json
import os
import re
import time
import uuid

import aiohttp

AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL = "https://api.giga.chat/v1/chat/completions"

# Баллы за статус тезиса. Неправда (contradicted) вычитается.
WEIGHTS = {"covered": 1.0, "partial": 0.5, "missing": 0.0, "contradicted": -1.0}

EVAL_SYSTEM = """Ты проверяешь ответ пользователя на технический вопрос по DevOps.
Тебе даны вопрос, пронумерованный список эталонных тезисов и ответ пользователя.

Для КАЖДОГО тезиса выбери ровно один статус:
- covered: в ответе по смыслу сказано то же самое, пусть другими словами.
- partial: сказана только часть тезиса или сказано расплывчато, без сути.
- missing: в ответе про это ничего нет.
- contradicted: в ответе прямо сказано обратное тому, что в тезисе. Простое отсутствие информации - это missing, а не contradicted.

Поле quote: точная цитата из ответа пользователя, на которой основан статус, скопированная дословно. Для missing - пустая строка.

Правила:
- Оценивай только тезисы из списка. Ничего не добавляй от себя.
- Ничего не приписывай пользователю: цитируй только то, что реально написано.
- Не оценивай стиль и не исправляй формулировки.
- Не пиши оценок и пояснений.

Верни только JSON, без текста до и после:
{"items": [{"n": 1, "status": "covered", "quote": "..."}]}
В items должно быть по одному элементу на каждый тезис, в том же порядке."""

KEYPOINTS_SYSTEM = """Ты опытный DevOps-инженер, который проводит технические собеседования.
Сформулируй 3-5 ключевых тезисов правильного ответа на вопрос.
Каждый тезис - одно законченное предложение с одной проверяемой мыслью, только факты.
Верни только JSON-массив строк, без текста до и после."""

_token: str | None = None
_token_until = 0.0


def _ssl_arg():
    verify = os.getenv("GIGACHAT_VERIFY_SSL", "false").lower() == "true"
    return None if verify else False


async def _get_token(session: aiohttp.ClientSession) -> str:
    global _token, _token_until
    if _token and time.time() < _token_until:
        return _token
    async with session.post(
        AUTH_URL,
        headers={
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {os.environ['GIGACHAT_AUTH_KEY']}",
        },
        data={"scope": "GIGACHAT_API_PERS"},
        ssl=_ssl_arg(),
    ) as resp:
        resp.raise_for_status()
        payload = await resp.json()
    _token = payload["access_token"]
    _token_until = time.time() + 25 * 60
    return _token


async def _chat(system: str, user: str, temperature: float = 0.2):
    global _token
    model = os.getenv("GIGACHAT_MODEL", "GigaChat-2")
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(2):
            token = await _get_token(session)
            async with session.post(
                CHAT_URL,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                json={
                    "model": model,
                    "temperature": temperature,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                ssl=_ssl_arg(),
            ) as resp:
                if resp.status == 401 and attempt == 0:
                    _token = None
                    continue
                resp.raise_for_status()
                data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            return content, data.get("usage", {})


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower().replace("ё", "е"))


def _quote_ok(quote: str, answer: str) -> bool:
    """Цитата считается валидной, если она достаточно близка к тексту ответа."""
    if not quote.strip():
        return False

    nq = _norm(quote)
    na = _norm(answer)

    if nq in na:
        return True

    words = [w for w in re.findall(r"[а-яёa-z0-9]+", quote.lower()) if len(w) > 3]
    if len(words) < 2:
        return False

    found = sum(1 for w in words if _norm(w) in na)
    return found / len(words) >= 0.65


def _extract_json(text: str):
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("в ответе модели нет JSON")
    return json.loads(text[start:end + 1])


def calc_score(items: list[dict]) -> float:
    total = sum(WEIGHTS[i["status"]] for i in items)
    return max(0.0, round(total / len(items) * 10, 1))


def confidence_from_score(score: float) -> int:
    if score < 3:
        return 1
    if score < 5:
        return 2
    if score < 7:
        return 3
    if score < 9:
        return 4
    return 5



def parse_evaluation(raw: str, theses: list[str], answer: str) -> dict:
    data = _extract_json(raw)
    by_n = {}

    for it in data.get("items", []):
        try:
            by_n[int(it.get("n"))] = it
        except (TypeError, ValueError):
            continue

    items = []
    for i, thesis in enumerate(theses, start=1):
        it = by_n.get(i, {})
        status = it.get("status")

        if status not in WEIGHTS:
            status = "missing"

        quote = (it.get("quote") or "").strip()
        note = ""

        if status == "missing":
            quote = ""
        elif not _quote_ok(quote, answer):
            # Не меняем смысловую оценку автоматически:
            # цитата может не пройти проверку из-за несовпадения
            # формулировок или особенностей нормализации.
            note = "Цитата не прошла проверку соответствия ответу"

        items.append({
            "thesis": thesis,
            "status": status,
            "quote": quote if _quote_ok(quote, answer) else "",
            "note": note,
        })

    return {"items": items, "score": calc_score(items)}



async def evaluate(question: str, theses: list[str], answer: str) -> dict:
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(theses, start=1))
    user = f"Вопрос: {question}\n\nТезисы:\n{numbered}\n\nОтвет пользователя: {answer}"
    last_error = None
    for _ in range(2):
        raw, usage = await _chat(EVAL_SYSTEM, user)
        try:
            result = parse_evaluation(raw, theses, answer)
        except ValueError as e:
            last_error = e
            continue
        result["usage"] = usage
        return result
    raise ValueError(f"не удалось разобрать ответ модели: {last_error}")


async def generate_key_points(question: str) -> list[str]:
    raw, _ = await _chat(KEYPOINTS_SYSTEM, f"Вопрос: {question}")
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end < start:
        raise ValueError("в ответе модели нет списка тезисов")
    points = [str(p).strip() for p in json.loads(raw[start:end + 1]) if str(p).strip()]
    if not 2 <= len(points) <= 8:
        raise ValueError(f"странное число тезисов: {len(points)}")
    return points