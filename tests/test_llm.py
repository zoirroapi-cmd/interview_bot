import asyncio
import json

import pytest

from app import llm

ANSWER = "NAT позволяет нескольким устройствам использовать один публичный IP."
THESES = [
    "NAT преобразует сетевые адреса.",
    "PAT позволяет нескольким устройствам делить один публичный IPv4-адрес.",
]


def make_raw(statuses, reference="Эталонный ответ.", comparison="Краткое сравнение."):
    return json.dumps({
        "reference": reference,
        "comparison": comparison,
        "items": [
            {"n": index, "status": status}
            for index, status in enumerate(statuses, start=1)
        ],
    })


def test_parse_evaluation_scores_against_each_thesis():
    raw = make_raw(["covered", "partial"])

    result = llm.parse_evaluation(raw, ANSWER, THESES)

    assert result["reference"] == "Эталонный ответ."
    assert result["comparison"] == "Краткое сравнение."
    assert result["items"] == [
        {"n": 1, "status": "covered"},
        {"n": 2, "status": "partial"},
    ]
    assert result["score"] == 7.5


def test_score_is_zero_for_all_contradicted_theses_and_capped_at_ten():
    assert llm.parse_evaluation(
        make_raw(["contradicted", "contradicted"]), ANSWER, THESES
    )["score"] == 0.0
    assert llm.parse_evaluation(
        make_raw(["covered", "covered"],), ANSWER, THESES
    )["score"] == 10.0


def test_parse_evaluation_normalizes_object_fields():
    raw = make_raw(
        ["covered", "missing"],
        reference={"эталон": "Полный ответ"},
        comparison={"верно": "раскрыта первая мысль"},
    )

    result = llm.parse_evaluation(raw, ANSWER, THESES)

    assert result["reference"] == "эталон: Полный ответ"
    assert result["comparison"] == "верно: раскрыта первая мысль"


@pytest.mark.parametrize(
    "statuses",
    [["covered"], ["covered", "unknown"]],
)
def test_parse_evaluation_rejects_missing_or_unknown_thesis_statuses(statuses):
    with pytest.raises(ValueError):
        llm.parse_evaluation(make_raw(statuses), ANSWER, THESES)


def test_evaluate_includes_precomputed_theses_in_model_request(monkeypatch):
    captured = {}

    async def fake_chat(system, user, temperature=0.2):
        captured["system"] = system
        captured["user"] = user
        captured["temperature"] = temperature
        return make_raw(["covered", "partial"]), {"total_tokens": 42}

    monkeypatch.setattr(llm, "_chat", fake_chat)
    result = asyncio.run(llm.evaluate("Что такое NAT?", THESES, ANSWER))

    assert "1. NAT преобразует сетевые адреса." in captured["user"]
    assert "2. PAT позволяет нескольким устройствам делить один публичный IPv4-адрес." in captured["user"]
    assert ANSWER in captured["user"]
    assert "Не комментируй грамматику" in captured["system"]
    assert result["score"] == 7.5
    assert result["usage"] == {"total_tokens": 42}


def test_confidence_from_score_boundaries():
    assert llm.confidence_from_score(0) == 1
    assert llm.confidence_from_score(3) == 2
    assert llm.confidence_from_score(5) == 3
    assert llm.confidence_from_score(7) == 4
    assert llm.confidence_from_score(9) == 5
