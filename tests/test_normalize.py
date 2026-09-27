import pytest

from vk2sc.normalize import (
    clean_artist_for_query,
    clean_title_for_query,
    compact,
    fold,
    parse_artist,
    parse_title,
    simplify,
    strip_username_suffix,
    translit,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  Imagine   DRAGONS ", "imagine dragons"),
        ("Ёлка", "елка"),
        ("Tiësto", "tiesto"),
        ("Beyoncé", "beyonce"),
        ("Йод", "йод"),  # й не должна превращаться в и
        ("Земфира — Хочешь?", "земфира - хочешь?"),
    ],
)
def test_fold(raw, expected):
    assert fold(raw) == expected


def test_simplify_and_compact():
    assert simplify("Don't Stop Me Now!") == "dont stop me now"
    assert simplify("A$AP Rocky") == "asap rocky"
    assert compact("Imagine Dragons") == compact("ImagineDragons") == "imaginedragons"


def test_translit():
    assert translit(simplify("Сплин")) == "splin"
    assert translit(simplify("Земфира")) == "zemfira"


def test_username_suffix():
    assert strip_username_suffix("ImagineDragonsOfficial") == "imaginedragons"
    assert strip_username_suffix("Muse Music") == "muse"
    assert strip_username_suffix("Tv") == "tv"  # слишком коротко, не трогаем


@pytest.mark.parametrize(
    "raw",
    [
        "Believer (Official Audio)",
        "Believer [Official Music Video]",
        "Believer (Lyrics)",
        "Believer (Remastered 2019)",
        "Believer (HD)",
        "Believer (prod. by Someone)",
        "Believer - Official Video",
    ],
)
def test_noise_is_removed(raw):
    parsed = parse_title(raw)
    assert parsed.core == "believer"
    assert parsed.tags == frozenset()


@pytest.mark.parametrize(
    "raw, tags",
    [
        ("Bad Romance (Skrillex Remix)", {"remix"}),
        ("Bad Romance - Skrillex Remix", {"remix"}),
        ("Believer Remix", {"remix"}),
        ("Хочешь? (Live)", {"live"}),
        ("Kosandra (Slowed + Reverb)", {"slowed"}),
        ("Numb (Instrumental)", {"instrumental"}),
        ("Get Lucky (Radio Edit)", {"radio_edit"}),
        ("One More Time (Extended Mix)", {"extended"}),
        ("Song (Nightcore)", {"sped_up"}),
    ],
)
def test_version_tags(raw, tags):
    assert parse_title(raw).tags == frozenset(tags)


def test_live_in_title_is_not_a_tag():
    parsed = parse_title("Live Your Life")
    assert parsed.core == "live your life"
    assert parsed.tags == frozenset()


def test_remix_keeps_remixer():
    assert parse_title("Bad Romance (Skrillex Remix)").extra == "skrillex remix"


@pytest.mark.parametrize(
    "raw",
    [
        "Starboy (feat. Daft Punk)",
        "Starboy feat. Daft Punk",
        "Starboy (ft. Daft Punk)",
        "Starboy ft. Daft Punk [Official Video]",
        "Starboy (with Daft Punk)",
    ],
)
def test_feat_in_title(raw):
    parsed = parse_title(raw)
    assert parsed.core == "starboy"
    assert parsed.feats == ("daft punk",)


def test_parse_artist_with_feat_and_multiple():
    a = parse_artist("The Weeknd feat. Daft Punk")
    assert a.names == ("the weeknd",) and a.feats == ("daft punk",)
    b = parse_artist("Miyagi & Andy Panda")
    assert b.names == ("miyagi", "andy panda")
    c = parse_artist("Macan x Scirena")
    assert c.names == ("macan", "scirena")
    assert parse_artist("Би-2").names == ("би 2",)


def test_query_cleanup():
    assert clean_title_for_query("Believer (Official Audio)") == "Believer"
    assert clean_title_for_query("Bad Romance (Skrillex Remix)") == "Bad Romance Skrillex Remix"
    assert clean_title_for_query("Starboy (feat. Daft Punk)") == "Starboy"
    assert clean_title_for_query("Get Lucky ft. Pharrell") == "Get Lucky"
    assert clean_artist_for_query("The Weeknd feat. Daft Punk") == "The Weeknd"


# ---- Технические хвосты VK ----

from vk2sc.normalize import is_vk_junk, strip_vk_junk  # noqa: E402


@pytest.mark.parametrize(
    "raw",
    [
        "Song Name (VK.COM)",
        "Song Name [VK.COM]",
        "Song Name (vk.com reupload)",
        "Song Name (Reupload)",
        "Song Name [reuploaded]",
        "Song Name (re-upload)",
        "Song Name (upload)",
        "Song Name (uploaded by user123)",
        "Song Name (Uploaded by DJ Some One)",
        "Song Name (VK Reuploads)",
        "Song Name [source: vk.com]",
        "Song Name (source)",
        "Song Name (vk rip)",
        "Song Name (rip)",
        "Song Name (vkontakte)",
        "Song Name (ВКонтакте)",
        "Song Name (vk.com/club123)",
        "Song Name - vk.com",
        "Song Name vk.com",
        "Song Name (VK.COM) [Reupload]",
        "Song Name [Reupload] (VK.COM)",
        "  Song   Name (VK.COM)  ",
    ],
)
def test_vk_junk_is_stripped(raw):
    assert strip_vk_junk(raw) == "Song Name"


@pytest.mark.parametrize(
    "raw",
    [
        "Song Name (Remix)",
        "Song Name (Live)",
        "Song Name (Acoustic)",
        "Song Name (Slowed)",
        "Song Name (Sped Up)",
        "Song Name (Instrumental)",
        "Song Name (Demo)",
        "Song Name (Radio Edit)",
        "Song Name (Deluxe)",
        "Song Name (Remastered)",
        "Song Name (feat. Artist)",
        "Song Name (Prod. Artist)",
        "Have You Ever Been Mellow (Flamman & Abraxas Radio Mix)",
        "Song Name (R.I.P.)",
        "Song Name (Upload Your Mind)",
        "Song Name (VK Remix)",
        "Upload",  # само название не трогаем
        "(VK.COM)",  # после чистки ничего бы не осталось — оставляем как есть
    ],
)
def test_meaningful_brackets_are_kept(raw):
    assert strip_vk_junk(raw) == raw


def test_junk_only_at_the_end_is_stripped_and_versions_survive():
    assert strip_vk_junk("Song (Remix) (VK.COM)") == "Song (Remix)"
    assert strip_vk_junk("Song (Live) [vk.com reupload]") == "Song (Live)"


def test_junk_is_noise_for_matching_and_query():
    assert is_vk_junk("VK.COM") and is_vk_junk("uploaded by someone") and not is_vk_junk("Remix")
    parsed = parse_title("Believer (VK.COM) [Reupload]")
    assert parsed.core == "believer" and parsed.extra == "" and parsed.tags == frozenset()
    assert clean_title_for_query("Believer (VK.COM)") == "Believer"
    assert clean_title_for_query("Bad Romance (Skrillex Remix) [vk.com]") == "Bad Romance Skrillex Remix"


@pytest.mark.parametrize("raw", ["Believer (Visualizer)", "Believer (Audio)", "Believer (Topic)", "Believer [Lyrics]",
                                 "Believer (Official Lyric Video)"])
def test_more_noise(raw):
    parsed = parse_title(raw)
    assert parsed.core == "believer" and parsed.extra == "" and parsed.tags == frozenset()
