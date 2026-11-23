"""Проверка ответов через GigaChat по заранее подготовленным тезисам."""
import json
import logging
import os
import re
import time
import uuid

import aiohttp

log = logging.getLogger(__name__)

AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL = "https://api.giga.chat/v1/chat/completions"

WEIGHTS = {"covered": 1.0, "partial": 0.5, "missing": 0.0, "contradicted": -1.0}

EVAL_SYSTEM = """Ты проверяешь ответ пользователя на технический вопрос по DevOps / Kubernetes.

Даны вопрос, заранее подготовленные эталонные тезисы и ответ пользователя.
Используй тезисы как критерии правильности и полноты. Не придумывай требования,
которых нет в вопросе и тезисах.

Верни:
1. Краткое сравнение с ответом пользователя: какие технические идеи раскрыты,
   какие важные моменты упущены или неточны, есть ли грубая фактическая ошибка.
   Не перечисляй тезисы по одному — дай связное резюме.
2. Массив items: ровно один объект на каждый тезис, в том же порядке, с его номером
   и статусом. Число объектов должно точно совпадать с числом тезисов во входном
   сообщении. Не объединяй тезисы и не пропускай объекты. Статусы: covered (смысл
   раскрыт), partial (раскрыт частично), missing (не упомянут), contradicted
   (пользователь утверждает обратное).

Оценивай только техническое содержание. Не комментируй грамматику, орфографию,
пунктуацию, опечатки, стиль или структуру речи. Не ставь числовую оценку: её
посчитает приложение по статусам.

Формат JSON: объект с полями comparison (строка), items (массив объектов с
полями n и status). Верни только JSON, без текста до и после.
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



def parse_evaluation(raw: str, answer: str, theses: list[str]) -> dict:
    data = _extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError("ответ модели должен быть JSON-объектом")
    if not theses:
        raise ValueError("для оценки нет эталонных тезисов")

    items = data.get("items")
    if not isinstance(items, list) or len(items) != len(theses):
        raise ValueError("число оценённых тезисов не совпадает с эталоном")

    normalized_items = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"оценка тезиса {index} должна быть JSON-объектом")
        status = item.get("status")
        if status not in WEIGHTS:
            raise ValueError(f"неизвестный статус тезиса {index}: {status!r}")
        normalized_items.append({"n": index, "status": status})

    return {
        "reference": " ".join(theses),
        "comparison": _text(data.get("comparison")),
        "score": calc_score(normalized_items),
        "items": normalized_items,
    }


async def evaluate(question: str, theses: list[str], answer: str) -> dict:
    numbered_theses = "\n".join(
        f"{index}. {thesis}" for index, thesis in enumerate(theses, start=1)
    )
    user = (
        f"Вопрос: {question}\n\n"
        f"Эталонные тезисы: {len(theses)}. В items верни ровно {len(theses)} "
        f"записи; номера — от 1 до {len(theses)}:\n"
        f"{numbered_theses}\n\n"
        f"Ответ пользователя:\n{answer}"
    )
    last_error = None
    for _ in range(2):
        raw, usage = await _chat(EVAL_SYSTEM, user, temperature=0.3)
        try:
            result = parse_evaluation(raw, answer, theses)
        except (ValueError, TypeError, AttributeError) as e:
            log.warning("не разобран ответ модели: %s | raw=%r", e, raw[:500])
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

def _text(value) -> str:
    """Модель иногда возвращает dict/list вместо строки: приводим к тексту."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        parts = (f"{k}: {_text(v)}" for k, v in value.items())
        return "\n".join(p for p in parts if not p.endswith(": "))
    if isinstance(value, list):
        return "\n".join(f"• {_text(v)}" for v in value if _text(v))
    return str(value).strip()
