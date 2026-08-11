"""Approved WhatsApp message templates for follow-ups.

Outside the 24-hour service window Meta only delivers *pre-approved* templates,
matched by name and language. Follow-ups are therefore never model-generated —
the copy here has to be exactly what was submitted in WhatsApp Manager, or the
send is rejected.

Each template takes two body variables:
    {{1}} the lead's first name, or a neutral greeting when we don't know it
    {{2}} the agent's name

`body` is the approved text, kept here so the CLI harness and the tests can show
what a lead would actually receive. It is not sent — Meta renders the stored
template from `name` + `language` + the variables.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import Language


@dataclass(frozen=True)
class MessageTemplate:
    name: str
    language: Language
    body: str


# Day 1, 3, 7, 14, then monthly. Every one gives the lead an easy way out —
# an ignored nudge is a cost, an annoyed lead is a complaint to Meta.
FOLLOW_UP_TEMPLATES: dict[tuple[int, Language], MessageTemplate] = {}


def _register(attempt: int, language: Language, name: str, body: str) -> None:
    FOLLOW_UP_TEMPLATES[(attempt, language)] = MessageTemplate(name, language, body)


_register(
    1,
    Language.ENGLISH,
    "followup_day_1",
    "Hi {{1}}, just checking in on your property enquiry. Happy to answer any "
    "questions or line up a viewing whenever suits you. — {{2}}'s assistant. "
    "Reply STOP to opt out.",
)
_register(
    2,
    Language.ENGLISH,
    "followup_day_3",
    "Hi {{1}}, a couple of options in your range have come up since we last spoke. "
    "Want me to send the details? — {{2}}'s assistant. Reply STOP to opt out.",
)
_register(
    3,
    Language.ENGLISH,
    "followup_day_7",
    "Hi {{1}}, still looking? I can shortlist a few places that match what you had "
    "in mind. — {{2}}'s assistant. Reply STOP to opt out.",
)
_register(
    4,
    Language.ENGLISH,
    "followup_day_14",
    "Hi {{1}}, no rush at all — if your plans have changed just let me know and "
    "I'll stop checking in. — {{2}}'s assistant. Reply STOP to opt out.",
)
_register(
    5,
    Language.ENGLISH,
    "followup_monthly",
    "Hi {{1}}, checking in once a month in case anything has changed. New listings "
    "come up regularly. — {{2}}'s assistant. Reply STOP to opt out.",
)
_register(
    6,
    Language.ENGLISH,
    "followup_final",
    "Hi {{1}}, this is my last check-in so I'm not cluttering your inbox. Message "
    "any time if you start looking again. — {{2}}'s assistant.",
)

_register(
    1,
    Language.HINDI,
    "followup_day_1_hi",
    "नमस्ते {{1}}, आपकी प्रॉपर्टी पूछताछ के बारे में जानना चाहता था। कोई भी सवाल हो या "
    "विज़िट करनी हो तो बताइए। — {{2}} का असिस्टेंट। रोकने के लिए STOP भेजें।",
)
_register(
    2,
    Language.HINDI,
    "followup_day_3_hi",
    "नमस्ते {{1}}, आपके बजट में कुछ नए विकल्प आए हैं। जानकारी भेजूँ? — {{2}} का असिस्टेंट। रोकने के लिए STOP भेजें।",
)
_register(
    3,
    Language.HINDI,
    "followup_day_7_hi",
    "नमस्ते {{1}}, अभी भी देख रहे हैं? आपकी पसंद के हिसाब से कुछ विकल्प चुन सकता हूँ। "
    "— {{2}} का असिस्टेंट। रोकने के लिए STOP भेजें।",
)
_register(
    4,
    Language.HINDI,
    "followup_day_14_hi",
    "नमस्ते {{1}}, कोई जल्दी नहीं — अगर योजना बदल गई हो तो बता दीजिए, मैं संदेश भेजना "
    "बंद कर दूँगा। — {{2}} का असिस्टेंट। रोकने के लिए STOP भेजें।",
)
_register(
    5,
    Language.HINDI,
    "followup_monthly_hi",
    "नमस्ते {{1}}, महीने में एक बार हाल पूछ रहा हूँ। नई प्रॉपर्टी आती रहती हैं। "
    "— {{2}} का असिस्टेंट। रोकने के लिए STOP भेजें।",
)
_register(
    6,
    Language.HINDI,
    "followup_final_hi",
    "नमस्ते {{1}}, यह मेरा आखिरी संदेश है। दोबारा देखना शुरू करें तो कभी भी लिखिए। — {{2}} का असिस्टेंट।",
)

_register(
    1,
    Language.GUJARATI,
    "followup_day_1_gu",
    "નમસ્તે {{1}}, તમારી પ્રોપર્ટી પૂછપરછ વિશે જાણવું હતું. કોઈ પ્રશ્ન હોય કે વિઝિટ "
    "ગોઠવવી હોય તો કહેજો. — {{2}}નો સહાયક. બંધ કરવા STOP લખો.",
)
_register(
    2,
    Language.GUJARATI,
    "followup_day_3_gu",
    "નમસ્તે {{1}}, તમારા બજેટમાં નવા વિકલ્પો આવ્યા છે. વિગતો મોકલું? — {{2}}નો સહાયક. બંધ કરવા STOP લખો.",
)
_register(
    3,
    Language.GUJARATI,
    "followup_day_7_gu",
    "નમસ્તે {{1}}, હજી શોધી રહ્યા છો? તમારી પસંદ પ્રમાણે થોડા વિકલ્પો પસંદ કરી શકું. "
    "— {{2}}નો સહાયક. બંધ કરવા STOP લખો.",
)
_register(
    4,
    Language.GUJARATI,
    "followup_day_14_gu",
    "નમસ્તે {{1}}, ઉતાવળ નથી — યોજના બદલાઈ હોય તો જણાવજો, હું સંદેશા બંધ કરી દઈશ. "
    "— {{2}}નો સહાયક. બંધ કરવા STOP લખો.",
)
_register(
    5,
    Language.GUJARATI,
    "followup_monthly_gu",
    "નમસ્તે {{1}}, મહિને એક વાર ખબર પૂછું છું. નવી પ્રોપર્ટી આવતી રહે છે. — {{2}}નો સહાયક. બંધ કરવા STOP લખો.",
)
_register(
    6,
    Language.GUJARATI,
    "followup_final_gu",
    "નમસ્તે {{1}}, આ મારો છેલ્લો સંદેશ છે. ફરી શોધવાનું શરૂ કરો ત્યારે લખજો. — {{2}}નો સહાયક.",
)

NEUTRAL_GREETING = {
    Language.ENGLISH: "there",
    Language.HINDI: "जी",
    Language.GUJARATI: "જી",
}


def follow_up_template(attempt: int, language: Language) -> MessageTemplate | None:
    """The template for this attempt, falling back to English if unlocalised."""
    return FOLLOW_UP_TEMPLATES.get((attempt, language)) or FOLLOW_UP_TEMPLATES.get(
        (attempt, Language.ENGLISH)
    )


def first_name(full_name: str | None, language: Language) -> str:
    """Templates address the lead by first name; never leave the variable empty."""
    if not full_name or not full_name.strip():
        return NEUTRAL_GREETING.get(language, NEUTRAL_GREETING[Language.ENGLISH])
    return full_name.strip().split()[0]


def render(template: MessageTemplate, lead_name: str, agent_name: str) -> str:
    """What the lead sees — for the CLI harness, tests and the dashboard."""
    return template.body.replace("{{1}}", lead_name).replace("{{2}}", agent_name)
