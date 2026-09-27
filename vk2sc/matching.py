"""Оценка совпадения трека из VK с кандидатом из SoundCloud.

Оценка 0–100 складывается из похожести исполнителя (45%) и названия (55%),
затем применяются штрафы: другая версия (ремикс/лайв/…), лишние или
пропавшие feat.-гости, расхождение длительности. Если длительность известна
с обеих сторон и совпадает в пределах ±5 сек, оценка подтягивается к 100.

auto_pick() выбирает кандидата без участия человека (обычный режим),
decide() — прежняя логика с вопросами для режима --interactive.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from typing import Iterable, Optional

from rapidfuzz import fuzz

from .normalize import (
    SOFT_TAGS,
    ParsedArtist,
    ParsedTitle,
    has_cyrillic,
    parse_artist,
    parse_title,
    strip_username_suffix,
    translit,
    translit_skeleton,
    url_tags,
)
from .tracks import Track

# Меняется при любом изменении оценки/выбора: записи state.json со старой версией
# можно пересчитать (--rematch), не трогая ручной выбор.
MATCHING_VERSION = 2

AUTO_THRESHOLD = 85  # не ниже — добавляем без вопросов
MEDIUM_BAND = 15  # от порога минус столько — берём, только если кандидат явно лучше остальных
MEDIUM_MARGIN = 10  # насколько лучший должен опережать другую запись в средней зоне
TIE_MARGIN = 3  # кандидаты ближе этого к лучшему считаются равными — решают длительность и т. п.
MIN_ARTIST_SIM = 70  # похожее название при чужом исполнителе автоматически не берём
MAX_AUTO_DURATION_DIFF = 20  # сек: при большей разнице автоматически не берём
MAX_VERSION_PENALTY_MEDIUM = 10  # в средней зоне — никаких признаков другой версии
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
    title_sim: float = 0.0
    version_penalty: float = 0.0
    extra_words: int = 0  # слов в названии кандидата, которых нет в исходнике

    @property
    def tie_key(self) -> tuple:
        """Выбор среди почти равных: ближе длительность, точнее исполнитель, меньше
        лишних слов, официальная загрузка, популярность. id — для полной детерминированности."""
        diff = self.duration_diff if self.duration_diff is not None else 10_000
        return (diff, -round(self.artist_sim), self.extra_words, not self.uploader_match,
                -self.candidate.playback_count, -self.score, self.candidate.id)

    @property
    def sort_key(self) -> tuple:
        return (self.score, self.uploader_match, self.candidate.playback_count)


@dataclass
class Decision:
    kind: str  # "auto" | "ask" | "low" | "none"
    ranked: list[Scored]
    reason: str = ""

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


def feat_penalty(vk_artist: ParsedArtist, vk_title: ParsedTitle, cand_artist: ParsedArtist, cand_title: ParsedTitle) -> float:
    vk_everyone = list(vk_artist.names) + list(vk_artist.feats) + list(vk_title.feats)
    cand_everyone = list(cand_artist.names) + list(cand_artist.feats) + list(cand_title.feats)
    cand_feats = list(cand_artist.feats) + list(cand_title.feats)
    vk_feats = list(vk_artist.feats) + list(vk_title.feats)
    extra = any(max((_sim(f, v) for v in vk_everyone), default=0) < 80 for f in cand_feats)
    # В названиях загрузок гостей часто опускают, поэтому их отсутствие штрафуем слабее.
    cand_text = " ".join([cand_artist.full, cand_title.core_full, cand_title.extra])
    missing = any(
        max((_sim(f, c) for c in cand_everyone), default=0) < 80 and f not in cand_text
        for f in vk_feats
    )
    return 8 * extra + 3 * missing


def duration_adjust(score: float, diff: Optional[int], names_match: bool = True) -> float:
    """≤5 с — сильный плюс, ≤10 с — небольшой, 10–20 с — небольшой штраф, дальше — сильный."""
    if diff is None:
        return score
    if diff <= DURATION_TOLERANCE:
        # Совпавшая длительность подтверждает похожие названия, но не спасает непохожие:
        # «The Business» и «The Business, Pt. II» могут оказаться одной длины.
        return score + (100 - score) * 0.3 if names_match else score
    if diff <= 10:
        return score + (100 - score) * 0.1 if names_match else score
    if diff <= 20:
        return score - (diff - 10) * 0.5
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
            # Пометки версии могут стоять и в «исполнительской» половине: «хочешь? - земфира (speed up)».
            a = parse_title(artist_part)
            t = replace(
                t,
                tags=t.tags | a.tags | slug_tags,
                extra=" ".join(x for x in (t.extra, a.extra) if x),
                feats=tuple(dict.fromkeys(t.feats + a.feats)),
            )
            yield parse_artist(artist_part), t, False


def score_candidate(track: Track, cand: Candidate) -> Scored:
    vk_artist = parse_artist(track.artist)
    vk_title = parse_title(track.title)
    diff = abs(track.duration - cand.duration) if track.duration and cand.duration else None

    vk_words = set(f"{vk_title.core_full} {vk_title.extra} {vk_artist.full} {' '.join(vk_title.feats)}".split())
    # «Artist — 320», «Taylor Swift — 22»: число совпадает только с тем же числом.
    numeric_title = vk_title.core.replace(" ", "").isdigit()

    best: Optional[tuple] = None
    for c_artist, c_title, from_uploader in _views(cand):
        a = artist_similarity(vk_artist, c_artist, c_title)
        t = title_similarity(vk_title, c_title)
        vp = version_penalty(vk_title, c_title)
        # Название штрафуем круче исполнителя: «The Business» vs «The Business Pt II» — разные песни.
        s = 0.45 * a + 0.55 * max(0.0, 100 - 2 * (100 - t))
        s -= vp
        s -= feat_penalty(vk_artist, vk_title, c_artist, c_title)
        s = duration_adjust(s, diff, names_match=t >= 90 and a >= 80 and vp < MAX_VERSION_PENALTY_MEDIUM)
        if numeric_title and c_title.core.replace(" ", "") != vk_title.core.replace(" ", ""):
            s = min(s, MIN_SHOW_SCORE + 15)
        uploader_match = from_uploader and a >= 85
        if best is None or (s, uploader_match) > (best[0], best[1]):
            extra = set(f"{c_title.core_full} {c_title.extra}".split()) - vk_words - set(c_artist.full.split())
            best = (s, uploader_match, c_title, a, t, vp, len(extra))

    assert best is not None
    score, uploader_match, parsed, a, t, vp, extra_words = best
    if cand.policy == "BLOCK":
        score -= 10
    return Scored(
        candidate=cand,
        score=int(round(max(0.0, min(100.0, score)))),
        uploader_match=uploader_match,
        title=parsed,
        duration_diff=diff,
        artist_sim=a,
        title_sim=t,
        version_penalty=vp,
        extra_words=extra_words,
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
    """Жёсткие условия автоматического выбора — одной похожести названия мало."""
    if s.duration_diff is not None and s.duration_diff > MAX_AUTO_DURATION_DIFF:
        return False
    return s.artist_sim >= MIN_ARTIST_SIM


def auto_pick(track: Track, ranked: list[Scored], auto_threshold: int = AUTO_THRESHOLD) -> Decision:
    """Выбор без вопросов. Результат детерминирован: те же кандидаты → тот же трек.

    - Лучший кандидат с оценкой ≥ порога (85) берётся сразу. Если рядом (±3) есть
      почти такие же, из них берётся ближайший по длительности, затем с более точным
      исполнителем, меньшим числом лишних слов, официальный, более популярный.
    - От порога−15 до порога (70–84) — только если нет признаков другой версии,
      длительность не расходится больше чем на 10 с и лучший опережает любую
      другую запись хотя бы на 10 баллов.
    - Иначе "low" (было что-то похожее, но сомнительное) или "none".
    """
    shown = [s for s in ranked if s.score >= MIN_SHOW_SCORE]
    if not shown:
        return Decision("none", ranked, "ничего похожего")
    eligible = [s for s in shown if _auto_ok(s)]
    if not eligible:
        top = shown[0]
        if top.duration_diff is not None and top.duration_diff > MAX_AUTO_DURATION_DIFF:
            return Decision("low", shown, f"длительность отличается на {top.duration_diff} с")
        return Decision("low", shown, "другой исполнитель")
    top_score = eligible[0].score
    close = [s for s in eligible if s.score >= top_score - TIE_MARGIN]
    best = min(close, key=lambda s: s.tie_key)
    ordered = [best] + [s for s in shown if s is not best]

    if best.score >= auto_threshold:
        others = [s for s in close if s is not best and not _same_recording(best, s)]
        reason = f"лучший из {len(others) + 1} похожих" if others else "уверенное совпадение"
        return Decision("auto", ordered, reason)

    medium = auto_threshold - MEDIUM_BAND
    if best.score >= medium:
        if best.title_sim < 85:
            return Decision("low", ordered, f"название заметно отличается ({best.score})")
        if best.version_penalty >= MAX_VERSION_PENALTY_MEDIUM:
            return Decision("low", ordered, f"похоже на другую версию ({best.score})")
        if best.duration_diff is not None and best.duration_diff > 10:
            return Decision("low", ordered, f"длительность отличается на {best.duration_diff} с")
        rivals = [s for s in shown if s is not best and not _same_recording(best, s)]
        if rivals and best.score - max(r.score for r in rivals) < MEDIUM_MARGIN:
            return Decision("low", ordered, f"несколько равных вариантов ({best.score})")
        return Decision("auto", ordered, "средняя уверенность, других вариантов нет")
    return Decision("low", ordered, f"низкая уверенность ({best.score})")
