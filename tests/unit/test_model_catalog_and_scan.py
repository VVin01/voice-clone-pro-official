from pathlib import Path
import pytest

from app.models.enums import AccountCategory, VoiceModelStatus, VoiceModelType
from app.models.schema import VoiceModel
from app.services.ledger import get_minute_balance, get_or_create_user


def test_find_model_sample_audio_discovery(tmp_path: Path):
    from app.services.models import find_model_sample_audio

    # Directory with sample.mp3
    d1 = tmp_path / "model1"
    d1.mkdir()
    (d1 / "sample.mp3").write_bytes(b"sample1")
    assert find_model_sample_audio(d1) == d1 / "sample.mp3"

    # Directory with preview.wav
    d2 = tmp_path / "model2"
    d2.mkdir()
    (d2 / "preview.wav").write_bytes(b"preview2")
    assert find_model_sample_audio(d2) == d2 / "preview.wav"

    # Directory with custom_sample.ogg
    d3 = tmp_path / "model3"
    d3.mkdir()
    (d3 / "my_voice_sample.ogg").write_bytes(b"sample3")
    assert find_model_sample_audio(d3) == d3 / "my_voice_sample.ogg"

    # Directory with no audio
    d4 = tmp_path / "model4"
    d4.mkdir()
    (d4 / "model.pth").write_bytes(b"pth")
    assert find_model_sample_audio(d4) is None


# Bot-chat /samples catalog playback and CloneFSM audio input handlers were retired in Round 13 Part K
# (samples & custom voice upload now live exclusively in the Mini App).



