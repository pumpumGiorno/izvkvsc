"""Оценка совпадения трека из VK с кандидатом из SoundCloud.

Оценка 0–100 складывается из похожести исполнителя (45%) и названия (55%),
затем применяются штрафы: другая версия (ремикс/лайв/…), лишние или
пропавшие feat.-гости, расхождение длительности. Если длительность известна
с обеих сторон и совпадает в пределах ±5 сек, оценка подтягивается к 100.

Кроме общей оценки у каждого кандидата считаются отдельные признаки:
похожесть исполнителя и названия, доля совпавших слов названия (containment),
разница длительности, конфликт версий, лишние гости и слова. auto_pick()
принимает решение по их совокупности (правила A–D, см. _accept_rule), а не по
одному порогу: точный исполнитель + совпавшая длительность позволяют названию
отличаться сильнее, но никогда не отменяют проверку версии.

auto_pick() выбирает кандидата без участия человека (обычный режим),
decide() — прежняя логика с вопросами для режима --interactive.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from typing import Iterable, Optional

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from .normalize import (
    SOFT_TAGS,
    ParsedArtist,
    ParsedTitle,
    has_cyrillic,
    parse_artist,
    parse_title,
    simplify,
    strip_username_suffix,
    translit,
    translit_skeleton,
    url_tags,
)
from .tracks import Track

# Меняется при любом изменении оценки/выбора: записи state.json со старой версией
# можно пересчитать (--rematch), не трогая ручной выбор.
MATCHING_VERSION = 3

AUTO_THRESHOLD = 85  # не ниже — добавляем без вопросов
MEDIUM_BAND = 15  # от порога минус столько — берём, только если кандидат явно лучше остальных
MEDIUM_MARGIN = 10  # насколько лучший должен опережать другую запись в средней зоне
TIE_MARGIN = 3  # кандидаты ближе этого к лучшему считаются равными — решают длительность и т. п.
MIN_ARTIST_SIM = 70  # похожее название при чужом исполнителе автоматически не берём
MAX_AUTO_DURATION_DIFF = 20  # сек: при большей разнице автоматически не берём
VERSION_CONFLICT_PENALTY = 10  # штраф версии от этого значения — это другая запись
RELAXED_BAND = 25  # правила A–D работают от порога минус столько (по умолчанию от 60)
BEATS_DURATION = 5  # соперник с длительностью хуже на столько секунд явно проигрывает
BEATS_SIM = 10  # … или с исполнителем/названием хуже на столько баллов

# Служебные слова не считаются при сравнении наборов слов названия.
_STOPWORDS = frozenset(
    "the a an to of and or in on at for is it be my me you your i we our with "
    "и в во на не с со по а о об я ты мы вы он она за из к у же".split()
)
# Слова, из-за которых похожие названия означают разные треки: «The Business» и
# «The Business, Pt. II», «Intro» и «Intro (Reprise)».
_PART_MARKERS = frozenset({
    "pt", "part", "ii", "iii", "iv", "vi", "vii", "viii", "ix", "vol", "volume", "chapter", "ch",
    "часть", "ч", "reprise", "interlude", "intro", "outro", "skit", "prologue", "epilogue",
    "snippet", "preview", "teaser", "medley", "megamix", "mashup", "sequel",
})
_YEAR_RE = re.compile(r"^(?:19|20)\d\d$")
_COVER_RE = re.compile(r"\b(?:cover|кавер)\b")
MIN_SHOW_SCORE = 35  # ниже — кандидат считается мусором
AMBIGUITY_MARGIN = 5  # соперник ближе этого к лучшему — спрашиваем
DURATION_TOLERANCE = 5  # сек
POPULARITY_RATIO = 5  # во столько раз официальная загрузка должна быть популярнее соперников

_TITLE_SPLIT_RE = re.compile(r"\s+[-–—|]\s+|\s+//\s+")
_VERSION_WORDS_RE = re.compile(
    r"\b(?:remix|rmx|mix|edit|version|bootleg|rework|flip|vip|official|ремикс|live|cover|кавер|"
    r"slowed|reverb|sped|speed|up|nightcore|acoustic|instrumental|extended|radio)\b"
)


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _printable(value: Optional[str]) -> str:
    """Названия задают загрузившие: вычищаем управляющие символы (в т. ч. ANSI-escape для терминала)."""
    return _CONTROL_RE.sub(" ", value or "").strip()


@dataclass
class Candidate:
    """Трек SoundCloud в том объёме, который нужен для сопоставления и отчёта."""

    id: int
    title: str
    username: str
    url: str
    duration: Optional[int] = None  # сек, полная длительность
    publisher_artist: Optional[str] = None
    policy: Optional[str] = None  # ALLOW / MONETIZE / SNIP (Go+) / BLOCK
    playback_count: int = 0
    variant: Optional[str] = None  # каким поисковым запросом найден (для отчёта)

    @classmethod
    def from_api(cls, d: dict) -> "Candidate":
        # У треков Go+ поле duration — это 30-секундное превью, настоящая длина в full_duration.
        ms = d.get("full_duration") or d.get("duration")
        return cls(
            id=int(d["id"]),
            title=_printable(d.get("title")),
            username=_printable((d.get("user") or {}).get("username")),
            url=_printable(d.get("permalink_url")),
            duration=round(ms / 1000) if ms else None,
            publisher_artist=_printable((d.get("publisher_metadata") or {}).get("artist")) or None,
            policy=d.get("policy"),
            playback_count=int(d.get("playback_count") or 0),
        )

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def artist(self) -> str:
        return self.publisher_artist or self.username

    @property
    def display(self) -> str:
        return f"{self.artist} — {self.title}"

    @property
    def flags(self) -> str:
        if self.policy == "SNIP":
            return "только Go+"
        if self.policy == "BLOCK":
            return "недоступен в регионе"
        return ""


@dataclass
class Scored:
    candidate: Candidate
    score: int
    uploader_match: bool  # исполнитель совпал с аккаунтом/лейблом, а не только с текстом названия
    title: ParsedTitle
    duration_diff: Optional[int]
    artist_sim: float = 0.0
    title_sim: float = 0.0  # итоговое сходство названий (см. TitleMatch.score)
    version_penalty: float = 0.0
    extra_words: int = 0  # слов в названии кандидата, которых нет в исходнике
    title_fuzzy: float = 0.0  # только нечёткое сравнение строк
    containment: float = 0.0  # доля слов кандидата, которые есть в названии из VK
    coverage: float = 0.0  # доля слов из VK, которые есть у кандидата
    title_conflict: bool = False  # «Pt. II», «2», «Interlude» — похоже на другой трек
    artist_exact: bool = False  # набор исполнителей совпал полностью (порядок не важен)
    feat_penalty: float = 0.0  # лишние/пропавшие feat.-гости

    # Названия признаков как в отчёте и логе.
    @property
    def overall_score(self) -> int:
        return self.score

    @property
    def artist_score(self) -> float:
        return self.artist_sim

    @property
    def title_score(self) -> float:
        return self.title_sim

    @property
    def duration_delta(self) -> Optional[int]:
        return self.duration_diff

    @property
    def version_conflict(self) -> bool:
        return self.version_penalty >= VERSION_CONFLICT_PENALTY

    @property
    def extra_artist_penalty(self) -> float:
        return self.feat_penalty

    @property
    def extra_title_noise(self) -> int:
        return self.extra_words

    def metrics(self) -> dict:
        """Признаки для state.json, отчёта и лога."""
        return {
            "score": self.score,
            "title": round(self.title_sim),
            "artist": round(self.artist_sim),
            "duration_delta": self.duration_diff,
            "version_conflict": self.version_conflict,
            "title_conflict": self.title_conflict,
            "containment": round(self.containment, 2),
            "artist_exact": self.artist_exact,
        }

    @property
    def tie_key(self) -> tuple:
        """Выбор среди почти равных: ближе длительность (по полосам 0–2, 3–5, 6–10, 11–20 с,
        чтобы секунда разницы не перевешивала точного исполнителя), точнее версия, исполнитель,
        меньше лишних слов, официальная загрузка, популярность. id — для детерминированности."""
        diff = self.duration_diff if self.duration_diff is not None else 10_000
        band = next((i for i, limit in enumerate((2, 5, 10, 20)) if diff <= limit), 4)
        return (band, self.version_penalty, not self.artist_exact, -round(self.artist_sim), diff, self.extra_words,
                not self.uploader_match, -self.candidate.playback_count, -self.score, self.candidate.id)

    @property
    def sort_key(self) -> tuple:
        return (self.score, self.uploader_match, self.candidate.playback_count)


@dataclass
class Decision:
    kind: str  # "auto" | "ask" | "low" | "none"
    ranked: list[Scored]
    reason: str = ""
    # Принят уверенно (score ≥ порога или правило A–D с совпавшей длительностью):
    # искать дальше незачем. Средняя зона без длительности — не уверенно.
    confident: bool = False

    @property
    def best(self) -> Optional[Scored]:
        return self.ranked[0] if self.ranked else None


def _sim(a: str, b: str) -> float:
    """Похожесть двух упрощённых строк 0–100 с учётом пробелов и транслита."""
    if not a or not b:
        return 0.0
    best = max(fuzz.ratio(a.replace(" ", ""), b.replace(" ", "")), fuzz.token_sort_ratio(a, b))
    if has_cyrillic(a) or has_cyrillic(b):
        for ta, tb in ((translit(a), translit(b)), (translit_skeleton(a), translit_skeleton(b))):
            best = max(best, fuzz.ratio(ta.replace(" ", ""), tb.replace(" ", "")), fuzz.token_sort_ratio(ta, tb))
    return best


def artist_similarity(vk: ParsedArtist, cand: ParsedArtist, cand_title: ParsedTitle) -> float:
    if not vk.full or not cand.full:
        return 0.0
    full = _sim(vk.full, cand.full)
    # Аккаунты вида «ImagineDragonsOfficial», «Muse Music».
    stripped = strip_username_suffix(cand.full)
    if stripped != cand.full.replace(" ", ""):
        full = max(full, _sim(vk.full, stripped))
    others = list(cand.names) + list(cand.feats) + list(cand_title.feats)
    per_artist = [max((_sim(n, c) for c in others), default=0.0) for n in vk.names]
    coverage = sum(per_artist) / len(per_artist) if per_artist else 0.0
    # «Miyagi & Andy Panda» у SoundCloud часто лежит на аккаунте одного Miyagi.
    first_only = 0.9 * per_artist[0] if len(per_artist) > 1 else 0.0
    return max(full, coverage, first_only)


def title_similarity(vk: ParsedTitle, cand: ParsedTitle) -> float:
    return max(_sim(vk.core_full, cand.core_full), 0.97 * _sim(vk.core, cand.core))


@dataclass(frozen=True)
class TitleMatch:
    score: float  # итоговое сходство 0–100
    fuzzy: float  # нечёткое сравнение строк (ratio / token_sort, с транслитом)
    containment: float  # доля слов кандидата, которые есть в исходнике
    coverage: float  # доля слов исходника, которые есть у кандидата
    extra: int  # слов у кандидата, которых нет в исходнике
    conflict: bool  # в «лишних» словах есть номер части, «Interlude» и т. п.


def _content_tokens(title: ParsedTitle) -> list[str]:
    tokens = title.core_full.split()
    return [t for t in tokens if t not in _STOPWORDS] or tokens


def _token_match(tok: str, others: Iterable[str]) -> bool:
    """Слово есть среди других: точно, с опечаткой, слитно («citygirls» ⊃ «girls») или в транслите."""
    for o in others:
        if tok == o:
            return True
        a, b = tok, o
        if has_cyrillic(a) != has_cyrillic(b):
            a, b = translit_skeleton(a), translit_skeleton(b)
            if a == b:
                return True
        if len(a) >= 4 and len(b) >= 4 and (a in b or b in a or fuzz.ratio(a, b) >= 85):
            return True
    return False


def _cover(tokens: list[str], others: list[str]) -> float:
    if not tokens:
        return 0.0
    return sum(_token_match(t, others) for t in tokens) / len(tokens)


def title_match(vk: ParsedTitle, cand: ParsedTitle, ignore: Iterable[str] = ()) -> TitleMatch:
    """Сходство названий по нескольким метрикам сразу.

    fuzzy — прежнее нечёткое сравнение; к нему добавляются нормализованная
    редакционная похожесть и, если все слова кандидата есть в исходнике,
    token set («Girls Just Want to Have Fun» ⊂ «Gotham CityGirls Just Want to Have Fun»).
    Если лишние слова есть у кандидата («Love» → «Love Story»), token set не
    учитывается: такое совпадение подозрительнее. ignore — слова имён исполнителей,
    которые не считаются лишними («Artist - Track»).
    """
    fuzzy = title_similarity(vk, cand)
    vt, ct = _content_tokens(vk), _content_tokens(cand)
    compact_vk, compact_cand = vk.core_full.replace(" ", ""), cand.core_full.replace(" ", "")
    containment, coverage = _cover(ct, vt), _cover(vt, ct)
    ignored = set(ignore)
    cand_extra = [t for t in ct if t not in ignored and not _token_match(t, vt)]
    if len(compact_cand) >= 5 and compact_cand in compact_vk:
        containment, cand_extra = 1.0, []
    if len(compact_vk) >= 5 and compact_vk in compact_cand:
        coverage = 1.0
    # Конфликт ищем по всему названию вместе со скобками: «Song (Part 2)» и «Song Part 2» — одно и то же.
    vk_all = vk.core_full.split() + vk.extra.split()
    cand_all = cand.core_full.split() + cand.extra.split()
    differing = [t for t in cand_all if t not in ignored and not _token_match(t, vk_all)] + \
                [t for t in vk_all if t not in ignored and not _token_match(t, cand_all)]
    conflict = any(t in _PART_MARKERS or (t.isdigit() and not _YEAR_RE.match(t)) for t in differing)
    score = fuzzy
    if compact_vk and compact_cand:
        score = max(score, 100 * Levenshtein.normalized_similarity(compact_vk, compact_cand))
    if ct and not cand_extra:
        tset = fuzz.token_set_ratio(" ".join(vt), " ".join(ct))
        score = max(score, (fuzzy + tset) / 2)
    return TitleMatch(score=score, fuzzy=fuzzy, containment=containment, coverage=coverage,
                      extra=len(cand_extra), conflict=conflict)


def artist_names(artist: ParsedArtist, title: ParsedTitle) -> list[str]:
    """Все исполнители трека: основные, feat. из поля исполнителя и из названия."""
    return list(dict.fromkeys(list(artist.names) + list(artist.feats) + list(title.feats)))


def artists_exact(vk: list[str], cand: list[str], similarity: float) -> bool:
    """Наборы исполнителей совпадают целиком: «Bladee, Ecco2k» = «Ecco2k & Bladee»."""
    if not vk or not cand:
        return False
    if len(vk) == 1 and len(cand) == 1:
        return similarity >= 95  # учитывает «ImagineDragonsOfficial», транслит
    if len(vk) != len(cand):
        return False
    return all(max(_sim(v, c) for c in cand) >= 90 for v in vk) and \
        all(max(_sim(c, v) for v in vk) >= 90 for c in cand)


def _version_author(extra: str) -> str:
    """«skrillex remix» → «skrillex»: остаётся автор версии без служебных слов."""
    return re.sub(r"\s+", " ", _VERSION_WORDS_RE.sub(" ", extra)).strip()


def version_penalty(vk: ParsedTitle, cand: ParsedTitle) -> float:
    hard_vk, hard_cand = vk.tags - SOFT_TAGS, cand.tags - SOFT_TAGS
    penalty = 0.0
    if hard_vk != hard_cand:
        # Оригинал против ремикса, студийная против концертной — это другая запись.
        penalty += 30 + 10 * (len(hard_vk ^ hard_cand) - 1)
    elif hard_vk:
        a, b = _version_author(vk.extra), _version_author(cand.extra)
        if a and b and _sim(a, b) < 70:
            penalty += 20  # оба ремиксы, но разных авторов
        elif a and not b:
            penalty += 10  # у нас «Skrillex Remix», у кандидата просто «Remix»
    else:
        # Пометки без известного тега: «[Phonk Edition]», «(hardstyle)», «(X-фактор)»,
        # «Перевод на русском». У кандидата это почти всегда не оригинал.
        a, b = _version_author(vk.extra), _version_author(cand.extra)
        if b and not a:
            penalty += 20
        elif a and not b:
            penalty += 5
        elif a and b and _sim(a, b) < 60:
            penalty += 15
    if (vk.tags & SOFT_TAGS) != (cand.tags & SOFT_TAGS):
        penalty += 4
    return penalty


def _known_artist(name: str, known: list[str]) -> bool:
    """Исполнитель есть в списке — с учётом аккаунтов вида «ImagineDragonsOfficial», «Lady Gaga VEVO»."""
    stripped = strip_username_suffix(name)
    return any(_sim(name, k) >= 80 or _sim(stripped, k.replace(" ", "")) >= 80 for k in known)


def feat_penalty(vk_artist: ParsedArtist, vk_title: ParsedTitle, cand_artist: ParsedArtist, cand_title: ParsedTitle) -> float:
    """Лишние и пропавшие исполнители: лишний feat.-гость −8, лишний основной исполнитель −10
    («FACE & Молчат Дома», «Yung Lean x Beach House» — обычно мэшап или другая запись),
    пропавший гость −3."""
    vk_everyone = list(vk_artist.names) + list(vk_artist.feats) + list(vk_title.feats)
    cand_everyone = list(cand_artist.names) + list(cand_artist.feats) + list(cand_title.feats)
    cand_feats = list(cand_artist.feats) + list(cand_title.feats)
    vk_feats = list(vk_artist.feats) + list(vk_title.feats)
    extra = any(max((_sim(f, v) for v in vk_everyone), default=0) < 80 for f in cand_feats)
    extra_main = len(cand_artist.names) > 1 and any(not _known_artist(n, vk_everyone) for n in cand_artist.names)
    # В названиях загрузок гостей часто опускают, поэтому их отсутствие штрафуем слабее.
    cand_text = " ".join([cand_artist.full, cand_title.core_full, cand_title.extra])
    missing = any(
        max((_sim(f, c) for c in cand_everyone), default=0) < 80 and f not in cand_text
        for f in vk_feats
    )
    return 8 * extra + 10 * extra_main + 3 * missing


def duration_adjust(score: float, diff: Optional[int], names_match: bool = True) -> float:
    """0–2 с — очень сильный плюс, 3–5 с — сильный, 6–10 с — умеренный,
    11–20 с — небольшой штраф, дальше — сильный штраф.

    Плюс даётся, только если названия и исполнитель и так похожи: совпавшая
    длительность подтверждает похожие названия, но не спасает непохожие."""
    if diff is None:
        return score
    if diff <= 10:
        if not names_match:
            return score
        boost = 0.45 if diff <= 2 else 0.3 if diff <= DURATION_TOLERANCE else 0.15
        return score + (100 - score) * boost
    if diff <= 20:
        return score - (diff - 10) * 0.8
    if diff <= 45:
        return score - 5 - (diff - 20)
    return score - 40


def _views(cand: Candidate) -> Iterable[tuple[ParsedArtist, ParsedTitle, bool]]:
    """Варианты прочтения кандидата: исполнитель из аккаунта или из названия «Artist - Title»."""
    slug_tags = url_tags(cand.url)
    title = parse_title(cand.title)
    title = replace(title, tags=title.tags | slug_tags)
    for name in dict.fromkeys(n for n in (cand.publisher_artist, cand.username) if n):
        yield parse_artist(name), title, True
    parts = _TITLE_SPLIT_RE.split(cand.title, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        left, right = parts
        for artist_part, title_part in ((left, right), (right, left)):
            t = parse_title(title_part)
            # Пометки версии могут стоять и в «исполнительской» половине: «хочешь? - земфира (speed up)»,
            # «Obedient - Bladee & Ecco2k Sygillain cover».
            a = parse_title(artist_part)
            half_tags = {"cover"} if _COVER_RE.search(simplify(artist_part)) else set()
            t = replace(
                t,
                tags=t.tags | a.tags | slug_tags | half_tags,
                extra=" ".join(x for x in (t.extra, a.extra) if x),
                feats=tuple(dict.fromkeys(t.feats + a.feats)),
            )
            yield parse_artist(artist_part), t, False


def score_candidate(track: Track, cand: Candidate) -> Scored:
    vk_artist = parse_artist(track.artist)
    vk_title = parse_title(track.title)
    diff = abs(track.duration - cand.duration) if track.duration and cand.duration else None

    vk_words = set(f"{vk_title.core_full} {vk_title.extra} {vk_artist.full} {' '.join(vk_title.feats)}".split())
    vk_names = artist_names(vk_artist, vk_title)
    # «Artist — 320», «Taylor Swift — 22»: число совпадает только с тем же числом.
    numeric_title = vk_title.core.replace(" ", "").isdigit()

    best: Optional[tuple] = None
    for c_artist, c_title, from_uploader in _views(cand):
        a = artist_similarity(vk_artist, c_artist, c_title)
        tm = title_match(vk_title, c_title, ignore=vk_artist.full.split() + c_artist.full.split())
        t = tm.score
        vp = version_penalty(vk_title, c_title)
        fp = feat_penalty(vk_artist, vk_title, c_artist, c_title)
        # Название штрафуем круче исполнителя: «The Business» vs «The Business Pt II» — разные песни.
        s = 0.45 * a + 0.55 * max(0.0, 100 - 2 * (100 - t))
        s -= vp
        s -= fp
        words_match = t >= 75 or (tm.containment >= 0.8 and tm.coverage >= 0.6)
        s = duration_adjust(s, diff, names_match=a >= 80 and words_match
                            and vp < VERSION_CONFLICT_PENALTY and not tm.conflict)
        if numeric_title and c_title.core.replace(" ", "") != vk_title.core.replace(" ", ""):
            s = min(s, MIN_SHOW_SCORE + 15)
        uploader_match = from_uploader and a >= 85
        if best is None or (s, uploader_match) > (best[0], best[1]):
            extra = set(f"{c_title.core_full} {c_title.extra}".split()) - vk_words - set(c_artist.full.split())
            exact = artists_exact(vk_names, artist_names(c_artist, c_title), a)
            best = (s, uploader_match, c_title, a, tm, vp, len(extra), exact, fp)

    assert best is not None
    score, uploader_match, parsed, a, tm, vp, extra_words, exact, fp = best
    if cand.policy == "BLOCK":
        score -= 10
    return Scored(
        candidate=cand,
        score=int(round(max(0.0, min(100.0, score)))),
        uploader_match=uploader_match,
        title=parsed,
        duration_diff=diff,
        artist_sim=a,
        title_sim=tm.score,
        version_penalty=vp,
        extra_words=extra_words,
        title_fuzzy=tm.fuzzy,
        containment=tm.containment,
        coverage=tm.coverage,
        title_conflict=tm.conflict,
        artist_exact=exact,
        feat_penalty=fp,
    )


def rank(track: Track, candidates: Iterable[Candidate]) -> list[Scored]:
    seen: set[int] = set()
    scored = []
    for c in candidates:
        if c.id in seen:
            continue
        seen.add(c.id)
        scored.append(score_candidate(track, c))
    scored.sort(key=lambda s: s.sort_key, reverse=True)
    return scored


def _same_recording(a: Scored, b: Scored) -> bool:
    """Два кандидата — одна и та же запись (перезаливка), а не разные версии."""
    if a.title.tags != b.title.tags or _sim(a.title.core, b.title.core) < 90:
        return False
    da, db = a.candidate.duration, b.candidate.duration
    return not (da and db and abs(da - db) > DURATION_TOLERANCE)


def decide(track: Track, ranked: list[Scored], auto_threshold: int = AUTO_THRESHOLD) -> Decision:
    shown = [s for s in ranked if s.score >= MIN_SHOW_SCORE]
    if not shown:
        return Decision("none", ranked, "ничего похожего")
    best = shown[0]
    if best.score < auto_threshold:
        return Decision("ask", shown, f"низкая уверенность ({best.score})")
    if best.duration_diff is not None and best.duration_diff > DURATION_TOLERANCE:
        return Decision("ask", shown, f"длительность отличается на {best.duration_diff} с")
    rivals = [
        s for s in shown[1:]
        if s.score >= best.score - AMBIGUITY_MARGIN and not _same_recording(best, s)
    ]
    if rivals and track.duration is None and best.uploader_match:
        # Без длительности версии не различить, но если официальная загрузка
        # во много раз популярнее остальных — выбор очевиден.
        plays = best.candidate.playback_count
        if all(plays >= POPULARITY_RATIO * max(1, r.candidate.playback_count) for r in rivals):
            rivals = []
    if rivals:
        return Decision("ask", shown, "несколько похожих вариантов")
    return Decision("auto", shown)


def _auto_ok(s: Scored) -> bool:
    """Жёсткие условия: при их нарушении кандидат не выбирается никаким правилом."""
    if s.duration_diff is not None and s.duration_diff > MAX_AUTO_DURATION_DIFF:
        return False
    return s.artist_sim >= MIN_ARTIST_SIM and not s.version_conflict and not s.title_conflict


def _same_version(a: Scored, b: Scored) -> bool:
    """Одна версия песни: те же теги (radio edit и т. п. не в счёт) и не разные авторы ремикса.
    «(Slowed)» и «(Slowed TikTok Version)» — одна версия; «(A Remix)» и «(B Remix)» — разные."""
    if a.title.tags - SOFT_TAGS != b.title.tags - SOFT_TAGS:
        return False
    va, vb = _version_author(a.title.extra), _version_author(b.title.extra)
    return not (va and vb and _sim(va, vb) < 70)


def _beats(best: Scored, rival: Scored) -> bool:
    """best однозначно лучше соперника по признакам, даже если общие оценки близки."""
    if not _auto_ok(rival) or rival.version_penalty > best.version_penalty:
        return True
    if best.duration_diff is not None and rival.duration_diff is not None \
            and rival.duration_diff >= best.duration_diff + BEATS_DURATION:
        return True
    return rival.artist_sim <= best.artist_sim - BEATS_SIM or rival.title_sim <= best.title_sim - BEATS_SIM


def _accept_rule(s: Scored) -> Optional[str]:
    """Правила, при которых сильные признаки позволяют названию отличаться сильнее.

    Все требуют известной длительности и отсутствия конфликта версий/частей.
    A: исполнитель ≥92, длительность ±5 с, название ≥75 (≥70 при точном наборе
       исполнителей или длительности ±2 с);
    B: исполнитель ≥85, длительность ±8 с, название ≥80;
    C: название ≥92, исполнитель ≥75, длительность ±10 с;
    D: исполнитель ≥95, длительность ±2 с, ≥65% слов кандидата есть в названии
       из VK и ≥50% слов из VK есть у кандидата.
    """
    d = s.duration_diff
    if d is None or s.version_conflict or s.title_conflict:
        return None
    a, t = s.artist_sim, s.title_sim
    if a >= 92 and d <= 5 and t >= (70 if s.artist_exact or d <= 2 else 75):
        return "точный исполнитель + длительность" if s.artist_exact else "исполнитель + длительность"
    if a >= 85 and d <= 8 and t >= 80:
        return "исполнитель + название + длительность"
    if t >= 92 and a >= 75 and d <= 10:
        return "название + длительность"
    if a >= 95 and d <= 2 and s.containment >= 0.65 and s.coverage >= 0.5:
        return "точный исполнитель + точная длительность, слова названия совпадают"
    return None


def _medium_ok(s: Scored) -> bool:
    """Средняя зона без подтверждения длительностью: исполнитель и название должны быть явными."""
    if s.version_conflict or s.title_conflict or s.artist_sim < 85:
        return False
    if s.duration_diff is not None and s.duration_diff > 10:
        return False
    return s.title_sim >= 85 or (s.containment >= 0.8 and s.coverage >= 0.7)


def _reject_reason(s: Scored) -> str:
    if s.version_conflict:
        return "другая версия"
    if s.duration_diff is not None and s.duration_diff > 10:
        return f"длительность отличается на {s.duration_diff} с"
    if s.artist_sim < MIN_ARTIST_SIM:
        return "другой исполнитель"
    if s.title_conflict:
        return "похоже на другую часть или другой трек"
    if s.title_sim < 70 and s.containment < 0.65:
        return "название не совпадает"
    return f"низкая уверенность ({s.score})"


def auto_pick(track: Track, ranked: list[Scored], auto_threshold: int = AUTO_THRESHOLD) -> Decision:
    """Выбор без вопросов. Результат детерминирован: те же кандидаты → тот же трек.

    1. Отсев: другая версия, другая часть трека («Pt. II», «2»), длительность хуже
       чем на 20 с, чужой исполнитель.
    2. Лучший из оставшихся — по общей оценке; почти равные (±3) различаются
       длительностью, версией, точностью исполнителя, лишними словами,
       официальностью, популярностью.
    3. Принятие по совокупности признаков:
       - общая оценка ≥ порога (85);
       - одно из правил A–D (сильный исполнитель + длительность при менее похожем
         названии), если лучший явно лучше соперников;
       - средняя зона (70–84) без длительности — только при явных исполнителе и
         названии и явном отрыве от соперников.
    4. Если рядом соперник, которого лучший не превосходит ни длительностью, ни
       исполнителем, ни названием, а у них разные версии — пропуск.
    """
    shown = [s for s in ranked if s.score >= MIN_SHOW_SCORE]
    if not shown:
        return Decision("none", ranked, "ничего похожего")
    eligible = [s for s in shown if _auto_ok(s)]
    if not eligible:
        return Decision("low", shown, _reject_reason(shown[0]))
    top_score = eligible[0].score
    close = [s for s in eligible if s.score >= top_score - TIE_MARGIN]
    best = min(close, key=lambda s: s.tie_key)
    ordered = [best] + [s for s in shown if s is not best]

    rivals = [s for s in shown if s is not best and not _same_recording(best, s)]
    # Соперники, которых лучший не превосходит явно ни по одному признаку.
    unresolved = [r for r in rivals if best.score - r.score < MEDIUM_MARGIN and not _beats(best, r)]
    rule = _accept_rule(best)

    if best.score >= auto_threshold:
        tied = [r for r in unresolved if abs(best.score - r.score) <= TIE_MARGIN and not _same_version(best, r)]
        if tied:
            return Decision("low", ordered, "почти равные варианты разных версий")
        others = [s for s in close if s is not best and not _same_recording(best, s)]
        if rule:
            reason = rule
        elif others:
            reason = f"лучший из {len(others) + 1} похожих"
        elif best.title_sim >= 95 and best.artist_sim >= 92:
            reason = "точное совпадение исполнителя и названия"
        else:
            reason = "высокий общий score"
        return Decision("auto", ordered, reason, confident=True)

    if rule and best.score >= auto_threshold - RELAXED_BAND:
        if unresolved:
            return Decision("low", ordered, f"несколько равных вариантов ({best.score})")
        return Decision("auto", ordered, rule, confident=True)

    if best.score >= auto_threshold - MEDIUM_BAND and _medium_ok(best):
        if unresolved:
            return Decision("low", ordered, f"несколько равных вариантов ({best.score})")
        return Decision("auto", ordered, "средняя уверенность, других вариантов нет")
    return Decision("low", ordered, _reject_reason(best))


def rival_margin(decision: Decision) -> Optional[int]:
    """Отрыв лучшего кандидата от лучшей другой записи, которую тоже можно было бы выбрать
    (отсеянные — другая версия, чужая длительность — не в счёт). Для лога и отчёта."""
    best = decision.best
    if best is None:
        return None
    rivals = [s.score for s in decision.ranked
              if s is not best and not _same_recording(best, s) and _auto_ok(s)]
    return best.score - max(rivals) if rivals else None
