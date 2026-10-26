"""Extractions the model returns for plain greeting, capability, intake and question messages."""

from __future__ import annotations

from tests.support.unit_fakes import make_extraction


def social_extraction(has_question: bool = False):
    return make_extraction(intent="social", has_question=has_question)


def capability_extraction(has_question: bool = False):
    return make_extraction(intent="capability", has_question=has_question)


def start_intake_extraction(has_question: bool = False):
    return make_extraction(intent="start_intake", has_question=has_question)


def question_extraction():
    return make_extraction(intent="question", has_question=True)
