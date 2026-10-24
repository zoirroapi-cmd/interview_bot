
import json

import pytest

from app.llm import calc_score, parse_evaluation


ANSWER = "NAT позволяет нескольким устройствам использовать один публичный IP."


def make_raw(status, quote=""):
    return json.dumps({
        "items": [
            {"n": 1, "status": status, "quote": quote}
        ]
    })


def test_covered_gets_full_score():
    result = parse_evaluation(
        make_raw("covered", ANSWER),
        ["NAT позволяет нескольким устройствам использовать один публичный IP."],
        ANSWER,
    )

    assert result["items"][0]["status"] == "covered"
    assert result["score"] == 10.0


def test_partial_gets_half_score():
    result = parse_evaluation(
        make_raw("partial", "NAT использует публичный IP"),
        ["NAT позволяет нескольким устройствам использовать один публичный IP."],
        ANSWER,
    )

    assert result["items"][0]["status"] == "partial"
    assert result["score"] == 5.0


def test_missing_gets_zero_score():
    result = parse_evaluation(
        make_raw("missing"),
        ["NAT позволяет нескольким устройствам использовать один публичный IP."],
        ANSWER,
    )

    assert result["items"][0]["status"] == "missing"
    assert result["items"][0]["quote"] == ""
    assert result["score"] == 0.0


def test_contradicted_loses_points_but_score_is_not_negative():
    result = parse_evaluation(
        make_raw("contradicted", "NAT не позволяет использовать один публичный IP"),
        ["NAT позволяет нескольким устройствам использовать один публичный IP."],
        ANSWER,
    )

    assert result["items"][0]["status"] == "contradicted"
    assert result["score"] == 0.0


def test_invalid_quote_does_not_turn_covered_into_missing():
    result = parse_evaluation(
        make_raw("covered", "совершенно посторонняя цитата"),
        ["NAT позволяет нескольким устройствам использовать один публичный IP."],
        ANSWER,
    )

    assert result["items"][0]["status"] == "covered"
    assert result["items"][0]["quote"] == ""
    assert result["score"] == 10.0


def test_calc_score_for_mixed_statuses():
    items = [
        {"status": "covered"},
        {"status": "partial"},
        {"status": "missing"},
        {"status": "contradicted"},
    ]

    assert calc_score(items) == 1.2


def test_unknown_status_is_treated_as_missing():
    result = parse_evaluation(
        make_raw("unknown_status", ANSWER),
        ["NAT позволяет нескольким устройствам использовать один публичный IP."],
        ANSWER,
    )

    assert result["items"][0]["status"] == "missing"
    assert result["score"] == 0.0
