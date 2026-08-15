"""Pydantic request models.

Note what is absent: no output path, and no HandBrake arguments. The preset
document is the only encode input the network can supply.
"""

from pydantic import BaseModel


class EncodeRequest(BaseModel):
    """Encode *source_path* using a named preset from *preset_json*."""

    source_path: str
    preset_json: dict
    preset_name: str
