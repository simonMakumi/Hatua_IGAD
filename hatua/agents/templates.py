"""
Pre-translated advisory templates for low-resource languages.

Why this exists — and why it is not a fallback
-----------------------------------------------
We measured it. Asked to write an Amharic drought advisory for Somali Region,
Ethiopia, a 70B-parameter model produced text that back-translates roughly to
*"there is a medical violation in the ground, Amhara region"* — incoherent, and
naming the wrong region. Afaan Oromo was comparably bad.

That is not a prompt problem. It is a data problem, and it is well documented:
Google Translate scores 75.8 chrF++ on Swahili and **30.2 on Amharic**;
Tigrinya is not benchmarked at all. Published evaluations put small
general-purpose LLMs at **3–11 chrF++** on these languages, which is noise.
Multilingual models are also documented to misidentify Tigrinya as Amharic.

For a system whose messages ask people to move their family or sell their
herd, free-form generation into a language we cannot evaluate is not
acceptable. So:

    English, Kiswahili, Somali   — generated, then verified
    Amharic, Tigrinya, Afaan Oromo, Arabic — pre-translated templates,
                                             machine-filled slots only

The templates below are fixed sentences with named slots. A native speaker can
review the entire surface area of what HATUA will ever say in Amharic by
reading one file — which is the only review that actually means anything. New
information changes the *slots*, never the sentence.

This is a deliberate trade. Templates are less fluent and less specific than
generated text. They are also the difference between a message a Tigrinya
speaker can act on and one that wastes their time or misleads them.

Slot values are numerals, place names and dates — all of which come from the
deterministic layer and are already verified, so the guarantee survives.
"""

from __future__ import annotations

import logging
from datetime import date

from ..models import ActionPlan, HazardType, Language, Severity

log = logging.getLogger("hatua.templates")

# Languages we will not generate freely into. Everything else goes through the
# Localizer and the Verifier as normal.
TEMPLATE_ONLY: frozenset[Language] = frozenset(
    {Language.AMHARIC, Language.TIGRINYA, Language.OROMO, Language.ARABIC}
)


# ---------------------------------------------------------------------------
# Severity words
# ---------------------------------------------------------------------------
# Each language's severity ladder, matching the register rules the generated
# path follows: advisory = informational, watch = preparatory,
# warning = directive, emergency = imperative.

SEVERITY_WORD: dict[Language, dict[Severity, str]] = {
    Language.AMHARIC: {
        Severity.ADVISORY: "ማሳወቂያ",
        Severity.WATCH: "ዝግጁ ይሁኑ",
        Severity.WARNING: "ማስጠንቀቂያ",
        Severity.EMERGENCY: "አስቸኳይ ማስጠንቀቂያ",
    },
    Language.TIGRINYA: {
        Severity.ADVISORY: "ሓበሬታ",
        Severity.WATCH: "ተዳለዉ",
        Severity.WARNING: "መጠንቀቕታ",
        Severity.EMERGENCY: "ህጹጽ መጠንቀቕታ",
    },
    Language.OROMO: {
        Severity.ADVISORY: "Beeksisa",
        Severity.WATCH: "Qophaa'aa",
        Severity.WARNING: "Akeekkachiisa",
        Severity.EMERGENCY: "Akeekkachiisa Ariifachiisaa",
    },
    Language.ARABIC: {
        Severity.ADVISORY: "إشعار",
        Severity.WATCH: "استعدوا",
        Severity.WARNING: "تحذير",
        Severity.EMERGENCY: "تحذير عاجل",
    },
}

HAZARD_WORD: dict[Language, dict[HazardType, str]] = {
    Language.AMHARIC: {
        HazardType.DROUGHT: "ድርቅ",
        HazardType.FLOOD: "ጎርፍ",
        HazardType.HEAVY_RAIN: "ከባድ ዝናብ",
        HazardType.HEAT_STRESS: "ከፍተኛ ሙቀት",
        HazardType.FOOD_INSECURITY: "የምግብ እጥረት",
        HazardType.DISEASE_OUTBREAK: "የበሽታ ወረርሽኝ",
    },
    Language.TIGRINYA: {
        HazardType.DROUGHT: "ድርቂ",
        HazardType.FLOOD: "ውሕጅ",
        HazardType.HEAVY_RAIN: "ብርቱዕ ዝናብ",
        HazardType.HEAT_STRESS: "ላዕለዋይ ሙቐት",
        HazardType.FOOD_INSECURITY: "ሕጽረት መግቢ",
        HazardType.DISEASE_OUTBREAK: "ለበዳ ሕማም",
    },
    Language.OROMO: {
        HazardType.DROUGHT: "Hongee",
        HazardType.FLOOD: "Lolaa",
        HazardType.HEAVY_RAIN: "Rooba cimaa",
        HazardType.HEAT_STRESS: "Ho'a guddaa",
        HazardType.FOOD_INSECURITY: "Hanqina nyaataa",
        HazardType.DISEASE_OUTBREAK: "Weerara dhukkubaa",
    },
    Language.ARABIC: {
        HazardType.DROUGHT: "جفاف",
        HazardType.FLOOD: "فيضان",
        HazardType.HEAVY_RAIN: "أمطار غزيرة",
        HazardType.HEAT_STRESS: "حرارة شديدة",
        HazardType.FOOD_INSECURITY: "نقص الغذاء",
        HazardType.DISEASE_OUTBREAK: "تفشي المرض",
    },
}


# ---------------------------------------------------------------------------
# Action instructions
# ---------------------------------------------------------------------------
# Keyed by action_id, so these stay in lockstep with the approved action
# library. An action with no translation here is skipped rather than
# machine-translated — a partial message is better than a wrong one.

ACTION_TEXT: dict[Language, dict[str, str]] = {
    Language.AMHARIC: {
        "dr_past_water_survey": "ከብቶችን ከማንቀሳቀስዎ በፊት በመንገዱ ላይ ያለውን ውኃ ያረጋግጡ።",
        "dr_past_early_migration": "ከብቶችን ወደ ደረቅ ወቅት ግጦሽ ቀድመው ያንቀሳቅሱ።",
        "dr_past_supplementary_feed": "ለእርጉዝ ከብቶችና ለጥጃዎች ተጨማሪ መኖ አሁን ያዘጋጁ።",
        "dr_past_vaccinate": "ከመንቀሳቀስዎ በፊት ከብቶችን ያስከትቡ።",
        "dr_farm_drought_seed": "ድርቅን የሚቋቋም ወይም ቀድሞ የሚደርስ ዘር ይጠቀሙ።",
        "dr_farm_delay_planting": "ዝናቡ እስኪረጋጋ ድረስ መዝራት ያዘግዩ።",
        "dr_farm_water_harvest": "የውኃ ማጠራቀሚያዎችን ከዝናቡ በፊት ያስተካክሉ።",
        "dr_hh_water_storage": "የቤት ውኃ ማጠራቀሚያዎችን አሁን ይሙሉና ይሸፍኑ።",
        "dr_hh_food_stock": "የሚቻል ከሆነ መሠረታዊ ምግብ አሁን ይግዙና ያከማቹ።",
        "fl_hh_move_higher": "አሁን ወደ ከፍታ ቦታ ይሂዱ። ሰነዶች፣ መድኃኒትና ውኃ ይዘው ይሂዱ።",
        "fl_hh_protect_documents": "ሰነዶችንና ስልክን በላስቲክ ከረጢት ውስጥ ከፍ ባለ ቦታ ያስቀምጡ።",
        "fl_hh_avoid_crossing": "የሚፈስ ውኃ አይሻገሩ። ጥልቀቱ አይታይም።",
        "fl_hh_water_treatment": "የመጠጥ ውኃን ሁሉ ያፍሉ ወይም ያክሙ።",
        "fl_past_move_livestock": "ከብቶችን አሁን ከጎርፍ ሜዳ ወደ ከፍታ ቦታ ያንቀሳቅሱ።",
        "fl_farm_early_harvest": "የደረሰ ሰብል አሁን ይሰብስቡ።",
        "fl_farm_raise_stores": "የተከማቸ እህልና ዘር ከወለል ከፍ ያድርጉ።",
        "hr_hh_secure_shelter": "ጣሪያውን ያጠናክሩ፤ በቤቱ ዙሪያ ያለውን ፍሳሽ ያጽዱ።",
        "ht_past_shade_water": "ከብቶችን ጠዋትና ማታ ያጠጡ፤ ቀን ጥላ ውስጥ ያኑሩ።",
        "ht_hh_protect_vulnerable": "ልጆችን፣ አረጋውያንንና ነፍሰ ጡሮችን ከፀሐይ ይጠብቁ፤ ውኃ ያብዙ።",
        "fi_hh_nutrition_screening": "ከአምስት ዓመት በታች ያሉ ልጆችን ወደ ጤና ተቋም ወስደው ያስመርምሩ።",
    },
    Language.TIGRINYA: {
        "dr_past_water_survey": "ማል ቅድሚ ምንቅስቓስ፡ ኣብ መገዲ ዘሎ ማይ ኣረጋግጹ።",
        "dr_past_early_migration": "ማል ናብ ናይ ደረቕ እዋን መጓሰዪ ኣቐዲምኩም ኣንቀሳቕሱ።",
        "dr_past_supplementary_feed": "ንጠናወርቲ ማልን ንዑሱባትን ተወሳኺ መግቢ ሕጂ ኣዳልዉ።",
        "dr_past_vaccinate": "ቅድሚ ምንቅስቓስ ማል ክታበት ግበሩ።",
        "dr_farm_drought_seed": "ድርቂ ዝጻወር ወይ ቀልጢፉ ዝበስል ዘርኢ ተጠቐሙ።",
        "dr_farm_delay_planting": "ዝናብ ክሳብ ዝረጋገጽ ምዝራእ ኣደንጉዩ።",
        "dr_hh_water_storage": "ናይ ገዛ መኽዘን ማይ ሕጂ መልኡን ክደኑን።",
        "dr_hh_food_stock": "እንተተኻኢሉ መሰረታዊ መግቢ ሕጂ ዓድጉን ኣኽዝኑን።",
        "fl_hh_move_higher": "ሕጂ ናብ በሪኽ ቦታ ኪዱ። ሰነዳት፡ መድሃኒትን ማይን ውሰዱ።",
        "fl_hh_protect_documents": "ሰነዳትን ተሌፎንን ኣብ ፕላስቲክ ጌርኩም ኣብ በሪኽ ኣቐምጡ።",
        "fl_hh_avoid_crossing": "ዝፈስስ ማይ ኣይትሳገሩ። ዕምቆቱ ኣይረአን።",
        "fl_hh_water_treatment": "ኩሉ መስተ ማይ ኣፍልሑ ወይ ኣጽርዩ።",
        "fl_past_move_livestock": "ማል ሕጂ ካብ ጎልጎል ውሕጅ ናብ በሪኽ ኣንቀሳቕሱ።",
        "fl_farm_early_harvest": "ዝበሰለ ኣእካል ሕጂ ዓጽዱ።",
        "ht_hh_protect_vulnerable": "ቆልዑ፡ ዓበይትን ጠናወርትን ካብ ጸሓይ ሓልዉ፤ ማይ ኣብዝሑ።",
        "fi_hh_nutrition_screening": "ትሕቲ ሓሙሽተ ዓመት ዘለዉ ቆልዑ ናብ ጥዕና ትካል ወሲድኩም ኣመርምሩ።",
    },
    Language.OROMO: {
        "dr_past_water_survey": "Horii socho'uu dura karaa irratti bishaan jiraachuu mirkaneeffadhu.",
        "dr_past_early_migration": "Horii gara dheedicha bonaa dursitanii sochoosaa.",
        "dr_past_supplementary_feed": "Horii ulfaatii fi ilmoolee dabalataan nyaata amma qopheessaa.",
        "dr_past_vaccinate": "Horii socho'uu dura talaallii kennaa.",
        "dr_farm_drought_seed": "Sanyii hongee dandamatu yookaan dafee bilchaatu fayyadamaa.",
        "dr_farm_delay_planting": "Roobni hamma mirkanaa'utti facaasuu tursiisaa.",
        "dr_hh_water_storage": "Qodaa bishaan manaa amma guutaatii uwwisaa.",
        "dr_hh_food_stock": "Yoo dandeessan nyaata bu'uuraa amma bitaatii kuusaa.",
        "fl_hh_move_higher": "Amma gara lafa ol ka'aa deemaa. Waraqaa, qoricha fi bishaan fudhadhaa.",
        "fl_hh_protect_documents": "Waraqaa fi bilbila keessan korojoo keessa galchitanii lafa ol ka'aa kaa'aa.",
        "fl_hh_avoid_crossing": "Bishaan yaa'u hin ce'inaa. Gadi fageenyi isaa hin mul'atu.",
        "fl_hh_water_treatment": "Bishaan dhugaatii hunda danfisaa yookaan qulqulleessaa.",
        "fl_past_move_livestock": "Horii amma lafa lolaa irraa gara lafa ol ka'aa sochoosaa.",
        "fl_farm_early_harvest": "Midhaan bilchaate amma haamaa.",
        "fl_farm_raise_stores": "Midhaan fi sanyii kuufame lafa irraa ol kaasaa.",
        "hr_hh_secure_shelter": "Baaburaa jabeessaatii naannoo manaa qulqulleessaa.",
        "ht_past_shade_water": "Horii ganama fi galgala obaasaa; guyyaa gaaddisa keessa turaa.",
        "ht_hh_protect_vulnerable": "Daa'imman, maanguddoota fi haadholii ulfaa aduu irraa eegaa; bishaan dabalaa.",
        "fi_hh_nutrition_screening": "Daa'imman waggaa shanii gadi gara buufata fayyaatti geessaatii sakatta'aa.",
    },
    Language.ARABIC: {
        "dr_past_water_survey": "تأكد من وجود الماء على الطريق قبل تحريك الماشية.",
        "dr_past_early_migration": "انقل الماشية إلى مراعي الموسم الجاف مبكراً.",
        "dr_past_supplementary_feed": "وفّر علفاً إضافياً للإناث الحوامل والصغار الآن.",
        "dr_hh_water_storage": "املأ خزانات المياه المنزلية وغطها الآن.",
        "dr_hh_food_stock": "اشترِ وخزّن الطعام الأساسي الآن إن أمكن.",
        "fl_hh_move_higher": "انتقل إلى مكان مرتفع الآن. خذ الوثائق والدواء والماء.",
        "fl_hh_protect_documents": "ضع الوثائق والهاتف في كيس محكم في مكان مرتفع.",
        "fl_hh_avoid_crossing": "لا تعبر المياه الجارية. العمق غير مرئي.",
        "fl_hh_water_treatment": "اغلِ أو عالج كل مياه الشرب.",
        "fl_past_move_livestock": "انقل الماشية من السهل الفيضي إلى مكان مرتفع الآن.",
        "fl_farm_early_harvest": "احصد المحصول الناضج الآن.",
        "ht_hh_protect_vulnerable": "احمِ الأطفال وكبار السن والحوامل من الشمس وزد شرب الماء.",
        "fi_hh_nutrition_screening": "خذ الأطفال دون الخامسة إلى أقرب مركز صحي لفحص التغذية.",
    },
}

# "act within N days"
DEADLINE_PHRASE: dict[Language, str] = {
    Language.AMHARIC: "በ{days} ቀናት ውስጥ።",
    Language.TIGRINYA: "ኣብ ውሽጢ {days} መዓልቲ።",
    Language.OROMO: "Guyyaa {days} keessatti.",
    Language.ARABIC: "خلال {days} يوماً.",
}


def is_template_only(language: Language) -> bool:
    return language in TEMPLATE_ONLY


def render(
    plan: ActionPlan,
    admin_name: str,
    language: Language,
    *,
    char_limit: int,
    window_end: date | None = None,
) -> str | None:
    """Build an advisory from reviewed components.

    Returns None if we cannot express this plan in this language, which is the
    correct outcome: an honest silence beats a message a native speaker would
    not recognise as their language.
    """
    severity_words = SEVERITY_WORD.get(language)
    hazard_words = HAZARD_WORD.get(language)
    action_words = ACTION_TEXT.get(language)
    if not (severity_words and hazard_words and action_words):
        return None

    hazard = hazard_words.get(plan.hazard)
    severity = severity_words.get(plan.severity)
    if not hazard or not severity:
        log.info(
            "no %s template for %s/%s", language.value, plan.hazard.value,
            plan.severity.value,
        )
        return None

    # Place name stays in Latin script. Transliterating a district name into
    # Ge'ez risks producing something the recipient does not recognise, and
    # place names are exactly where an error is least recoverable.
    parts = [f"{severity} — {admin_name}: {hazard}."]

    used = 0
    for action in plan.actions:
        text = action_words.get(action.action_id)
        if not text:
            # No reviewed translation. Skip rather than machine-translate.
            log.info(
                "no %s translation for action %s — omitted",
                language.value, action.action_id,
            )
            continue
        candidate = " ".join(parts + [text])
        if len(candidate) > char_limit:
            break
        parts.append(text)
        used += 1
        if used >= 2:  # two actions is what a person acts on
            break

    if used == 0:
        return None

    deadline = DEADLINE_PHRASE.get(language)
    if deadline and plan.actions:
        phrase = deadline.format(days=plan.actions[0].deadline_days)
        if len(" ".join(parts + [phrase])) <= char_limit:
            parts.append(phrase)

    return " ".join(parts)


def coverage() -> dict[str, dict[str, int]]:
    """What a reviewer can actually check.

    Surfaced on the dashboard and in the README so the limits of the
    template path are visible rather than implied.
    """
    from . import actions as action_library

    total_actions = len(action_library.ACTIONS)
    return {
        language.value: {
            "actions_translated": len(ACTION_TEXT.get(language, {})),
            "actions_total": total_actions,
            "hazards_covered": len(HAZARD_WORD.get(language, {})),
        }
        for language in TEMPLATE_ONLY
    }
