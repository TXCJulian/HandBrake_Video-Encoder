from app.config import parse_roots


def test_parse_roots_splits_and_normalises():
    assert parse_roots("/media1, /media2/") == ["/media1", "/media2"]


def test_parse_roots_ignores_blank_entries():
    assert parse_roots("/media1,,  ,/media2") == ["/media1", "/media2"]


def test_parse_roots_of_empty_string_is_empty():
    assert parse_roots("") == []
