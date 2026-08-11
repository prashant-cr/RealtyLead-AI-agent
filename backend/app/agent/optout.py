"""Opt-out detection.

Deliberately deterministic rather than model-judged: "honour opt-out instantly"
is a compliance obligation (DPDP / TRAI / WhatsApp policy), and a compliance
control should not depend on a model's interpretation of a message. The check
runs before the model is called at all.

Covers English, Hindi and Gujarati — the launch languages.
"""

from __future__ import annotations

import re

# Matched as whole words against a normalised message.
_STOP_WORDS = {
    # English
    "stop",
    "unsubscribe",
    "opt out",
    "optout",
    "remove me",
    "do not contact",
    "dont contact",
    "don't contact",
    "not interested",
    "no longer interested",
    "leave me alone",
    "stop messaging",
    "stop texting",
    # Hindi (Latin + Devanagari)
    "band karo",
    "band karein",
    "mat bhejo",
    "message mat karo",
    "nahi chahiye",
    "interested nahi",
    "बंद करो",
    "मत भेजो",
    "नहीं चाहिए",
    "रुचि नहीं",
    # Gujarati (Latin + Gujarati script)
    "bandh karo",
    "nathi joitu",
    "ras nathi",
    "બંધ કરો",
    "નથી જોઈતું",
    "રસ નથી",
}

_NORMALISE_RE = re.compile(r"[^\w\sऀ-ॿ઀-૿']+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", _NORMALISE_RE.sub(" ", text.lower())).strip()


# Single words that only mean "stop contacting me" when they are the whole message.
# "stop by the site on Sunday" and "what's the stop for the metro" are not opt-outs.
_STANDALONE_ONLY = {"stop", "unsubscribe", "optout", "band", "bandh"}

# Politeness that can wrap a bare opt-out without changing its meaning.
_FILLER = {"please", "pls", "plz", "thanks", "thank", "you", "no", "kindly", "just", "ok"}


def is_opt_out(text: str) -> bool:
    """True when the message is an unambiguous request to stop being contacted."""
    normalised = _normalise(text)
    if not normalised:
        return False

    # Multi-word phrases are unambiguous, so they match anywhere in the message.
    if any(" " in phrase and phrase in normalised for phrase in _STOP_WORDS):
        return True

    # Single-word triggers must stand alone, allowing only politeness around them.
    words = normalised.split()
    remainder = [word for word in words if word not in _FILLER]
    return len(remainder) == 1 and remainder[0] in _STANDALONE_ONLY


OPT_OUT_CONFIRMATION = {
    "en": "Understood — I won't message you again. If you change your mind, just reply here.",
    "hi": "ठीक है, अब मैं आपको संदेश नहीं भेजूँगा। ज़रूरत हो तो यहीं जवाब दे दीजिए।",
    "gu": "સમજી ગયો — હવે હું તમને સંદેશ નહીં મોકલું. જરૂર પડે તો અહીં જ જવાબ આપજો.",
}


def opt_out_confirmation(language: str) -> str:
    return OPT_OUT_CONFIRMATION.get(language, OPT_OUT_CONFIRMATION["en"])
