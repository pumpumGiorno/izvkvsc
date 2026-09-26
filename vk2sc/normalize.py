"""Нормализация названий треков и имён исполнителей для сравнения.

Строка приводится к «сравнимому» виду: нижний регистр, ё→е, без диакритики
в латинице, без пунктуации и лишних пробелов. Отдельно разбираются пометки
в скобках: шум («Official Audio», «Lyrics», «Remastered») выбрасывается,
версии («Remix», «Live», «Acoustic») сохраняются как теги, а «feat./ft.»
уходит в список приглашённых исполнителей.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_LATIN_SPECIAL = str.maketrans(
    {"ß": "ss", "ø": "o", "æ": "ae", "œ": "oe", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "ı": "i"}
)

_TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
        "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
        "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
        "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
        "ь": "", "э": "e", "ю": "yu", "я": "ya", "і": "i", "ї": "yi", "є": "ye",
        "ґ": "g", "ў": "u",
    }
)

_CYRILLIC_RE = re.compile(r"[а-яёіїєґў]")
_BRACKETS_RE = re.compile(r"[(\[{【]([^)\]}】]*)[)\]}】]")
# Части названия после « - », « | », « // » («Believer - Live», «Трек | Премьера»).
_SEGMENT_SPLIT_RE = re.compile(r"\s+(?:-|\||//|/)\s+")

# «feat.», «ft.», «при уч.» и т. п. В названии трека «with» тоже часто означает гостя,
# но только в скобках: «Dancing With Myself» трогать нельзя.
_FEAT_WORDS = r"feat\.?|ft\.?|featuring|при уч\.?|при участии|с участием|совм\.?"
_FEAT_RE = re.compile(rf"(?:^|\s)(?:{_FEAT_WORDS})(?=\s|$)")
_FEAT_IN_BRACKETS_RE = re.compile(rf"^(?:{_FEAT_WORDS}|with|w/)\s+")

_ARTIST_SPLIT_RE = re.compile(
    rf"\s*(?:,|;|&|\+|\s/\s|\s(?:x|х|and|и|vs\.?|with|{_FEAT_WORDS})\s)\s*"
)

# Пометки, которые не меняют запись: их просто выбрасываем.
_NOISE_RE = re.compile(
    r"^(?:"
    r"(?:official\s+)?(?:music\s+|lyrics?\s+|hd\s+|hq\s+)?(?:video|audio|visuali[sz]er|clip)(?:\s+clip)?"
    r"|official(?:\s+(?:version|single|release))?"
    r"|lyrics?|with lyrics|текст(?:\s+песни)?|караоке\s+текст"
    r"|клип|видео|видеоклип|аудио|official\s+клип|премьера(?:\s+(?:клипа|песни|трека|альбома|сингла))?(?:\s*\d{4})?"
    r"|новинка(?:\s*\d{4})?|хит(?:\s*\d{4})?|new(?:\s*\d{4})?|single|сингл"
    r"|(?:hd|hq|4k|\d{3,4}p)(?:\s+(?:upgrade|remaster(?:ed)?|version|quality|video|audio))?"
    r"|explicit|clean|dirty|censored|uncensored"
    r"|(?:\d{4}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?"
    r"|original(?:\s+(?:mix|version|edit))?|album\s+version|single\s+version|full\s+version|полная\s+версия"
    r"|prod(?:uced)?\.?(?:\s+by)?\s+.+|free\s+(?:download|dl)|out\s+now|bonus(?:\s+track)?|mono|stereo"
    r"|(?:из|from|ost|саундтрек|soundtrack)\b.*"
    r"|\d{4}|ncs\s+release|copyright\s+free|no\s+copyright"
    r")$"
)

# Теги версий. «Сильные» ищем везде, «слабые» только в скобках или после « - »,
# чтобы не путать «Live Your Life» с концертной записью.
_STRONG_TAGS = {
    "remix": r"remix(?:ed|er)?|rmx|ремикс|rework|bootleg|mashup|мэшап|flip|vip\s+mix",
    "instrumental": r"instrumental|инструментал|минусовка|karaoke|караоке|backing\s+track",
    "slowed": r"slowed(?:\s*(?:\+|and|&)?\s*reverb(?:ed)?)?|замедленн\w*",
    "sped_up": r"sped\s+up|speed\s+up|nightcore|ускоренн\w*",
    "acoustic": r"acoustic|акустика|акустическая(?:\s+версия)?|unplugged",
    "8d": r"8d(?:\s+audio)?",
    "bass_boosted": r"bass\s*boost(?:ed)?",
}
_WEAK_TAGS = {
    "live": r"live(?:\s+(?:version|session|at\b.*))?|концерт\w*|лайв|вживую",
    "cover": r"cover|кавер",
    "extended": r"extended(?:\s+(?:mix|version|edit))?|club\s+mix",
    "demo": r"demo",
    "radio_edit": r"radio\s+(?:edit|mix|version)",
    "remix": r"(?:\w+\s+)*mix",  # «Skrillex Mix»; «Original Mix» к этому моменту уже отброшен как шум
    "version": r"(?:\w+\s+)*(?:edit|version|remake)",  # «Russian Version», «Clean Edit»
}
# Небольшие расхождения, из-за которых запись не становится другой.
SOFT_TAGS = frozenset({"radio_edit"})

_STRONG_RE = {tag: re.compile(rf"\b(?:{p})\b") for tag, p in _STRONG_TAGS.items()}
_WEAK_RE = {tag: re.compile(rf"\b(?:{p})\b") for tag, p in _WEAK_TAGS.items()}

_USERNAME_SUFFIXES = ("official", "officiel", "offical", "music", "musik", "vevo", "topic", "tv", "channel", "records", "band")


def fold(text: str) -> str:
    """Регистр, ё→е, диакритика латиницы, типографские тире/кавычки, пробелы."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("ё", "е").translate(_LATIN_SPECIAL)
    out: list[str] = []
    for ch in unicodedata.normalize("NFD", text):
        # Убираем диакритику только у латиницы: «й» в NFD — это «и» + бреве.
        if unicodedata.combining(ch) and out and "a" <= out[-1] <= "z":
            continue
        out.append(ch)
    text = unicodedata.normalize("NFC", "".join(out))
    text = re.sub(r"[‐‑‒–—―−]", "-", text)
    text = re.sub(r"[’‘`´ʼ]", "'", text)
    text = re.sub(r"[“”„«»]", '"', text)
    return re.sub(r"\s+", " ", text).strip()


def simplify(text: str) -> str:
    """Сравнимая форма: fold + без пунктуации. «Don't Stop!» → «dont stop»."""
    text = fold(text)
    text = re.sub(r"(?<=\w)\$(?=\w)", "s", text)  # A$AP → asap, Ke$ha → kesha
    text = text.replace("'", "").replace('"', " ")
    text = re.sub(r"[^\w\s]|_", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def compact(text: str) -> str:
    """Без пробелов: «Imagine Dragons» и «ImagineDragons» совпадают."""
    return simplify(text).replace(" ", "")


def has_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC_RE.search(text))


def translit(text: str) -> str:
    """Упрощённая транслитерация кириллицы: «Сплин» → «splin»."""
    return text.translate(_TRANSLIT)


def strip_username_suffix(name: str) -> str:
    """«imaginedragonsofficial» → «imaginedragons», «Muse Music» → «muse»."""
    name = compact(name)
    changed = True
    while changed:
        changed = False
        for suffix in _USERNAME_SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix) + 2:
                name = name[: -len(suffix)]
                changed = True
    return name


def split_artists(text: str) -> list[str]:
    """«Miyagi & Andy Panda» → ['miyagi', 'andy panda']."""
    parts = _ARTIST_SPLIT_RE.split(fold(text))
    return [s for s in (simplify(p) for p in parts) if s]


def is_noise(fragment: str) -> bool:
    return bool(_NOISE_RE.match(simplify_keep_dots(fragment)))


def simplify_keep_dots(text: str) -> str:
    """Как simplify, но оставляет точки: нужно для «prod.», «feat.»."""
    text = fold(text).replace('"', " ")
    text = re.sub(r"[^\w\s.+&/]|_", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _strong_tags(text: str) -> set[str]:
    return {tag for tag, rx in _STRONG_RE.items() if rx.search(text)}


def url_tags(url: str) -> frozenset[str]:
    """Пометки версии из адреса трека: название могли переименовать, а slug остался.
    «…/zemfira-superhit-want-remix» → {"remix"}."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return frozenset(_strong_tags(simplify(slug.replace("-", " ").replace("_", " "))))


def _weak_tags(text: str) -> set[str]:
    tags = _strong_tags(text)
    explicit_remix = "remix" in tags
    for tag, rx in _WEAK_RE.items():
        if rx.search(text):
            tags.add(tag)
    # «Radio Edit», «Extended Mix», «Live Version» уже описаны точнее общих mix/edit/version.
    if tags & {"radio_edit", "extended"} and not explicit_remix:
        tags.discard("remix")
    if len(tags) > 1:
        tags.discard("version")
    return tags


def _looks_like_version(fragment: str) -> bool:
    text = simplify_keep_dots(fragment)
    return is_noise(fragment) or bool(_weak_tags(text)) or bool(_FEAT_IN_BRACKETS_RE.match(text))


@dataclass(frozen=True)
class ParsedTitle:
    core: str  # основная часть: «believer»
    core_full: str  # все значимые куски без скобок
    tags: frozenset[str] = frozenset()  # {"remix"}, {"live"}, ...
    extra: str = ""  # значимое содержимое скобок: «skrillex remix»
    feats: tuple[str, ...] = ()  # приглашённые исполнители из названия


@dataclass(frozen=True)
class ParsedArtist:
    full: str  # «miyagi andy panda»
    names: tuple[str, ...] = ()  # основные исполнители
    feats: tuple[str, ...] = ()  # приглашённые
    raw: str = field(default="", compare=False)


def parse_title(title: str) -> ParsedTitle:
    text = fold(title)
    groups = [g.strip() for g in _BRACKETS_RE.findall(text)]
    rest = _BRACKETS_RE.sub(" ", text)

    segments = [s.strip() for s in _SEGMENT_SPLIT_RE.split(rest) if s.strip()]
    core_segments: list[str] = []
    for i, seg in enumerate(segments):
        if i > 0 and _looks_like_version(seg):
            groups.append(seg)
        else:
            core_segments.append(seg)

    feats: list[str] = []
    cleaned_core: list[str] = []
    for seg in core_segments:
        m = _FEAT_RE.search(seg)
        if m:
            feats.extend(split_artists(seg[m.end():]))
            seg = seg[: m.start()]
        cleaned_core.append(seg)

    tags: set[str] = set()
    extras: list[str] = []
    for g in groups:
        g_dots = simplify_keep_dots(g)
        m = _FEAT_IN_BRACKETS_RE.match(g_dots)
        if m:
            feats.extend(split_artists(g_dots[m.end():]))
            continue
        # «(Radio Edit - feat. Pharrell Williams and Nile Rodgers)»: гости в конце скобки.
        m = _FEAT_RE.search(g_dots)
        if m:
            feats.extend(split_artists(g_dots[m.end():]))
            g = g_dots = g_dots[: m.start()].strip(" -,")
        if not g_dots or is_noise(g):
            continue
        tags |= _weak_tags(g_dots)
        extras.append(g)

    core_texts = []
    for seg in cleaned_core:
        s = simplify(seg)
        found = _strong_tags(s)
        if found:
            stripped = s
            for tag in found:
                stripped = _STRONG_RE[tag].sub(" ", stripped)
            stripped = re.sub(r"\s+", " ", stripped).strip()
            # «Karaoke» (Drake), «Acoustic» — это само название песни, а не пометка версии.
            if stripped:
                tags |= found
                s = stripped
        if s:
            core_texts.append(s)

    core = core_texts[0] if core_texts else ""
    # Хвосты после « - », не похожие на версию («Перевод на русском», подпись загрузившего),
    # тоже считаем пометкой: оригинал так обычно не называют.
    extras.extend(core_texts[1:])
    return ParsedTitle(
        core=core,
        core_full=" ".join(core_texts),
        tags=frozenset(tags),
        extra=simplify(" ".join(extras)),
        feats=tuple(dict.fromkeys(f for f in feats if f)),
    )


def parse_artist(artist: str) -> ParsedArtist:
    text = fold(artist)
    groups = [g.strip() for g in _BRACKETS_RE.findall(text)]
    text = _BRACKETS_RE.sub(" ", text)
    feats: list[str] = []
    m = _FEAT_RE.search(text)
    if m:
        feats.extend(split_artists(text[m.end():]))
        text = text[: m.start()]
    for g in groups:
        g_dots = simplify_keep_dots(g)
        fm = _FEAT_IN_BRACKETS_RE.match(g_dots)
        if fm:
            feats.extend(split_artists(g_dots[fm.end():]))
    names = split_artists(text)
    return ParsedArtist(
        full=simplify(text),
        names=tuple(dict.fromkeys(names)),
        feats=tuple(dict.fromkeys(feats)),
        raw=artist,
    )


def clean_title_for_query(title: str) -> str:
    """Название для поискового запроса: без шума и feat., но с пометками версии."""
    def repl(m: re.Match) -> str:
        inner = m.group(1)
        inner_dots = simplify_keep_dots(inner)
        if not inner_dots or is_noise(inner) or _FEAT_IN_BRACKETS_RE.match(inner_dots):
            return " "
        return f" {inner} "

    text = _BRACKETS_RE.sub(repl, unicodedata.normalize("NFKC", title))
    m = _FEAT_RE.search(text.lower())
    if m:
        text = text[: m.start()]
    return re.sub(r"\s+", " ", text).strip(" -|")


def clean_artist_for_query(artist: str) -> str:
    """Исполнитель для запроса: без feat.-гостей."""
    text = unicodedata.normalize("NFKC", artist)
    m = _FEAT_RE.search(text.lower())
    if m:
        text = text[: m.start()]
    return re.sub(r"\s+", " ", text).strip(" ,&")
