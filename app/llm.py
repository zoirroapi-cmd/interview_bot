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

WEIGHTS = {"covered": 1.0, "partial": 0.5, "missing": 0.0, "contradicted": -1.0}

EVAL_SYSTEM = """Ты проверяешь ответ пользователя на технический вопрос по DevOps / Kubernetes.

Тебе даны вопрос и ответ пользователя.

Сделай следующее:
1. Напиши короткий эталонный ответ (3–6 предложений). Только факты, без воды.
2. Сравни ответ пользователя с эталоном:
   - что сказано правильно;
   - что упущено или сказано неточно;
   - есть ли грубые ошибки.
3. Поставь общую оценку от 0 до 10.

Верни только JSON, без текста до и после:
{
  "reference": "эталонный ответ...",
  "comparison": "сравнение...",
  "score": 7.5
}
"""

KEYPOINTS_SYSTEM = """Ты опытный DevOps-инженер, который проводит технические собеседования.
Сформулируй 3-5 ключевых тезисов правильного ответа на вопрос.
Каждый тезис - одно законченное предложение с одной проверяемой мыслью, только факты.
Вопрос относится к теме, которая указана в сообщении. Отвечай строго в контексте этой темы:
например, "Service" в теме Kubernetes - это объект Kubernetes, а не сервис вообще.
Каждый элемент массива - строка, а не объект.
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



def parse_evaluation(raw: str, answer: str) -> dict:
    data = _extract_json(raw)
    score = float(data.get("score", 0))
    score = max(0.0, min(10.0, round(score, 1)))
    return {
        "reference": (data.get("reference") or "").strip(),
        "comparison": (data.get("comparison") or "").strip(),
        "score": score,
        "items": [],          # оставляем для совместимости
    }


async def evaluate(question: str, theses: list[str], answer: str) -> dict:
    # theses больше не используем, но сигнатуру оставляем
    user = f"Вопрос: {question}\n\nОтвет пользователя: {answer}"
    last_error = None
    for _ in range(2):
        raw, usage = await _chat(EVAL_SYSTEM, user, temperature=0.3)
        try:
            result = parse_evaluation(raw, answer)
        except ValueError as e:
            last_error = e
            continue
        result["usage"] = usage
        return result
    raise ValueError(f"не удалось разобрать ответ модели: {last_error}")

async def generate_key_points(question: str, topic: str) -> list[str]:
    raw, _ = await _chat(KEYPOINTS_SYSTEM, f"Тема: {topic}\nВопрос: {question}")
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end < start:
        raise ValueError("в ответе модели нет списка тезисов")
    items = json.loads(raw[start:end + 1])
    if not isinstance(items, list) or not all(isinstance(p, str) for p in items):
        raise ValueError("тезисы должны быть списком строк")
    points = [p.strip() for p in items if p.strip()]
    if not 2 <= len(points) <= 8:
        raise ValueError(f"странное число тезисов: {len(points)}")
    return points