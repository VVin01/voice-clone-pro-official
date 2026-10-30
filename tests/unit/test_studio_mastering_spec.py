"""Studio mastering spec unification (2026-10-30).

The komin/mahnin pipeline (deesser -> highpass 70 -> body EQ ->
presence EQ -> 7200Hz cut -> loudnorm, pitch -3/+2Hz, formant
0.96/1.025) is now the persona mastering for the other 10 personas.
Flagships keep their owner-approved parameter sets exactly.
"""
from app.services import curated_voices as cv

NON_FLAGSHIP_MALE = ["kothura", "koaung", "kozayar", "komyat", "kophone"]
NON_FLAGSHIP_FEMALE = ["manadi", "mamay", "mayoon", "machaw", "mathiri"]


def test_flagships_keep_authored_processing():
    assert cv.persona_mastering_filter("komin") is None
    assert cv.persona_mastering_filter("mahnin") is None
    # komyat resolves to komin, so it keeps flagship handling too.
    assert cv.persona_mastering_filter("komyat") is None
    assert cv.persona_mastering_filter("unknown-persona") is None
    komin = cv.CURATED_VOICES["komin"]
    mahnin = cv.CURATED_VOICES["mahnin"]
    assert "afftdn" in komin["af_filter"] and "afftdn" in mahnin["af_filter"]
    assert "highpass=f=75" in mahnin["af_filter"]


def test_non_flagship_params_match_spec():
    for pid in NON_FLAGSHIP_MALE:
        info = cv.CURATED_VOICES[pid]
        assert info["pitch"] == "-3Hz", pid
        assert info["formant_ratio"] == 0.96, pid
    for pid in NON_FLAGSHIP_FEMALE:
        info = cv.CURATED_VOICES[pid]
        assert info["pitch"] == "+2Hz", pid
        assert info["formant_ratio"] == 1.025, pid


def test_non_flagship_chain_is_the_spec_chain():
    for pid in NON_FLAGSHIP_MALE + NON_FLAGSHIP_FEMALE:
        info = cv.CURATED_VOICES[pid]
        chain = cv.build_studio_mastering_filter(info["gender"], info["formant_ratio"])
        # The Edge fallback path (dict af_filter) and the shared builder
        # must never drift apart.
        assert info["af_filter"] == chain, pid
        assert "deesser=i=0.5:m=0.5:f=0.5:s=o" in chain
        assert "highpass=f=70" in chain
        assert "equalizer=f=7200:width_type=q:w=2:g=-3.5" in chain
        assert "loudnorm=I=-15:TP=-1.0:LRA=7" in chain
        # DSP order: deesser before highpass before loudnorm.
        assert chain.index("deesser") < chain.index("highpass") < chain.index("loudnorm")
    male = cv.build_studio_mastering_filter("male", 0.96)
    assert male.startswith("asetrate=42336,aresample=44100,atempo=1.0417,")
    assert "equalizer=f=130:width_type=o:w=1:g=3.0" in male
    assert "equalizer=f=3200:width_type=o:w=1:g=1.5" in male
    female = cv.build_studio_mastering_filter("female", 1.025)
    assert female.startswith("asetrate=45203,aresample=44100,atempo=0.9756,")
    assert "equalizer=f=240:width_type=o:w=1:g=1.8" in female
    assert "equalizer=f=3500:width_type=o:w=1:g=1.5" in female


def test_machaw_follows_mamay_mastering():
    assert cv.persona_mastering_filter("machaw") == cv.persona_mastering_filter("mamay")


def test_spec_prompts_are_the_flagship_prompts():
    assert cv.STUDIO_SPEC_MALE_PROMPT == cv.CURATED_VOICES["komin"]["control_instruction"]
    assert cv.STUDIO_SPEC_FEMALE_SWEET_PROMPT == cv.CURATED_VOICES["mahnin"]["control_instruction"]
