"""Tests for schemas.py validators that aren't already exercised indirectly
through an endpoint test (specs/002-video-editor's Track/TimelineState
validators are covered via api/routers/test_jobs.py; the new
ScriptJobCreate validator below isn't wired to an endpoint yet — see
specs/003-script-first-ingestion/tasks.md — so it needs its own direct
coverage)."""

import pytest
from pydantic import ValidationError

from api.schemas.schemas import DialogueLineOut, ScriptJobCreate, VoiceAssignment


def _line(segment_code: str, character_name: str = "Cat", text: str = "Hello!") -> dict:
    return {"segment_code": segment_code, "character_name": character_name, "text": text}


def test_script_job_create_accepts_a_valid_script():
    payload = ScriptJobCreate(
        lines=[_line("001"), _line("002")],
        target_languages=["id"],
        character_voice_assignments={"Cat": {"voice_name": "Puck"}},
    )
    assert len(payload.lines) == 2


def test_script_job_create_rejects_duplicate_segment_code():
    """Regression test for FR-002 ("no duplicate segment identifiers") —
    mirrors the manual dubbing process's own "preserve 100% of rows, no
    merging" rule (docs/manual-dubbing-process.docx): a duplicate
    segment_code means 2 rows would collide, so it's rejected up front rather
    than silently letting one shadow the other downstream."""
    with pytest.raises(ValidationError, match="Duplicate segment_code"):
        ScriptJobCreate(
            lines=[_line("001"), _line("001")],
            target_languages=["id"],
            character_voice_assignments={"Cat": {"voice_name": "Puck"}},
        )


def test_script_job_create_rejects_character_with_no_voice_assignment():
    """Regression test for US2/FR-003 ("a job cannot proceed past this point
    with any character left unassigned") — a character appearing in the
    script but missing from character_voice_assignments must be rejected at
    the API boundary, not discovered later deep in the pipeline."""
    with pytest.raises(ValidationError, match="Missing voice assignment"):
        ScriptJobCreate(
            lines=[_line("001", character_name="Dog")],
            target_languages=["id"],
            character_voice_assignments={"Cat": {"voice_name": "Puck"}},
        )


def test_script_job_create_rejects_empty_lines():
    with pytest.raises(ValidationError):
        ScriptJobCreate(lines=[], target_languages=["id"], character_voice_assignments={})


def test_script_job_create_rejects_empty_text():
    with pytest.raises(ValidationError):
        ScriptJobCreate(
            lines=[_line("001", text="")],
            target_languages=["id"],
            character_voice_assignments={"Cat": {"voice_name": "Puck"}},
        )


@pytest.mark.parametrize("field", ["segment_code", "character_name", "text"])
def test_script_job_create_rejects_whitespace_only_fields(field):
    """Regression test (found via code-reviewer, review 2026-10-10):
    Field(min_length=1) alone accepts a whitespace-only string like " "
    (pydantic counts raw characters, it does not strip first) — a script row
    with e.g. text="   " would otherwise silently become a dialogue line with
    blank-but-"present" dialogue text instead of being rejected."""
    line = _line("001")
    line[field] = "   "
    with pytest.raises(ValidationError, match="blank"):
        ScriptJobCreate(
            lines=[line],
            target_languages=["id"],
            character_voice_assignments={"Cat": {"voice_name": "Puck"}},
        )


def test_voice_assignment_rejects_whitespace_only_voice_name():
    with pytest.raises(ValidationError, match="blank"):
        VoiceAssignment(voice_name="   ")


def test_dialogue_line_out_segment_code_defaults_to_none():
    """A line produced by transcription (not a script import) has no
    segment_code at all — must default to None, not be a required field that
    would break deserializing every pre-existing DialogueLine row."""
    line = DialogueLineOut(
        id="11111111-1111-1111-1111-111111111111",
        character_id=None,
        start_time=0.0,
        end_time=1.0,
        original_text="hi",
        translated_text=None,
        is_song=False,
        safety_status="ok",
        safety_reasoning=None,
        dubbed_audio_gcs_uri=None,
    )
    assert line.segment_code is None
