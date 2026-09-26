from vk2sc.matching import AUTO_THRESHOLD, Candidate, decide, rank, score_candidate
from vk2sc.tracks import Track


def cand(id, title, username, duration=None, publisher=None, plays=0, policy="ALLOW"):
    return Candidate(
        id=id,
        title=title,
        username=username,
        url=f"https://soundcloud.com/u/{id}",
        duration=duration,
        publisher_artist=publisher,
        policy=policy,
        playback_count=plays,
    )


def test_example_from_task():
    """«Imagine Dragons — Believer (Official Audio)» против «Believer» от «ImagineDragons»."""
    track = Track("Imagine Dragons", "Believer (Official Audio)")
    scored = score_candidate(track, cand(1, "Believer", "ImagineDragons"))
    assert scored.score >= 95
    assert decide(track, [scored]).kind == "auto"


def test_artist_in_title_uploaded_by_someone_else():
    track = Track("Imagine Dragons", "Believer", 204)
    scored = score_candidate(track, cand(2, "Imagine Dragons - Believer", "random uploader", 205))
    assert scored.score >= 95


def test_reversed_title_order():
    track = Track("Imagine Dragons", "Believer")
    scored = score_candidate(track, cand(3, "Believer - Imagine Dragons", "someone"))
    assert scored.score >= 90


def test_remix_vs_original_is_penalized():
    track = Track("Lady Gaga", "Bad Romance")
    original = score_candidate(track, cand(1, "Bad Romance", "Lady Gaga"))
    remix = score_candidate(track, cand(2, "Bad Romance (Skrillex Remix)", "Lady Gaga"))
    assert original.score >= 95
    assert remix.score < AUTO_THRESHOLD
    assert original.score - remix.score >= 25


def test_remix_matches_same_remix():
    track = Track("Lady Gaga", "Bad Romance (Skrillex Remix)")
    same = score_candidate(track, cand(1, "Lady Gaga - Bad Romance (Skrillex Remix)", "skrillex fan"))
    other = score_candidate(track, cand(2, "Bad Romance (Hercules Remix)", "Lady Gaga"))
    original = score_candidate(track, cand(3, "Bad Romance", "Lady Gaga"))
    assert same.score >= AUTO_THRESHOLD
    assert same.score > other.score > original.score


def test_cyrillic_and_yo():
    track = Track("Ёлка", "Прованс")
    scored = score_candidate(track, cand(1, "Прованс", "Елка"))
    assert scored.score >= 95


def test_cyrillic_vs_translit_uploader():
    track = Track("Земфира", "Хочешь?")
    scored = score_candidate(track, cand(1, "Хочешь?", "Zemfira"))
    assert scored.score >= AUTO_THRESHOLD


def test_feat_in_artist_field_vs_title():
    track = Track("The Weeknd feat. Daft Punk", "Starboy")
    scored = score_candidate(track, cand(1, "Starboy (feat. Daft Punk)", "The Weeknd"))
    assert scored.score >= 95


def test_unexpected_feat_is_penalized():
    track = Track("Imagine Dragons", "Believer")
    plain = score_candidate(track, cand(1, "Believer", "Imagine Dragons"))
    with_feat = score_candidate(track, cand(2, "Believer (feat. Lil Wayne)", "Imagine Dragons"))
    assert plain.score > with_feat.score


def test_duration_within_tolerance_boosts_and_mismatch_penalizes():
    track = Track("Imagine Dragons", "Believer", 204)
    close = score_candidate(track, cand(1, "Believer", "Imagine Dragons", 206))
    far = score_candidate(track, cand(2, "Believer", "Imagine Dragons", 260))
    assert close.score >= 95
    assert far.score < AUTO_THRESHOLD
    assert close.duration_diff == 2 and far.duration_diff == 56


def test_duration_off_by_more_than_tolerance_asks():
    track = Track("Imagine Dragons", "Believer", 204)
    ranked = rank(track, [cand(1, "Believer", "Imagine Dragons", 213)])
    decision = decide(track, ranked)
    assert decision.kind == "ask"


def test_wrong_artist_same_title():
    track = Track("Imagine Dragons", "Believer")
    scored = score_candidate(track, cand(1, "Believer", "Kaskade"))
    assert scored.score < AUTO_THRESHOLD


def test_completely_different_is_none():
    track = Track("Кино", "Группа крови")
    ranked = rank(track, [cand(1, "Summer Vibes Mix 2019", "dj someone")])
    assert decide(track, ranked).kind == "none"


def test_several_versions_cause_question():
    track = Track("Daft Punk", "Get Lucky")
    ranked = rank(
        track,
        [
            cand(1, "Get Lucky", "Daft Punk", 369),
            cand(2, "Get Lucky (Radio Edit)", "Daft Punk", 248),
        ],
    )
    # Радио-версия отличается лишь «мягким» тегом, но длительность у записей разная.
    assert decide(track, ranked).kind == "ask"


def test_reuploads_of_same_recording_are_not_ambiguous():
    track = Track("Imagine Dragons", "Believer", 204)
    ranked = rank(
        track,
        [
            cand(1, "Imagine Dragons - Believer", "fan", 204, plays=10),
            cand(2, "Believer", "Imagine Dragons", 204, publisher="Imagine Dragons", plays=1000),
        ],
    )
    decision = decide(track, ranked)
    assert decision.kind == "auto"
    # При равной оценке выигрывает официальная загрузка.
    assert decision.best.candidate.id == 2


def test_candidate_from_api_uses_full_duration_for_go_plus():
    c = Candidate.from_api(
        {
            "id": 305338425,
            "title": "Believer",
            "duration": 30000,
            "full_duration": 203833,
            "policy": "SNIP",
            "permalink_url": "https://soundcloud.com/imaginedragons/believer",
            "publisher_metadata": {"artist": "Imagine Dragons"},
            "user": {"username": "Imagine Dragons"},
            "playback_count": 5,
        }
    )
    assert c.duration == 204
    assert c.flags == "только Go+"
    assert Candidate.from_dict(c.to_dict()) == c


# ---- Случаи из живого прогона по SoundCloud ----


def test_unknown_notes_are_not_original():
    track = Track("Ёлка", "Прованс")
    for title in ("Ёлка - Прованс [Phonk Edition]", "Ёлка - Прованс (hardstyle)",
                  "Елка - Прованс (Елена сокирко X-фактор)", "Ёлка - Прованс (RemixeR)"):
        assert score_candidate(track, cand(1, title, "someone")).score < AUTO_THRESHOLD, title


def test_translation_tail_is_penalized():
    track = Track("Imagine Dragons", "Believer", 204)
    junk = score_candidate(track, cand(1, "Imagine Dragons - Believer - Перевод на русском", "danon_", 211))
    assert junk.score < AUTO_THRESHOLD


def test_tag_in_artist_half_of_reversed_title():
    track = Track("Земфира", "Хочешь?")
    sped = score_candidate(track, cand(1, "хочешь? - земфира (speed up)", "sarqunil"))
    assert sped.score < AUTO_THRESHOLD


def test_feat_inside_version_brackets():
    track = Track("Daft Punk", "Get Lucky (Radio Edit) ft. Pharrell Williams", 248)
    official = cand(1, "Get Lucky (Radio Edit - feat. Pharrell Williams and Nile Rodgers)", "Daft Punk", 248,
                    publisher="Daft Punk, Pharrell Williams, Nile Rodgers")
    assert score_candidate(track, official).score >= 90


def test_same_length_different_song_not_boosted():
    track = Track("Tiësto", "The Business", 164)
    part2 = score_candidate(track, cand(1, "Tiësto & Ty Dolla $ign - The Business, Pt. II", "Tiesto", 164))
    assert part2.score < AUTO_THRESHOLD


def test_popular_official_upload_wins_without_duration():
    track = Track("Сплин", "Выхода нет")
    ranked = rank(track, [
        cand(1, "Выхода нет", "СПЛИН", 238, publisher="Сплин", plays=3_682_588),
        cand(2, "Выхода нет", "Сплин", 228, publisher="Сплин", plays=20_954),
    ])
    decision = decide(track, ranked)
    assert decision.kind == "auto" and decision.best.candidate.id == 1


def test_comparable_official_versions_still_ask_without_duration():
    track = Track("Кино", "Группа крови")
    ranked = rank(track, [
        cand(1, "Группа крови", "Кино", 284, publisher="Кино", plays=387_172),
        cand(2, "Группа крови", "Кино", 247, plays=232_296),
    ])
    assert decide(track, ranked).kind == "ask"


def test_duration_from_vk_resolves_versions():
    track = Track("Кино", "Группа крови", 285)
    ranked = rank(track, [
        cand(1, "Группа крови", "Кино", 284, publisher="Кино", plays=387_172),
        cand(2, "Группа крови", "Кино", 247, plays=232_296),
    ])
    decision = decide(track, ranked)
    assert decision.kind == "auto" and decision.best.candidate.id == 1


def test_remix_hidden_in_url_slug():
    track = Track("Земфира", "Хочешь?")
    renamed = Candidate(id=1, title="Хочешь", username="Клубная Музыка", publisher_artist="Земфира",
                        url="https://soundcloud.com/russian-club-house/zemfira-superhit-want-remix", duration=128)
    assert score_candidate(track, renamed).score < AUTO_THRESHOLD


def test_title_that_is_only_a_tag_word():
    track = Track("Drake", "Karaoke", 229)
    assert score_candidate(track, cand(1, "Karaoke", "Drake", 229)).score >= 95
    track = Track("Pleymo", "Acoustic")
    assert score_candidate(track, cand(1, "Acoustic", "Pleymo")).score >= 95


def test_control_characters_stripped_from_api_fields():
    c = Candidate.from_api({"id": 1, "title": "Song\x1b[2J\x07", "user": {"username": "U\x1b]0;x\x07"},
                            "permalink_url": "https://soundcloud.com/u/s"})
    assert "\x1b" not in c.title and "\x07" not in c.title and "\x1b" not in c.username
