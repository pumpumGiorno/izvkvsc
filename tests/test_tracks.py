import pytest

from vk2sc.tracks import Track, TracksFileError, format_duration, parse_line, read_tracks, track_keys


@pytest.mark.parametrize(
    "line, artist, title, duration",
    [
        ("Imagine Dragons — Believer", "Imagine Dragons", "Believer", None),
        ("Imagine Dragons – Believer", "Imagine Dragons", "Believer", None),
        ("Imagine Dragons - Believer", "Imagine Dragons", "Believer", None),
        ("Imagine Dragons—Believer", "Imagine Dragons", "Believer", None),
        ("  Кино — Группа крови | 4:45 ", "Кино", "Группа крови", 285),
        ("Кино — Группа крови\t4:45", "Кино", "Группа крови", 285),
        ("Artist — Long Mix | 1:02:03", "Artist", "Long Mix", 3723),
        ("Би-2 — Полковнику никто не пишет", "Би-2", "Полковнику никто не пишет", None),
        ("Lady Gaga — Bad Romance - Skrillex Remix", "Lady Gaga", "Bad Romance - Skrillex Remix", None),
    ],
)
def test_parse_line(line, artist, title, duration):
    assert parse_line(line) == Track(artist, title, duration)


@pytest.mark.parametrize("line", ["", "   ", "# комментарий"])
def test_skip_lines(line):
    assert parse_line(line) is None


def test_bad_line():
    with pytest.raises(ValueError):
        parse_line("Просто название без исполнителя")


def test_read_tracks_utf8_bom_and_warnings(tmp_path):
    f = tmp_path / "tracks.txt"
    f.write_bytes("﻿Кино — Группа крови\nбез разделителя\n\n# c\nЗемфира — Хочешь? | 3:45\n".encode("utf-8"))
    tracks, warnings = read_tracks(f)
    assert [t.artist for t in tracks] == ["Кино", "Земфира"]
    assert tracks[1].duration == 225 and tracks[1].line_no == 5
    assert len(warnings) == 1 and "строка 2" in warnings[0]


def test_read_tracks_cp1251(tmp_path):
    f = tmp_path / "tracks.txt"
    f.write_bytes("Кино — Звезда по имени Солнце\n".encode("cp1251"))
    tracks, _ = read_tracks(f)
    assert tracks[0].title == "Звезда по имени Солнце"


def test_read_tracks_errors(tmp_path):
    with pytest.raises(TracksFileError):
        read_tracks(tmp_path / "missing.txt")
    empty = tmp_path / "empty.txt"
    empty.write_text("# ничего\n", encoding="utf-8")
    with pytest.raises(TracksFileError):
        read_tracks(empty)


def test_track_keys_distinguish_duplicates_and_ignore_case():
    tracks = [Track("Кино", "Звезда"), Track("КИНО", "звезда"), Track("Ёлка", "Прованс")]
    assert track_keys(tracks) == ["кино — звезда #1", "кино — звезда #2", "елка — прованс #1"]


def test_format_duration():
    assert format_duration(204) == "3:24"
    assert format_duration(3723) == "1:02:03"
    assert format_duration(None) == ""


def test_hash_artist_is_not_a_comment():
    assert parse_line("#2Маши — Босая | 3:10") == Track("#2Маши", "Босая", 190)
    assert parse_line("# Экспорт из VK 2026-09-26") is None
    assert parse_line("#") is None


def test_unreadable_encoding_is_a_clear_error(tmp_path):
    f = tmp_path / "tracks.txt"
    f.write_bytes(b"\x98\x98 \xe2\x80 bad")
    with pytest.raises(TracksFileError, match="UTF-8"):
        read_tracks(f)


def test_utf16_file(tmp_path):
    f = tmp_path / "tracks.txt"
    f.write_bytes("Кино — Звезда\n".encode("utf-16"))
    tracks, _ = read_tracks(f)
    assert tracks[0].artist == "Кино"


@pytest.mark.parametrize(
    "title", ["~128", "~192", "256 kbps", "128kbps", "~320 KBPS", "~ 128", "320 кбит/с", "192 kbit/s"]
)
def test_explicit_bitrate_titles_are_always_broken(title):
    assert Track("@ФакШиза", title).broken_title


@pytest.mark.parametrize("title", ["320", " 320 ", "128"])
def test_plain_number_is_bitrate_only_in_context(title):
    # Одиночное «Artist — 320» может быть настоящей песней.
    assert not Track("Artist", title).broken_title
    assert Track("Artist", title, bitrate_context=True).broken_title


def test_read_tracks_sets_bitrate_context(tmp_path):
    f = tmp_path / "tracks.txt"
    f.write_text("Artist — 320\nImagine Dragons — Believer\n", encoding="utf-8")
    tracks, _ = read_tracks(f)
    assert not tracks[0].broken_title  # без контекста — обычное название
    f.write_text("Artist — 320\nOther — ~128\nImagine Dragons — Believer\n", encoding="utf-8")
    tracks, _ = read_tracks(f)
    assert tracks[0].broken_title and tracks[1].broken_title and not tracks[2].broken_title
    f.write_text("Artist — 320\nOther — 256\nImagine Dragons — Believer\n", encoding="utf-8")
    tracks, _ = read_tracks(f)
    assert tracks[0].broken_title and tracks[1].broken_title  # два голых битрейта — тоже баг экспорта
    assert tracks[0] == Track("Artist", "320", None, 1)  # контекст не влияет на сравнение


@pytest.mark.parametrize("title", ["22", "505", "99", "911", "1979", "Believer", "320 Degrees", "~ночь", ""])
def test_real_titles_are_not_broken(title):
    # «Arctic Monkeys — 505», «Taylor Swift — 22»: числа, но не стандартный битрейт.
    assert not Track("Artist", title).broken_title
