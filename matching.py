"""
ZEIT AdCP Matching Engine v3.8
==============================

Konsolidierte Match- und Pricing-Engine fuer den ZEIT AdCP Pilot.
Loest die ursprungs-Architektur Option C aus Zwischenbericht Nr. 5 ein:
EINE matching.py mit Router und drei spezialisierten Score-Funktionen.

Architektur:
    parse_brief()        einheitlicher Parser, freier String-Brief
    Schema-Adapter       kapseln v3 Hybrid-Block-Strukturen
    score_print()        Print-Logik (Format, Branche, Audience, Budget)
    score_newsletter()   Newsletter-Logik (Topical, Audience, Brand, Reach, Budget)
    score_podcast()      Podcast-Logik (TKP, Cluster, Performance-Klasse)
    match_products()     Router, dispatcht nach product_type

Schema:
    Liest aus Schema v3.4 Hybrid-Block-Modell (Print/Newsletter/Podcast).

Changelog:
    v3.8: Multi-Channel-Erkennung erweitert (Fix A).
          Explizite Channel-Kombinationen im Brief ("aus Print und Newsletter",
          "Print, Newsletter und Podcast") werden jetzt erkannt und setzen
          mehrere Channel-Hints ohne Single-Channel-Malus (-15 Pkt).
          Betrifft parse_brief() Block A5b. Kein Eingriff in Scoring-Logik.

    v3.7: Score-Spread statt harte Schwelle. Top-1 immer drin, Top 2-N nur
          wenn Score >= 70% des Top-1-Score. Adaptive Logik: bei starkem
          Top-Match strenger filtern, bei vagem Brief grosszuegiger.
          AUDIENCE_KEYWORDS fuer Geschaeftsfuehrer / B2B / Entscheider
          erweitert: matcht jetzt auch auf "selbststaendige_und_unternehmer"
          (relevant fuer ZEIT FUER UNTERNEHMER).

    v3.6: Print-Bonus +15 Pkt. Channel-Hint-Filter haerter.
          KULTURKUNDE_HINTS, MULTI_CHANNEL_TRIGGERS, Topical-Tags erweitert.
          format_name_hint Alias-Mapping, Token-basierter Soft-Match.
          format_newsletter_schedule() FREQ_DE erweitert.

    v3.5: format_issues_summary() als eigenstaendige Funktion extrahiert.
          NameError-Fix fuer Erscheinungstermin-Queries.
"""

import re
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Tuple


# =====================================================
# Konstanten: product_type-Gruppen
# =====================================================

PRINT_TYPES = {
    "magazin", "sonderheft", "beilage", "wochenzeitung",
    "submagazin", "b2b_magazin", "kindermagazin",
}
NEWSLETTER_TYPES = {"newsletter"}
PODCAST_TYPES = {"podcast"}


# =====================================================
# Keyword-Kataloge
# =====================================================

AUDIENCE_KEYWORDS = {
    "luxus": ["luxusaffin"],
    "luxury": ["luxusaffin"],
    "premium": ["qualitaetsbewusste_konsumenten"],
    "qualitaet": ["qualitaetsbewusste_konsumenten"],
    "gehoben": ["qualitaetsbewusste_konsumenten"],
    "kultur": ["kulturpublikum", "kulturinteressierte"],
    "kunst": ["kunst_und_musik_interessierte", "kunstinteressierte", "kunstsammler"],
    "kunstinteressiert": ["kunst_und_musik_interessierte", "kunstinteressierte"],
    "kunstinteressierte": ["kunst_und_musik_interessierte", "kunstinteressierte"],
    "kunstaffin": ["kunst_und_musik_interessierte", "kunstinteressierte"],
    "kunstsammler": ["kunstsammler"],
    "galerie": ["galerist", "kunstsammler"],
    "galerien": ["galerist", "kunstsammler"],
    "galerist": ["galerist"],
    "kurator": ["kurator"],
    "musik": ["kunst_und_musik_interessierte"],
    "kulturschaffend": ["kulturschaffende"],
    "kulturschaffende": ["kulturschaffende"],
    "intellektuell": ["intellektuelle", "meinungsbildner"],
    "akademiker": ["akademisch_gebildete", "bildungsbuerger", "jung_akademisch"],
    "akademisch": ["akademisch_gebildete", "jung_akademisch"],
    "junge akademiker": ["jung_akademisch", "berufseinsteiger"],
    "junge akademikerinnen": ["jung_akademisch", "berufseinsteiger"],
    "berufseinsteiger": ["berufseinsteiger", "jung_akademisch"],
    "absolvent": ["berufseinsteiger", "jung_akademisch"],
    "absolventen": ["berufseinsteiger", "jung_akademisch"],
    "abiturient": ["abiturient", "schueler_oberstufe_und_studieninteressierte"],
    "abiturienten": ["abiturient", "schueler_oberstufe_und_studieninteressierte"],
    "schueler": ["schueler", "schueler_oberstufe_und_studieninteressierte"],
    "studieninteressiert": ["schueler_oberstufe_und_studieninteressierte"],
    "studieninteressierte": ["schueler_oberstufe_und_studieninteressierte"],
    "student": ["student", "studierende", "jung_akademisch"],
    "studenten": ["student", "studierende", "jung_akademisch"],
    "studierende": ["studierende", "student", "jung_akademisch"],
    "familie": ["familien"],
    "familien": ["familien"],
    "eltern": ["familien", "eltern"],
    "kinder": ["familien_mit_kindern", "familien"],
    "geschaeftsfuehrer": ["entscheider", "fuehrungskraefte", "selbststaendige_und_unternehmer", "unternehmer"],
    "geschaeftsfuehrung": ["entscheider", "fuehrungskraefte", "selbststaendige_und_unternehmer", "unternehmer"],
    "entscheider": ["entscheider", "fuehrungskraefte", "selbststaendige_und_unternehmer"],
    "b2b": ["entscheider", "fuehrungskraefte", "selbststaendige_und_unternehmer"],
    "b2b-software": ["entscheider", "fuehrungskraefte"],
    "b2b software": ["entscheider", "fuehrungskraefte"],
    "softwareanbieter": ["entscheider", "fuehrungskraefte", "selbststaendige_und_unternehmer"],
    "saas": ["entscheider", "fuehrungskraefte"],
    "selbststaendig": ["selbststaendige_und_unternehmer", "selbststaendige", "unternehmer"],
    "selbststaendige": ["selbststaendige_und_unternehmer", "selbststaendige", "unternehmer"],
    "unternehmer": ["selbststaendige_und_unternehmer", "selbststaendige", "unternehmer"],
    "manager": ["fuehrungskraefte"],
    "fuehrungskraft": ["fuehrungskraefte"],
    "fuehrungskraefte": ["fuehrungskraefte"],
    "einkommensstark": ["einkommensstarke"],
    "einkommensstarke": ["einkommensstarke"],
    "vermoegend": ["vermoegende"],
    "wohlhabend": ["vermoegende"],
}

INDUSTRY_KEYWORDS = {
    "uhren": "uhren_schmuck",
    "uhr": "uhren_schmuck",
    "schmuck": "uhren_schmuck",
    "luxus-uhr": "luxusgueter",
    "luxusuhr": "luxusgueter",
    "automobil": "premium_automobil",
    "auto": "premium_automobil",
    "fahrzeug": "premium_automobil",
    "mode": "mode_fashion",
    "fashion": "mode_fashion",
    "kosmetik": "beauty_cosmetics",
    "beauty": "beauty_cosmetics",
    "buchverlag": "buchverlage",
    "buchverlage": "buchverlage",
    "musikverlag": "musikverlage",
    "musikverlage": "musikverlage",
    "stiftung": "stiftungen",
    "stiftungen": "stiftungen",
    "bildung": "bildung_staatlich_anerkannt",
    "universitaet": "bildung_staatlich_anerkannt",
    "universitaeten": "bildung_staatlich_anerkannt",
    "hochschule": "bildung_staatlich_anerkannt",
    "reise": "reise_dach",
    "reisen": "reise_dach",
    "tourismus": "reise_dach",
    "immobilien": "immobilien",
    "finanz": "finanzdienstleistung",
    "bank": "finanzdienstleistung",
    "versicherung": "finanzdienstleistung",
    "kultur": "kulturinstitutionen",
    "museum": "kulturinstitutionen",
    "galerie": "kulturinstitutionen",
    "theater": "kulturinstitutionen",
    "konzerthaus": "kulturinstitutionen",
    "dating": "kennenlernen_dating",
    "partnervermittlung": "kennenlernen_dating",
    "employer": "employer_branding_alle_branchen_inkl_wirtschaft",
    "personalmarketing": "employer_branding_alle_branchen_inkl_wirtschaft",
    "recruiting": "employer_branding_alle_branchen_inkl_wirtschaft",
    "luxus": "luxusgueter",
    "premium-marke": "luxusgueter",
    "finanzdienst": "finanzdienstleister",
    "bank-werbung": "finanzdienstleister",
}

GOAL_KEYWORDS = {
    "bekanntheit": "awareness",
    "awareness": "awareness",
    "image": "image_aufbau",
    "imageaufbau": "image_aufbau",
    "abverkauf": "abverkauf",
    "verkauf": "abverkauf",
    "sales": "abverkauf",
    "performance": "performance",
    "leadgenerierung": "leadgenerierung",
    "leads": "leadgenerierung",
    "branding": "branding",
    "produktlaunch": "produkt_launch",
    "launch": "produkt_launch",
    "neueinfuehrung": "produkt_launch",
}

VALUE_KEYWORDS = {
    "nachhaltig": "nachhaltigkeit",
    "nachhaltigkeit": "nachhaltigkeit",
    "oeko": "nachhaltigkeit",
    "umwelt": "nachhaltigkeit",
    "klima": "nachhaltigkeit",
    "fair": "fairness",
    "fairness": "fairness",
    "verantwortung": "verantwortung",
    "tradition": "tradition",
    "handwerk": "tradition",
    "innovation": "innovation",
    "modern": "modernitaet",
    "modernitaet": "modernitaet",
    "exklusiv": "exklusivitaet",
    "exklusivitaet": "exklusivitaet",
    "diskretion": "diskretion",
    "qualitaet": "qualitaet",
    "design": "design",
    "asthetisch": "aesthetik",
    "aesthetik": "aesthetik",
    "aesthetisch": "aesthetik",
}

PREMIUM_INDICATORS = {
    "luxus", "luxury", "premium", "exklusiv", "high-end", "highend",
    "edel", "gehoben", "noble", "vermoegend",
}

CHANNEL_HINTS = {
    "magazin": "print",
    "magazine": "print",
    "heft": "print",
    "print": "print",
    "anzeige": "print",
    "wochenzeitung": "print",
    "zeitung": "print",
    "beilage": "print",
    "newsletter": "newsletter",
    "newsletter-werbung": "newsletter",
    "podcast": "podcast",
    "podcasts": "podcast",
    "audio-ad": "podcast",
    "preroll": "podcast",
    "midroll": "podcast",
    "postroll": "podcast",
}

KULTURKUNDE_HINTS = {
    "kulturkunde", "kulturpreis", "kultur-kunde",
    "buchverlag", "buchverlage", "musikverlag", "musikverlage",
    # Museen
    "museum", "stadtmuseum", "kunstmuseum", "landesmuseum",
    "historisches museum", "naturkundemuseum",
    # Buehne
    "oper", "philharmonie", "konzerthaus",
    "theater", "schauspielhaus", "staatstheater",
    # Kunst
    "galerie", "galerien", "kunsthalle", "kunstverein",
    # Bibliotheken
    "bibliothek", "stadtbibliothek",
    # Stiftungen
    "stiftung", "kulturstiftung",
}

PODCAST_AD_TYPE_HINTS = {
    "audio-ad-20s": "audio_ad_20s",
    "audio ad 20s": "audio_ad_20s",
    "20s": "audio_ad_20s",
    "20 sekunden": "audio_ad_20s",
    "native 30s": "native_audio_ad_30s",
    "native-30s": "native_audio_ad_30s",
    "native audio ad 30s": "native_audio_ad_30s",
    "30s": "native_audio_ad_30s",
    "30 sekunden": "native_audio_ad_30s",
    "native 60s": "native_audio_ad_60s",
    "60 sekunden": "native_audio_ad_60s",
    "native 240s": "native_audio_ad_240s",
    "240 sekunden": "native_audio_ad_240s",
    "240s": "native_audio_ad_240s",
}

PODCAST_SLOT_HINTS = {
    "preroll": "preroll",
    "pre-roll": "preroll",
    "pre roll": "preroll",
    "midroll": "midroll",
    "mid-roll": "midroll",
    "mid roll": "midroll",
    "postroll": "postroll",
    "post-roll": "postroll",
    "post roll": "postroll",
    "adbundle plus": "adbundle_plus",
    "adbundle+": "adbundle_plus",
    "ad-bundle plus": "adbundle_plus",
    "adbundle": "adbundle",
    "ad-bundle": "adbundle",
    "storytelling": "storytelling",
}

PREMIUM_TARGETING_HINTS = {
    "geo-targeting", "geo targeting", "plz-targeting", "postleitzahl",
    "contextual segment", "contextual segments", "kontext-segment",
    "predictive audience", "predictive segments",
    "lebensphasen", "lebensphase",
    "einkommens-segment", "einkommen-segment", "einkommensstark targeting",
    "premium-targeting", "premium targeting",
}


# =====================================================
# ParsedBrief
# =====================================================

@dataclass
class ParsedBrief:
    """
    Parsed Brief, channel-uebergreifend.
    Ein Brief kann gleichzeitig Print, Newsletter und Podcast triggern.
    """
    audience_tags: List[str] = field(default_factory=list)
    industry_tags: List[str] = field(default_factory=list)
    goal_tags: List[str] = field(default_factory=list)
    values_hints: List[str] = field(default_factory=list)
    premium_level_hint: bool = False
    gender_hint: Optional[str] = None
    b2b_hint: bool = False
    b2c_hint: bool = False
    budget_eur: Optional[float] = None
    channel_hints: List[str] = field(default_factory=list)

    # Print-spezifisch
    product_id_hint: Optional[str] = None
    format_name_hint: Optional[str] = None

    # Newsletter-spezifisch
    is_kulturkunde: bool = False
    preferred_format_id: Optional[str] = None

    # Podcast-spezifisch
    podcast_format_slot: Optional[str] = None
    podcast_ad_type_length: Optional[str] = None
    booked_audio_impressions: Optional[int] = None
    has_premium_targeting: bool = False

    # Format-Uebersicht (alle Formate eines Produkts anzeigen)
    wants_format_overview: bool = False

    # Erscheinungstermine / Anzeigenschluss-Abfrage
    wants_issue_dates: bool = False

    # Topical (fuer Newsletter)
    topical_tags: List[str] = field(default_factory=list)

    # Volltext
    raw_text: str = ""
    raw_lower: str = ""


def parse_brief(brief: str) -> ParsedBrief:
    """
    Parst einen freien String-Brief in einen ParsedBrief.
    Sammelt Signale fuer alle drei Channels gleichzeitig.
    """
    pb = ParsedBrief()
    pb.raw_text = brief or ""
    pb.raw_lower = pb.raw_text.lower()
    text = pb.raw_lower

    for kw, tags in AUDIENCE_KEYWORDS.items():
        if kw in text:
            tag_list = tags if isinstance(tags, list) else [tags]
            for t in tag_list:
                if t not in pb.audience_tags:
                    pb.audience_tags.append(t)

    for kw, tags in INDUSTRY_KEYWORDS.items():
        if kw in text:
            tag_list = tags if isinstance(tags, list) else [tags]
            for t in tag_list:
                if t not in pb.industry_tags:
                    pb.industry_tags.append(t)

    for kw, tag in GOAL_KEYWORDS.items():
        if kw in text:
            if tag not in pb.goal_tags:
                pb.goal_tags.append(tag)

    for kw, tag in VALUE_KEYWORDS.items():
        if kw in text:
            if tag not in pb.values_hints:
                pb.values_hints.append(tag)

    for ind in PREMIUM_INDICATORS:
        if ind in text:
            pb.premium_level_hint = True
            break

    if re.search(r"\b(maenner|maennlich|herren|m[\W_]+40|m[\W_]+50)", text):
        pb.gender_hint = "male"
    elif re.search(r"\b(frauen|weiblich|damen|f[\W_]+40|f[\W_]+50)", text):
        pb.gender_hint = "female"

    if "b2b" in text or "geschaeftskunde" in text or "geschaeftskunden" in text:
        pb.b2b_hint = True
    if "b2c" in text or "endkunde" in text or "konsument" in text or "verbraucher" in text:
        pb.b2c_hint = True

    bud_match = re.search(
        r"(?:budget|etat)[^\d]*([\d\.\,]+)\s*(?:k|tausend|tsd)?\s*(?:eur|euro|\€)",
        text
    )
    if not bud_match:
        bud_match = re.search(
            r"([\d\.\,]+)\s*(?:k|tausend|tsd)?\s*(?:eur|euro|\€)\s*(?:netto|brutto|n\.|b\.)?",
            text
        )
    if bud_match:
        raw = bud_match.group(1).replace(".", "").replace(",", ".")
        try:
            val = float(raw)
            ctx = bud_match.group(0)
            if re.search(r"\b(?:k|tausend|tsd)\s*(?:eur|euro)", ctx):
                val *= 1000
            pb.budget_eur = val
        except ValueError:
            pass

    for kw, ch in CHANNEL_HINTS.items():
        if kw in text:
            if ch not in pb.channel_hints:
                pb.channel_hints.append(ch)

    for kw in KULTURKUNDE_HINTS:
        if kw in text:
            pb.is_kulturkunde = True
            break

    if "doppelseite" in text or "opening spread" in text or "double page" in text:
        pb.format_name_hint = "Doppelseite"
    elif "1/1" in text or "ganzseit" in text or "1/1-seite" in text:
        pb.format_name_hint = "1/1 Seite"
    elif "1/2" in text or "halbe seite" in text:
        pb.format_name_hint = "1/2 Seite"
    elif "1/3" in text or "drittel seite" in text:
        pb.format_name_hint = "1/3 Seite"
    elif "1/4" in text or "viertel seite" in text:
        pb.format_name_hint = "1/4 Seite"
    elif re.search(r"\bu2\b", text):
        pb.format_name_hint = "U2"
    elif re.search(r"\bu3\b", text):
        pb.format_name_hint = "U3"
    elif re.search(r"\bu4\b", text):
        pb.format_name_hint = "U4"

    for kw, slot in PODCAST_SLOT_HINTS.items():
        if kw in text:
            pb.podcast_format_slot = slot
            break

    for kw, atl in PODCAST_AD_TYPE_HINTS.items():
        if kw in text:
            pb.podcast_ad_type_length = atl
            break

    ai_match = re.search(
        r"([\d\.\,]+)\s*(?:k|tausend|tsd|m|mio|mil|millionen)?\s*"
        r"(?:audio[\s_-]*impressions?|ai|hoererzahl|hoerer|impressions)",
        text
    )
    if ai_match:
        raw = ai_match.group(1).replace(".", "").replace(",", "")
        try:
            val = int(float(raw))
            ctx = ai_match.group(0)
            if re.search(r"\bk\b|tausend|tsd", ctx):
                val *= 1000
            elif re.search(r"\bm\b|mio|mil|millionen", ctx):
                val *= 1000000
            pb.booked_audio_impressions = val
        except ValueError:
            pass

    for kw in PREMIUM_TARGETING_HINTS:
        if kw in text:
            pb.has_premium_targeting = True
            break

    topical_candidates = [
        ("kultur", ["kultur", "kulturszene"]),
        ("kunst", ["kunst", "art"]),
        ("musik", ["musik", "music"]),
        ("literatur", ["literatur", "literature", "buecher", "lesen"]),
        ("film", ["film", "kino"]),
        ("theater", ["theater"]),
        ("politik", ["politik", "tagespolitik"]),
        ("tagesgeschehen", ["tagesgeschehen", "news", "nachrichten", "tagespolitik"]),
        ("wirtschaft", ["wirtschaft", "business"]),
        ("finanzen", ["finanzen", "geld"]),
        ("geld", ["geld", "finanzen"]),
        ("familie", ["familie", "familien"]),
        ("erziehung", ["erziehung", "kinder"]),
        ("kinder", ["kinder", "familien_mit_kindern"]),
        ("gesundheit", ["gesundheit", "health"]),
        ("ernaehrung", ["ernaehrung", "essen"]),
        ("fitness", ["fitness", "sport"]),
        ("wellness", ["wellness"]),
        ("reise", ["reise", "reisen", "travel"]),
        ("urlaub", ["urlaub", "reisen"]),
        ("tourismus", ["tourismus", "reise"]),
        ("wissenschaft", ["wissenschaft", "forschung"]),
        ("forschung", ["forschung", "wissenschaft"]),
        ("bildung", ["bildung", "schule", "studium"]),
        ("technologie", ["technologie", "tech", "digital"]),
        ("digital", ["digital", "technologie"]),
        ("ki", ["ki", "kuenstliche_intelligenz", "ai"]),
        ("kuenstliche intelligenz", ["ki", "kuenstliche_intelligenz", "ai"]),
        ("nachhaltigkeit", ["nachhaltigkeit", "umwelt", "klima"]),
        ("umwelt", ["umwelt", "nachhaltigkeit"]),
        ("klima", ["klima", "klimawandel", "nachhaltigkeit"]),
        ("sport", ["sport", "fitness"]),
        ("fussball", ["fussball", "sport"]),
        ("mode", ["mode", "fashion"]),
        ("lifestyle", ["lifestyle"]),
        ("verbrechen", ["verbrechen", "true_crime", "kriminalfaelle", "krimi"]),
        ("krimi", ["krimi", "true_crime", "verbrechen"]),
        ("true crime", ["true_crime", "verbrechen", "krimi"]),
        ("geschichte", ["geschichte", "historisch"]),
        ("historisch", ["historisch", "geschichte"]),
        ("schule", ["schule", "bildung"]),
        ("studium", ["studium", "studieren", "bildung"]),
        ("studieren", ["studieren", "studium", "bildung"]),
        ("auto", ["auto", "automobil", "mobilitaet"]),
        ("mobilitaet", ["mobilitaet", "automobil"]),
        ("immobilien", ["immobilien", "wohnen"]),
        ("wohnen", ["wohnen", "immobilien"]),
        ("buch", ["buch", "buecher", "literatur"]),
        ("buecher", ["buecher", "literatur"]),
        ("lesen", ["lesen", "literatur"]),
        # Stellenmarkt / Karriere (NEU v3.6)
        ("stellenmarkt", ["stellenmarkt", "jobs", "karriere", "stellenanzeige"]),
        ("jobmail", ["stellenmarkt", "jobs"]),
        ("job", ["jobs", "karriere", "stellenmarkt"]),
        ("jobs", ["jobs", "karriere", "stellenmarkt"]),
        ("karriere", ["karriere", "jobs", "berufseinstieg"]),
        ("stellenanzeige", ["stellenanzeige", "stellenmarkt", "jobs"]),
        # Studium / Hochschule (NEU v3.6)
        ("masterstudium", ["masterstudium", "studium", "bildung"]),
        ("master", ["masterstudium", "studium", "bildung"]),
        ("bachelor", ["studium", "bildung", "berufseinsteiger"]),
        ("absolvent", ["studium", "berufseinstieg", "berufseinsteiger"]),
        ("absolventen", ["studium", "berufseinstieg", "berufseinsteiger"]),
        ("hochschule", ["hochschule", "studium", "bildung"]),
        # Finanzen / Banking (NEU v3.6)
        ("vermoegen", ["vermoegen", "private_banking", "wealth"]),
        ("vermoegensverwaltung", ["private_banking", "wealth", "finanzen"]),
        ("private banking", ["private_banking", "wealth"]),
        ("privatbank", ["private_banking", "finanzen"]),
        ("bank", ["finanzen", "private_banking"]),
        ("anlage", ["finanzen", "vermoegen"]),
        ("investment", ["finanzen", "investment"]),
    ]
    for trigger, expansions in topical_candidates:
        if trigger in text:
            for tag in expansions:
                if tag not in pb.topical_tags:
                    pb.topical_tags.append(tag)

    # A5: Multi-Channel-Trigger ("kanaluebergreifend" -> alle drei Channels)
    MULTI_CHANNEL_TRIGGERS = [
        "kanaluebergreifend", "kanalübergreifend", "crossmedial",
        "multichannel", "ueber alle kanaele", "über alle kanäle",
        "alle kanaele", "alle kanäle",
    ]
    for trigger in MULTI_CHANNEL_TRIGGERS:
        if trigger in text:
            for ch in ("print", "newsletter", "podcast"):
                if ch not in pb.channel_hints:
                    pb.channel_hints.append(ch)
            break

    # A5b: Explizite Mehrkanal-Kombinationen im Brief ("aus Print und Newsletter" etc.)
    # Erkennt wenn User zwei oder drei Channels explizit benennt -> alle genannten Channels setzen
    # ohne Malus auf nicht-genannte Channels (da kein Single-Channel-Filter greift)
    _detected = set(pb.channel_hints)
    _has_print = "print" in _detected
    _has_newsletter = "newsletter" in _detected
    _has_podcast = "podcast" in _detected
    _multi = sum([_has_print, _has_newsletter, _has_podcast])
    if _multi >= 2:
        # Schon mehrere Channels erkannt -> nichts tun, passt
        pass
    else:
        # Pruefe ob Brief zwei oder mehr Channels explizit kombiniert
        _combo_print = any(kw in text for kw in ("print", "magazin", "magazine", "heft", "zeitung", "beilage", "anzeige"))
        _combo_nl = any(kw in text for kw in ("newsletter",))
        _combo_pod = any(kw in text for kw in ("podcast", "podcasts", "audio-ad"))
        _combo_count = sum([_combo_print, _combo_nl, _combo_pod])
        if _combo_count >= 2:
            if _combo_print and "print" not in pb.channel_hints:
                pb.channel_hints.append("print")
            if _combo_nl and "newsletter" not in pb.channel_hints:
                pb.channel_hints.append("newsletter")
            if _combo_pod and "podcast" not in pb.channel_hints:
                pb.channel_hints.append("podcast")

    FORMAT_OVERVIEW_TRIGGERS = [
        "alle formate", "preisliste", "welche formate", "formatuebersicht",
        "formatliste", "format uebersicht", "anzeigenformate",
        "welche anzeigen", "was kostet werbung", "alle anzeigenformate",
    ]
    for trigger in FORMAT_OVERVIEW_TRIGGERS:
        if trigger in text:
            pb.wants_format_overview = True
            break

    ISSUE_DATE_TRIGGERS = [
        "wann erscheint", "erscheinungstermin", "erscheinungstermine",
        "anzeigenschluss", "anzeigenschluesse", "druckunterlagenschluss",
        "materialschluss", "wann kommt", "naechste ausgabe",
        "ausgabentermine", "terminplan", "mediaplan", "erscheinungsdaten",
    ]
    for trigger in ISSUE_DATE_TRIGGERS:
        if trigger in text:
            pb.wants_issue_dates = True
            break

    return pb


# =====================================================
# Schema-Adapter
# =====================================================

def get_audience(product: dict) -> dict:
    return product.get("audience") or {}

def get_matching_metadata(product: dict) -> dict:
    return product.get("matching_metadata") or {}

def get_reach(product: dict) -> dict:
    return product.get("reach") or {}

def get_pricing_models(product: dict) -> List[str]:
    pm = product.get("pricing_models")
    if isinstance(pm, list):
        return [m for m in pm if isinstance(m, str)]
    return []

def get_print_specifics(product: dict) -> dict:
    return product.get("print_specifics") or {}

def get_newsletter_specifics(product: dict) -> dict:
    return product.get("newsletter_specifics") or {}

def get_podcast_specifics(product: dict) -> dict:
    return product.get("podcast_specifics") or {}

def get_issues(product: dict) -> List[dict]:
    issues = product.get("issues") or []
    if not issues:
        issues = get_print_specifics(product).get("issues") or []
    return issues

def get_print_ad_formats(product: dict) -> List[dict]:
    ps = get_print_specifics(product)
    return ps.get("ad_formats") or []

def get_print_advertiser_matching(product: dict) -> dict:
    ps = get_print_specifics(product)
    return ps.get("advertiser_matching") or {}

def get_newsletter_pricing(product: dict) -> dict:
    ns = get_newsletter_specifics(product)
    return ns.get("pricing") or {}

def get_newsletter_formats(product: dict) -> List[dict]:
    return get_newsletter_pricing(product).get("formats") or []

def get_newsletter_pricing_model(product: dict) -> Optional[str]:
    models = get_pricing_models(product)
    if models:
        return models[0]
    return get_newsletter_pricing(product).get("pricing_model")

def get_newsletter_parent_relationship(product: dict) -> dict:
    ns = get_newsletter_specifics(product)
    return ns.get("parent_relationship") or {}

def get_podcast_pricing_model(product: dict) -> Optional[str]:
    models = get_pricing_models(product)
    if models:
        return models[0]
    return None

def get_podcast_fixed_placement_pricing(product: dict) -> dict:
    ps = get_podcast_specifics(product)
    return ps.get("fixed_placement_pricing") or {}

def get_podcast_tkp_pricing(product: dict) -> dict:
    ps = get_podcast_specifics(product)
    return ps.get("tkp_pricing") or {}

def get_cross_referenced_products(product: dict) -> List[dict]:
    cr = product.get("cross_referenced_products")
    return cr if isinstance(cr, list) else []


# =====================================================
# Hilfsfunktionen: Termine und Schedule
# =====================================================

def format_newsletter_schedule(product: dict) -> str:
    """
    Gibt den Versandrhythmus eines Newsletters als lesbaren String zurueck.
    Liest aus newsletter_specifics.channel.
    """
    ns = get_newsletter_specifics(product)
    channel = ns.get("channel") or {}

    frequency = channel.get("frequency", "")
    pub_days = channel.get("publication_days") or []
    issues_per_year = channel.get("issues_per_year")

    DAY_DE = {
        "mon": "Montag", "tue": "Dienstag", "wed": "Mittwoch",
        "thu": "Donnerstag", "fri": "Freitag", "sat": "Samstag", "sun": "Sonntag"
    }
    FREQ_DE = {
        "daily": "taeglich",
        "daily_workdays": "werktaeglich",
        "weekly": "woechentlich",
        "biweekly": "zweiwoechentlich",
        "bi_weekly": "zweiwoechentlich",
        "monthly": "monatlich",
        "irregular": "unregelmaessig",
        "per_issue": "pro Magazinausgabe",
    }

    parts = []
    if frequency:
        parts.append(FREQ_DE.get(frequency, frequency))
    if pub_days:
        day_names = [DAY_DE.get(d.lower(), d) for d in pub_days]
        parts.append(f"jeden {' / '.join(day_names)}")
    if issues_per_year:
        parts.append(f"{issues_per_year}x pro Jahr")

    return ", ".join(parts) if parts else ""


def format_issues_summary(product: dict) -> str:
    """
    Erzeugt einen lesbaren Terminplan aus den Issues eines Produkts.
    Gibt ET, Anzeigenschluss und Druckunterlagenschluss aus.
    Nur Ausgaben ab 2026.
    """
    issues = get_issues(product)
    if not issues:
        return ""

    lines = []
    for issue in issues:
        pub = issue.get("publication_date", "")
        booking = issue.get("booking_deadline", "")
        material = issue.get("material_deadline", "")
        issue_id = issue.get("issue_id", "")
        themes = issue.get("issue_themes") or []
        special = issue.get("special_theme") or ""

        if not pub.startswith("2026"):
            continue

        label = f"Ausgabe {issue_id}" if issue_id else "Ausgabe"
        theme_str = ""
        if special:
            theme_str = f" | Thema: {special}"
        elif themes:
            theme_str = f" | Thema: {', '.join(themes)}"

        line = f"ET {pub}{theme_str}"
        if booking:
            line += f" | AS: {booking}"
        if material and material != booking:
            line += f" | DU: {material}"
        lines.append(line)

    return "\n".join(lines)


# =====================================================
# Gemeinsame Score-Komponenten
# =====================================================

def score_audience(parsed: ParsedBrief, product: dict) -> Tuple[float, List[str]]:
    score = 0.0
    reasons = []
    if not parsed.audience_tags:
        return score, reasons

    aud = get_audience(product)
    pt = product.get("product_type")

    tags = set()
    for k in ("primary", "secondary"):
        v = aud.get(k)
        if isinstance(v, list):
            tags.update(v)
    for k in ("primary_segments", "interests"):
        v = aud.get(k)
        if isinstance(v, list):
            tags.update(v)
    if pt == "podcast":
        ps = get_podcast_specifics(product)
        ad = ps.get("audience_detail") or {}
        for k in ("primary_segments", "interests", "lebensphasen"):
            v = ad.get(k)
            if isinstance(v, list):
                tags.update(v)

    mm = get_matching_metadata(product)
    sig = mm.get("audience_signals")
    if isinstance(sig, list):
        tags.update(sig)
    elif isinstance(sig, dict):
        for v in sig.values():
            if isinstance(v, list):
                tags.update(v)

    if not tags:
        return score, reasons

    tags_lower = {t.lower() if isinstance(t, str) else "" for t in tags}
    brief_lower = {t.lower() for t in parsed.audience_tags}
    overlap = brief_lower & tags_lower

    if overlap:
        s = min(30, len(overlap) * 12)
        score += s
        reasons.append(f"Zielgruppen-Match: {', '.join(sorted(overlap))} (+{s:.0f} Pkt)")

    return score, reasons


def score_industry(parsed: ParsedBrief, product: dict) -> Tuple[float, List[str]]:
    score = 0.0
    reasons = []
    if not parsed.industry_tags:
        return score, reasons

    pt = product.get("product_type")
    industries = set()

    if pt in PRINT_TYPES:
        am = get_print_advertiser_matching(product)
        cats = am.get("suitable_advertiser_categories") or []
        if isinstance(cats, list):
            industries.update(cats)

    mm = get_matching_metadata(product)
    brh = mm.get("buyer_relevance_hints")
    if isinstance(brh, list):
        industries.update(brh)
    elif isinstance(brh, dict):
        for v in brh.values():
            if isinstance(v, list):
                industries.update(v)
            elif isinstance(v, str):
                industries.add(v)

    if not industries:
        return score, reasons

    ind_lower = {i.lower() if isinstance(i, str) else "" for i in industries}
    brief_lower = {i.lower() for i in parsed.industry_tags}
    overlap = brief_lower & ind_lower

    if not overlap:
        soft = []
        for bt in brief_lower:
            for nt in ind_lower:
                if bt and nt and bt in nt:
                    soft.append(bt)
                    break
        if soft:
            s = min(15, len(soft) * 8)
            score += s
            reasons.append(f"Branchen-Soft-Match: {', '.join(sorted(set(soft)))} (+{s:.0f} Pkt)")
        return score, reasons

    if overlap:
        s = min(25, len(overlap) * 12)
        score += s
        reasons.append(f"Branchen-Match: {', '.join(sorted(overlap))} (+{s:.0f} Pkt)")

    return score, reasons


def score_goals(parsed: ParsedBrief, product: dict) -> Tuple[float, List[str]]:
    score = 0.0
    reasons = []
    if not parsed.goal_tags:
        return score, reasons

    goals = set()
    if product.get("product_type") in PRINT_TYPES:
        am = get_print_advertiser_matching(product)
        g = am.get("communication_goals") or []
        if isinstance(g, list):
            goals.update(g)

    if not goals:
        return score, reasons

    overlap = set(parsed.goal_tags) & goals
    if overlap:
        s = min(15, len(overlap) * 8)
        score += s
        reasons.append(f"Ziel-Match: {', '.join(sorted(overlap))} (+{s:.0f} Pkt)")

    return score, reasons


def score_values(parsed: ParsedBrief, product: dict) -> Tuple[float, List[str]]:
    score = 0.0
    reasons = []
    if not parsed.values_hints:
        return score, reasons

    text_fields = []
    for k in ("editorial_focus", "description_long", "brand_claim"):
        v = product.get(k)
        if isinstance(v, str):
            text_fields.append(v.lower())
    text = " ".join(text_fields)

    matches = []
    for val in parsed.values_hints:
        if val in text:
            matches.append(val)

    if matches:
        s = min(10, len(matches) * 4)
        score += s
        reasons.append(f"Werte-Match: {', '.join(matches)} (+{s:.0f} Pkt)")

    return score, reasons


def score_premium_level(parsed: ParsedBrief, product: dict) -> Tuple[float, List[str]]:
    score = 0.0
    reasons = []
    if not parsed.premium_level_hint:
        return score, reasons

    aud = get_audience(product)
    for k, threshold in [
        ("hhne_above_threshold_pct", 50),
        ("hhne_7000_plus_pct", 25),
        ("socioeconomic_status_top_tiers_pct", 50),
        ("education_abitur_or_higher_pct", 60),
    ]:
        v = aud.get(k)
        if isinstance(v, (int, float)) and v >= threshold:
            score += 5
            reasons.append(f"Premium-Indikator {k}={v} (+5 Pkt)")

    mm = get_matching_metadata(product)
    sig = mm.get("audience_signals")
    if isinstance(sig, dict):
        for v in sig.values():
            if isinstance(v, list):
                if any("premium" in s.lower() or "luxus" in s.lower() or "vermoeg" in s.lower()
                       for s in v if isinstance(s, str)):
                    score += 5
                    reasons.append("Premium-Audience-Signal in matching_metadata (+5 Pkt)")
                    break

    score = min(score, 20)
    return score, reasons


def score_topical(parsed: ParsedBrief, product: dict) -> Tuple[float, List[str]]:
    score = 0.0
    reasons = []
    if not parsed.topical_tags:
        return score, reasons

    mm = get_matching_metadata(product)
    tags = mm.get("topical_tags") or []
    if not isinstance(tags, list):
        return score, reasons

    nl_tag_set = {t.lower() if isinstance(t, str) else "" for t in tags}
    brief_tag_set = {t.lower() for t in parsed.topical_tags}
    overlap = nl_tag_set & brief_tag_set

    if not overlap:
        soft = []
        for bt in brief_tag_set:
            for nt in nl_tag_set:
                if bt and nt and (bt in nt or nt in bt):
                    soft.append(bt)
                    break
        if soft:
            ratio = len(soft) / max(len(brief_tag_set), 1)
            s = round(20 * ratio)
            score += s
            reasons.append(f"Topical-Soft-Match: {', '.join(sorted(set(soft)))} (+{s:.0f} Pkt)")
            return score, reasons
        return score, reasons

    ratio = len(overlap) / max(len(brief_tag_set), 1)
    s = round(35 * ratio)
    score += s
    reasons.append(f"Topical-Match: {', '.join(sorted(overlap))} (+{s:.0f} Pkt)")
    return score, reasons


def score_counter_indicators(parsed: ParsedBrief, product: dict) -> Tuple[float, List[str]]:
    score = 0.0
    reasons = []
    mm = get_matching_metadata(product)
    counters = mm.get("counter_indicators") or []
    if not isinstance(counters, list):
        return score, reasons

    text = parsed.raw_lower
    hits = []
    for c in counters:
        if isinstance(c, str) and c.lower() in text:
            hits.append(c)

    if hits:
        score -= min(20, len(hits) * 8)
        reasons.append(f"Counter-Indikator: {', '.join(hits)} ({score:.0f} Pkt)")

    return score, reasons


def score_brand_proximity(parsed: ParsedBrief, product: dict) -> Tuple[float, List[str]]:
    score = 0.0
    reasons = []
    mm = get_matching_metadata(product)
    bp = mm.get("brand_proximity")

    if isinstance(bp, str):
        if bp == "magazine_companion":
            score += 15
        elif bp == "thematic_sibling":
            score += 10
        elif bp == "regional_companion":
            score += 12
        elif bp == "brand_member":
            score += 6
        if score > 0:
            reasons.append(f"Brand-Proximity: {bp} (+{score:.0f} Pkt)")
            return score, reasons

    pr = get_newsletter_parent_relationship(product)
    rel_type = pr.get("relationship_type")
    proximity_map = {
        "magazine_companion": 15,
        "thematic_sibling": 10,
        "regional_companion": 12,
        "brand_member": 6,
        "newsletter_of": 8,
    }
    if rel_type in proximity_map:
        s = proximity_map[rel_type]
        score += s
        reasons.append(f"Newsletter-Brand-Proximity: {rel_type} (+{s:.0f} Pkt)")

    return score, reasons


def score_product_id_hint(parsed: ParsedBrief, product: dict) -> Tuple[float, List[str]]:
    score = 0.0
    reasons = []
    if not parsed.product_id_hint:
        name = product.get("product_name") or ""
        if name and len(name) > 4:
            name_lower = name.lower()
            # 1. Exakter Substring-Match (Vollname im Brief)
            if name_lower in parsed.raw_lower:
                score += 50
                reasons.append(f"Produkt explizit im Brief genannt: {name} (+50 Pkt)")
                return score, reasons
            # 2. Token-Match: mind. 2 markante Woerter aus Produktname im Brief
            tokens = [t for t in re.split(r'\W+', name_lower) if len(t) > 3]
            # Stopwords ausfiltern, die in vielen Produkten vorkommen
            stopwords = {"zeit", "newsletter", "podcast", "magazin", "die", "der", "das"}
            distinctive_tokens = [t for t in tokens if t not in stopwords]
            if len(distinctive_tokens) >= 2:
                hits = sum(1 for t in distinctive_tokens if t in parsed.raw_lower)
                if hits >= 2:
                    score += 35
                    reasons.append(f"Produktname-Tokens im Brief: {name} (+35 Pkt)")
                    return score, reasons
                elif hits == 1 and len(distinctive_tokens) <= 2:
                    score += 20
                    reasons.append(f"Produktname-Token im Brief: {name} (+20 Pkt)")
                    return score, reasons
            elif len(distinctive_tokens) == 1:
                if distinctive_tokens[0] in parsed.raw_lower:
                    score += 25
                    reasons.append(f"Produktname-Token im Brief: {name} (+25 Pkt)")
                    return score, reasons
        return score, reasons

    if product.get("product_id") == parsed.product_id_hint:
        score += 50
        reasons.append(
            f"Produkt explizit im Brief genannt: {product.get('product_name')} (+50 Pkt)"
        )
    return score, reasons


# =====================================================
# Print: Pricing-Resolver
# =====================================================

def resolve_print_format(parsed: ParsedBrief, product: dict) -> Optional[dict]:
    formats = get_print_ad_formats(product)
    if not formats:
        return None

    # Alias-Mapping: gleiches Format kann je Produkt unterschiedlich heissen
    FORMAT_ALIASES = {
        "Doppelseite": ["Doppelseite", "2/1 Seite", "Opening Spread", "Double Page"],
        "1/1 Seite": ["1/1 Seite", "Ganzseite", "Vollseite"],
        "1/2 Seite": ["1/2 Seite", "1/2 Seite hoch", "1/2 Seite quer", "Halbe Seite"],
        "1/3 Seite": ["1/3 Seite", "1/3 Seite hoch", "1/3 Seite quer"],
        "1/4 Seite": ["1/4 Seite", "1/4 Seite hoch", "1/4 Seite quer", "1/4 Seite Magazinformat"],
    }

    if parsed.format_name_hint:
        candidates = FORMAT_ALIASES.get(parsed.format_name_hint, [parsed.format_name_hint])
        for af in formats:
            if af.get("format_name") in candidates:
                return af

    in_scope = [af for af in formats if af.get("mvp_in_scope") and af.get("price_net_eur")]
    if not in_scope:
        in_scope = [af for af in formats if af.get("price_net_eur")]
    if not in_scope:
        return None

    if parsed.budget_eur:
        affordable = [af for af in in_scope if af["price_net_eur"] <= parsed.budget_eur]
        if affordable:
            return max(affordable, key=lambda af: af["price_net_eur"])

    for pref in ("1/1 Seite", "Doppelseite"):
        for af in in_scope:
            if af.get("format_name") == pref:
                return af

    return max(in_scope, key=lambda af: af["price_net_eur"])


def resolve_all_print_formats(product: dict) -> List[dict]:
    """
    Gibt alle verfuegbaren Print-Formate zurueck (ad_formats + special_formats).
    Fuer den Format-Uebersicht-Modus ("alle Formate").
    """
    ps = get_print_specifics(product)
    result = []

    for af in get_print_ad_formats(product):
        if af.get("price_net_eur"):
            result.append(af)

    SPECIAL_FORMAT_LABELS = {
        "panorama_anzeige_klein": "Panorama-Anzeige (klein)",
        "panorama_anzeige_gross": "Panorama-Anzeige (gross)",
        "panorama_tunnelanzeige": "Panorama-Tunnelanzeige",
        "center_page": "Center-Page",
        "l_anzeige": "L-Anzeige",
        "saeulen_anzeige": "Saeulen-Anzeige",
        "insel_anzeige": "Insel-Anzeige",
        "sandwich_anzeige": "Sandwich-Anzeige",
        "tunnel_anzeige": "Tunnel-Anzeige",
        "titelkopf_anzeige": "Titelkopf-Anzeige",
        "ressortkopf_anzeige": "Ressortkopf-Anzeige",
        "half_cover": "Half Cover",
    }
    for sf in ps.get("special_formats") or []:
        if sf.get("price_net_eur") and sf.get("mvp_in_scope", True):
            sf_type = sf.get("special_format_type", "")
            label = SPECIAL_FORMAT_LABELS.get(sf_type, sf_type.replace("_", " ").title())
            result.append({
                "format_name": label,
                "format_type": "sonderformat",
                "price_net_eur": sf["price_net_eur"],
                "notes": sf.get("price_note"),
                "mvp_in_scope": True,
            })

    return result


def check_industry_discount(parsed: ParsedBrief, product: dict, fmt: Optional[dict]) -> Optional[dict]:
    if not fmt or not parsed.industry_tags:
        return None
    discounts = fmt.get("industry_discounts") or []
    if not discounts:
        return None
    d = discounts[0]
    return {
        "cluster_label": d.get("cluster_key"),
        "discount_pct": d.get("discount_pct"),
        "price_net_eur": d.get("price_net_eur"),
        "price_original_eur": fmt.get("price_net_eur"),
    }


# =====================================================
# Print: Score-Funktion
# =====================================================

def score_print(parsed: ParsedBrief, product: dict) -> Tuple[float, List[str], List[str], Optional[dict], Optional[dict], List[dict]]:
    """
    Returns: (score, reasons, assumptions, best_format, discount_info, format_candidates)
    """
    score = 0.0
    reasons = []
    assumptions = []

    s, r = score_product_id_hint(parsed, product)
    score += s; reasons += r

    if parsed.format_name_hint:
        formats = get_print_ad_formats(product)
        if any(af.get("format_name") == parsed.format_name_hint for af in formats):
            score += 25
            reasons.append(f"Format explizit im Brief genannt: {parsed.format_name_hint} (+25 Pkt)")

    s, r = score_audience(parsed, product); score += s; reasons += r
    has_audience_match = s > 0

    s, r = score_industry(parsed, product); score += s; reasons += r
    has_industry_match = s > 0

    s, r = score_goals(parsed, product); score += s; reasons += r
    s, r = score_values(parsed, product); score += s; reasons += r
    s, r = score_premium_level(parsed, product); score += s; reasons += r
    s, r = score_topical(parsed, product); score += s; reasons += r
    s, r = score_counter_indicators(parsed, product); score += s; reasons += r

    aud = get_audience(product)
    if parsed.gender_hint:
        product_gender = aud.get("gender_primary")
        if product_gender == parsed.gender_hint:
            score += 20
            reasons.append(f"Geschlecht exakt passend ({product_gender}, +20 Pkt)")
        elif product_gender == "all":
            score += 5
            reasons.append("Geschlecht passt (all, +5 Pkt)")
        elif product_gender and product_gender != parsed.gender_hint:
            score -= 20
            reasons.append(f"Geschlecht passt nicht ({product_gender} vs. {parsed.gender_hint}, -20 Pkt)")

    ctx = aud.get("context")
    if parsed.b2b_hint and ctx == "b2b":
        score += 10; reasons.append("B2B-Kontext passt (+10 Pkt)")
    elif parsed.b2c_hint and ctx == "b2c":
        score += 10; reasons.append("B2C-Kontext passt (+10 Pkt)")
    elif parsed.b2b_hint and ctx == "b2c":
        score -= 15; reasons.append("B2B gesucht, Produkt ist B2C (-15 Pkt)")
    elif parsed.b2c_hint and ctx == "b2b":
        score -= 15; reasons.append("B2C gesucht, Produkt ist B2B (-15 Pkt)")

    best_format = resolve_print_format(parsed, product)
    discount = check_industry_discount(parsed, product, best_format) if best_format else None

    has_content_match = has_audience_match or has_industry_match or score >= 50
    if parsed.budget_eur and has_content_match:
        formats = get_print_ad_formats(product)
        af_in_scope = [af for af in formats if af.get("price_net_eur")]
        if af_in_scope:
            min_price = min(af["price_net_eur"] for af in af_in_scope)
            max_price = max(af["price_net_eur"] for af in af_in_scope)
            if parsed.budget_eur >= min_price:
                if parsed.budget_eur >= max_price * 0.5:
                    score += 20
                    reasons.append(
                        f"Budget {parsed.budget_eur:.0f} EUR reicht gut "
                        f"(ab {min_price:.0f} EUR, +20 Pkt)"
                    )
                else:
                    score += 10
                    reasons.append(
                        f"Budget {parsed.budget_eur:.0f} EUR reicht fuer kleinere Formate "
                        f"(ab {min_price:.0f} EUR, +10 Pkt)"
                    )
            else:
                score -= 30
                reasons.append(
                    f"Budget {parsed.budget_eur:.0f} EUR unter Mindestpreis "
                    f"({min_price:.0f} EUR, -30 Pkt)"
                )
                assumptions.append(
                    f"Budget unter Mindestbuchung. Kleinstes Format kostet "
                    f"{min_price:.0f} EUR (Listenpreis netto zzgl. MwSt.)."
                )

    if best_format:
        assumptions.insert(0, (
            f"Empfohlenes Format: {best_format.get('format_name')} "
            f"fuer {best_format.get('price_net_eur')} EUR (Listenpreis netto zzgl. MwSt.)"
        ))
        if discount and discount.get("discount_pct"):
            assumptions.append(
                f"Industry-Discount: {discount.get('discount_pct')}% "
                f"({discount.get('cluster_label')}) -> "
                f"{discount.get('price_net_eur')} EUR statt "
                f"{discount.get('price_original_eur')} EUR"
            )

    format_candidates = []
    if parsed.wants_format_overview:
        format_candidates = resolve_all_print_formats(product)
    elif not parsed.format_name_hint:
        all_fmts = get_print_ad_formats(product)
        format_candidates = [af for af in all_fmts if af.get("price_net_eur")]

    return score, reasons, assumptions, best_format, discount, format_candidates


# =====================================================
# Newsletter: Pricing-Resolver
# =====================================================

def resolve_newsletter_price(
    product: dict,
    definitions: Optional[dict],
    requested_format_id: Optional[str],
    advertiser_industry: Optional[str],
    is_kulturkunde: bool,
) -> dict:
    reasoning = []
    errors = []
    pricing = get_newsletter_pricing(product)
    formats = get_newsletter_formats(product)
    pricing_model = get_newsletter_pricing_model(product)

    if not formats:
        errors.append("Keine Newsletter-Formate im Produkt.")
        return _empty_price_result(requested_format_id, pricing_model, errors)

    fmt = None
    if requested_format_id:
        fmt = next((f for f in formats if f.get("format_id") == requested_format_id), None)
        if not fmt:
            errors.append(f"Format '{requested_format_id}' nicht verfuegbar.")
    if not fmt:
        fmt = formats[0]

    fmt_id = fmt.get("format_id")
    reasoning.append(f"Newsletter: {product.get('product_id')}")
    reasoning.append(f"Pricing-Modell: {pricing_model}")
    reasoning.append(f"Format: {fmt_id}")

    if pricing_model == "flat":
        price = fmt.get("price_eur_net")
        if price is None:
            errors.append("Kein Festpreis im flat-Modell.")
            return _empty_price_result(fmt_id, pricing_model, errors)
        return {
            "price_eur_net": price,
            "price_unit": fmt.get("price_unit"),
            "applied_pricing_model": "flat",
            "applied_cluster": None,
            "applied_kulturpreis": False,
            "format_id": fmt_id,
            "format_display_name": fmt.get("format_display_name"),
            "reasoning": reasoning,
            "errors": errors,
        }

    if pricing_model == "four_cluster":
        cluster_id = _resolve_newsletter_cluster(advertiser_industry, definitions, reasoning)
        cluster_prices = fmt.get("cluster_prices") or {}
        applicable = pricing.get("applicable_clusters")
        if applicable and cluster_id not in applicable:
            reasoning.append(
                f"Cluster '{cluster_id}' nicht anwendbar (applicable: {applicable}). Fallback grundpreis."
            )
            cluster_id = "grundpreis"
        price = cluster_prices.get(cluster_id) if isinstance(cluster_prices, dict) else None
        if price is None:
            errors.append(f"Kein Preis fuer Cluster '{cluster_id}'.")
            return _empty_price_result(fmt_id, pricing_model, errors)
        return {
            "price_eur_net": price,
            "price_unit": fmt.get("price_unit"),
            "applied_pricing_model": "four_cluster",
            "applied_cluster": cluster_id,
            "applied_kulturpreis": False,
            "format_id": fmt_id,
            "format_display_name": fmt.get("format_display_name"),
            "reasoning": reasoning,
            "errors": errors,
        }

    if pricing_model == "dual_column":
        kp = fmt.get("kulturpreis_eur_net")
        if is_kulturkunde and kp is not None:
            return {
                "price_eur_net": kp,
                "price_unit": fmt.get("price_unit"),
                "applied_pricing_model": "dual_column",
                "applied_cluster": None,
                "applied_kulturpreis": True,
                "format_id": fmt_id,
                "format_display_name": fmt.get("format_display_name"),
                "reasoning": reasoning,
                "errors": errors,
            }
        price = fmt.get("price_eur_net")
        if price is None:
            errors.append("Grundpreis im dual_column-Modell fehlt.")
            return _empty_price_result(fmt_id, pricing_model, errors)
        return {
            "price_eur_net": price,
            "price_unit": fmt.get("price_unit"),
            "applied_pricing_model": "dual_column",
            "applied_cluster": None,
            "applied_kulturpreis": False,
            "format_id": fmt_id,
            "format_display_name": fmt.get("format_display_name"),
            "reasoning": reasoning,
            "errors": errors,
        }

    errors.append(f"Unbekanntes pricing_model: '{pricing_model}'")
    return _empty_price_result(fmt_id, pricing_model, errors)


def _resolve_newsletter_cluster(
    industry: Optional[str], definitions: Optional[dict], reasoning: List[str]
) -> str:
    if not industry:
        reasoning.append("Keine Branche im Brief, Fallback grundpreis.")
        return "grundpreis"
    if not definitions:
        reasoning.append("Definitions nicht verfuegbar, Fallback grundpreis.")
        return "grundpreis"

    clusters = definitions.get("industry_clusters") or {}
    for cluster_id, cluster_def in clusters.items():
        if cluster_id == "grundpreis":
            continue
        if not isinstance(cluster_def, dict):
            continue
        branchen = cluster_def.get("branches") or cluster_def.get("branchen") or []
        if industry in branchen:
            reasoning.append(f"Branche '{industry}' -> Cluster '{cluster_id}'")
            return cluster_id

    reasoning.append(f"Branche '{industry}' nicht gemappt, Fallback grundpreis.")
    return "grundpreis"


def _empty_price_result(fmt_id, model, errors) -> dict:
    return {
        "price_eur_net": None,
        "price_unit": None,
        "applied_pricing_model": model,
        "applied_cluster": None,
        "applied_kulturpreis": False,
        "format_id": fmt_id,
        "format_display_name": None,
        "reasoning": [],
        "errors": errors,
    }


# =====================================================
# Newsletter: Score-Funktion
# =====================================================

def score_newsletter(
    parsed: ParsedBrief, product: dict, definitions: Optional[dict] = None
) -> Tuple[float, List[str], List[str], Optional[dict]]:
    reasons = []
    assumptions = []
    score = 0.0

    s, r = score_product_id_hint(parsed, product); score += s; reasons += r
    s, r = score_topical(parsed, product); score += s; reasons += r
    s, r = score_audience(parsed, product); score += s; reasons += r
    s, r = score_brand_proximity(parsed, product); score += s; reasons += r
    s, r = score_industry(parsed, product); score += s; reasons += r
    s, r = score_values(parsed, product); score += s; reasons += r
    s, r = score_counter_indicators(parsed, product); score += s; reasons += r

    aud = get_audience(product)
    subscribers = aud.get("subscribers_total")
    if isinstance(subscribers, int):
        if subscribers >= 100000:
            score += 10; reasons.append(f"Reach gross ({subscribers} Abonnenten, +10 Pkt)")
        elif subscribers >= 30000:
            score += 6; reasons.append(f"Reach mittel ({subscribers} Abonnenten, +6 Pkt)")
        else:
            score += 3; reasons.append(f"Reach klein ({subscribers} Abonnenten, +3 Pkt)")

    pricing_result = None
    pm = get_newsletter_pricing_model(product)
    if pm:
        adv_industry = parsed.industry_tags[0] if parsed.industry_tags else None
        pricing_result = resolve_newsletter_price(
            product=product,
            definitions=definitions,
            requested_format_id=parsed.preferred_format_id,
            advertiser_industry=adv_industry,
            is_kulturkunde=parsed.is_kulturkunde,
        )

        if pricing_result and pricing_result.get("price_eur_net") is not None:
            price = pricing_result["price_eur_net"]
            if parsed.budget_eur:
                if price <= parsed.budget_eur:
                    score += 8
                    reasons.append(f"Preis {price} EUR passt in Budget {parsed.budget_eur:.0f} EUR (+8 Pkt)")
                else:
                    score -= 10
                    reasons.append(f"Preis {price} EUR ueber Budget {parsed.budget_eur:.0f} EUR (-10 Pkt)")

            assumptions.insert(0,
                f"Empfohlenes Format: {pricing_result.get('format_display_name') or pricing_result.get('format_id')} "
                f"fuer {price} EUR (Listenpreis netto zzgl. MwSt.)"
            )
            if pricing_result.get("applied_kulturpreis"):
                assumptions.append("Kulturpreis angewendet (Kulturkunde-Konditionen)")
            if pricing_result.get("applied_cluster"):
                assumptions.append(f"Cluster: {pricing_result['applied_cluster']}")

    return score, reasons, assumptions, pricing_result


# =====================================================
# Podcast: Pricing-Resolver
# =====================================================

PREMIUM_TARGETING_KEYS = {
    "geo_plz", "contextual_segments", "predictive_audience_segments",
    "lebensphasen_segments", "einkommen_segments",
}


def map_branche_to_podcast_cluster(industry: Optional[str], definitions: Optional[dict]) -> str:
    if not industry or not definitions:
        return "grundpreis"
    bc = definitions.get("branchen_cluster") or definitions.get("industry_clusters") or {}
    for cluster_id in ("branchenpreis_I", "branchenpreis_II", "branchenpreis_III"):
        cdef = bc.get(cluster_id)
        if not isinstance(cdef, dict):
            continue
        branchen = cdef.get("branchen") or cdef.get("branches") or []
        if industry in branchen:
            return cluster_id
    return "grundpreis"


def resolve_podcast_performance_class(parsed: ParsedBrief) -> str:
    return "PK_I" if parsed.has_premium_targeting else "PK_II"


def resolve_podcast_fixed_placement(
    product: dict, format_slot: str, industry: Optional[str], definitions: dict
) -> dict:
    cluster = map_branche_to_podcast_cluster(industry, definitions)
    fp = get_podcast_fixed_placement_pricing(product)
    formats = fp.get("formats") or []

    fmt = None
    for f in formats:
        if f.get("slot") == format_slot or f.get("ad_type_id") == format_slot or f.get("format_id") == format_slot:
            fmt = f
            break

    if not fmt:
        available = [f.get("slot") or f.get("ad_type_id") or f.get("format_id") for f in formats]
        return {
            "errors": [f"Format '{format_slot}' nicht in fixed_placement verfuegbar. Verfuegbar: {available}"],
            "price_eur_net": None,
        }

    cluster_prices = fmt.get("cluster_prices") or fmt.get("cluster_prices_eur_net") or {}
    price = cluster_prices.get(cluster) if isinstance(cluster_prices, dict) else None
    if price is None:
        return {
            "errors": [f"Kein Preis fuer Cluster '{cluster}' im Format '{format_slot}'"],
            "price_eur_net": None,
        }

    mbv_table = definitions.get("minimum_booking_value_eur_net") or {}
    mbv = mbv_table.get(cluster) if isinstance(mbv_table, dict) else None

    return {
        "pricing_model": "fixed_placement",
        "show": product.get("product_name"),
        "format_slot": format_slot,
        "industry": industry,
        "cluster": cluster,
        "price_eur_net": price,
        "billing_unit": "per_episode_fixed_price",
        "mbv_eur_net": mbv,
        "mbv_satisfied": mbv is None or price >= mbv,
        "currency": "EUR",
        "errors": [],
    }


def resolve_podcast_tkp(
    product: dict,
    format_slot: str,
    ad_type_length: str,
    booked_audio_impressions: int,
    industry: Optional[str],
    has_premium_targeting: bool,
    definitions: dict,
) -> dict:
    cluster = map_branche_to_podcast_cluster(industry, definitions)
    pk = "PK_I" if has_premium_targeting else "PK_II"

    table = definitions.get("tkp_pricing_table") or {}
    if ad_type_length not in table:
        return {
            "errors": [f"Ad-Type/Length '{ad_type_length}' nicht in TKP-Tabelle"],
            "price_eur_net": None,
        }

    slot_mapping = {
        "preroll": "preroll_or_midroll",
        "midroll": "preroll_or_midroll",
        "postroll": "adbundle_or_postroll",
        "adbundle_plus": "adbundle_plus",
        "adbundle": "adbundle_or_postroll",
    }
    if ad_type_length == "native_audio_ad_60s":
        slot_mapping = {"midroll": "midroll", "postroll": "postroll"}
    elif ad_type_length == "native_audio_ad_240s":
        slot_mapping = {"postroll": "postroll_only"}

    slot_key = slot_mapping.get(format_slot)
    if not slot_key or slot_key not in table.get(ad_type_length, {}):
        return {
            "errors": [f"Slot '{format_slot}' nicht verfuegbar fuer '{ad_type_length}'"],
            "price_eur_net": None,
        }

    tkp_eur = table[ad_type_length][slot_key][pk][cluster]
    total = (booked_audio_impressions / 1000.0) * tkp_eur
    mbv_table = definitions.get("minimum_booking_value_eur_net") or {}
    mbv = mbv_table.get(cluster) if isinstance(mbv_table, dict) else None

    return {
        "pricing_model": "tkp_based",
        "show": product.get("product_name"),
        "format_slot": format_slot,
        "ad_type_length": ad_type_length,
        "slot_in_table": slot_key,
        "performance_class": pk,
        "industry": industry,
        "cluster": cluster,
        "tkp_eur_net": tkp_eur,
        "booked_audio_impressions": booked_audio_impressions,
        "total_price_eur_net": round(total, 2),
        "billing_unit": "EUR_per_1000_audio_impressions",
        "mbv_eur_net": mbv,
        "mbv_satisfied": mbv is None or total >= mbv,
        "currency": "EUR",
        "errors": [],
    }


def resolve_podcast_price(
    parsed: ParsedBrief, product: dict, definitions: Optional[dict]
) -> Optional[dict]:
    if not definitions:
        return {"errors": ["Podcast-Definitions nicht verfuegbar"], "price_eur_net": None}

    pm = get_podcast_pricing_model(product)

    if pm in ("tkp_based", "mixed_fixed_and_tkp"):
        if parsed.podcast_format_slot and parsed.podcast_ad_type_length and parsed.booked_audio_impressions:
            industry = parsed.industry_tags[0] if parsed.industry_tags else None
            return resolve_podcast_tkp(
                product=product,
                format_slot=parsed.podcast_format_slot,
                ad_type_length=parsed.podcast_ad_type_length,
                booked_audio_impressions=parsed.booked_audio_impressions,
                industry=industry,
                has_premium_targeting=parsed.has_premium_targeting,
                definitions=definitions,
            )

    if pm in ("four_cluster", "fixed_placement_four_cluster", "mixed_fixed_and_tkp"):
        if parsed.podcast_format_slot:
            industry = parsed.industry_tags[0] if parsed.industry_tags else None
            return resolve_podcast_fixed_placement(
                product=product,
                format_slot=parsed.podcast_format_slot,
                industry=industry,
                definitions=definitions,
            )

    return {
        "pricing_model": pm,
        "price_eur_net": None,
        "hint": (
            "Konkreter Preis benoetigt zusaetzliche Angaben: Format-Slot "
            "(preroll/midroll/postroll/adbundle), Ad-Type-Length "
            "(audio_ad_20s/native_audio_ad_30s/60s/240s), Audio-Impressions, "
            "Branche und Targeting-Konfiguration."
        ),
        "errors": [],
    }


# =====================================================
# Podcast: Score-Funktion
# =====================================================

def score_podcast(
    parsed: ParsedBrief, product: dict, definitions: Optional[dict] = None
) -> Tuple[float, List[str], List[str], Optional[dict]]:
    reasons = []
    assumptions = []
    score = 0.0

    s, r = score_product_id_hint(parsed, product); score += s; reasons += r
    s, r = score_topical(parsed, product); score += s; reasons += r
    s, r = score_audience(parsed, product); score += s; reasons += r
    s, r = score_industry(parsed, product); score += s; reasons += r
    s, r = score_values(parsed, product); score += s; reasons += r
    s, r = score_brand_proximity(parsed, product); score += s; reasons += r
    s, r = score_counter_indicators(parsed, product); score += s; reasons += r

    if "podcast" in parsed.channel_hints:
        score += 5
        reasons.append("Brief spezifiziert Podcast-Werbung (+5 Pkt)")

    pricing_result = resolve_podcast_price(parsed, product, definitions)

    if pricing_result:
        if pricing_result.get("errors"):
            assumptions.append(
                "Pricing-Hinweis: " + "; ".join(pricing_result["errors"])
            )
        elif pricing_result.get("hint"):
            assumptions.insert(0, "Pricing: " + pricing_result["hint"])
        else:
            price = pricing_result.get("price_eur_net") or pricing_result.get("total_price_eur_net")
            if price is not None:
                if pricing_result.get("pricing_model") == "tkp_based":
                    tkp = pricing_result.get("tkp_eur_net")
                    ai = pricing_result.get("booked_audio_impressions")
                    pk = pricing_result.get("performance_class")
                    assumptions.insert(0,
                        f"TKP {tkp} EUR x {ai} AI / 1000 = {price} EUR (Listenpreis netto zzgl. MwSt.) "
                        f"({pk}, Cluster {pricing_result.get('cluster')})"
                    )
                else:
                    assumptions.insert(0,
                        f"Festplatzierung {pricing_result.get('format_slot')}: "
                        f"{price} EUR (Listenpreis netto zzgl. MwSt.) (Cluster {pricing_result.get('cluster')})"
                    )
                if pricing_result.get("mbv_satisfied") is False:
                    assumptions.append(
                        f"Achtung: Preis unter MBV ({pricing_result.get('mbv_eur_net')} EUR)"
                    )

                if parsed.budget_eur:
                    if price <= parsed.budget_eur:
                        score += 8
                        reasons.append(f"Preis {price} EUR passt in Budget {parsed.budget_eur:.0f} EUR (+8 Pkt)")
                    else:
                        score -= 10
                        reasons.append(f"Preis {price} EUR ueber Budget {parsed.budget_eur:.0f} EUR (-10 Pkt)")

    return score, reasons, assumptions, pricing_result


# =====================================================
# Router: match_products
# =====================================================

def match_products(
    brief: str,
    products: List[dict],
    definitions: Optional[Dict[str, dict]] = None,
    max_results: int = 10,
) -> List[dict]:
    """
    Router: parst Brief, dispatcht nach product_type, sortiert nach Score,
    gibt top-N Matches zurueck.
    """
    parsed = parse_brief(brief)
    matches = []

    nl_defs = (definitions or {}).get("newsletter") if definitions else None
    pod_defs = (definitions or {}).get("podcast") if definitions else None

    for product in products:
        pt = product.get("product_type")
        score = 0.0
        reasoning = []
        assumptions = []
        best_format = None
        discount = None
        pricing = None
        format_candidates = []

        if pt in PRINT_TYPES:
            score, reasoning, assumptions, best_format, discount, format_candidates = score_print(parsed, product)
        elif pt in NEWSLETTER_TYPES:
            score, reasoning, assumptions, pricing = score_newsletter(parsed, product, nl_defs)
        elif pt in PODCAST_TYPES:
            score, reasoning, assumptions, pricing = score_podcast(parsed, product, pod_defs)
        else:
            continue

        if score <= 0:
            continue

        # A4 (v3.6): Channel-Hint-Filter haerter
        # Bei expliziter Print-Praeferenz Newsletter/Podcasts komplett ausfiltern
        if parsed.channel_hints:
            channel_match = (
                (pt in PRINT_TYPES and "print" in parsed.channel_hints) or
                (pt in NEWSLETTER_TYPES and "newsletter" in parsed.channel_hints) or
                (pt in PODCAST_TYPES and "podcast" in parsed.channel_hints)
            )
            if channel_match:
                score += 10
                reasoning.append("Channel-Hint im Brief passt (+10 Pkt)")
            else:
                # Bei expliziter Single-Channel-Praeferenz andere komplett ausfiltern
                # ABER: Multi-Channel-Briefs ("kanaluebergreifend") setzen alle drei
                # Hints, dann sollte der else-Pfad nie greifen.
                if len(parsed.channel_hints) == 1:
                    only_channel = parsed.channel_hints[0]
                    product_channel_match = (
                        (only_channel == "print" and pt in PRINT_TYPES) or
                        (only_channel == "newsletter" and pt in NEWSLETTER_TYPES) or
                        (only_channel == "podcast" and pt in PODCAST_TYPES)
                    )
                    if not product_channel_match:
                        continue  # Produkt komplett ueberspringen
                score -= 15
                reasoning.append("Channel-Hint im Brief weicht ab (-15 Pkt)")

        # A3 (v3.6): Print-Bevorzugung (hoeherer Deckungsbeitrag)
        if pt in PRINT_TYPES:
            score += 15
            reasoning.append("Print-Bevorzugung (+15 Pkt)")

        # A1 (v3.7): Score-Schwelle wurde durch Score-Spread ersetzt.
        # Filterung erfolgt jetzt NACH dem Sortieren (siehe unten).

        if score >= 70:
            match_type = "exact_match"
        elif score >= 30:
            match_type = "counter_proposal"
        else:
            match_type = "suggestion"

        mm = get_matching_metadata(product)
        rationale = mm.get("match_rationale")
        score_details = " | ".join(reasoning) if reasoning else "Keine spezifischen Treffer"
        if rationale:
            full_reasoning = f"[Rationale] {rationale}  ||  [Score] {score_details}"
        else:
            full_reasoning = score_details

        matches.append({
            "product": product,
            "score": round(score, 1),
            "reasoning": full_reasoning,
            "assumptions": assumptions,
            "match_type": match_type,
            "best_format": best_format,
            "discount": discount,
            "pricing": pricing,
            "format_candidates": format_candidates,
            "issue_dates": format_issues_summary(product) if parsed.wants_issue_dates else "",
            "newsletter_schedule": format_newsletter_schedule(product) if pt in NEWSLETTER_TYPES else "",
            "channel": pt,
        })

    matches.sort(key=lambda x: x["score"], reverse=True)

    # A1 (v3.7): Score-Spread statt harte Schwelle.
    # Top-1 immer drin. Top 2-N nur wenn Score >= 70% des Top-1-Score.
    # Adaptiv: starker Top-Match -> strenger Filter, vager Brief -> grosszuegig.
    if matches:
        top_score = matches[0]["score"]
        threshold = top_score * 0.7
        matches = [
            m for m in matches
            if m["score"] >= threshold or m is matches[0]
        ]

    return matches[:max_results]


# =====================================================
# ProductIndex
# =====================================================

class ProductIndex:
    """
    Laedt Produkt-JSONs rekursiv aus einem Verzeichnis.
    Wird vom Server (mcp_server.py) instanziiert.
    """

    def __init__(self, products: List[dict]):
        self.products = products

    @classmethod
    def load_from_directory(cls, directory: Path) -> "ProductIndex":
        products = []
        directory = Path(directory)
        for path in sorted(directory.rglob("*.json")):
            name = path.name.lower()
            if "schema" in name or name == "adagents.json":
                continue
            if "definitions" in name:
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if "product_id" in data:
                products.append(data)
        return cls(products)
