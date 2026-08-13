import os
from unittest.mock import patch

import pytest

from app import config
from app.paths import (
    PathNotAllowed,
    SourceNotFound,
    derive_output_path,
    validate_source_path,
)


def test_accepts_a_file_inside_an_allowed_root(tmp_path):
    media = tmp_path / "media1"
    media.mkdir()
    movie = media / "movie.mkv"
    movie.write_text("x")
    assert validate_source_path(str(movie), [str(media)]) == os.path.realpath(str(movie))


def test_rejects_a_path_outside_the_allowed_roots(tmp_path):
    media = tmp_path / "media1"
    media.mkdir()
    outside = tmp_path / "secrets.txt"
    outside.write_text("x")
    with pytest.raises(PathNotAllowed):
        validate_source_path(str(outside), [str(media)])


def test_rejects_a_sibling_root_sharing_a_name_prefix(tmp_path):
    """/media1x must not pass for the root /media1 — startswith would allow it."""
    media = tmp_path / "media1"
    media.mkdir()
    sibling = tmp_path / "media1x"
    sibling.mkdir()
    intruder = sibling / "movie.mkv"
    intruder.write_text("x")
    with pytest.raises(PathNotAllowed):
        validate_source_path(str(intruder), [str(media)])


def test_rejects_when_no_roots_are_configured(tmp_path):
    movie = tmp_path / "movie.mkv"
    movie.write_text("x")
    with pytest.raises(PathNotAllowed):
        validate_source_path(str(movie), [])


def test_raises_source_not_found_for_a_missing_file_inside_a_root(tmp_path):
    media = tmp_path / "media1"
    media.mkdir()
    with pytest.raises(SourceNotFound):
        validate_source_path(str(media / "absent.mkv"), [str(media)])


def test_rejects_a_directory_even_inside_a_root(tmp_path):
    media = tmp_path / "media1"
    media.mkdir()
    with pytest.raises(SourceNotFound):
        validate_source_path(str(media), [str(media)])


def test_derives_a_hidden_sibling_output_path():
    out = derive_output_path("/media1/Movies/Dune.mkv", "abc123", "mkv")
    assert out == os.path.join("/media1/Movies", ".hbenc-abc123.mkv")


def test_derived_output_rejects_a_traversal_job_id():
    with pytest.raises(PathNotAllowed):
        derive_output_path("/media1/Movies/Dune.mkv", "../evil", "mkv")


def test_derived_output_rejects_a_traversal_extension():
    with pytest.raises(PathNotAllowed):
        derive_output_path("/media1/Movies/Dune.mkv", "abc123", "../mkv")


def test_rejects_a_symlink_pointing_outside_the_allowed_root(tmp_path):
    """Symlink inside root pointing to file outside must be rejected."""
    media = tmp_path / "media1"
    media.mkdir()
    outside = tmp_path / "secrets.txt"
    outside.write_text("x")

    symlink = media / "link.mkv"
    try:
        symlink.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this platform")

    with pytest.raises(PathNotAllowed):
        validate_source_path(str(symlink), [str(media)])


def test_accepts_a_symlink_pointing_inside_the_allowed_root(tmp_path):
    """Symlink inside root pointing to file inside must be accepted and resolved."""
    media = tmp_path / "media1"
    media.mkdir()
    target = media / "target.mkv"
    target.write_text("x")

    symlink = media / "link.mkv"
    try:
        symlink.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this platform")

    result = validate_source_path(str(symlink), [str(media)])
    # Should return the resolved path, not the symlink path
    assert result == os.path.realpath(str(target))


def test_uses_config_allowed_roots_by_default(tmp_path):
    """validate_source_path with no roots argument uses config.ALLOWED_ROOTS."""
    media = tmp_path / "media1"
    media.mkdir()
    movie = media / "movie.mkv"
    movie.write_text("x")

    with patch.object(config, "ALLOWED_ROOTS", [str(media)]):
        result = validate_source_path(str(movie))
        assert result == os.path.realpath(str(movie))


def test_raises_path_not_allowed_when_config_roots_empty(tmp_path):
    """validate_source_path raises PathNotAllowed when config.ALLOWED_ROOTS is empty."""
    movie = tmp_path / "movie.mkv"
    movie.write_text("x")

    with patch.object(config, "ALLOWED_ROOTS", []):
        with pytest.raises(PathNotAllowed):
            validate_source_path(str(movie))
