# Spec: Core Dubbing & Localization Pipeline

## User Story
As a content creator making children's animated content, I want to upload one video
and select multiple JAPAC target languages, so that I get back a dubbed video that
keeps each character's voice consistent, preserves song rhyme, and warns me about
any culturally inappropriate content for the target market — before publishing.

## Problem Statement
Existing dubbing tools on the market (ElevenLabs Dub, Deepdub, HeyGen...) do not
properly handle the specific needs of children's content: they don't keep character
voices consistent across multiple episodes, they translate songs literally and break
the rhyme, and they don't check cultural appropriateness for specific JAPAC target
markets. Teams doing translation/dubbing manually spend a lot of time and get
inconsistent results across episodes.

## Success Criteria
- [ ] Upload 1 video, select ≥1 target language, the system runs the entire pipeline
      automatically with no manual intervention required (except the final review step)
- [ ] The same character appearing across multiple dialogue lines gets dubbed with a
      consistent voice
- [ ] Dialogue lines marked `is_song=true` are transcreated to preserve rhyme instead of
      being translated literally
- [ ] Every dialogue line with potential cultural sensitivity is flagged along with a
      reason in natural language, shown in the Review Dashboard
- [ ] The content creator can edit the translated text and request a re-dub for just
      that one line, without re-running the entire pipeline
- [ ] The video is only published after the creator clicks approve

## Out of Scope (for the first version)
- Automatically detecting which lines are songs (marked manually while reviewing the transcript)
- Fully accurate automatic character recognition (the creator can manually correct the
  character mapping if Agent 2 misidentifies it)
- Handling videos longer than 30 minutes / multiple overlapping speakers at once

## Related Constraints (cross-check against constitution.md)
- **What role does Gemini play**: it is the core of all 5 main agents (transcription,
  voice profiling, translation/transcreation, cultural safety, dubbing via Native
  Audio) — without Gemini, the pipeline would not exist.
- **Human approval step**: required at the Review Dashboard before transitioning
  status to `published` (see `docs/agent-pipeline.md`).
