import pytest

from app.presets import (
    PresetError,
    find_preset,
    iter_presets,
    preset_encoder,
    preset_extension,
    preset_video_preset,
)

DOC = {
    "PresetList": [
        {
            "Folder": True,
            "PresetName": "Custom Presets",
            "ChildrenArray": [
                {
                    "PresetName": "HQ 2160p30 Surround (h.264 - QSV)",
                    "VideoEncoder": "qsv_h264",
                    "FileFormat": "av_mkv",
                },
                {
                    "PresetName": "HQ 2160p30 Surround (h.264 - NVENC)",
                    "VideoEncoder": "nvenc_h264",
                    "FileFormat": "av_mkv",
                },
            ],
        },
        {
            "PresetName": "Top Level MP4",
            "VideoEncoder": "x265_10bit",
            "FileFormat": "av_mp4",
        },
    ]
}


def test_iter_presets_returns_only_leaves():
    names = [p["PresetName"] for p in iter_presets(DOC)]
    assert names == [
        "HQ 2160p30 Surround (h.264 - QSV)",
        "HQ 2160p30 Surround (h.264 - NVENC)",
        "Top Level MP4",
    ]


def test_iter_presets_excludes_the_folder_node():
    assert all(not p.get("Folder") for p in iter_presets(DOC))


def test_find_preset_returns_the_named_leaf():
    found = find_preset(DOC, "HQ 2160p30 Surround (h.264 - NVENC)")
    assert found["VideoEncoder"] == "nvenc_h264"


def test_find_preset_raises_for_an_unknown_name():
    with pytest.raises(PresetError, match="not found"):
        find_preset(DOC, "No Such Preset")


def test_find_preset_raises_for_a_folder_name():
    with pytest.raises(PresetError, match="not found"):
        find_preset(DOC, "Custom Presets")


def test_preset_encoder_reads_video_encoder():
    assert preset_encoder(find_preset(DOC, "Top Level MP4")) == "x265_10bit"


def test_preset_encoder_raises_when_absent():
    with pytest.raises(PresetError, match="VideoEncoder"):
        preset_encoder({"PresetName": "x"})


@pytest.mark.parametrize(
    "file_format,expected",
    [("av_mkv", "mkv"), ("av_mp4", "mp4"), ("av_webm", "webm")],
)
def test_preset_extension_maps_the_muxer(file_format, expected):
    assert preset_extension({"FileFormat": file_format}) == expected


def test_preset_extension_raises_for_an_unknown_format():
    with pytest.raises(PresetError, match="FileFormat"):
        preset_extension({"FileFormat": "av_wat"})


def test_preset_extension_raises_when_absent():
    with pytest.raises(PresetError, match="FileFormat"):
        preset_extension({"PresetName": "x"})


def test_iter_presets_of_an_empty_document_is_empty():
    assert iter_presets({}) == []


def test_video_preset_returns_the_speed_preset():
    assert preset_video_preset({"VideoPreset": "veryfast"}) == "veryfast"


def test_video_preset_is_empty_when_absent():
    """Absence is legal: HandBrake picks its own default, so it must not be
    treated as a validation failure."""
    assert preset_video_preset({"PresetName": "x"}) == ""


def test_video_preset_is_empty_when_blank_or_wrong_type():
    assert preset_video_preset({"VideoPreset": ""}) == ""
    assert preset_video_preset({"VideoPreset": "   "}) == ""
    assert preset_video_preset({"VideoPreset": None}) == ""
    assert preset_video_preset({"VideoPreset": 7}) == ""


def test_video_preset_is_stripped():
    assert preset_video_preset({"VideoPreset": " balanced "}) == "balanced"
