"""Shared helpers: text tokenisation, money formatting, request parsing, JSON."""

import datetime
import json
import re

STOPWORDS = ["a", "an", "and", "the", "for", "with", "of", "to", "in", "on", "by", "at", "or"]


class HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def tokenize(text):
    """Lower-case alphanumeric words, without stopwords or duplicates, in first-seen order."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    terms = []
    for word in words:
        if word in STOPWORDS or len(word) < 2:
            continue
        if word not in terms:
            terms.append(word)
    return terms


def format_money(cents):
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def parse_int(value, name, low, high):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HTTPError(400, f"{name} must be an integer") from None
    if number < low or number > high:
        raise HTTPError(400, f"{name} must be between {low} and {high}")
    return number


def parse_date(value, name):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPError(400, f"{name} must be an ISO date (YYYY-MM-DD)") from None


def parse_timestamp(value, name):
    try:
        ts = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPError(400, f"{name} must be an ISO timestamp") from None
    if ts.tzinfo is None:
        raise HTTPError(400, f"{name} must include a timezone offset")
    return ts


def to_json(obj, compact):
    if compact:
        return json.dumps(obj, separators=(",", ":")).encode()
    return json.dumps(obj).encode()
