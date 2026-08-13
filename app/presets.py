"""Pure parsing of HandBrake preset documents.

A preset file is a tree: folder nodes carry ``"Folder": true`` and hold
children under ``ChildrenArray``; only leaves are usable presets. Nothing
here touches the filesystem or a subprocess, so it is fully testable
without HandBrake installed.
"""

from typing import Any

# HandBrake muxer name -> the file extension the output must carry.
_FORMAT_TO_EXTENSION = {
    "av_mkv": "mkv",
    "av_mp4": "mp4",
    "av_webm": "webm",
}


class PresetError(ValueError):
    """Raised when a preset document is malformed or a preset is unusable."""


def iter_presets(doc: dict) -> list[dict]:
    """Every leaf preset in *doc*, depth-first, in document order."""
    found: list[dict] = []

    def walk(nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("Folder"):
                walk(node.get("ChildrenArray"))
            else:
                found.append(node)

    walk(doc.get("PresetList"))
    return found


def find_preset(doc: dict, name: str) -> dict:
    """The leaf preset called *name*.

    Folder names are deliberately not matched: a folder carries no encoder
    settings, so selecting one could never produce an encode.
    """
    for preset in iter_presets(doc):
        if preset.get("PresetName") == name:
            return preset
    raise PresetError(f"Preset not found in document: {name!r}")


def preset_encoder(preset: dict) -> str:
    """The encoder a preset asks for, e.g. ``nvenc_h264``."""
    encoder = preset.get("VideoEncoder")
    if not encoder or not isinstance(encoder, str):
        raise PresetError(
            f"Preset {preset.get('PresetName')!r} has no VideoEncoder field"
        )
    return encoder


def preset_extension(preset: dict) -> str:
    """The file extension the output must use, from the preset's muxer.

    Derived from the preset rather than accepted from the caller so the
    extension can never contradict the container the encode actually writes.
    """
    file_format = preset.get("FileFormat")
    extension = _FORMAT_TO_EXTENSION.get(str(file_format))
    if extension is None:
        raise PresetError(
            f"Preset {preset.get('PresetName')!r} has an unsupported or missing "
            f"FileFormat: {file_format!r}. Known: {sorted(_FORMAT_TO_EXTENSION)}"
        )
    return extension
