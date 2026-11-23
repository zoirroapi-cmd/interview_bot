import json

from app.llm import (
    calc_score,
    confidence_from_score,
    parse_evaluation,
)

ANSWER = "NAT позволяет нескольким устройствам использовать один публичный IP."


def make_raw(score=7.5, reference=None, comparison=None):
    return json.dumps({
        "reference": reference or (
            "NAT позволяет нескольким устройствам использовать один публичный IP."
        ),
        "comparison": comparison or "Ответ правильный, но недостаточно подробный.",
        "score": score,
    })


def test_parse_evaluation_returns_all_fields():
    result = parse_evaluation(make_raw(), ANSWER)

    assert result["reference"] == (
        "NAT позволяет нескольким устройствам использовать один публичный IP."
    )
    assert result["comparison"] == "Ответ правильный, но недостаточно подробный."
    assert result["score"] == 7.5
    assert result["items"] == []


def test_score_is_clamped_and_rounded():
    assert parse_evaluation(make_raw(15), ANSWER)["score"] == 10.0
    assert parse_evaluation(make_raw(-5), ANSWER)["score"] == 0.0
    assert parse_evaluation(make_raw(7.56), ANSWER)["score"] == 7.6


def test_dict_fields_are_converted_to_text():
    result = parse_evaluation(
        make_raw(
            reference={"эталон": "Полный ответ"},
            comparison={"верно": "названы ключевые отличия"},
        ),
        ANSWER,
    )

    assert "эталон: Полный ответ" in result["reference"]
    assert "верно: названы ключевые отличия" in result["comparison"]


def test_calc_score_for_mixed_statuses():
    items = [
        {"status": "covered"},
        {"status": "partial"},
        {"status": "missing"},
        {"status": "contradicted"},
    ]

    assert calc_score(items) == 1.2


def test_confidence_from_score_boundaries():
    assert confidence_from_score(0) == 1
    assert confidence_from_score(3) == 2
    assert confidence_from_score(5) == 3
    assert confidence_from_score(7) == 4
    assert confidence_from_score(9) == 5
