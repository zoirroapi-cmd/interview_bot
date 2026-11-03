import json

from app.llm import parse_evaluation


def make_raw(score=7.5):
    return json.dumps({
        "reference": "NAT позволяет нескольким устройствам использовать один публичный IP.",
        "comparison": "Ответ правильный, но недостаточно подробный.",
        "score": score,
    })


def test_parse_evaluation_returns_all_fields():
    result = parse_evaluation(make_raw(), "NAT использует один публичный IP.")

    assert result["reference"] == (
        "NAT позволяет нескольким устройствам использовать один публичный IP."
    )
    assert result["comparison"] == "Ответ правильный, но недостаточно подробный."
    assert result["score"] == 7.5


def test_score_is_limited_to_ten():
    result = parse_evaluation(make_raw(15), "Ответ")

    assert result["score"] == 10.0


def test_score_is_not_negative():
    result = parse_evaluation(make_raw(-5), "Ответ")

    assert result["score"] == 0.0


def test_score_is_rounded_to_one_decimal():
    result = parse_evaluation(make_raw(7.56), "Ответ")

    assert result["score"] == 7.6