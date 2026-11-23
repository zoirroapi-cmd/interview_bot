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

Тебе даны вопрос, эталонные тезисы и ответ пользователя.

Для каждого тезиса определи статус:
- covered — тезис раскрыт правильно;
- partial — раскрыт частично или неточно;
- missing — в ответе его нет;
- contradicted — ответ противоречит тезису.

Для covered, partial и contradicted приведи короткую цитату из ответа пользователя. Цитата должна действительно присутствовать в ответе.
Для missing верни пустую цитату.
Если не можешь привести точную цитату, не выдумывай её.
Верни один элемент items на каждый тезис, в том же порядке. Не рассчитывай общий score.

Верни только JSON:
{
  "reference": "Краткий эталонный ответ",
  "comparison": "Краткое сравнение",
  "items": [
    {
      "n": 1,
      "status": "covered",
      "quote": "Цитата из ответа",
      "note": "Краткое пояснение"
    }
  ]
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



def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def parse_evaluation(raw: str, key_points: list[str], answer: str) -> dict:
    data = _extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError("ответ модели должен быть JSON-объектом")
    raw_items = data.get("items", [])
    if not isinstance(raw_items, list):
        raw_items = []

    items = []
    for index, thesis in enumerate(key_points):
        item = (
            raw_items[index]
            if index < len(raw_items) and isinstance(raw_items[index], dict)
            else {}
        )
        status = _as_text(item.get("status")) or "missing"
        quote = _as_text(item.get("quote"))
        note = _as_text(item.get("note"))

        if status not in {"covered", "partial", "missing", "contradicted"}:
            status = "missing"

        if status in {"covered", "partial", "contradicted"} and not _quote_ok(quote, answer):
            status = "missing"
            quote = ""
            note = note or "Цитата не найдена в ответе пользователя."
        if status == "missing":
            quote = ""

        items.append({
            "n": index + 1,
            "thesis": thesis,
            "status": status,
            "quote": quote,
            "note": note,
        })

    score = calc_score(items)
    return {
        "reference": _as_text(data.get("reference")),
        "comparison": _as_text(data.get("comparison")),
        "score": score,
        "items": items,
    }


async def evaluate(question: str, theses: list[str], answer: str) -> dict:
    user = (
        f"Вопрос: {question}" + chr(10) * 2
        + "Эталонные тезисы:" + chr(10)
        + chr(10).join(
            f"{index}. {thesis}"
            for index, thesis in enumerate(theses, start=1)
        )
        + chr(10) * 2
        + f"Ответ пользователя: {answer}"
    )
    last_error = None
    for _ in range(2):
        raw, usage = await _chat(EVAL_SYSTEM, user, temperature=0.3)
        try:
            result = parse_evaluation(raw, theses, answer)
        except (ValueError, TypeError, AttributeError) as error:
            last_error = error
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
