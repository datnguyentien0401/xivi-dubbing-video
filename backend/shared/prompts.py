"""All prompts for calling Gemini live here — never hardcoded in an agent (see
AGENTS.md principle #3 and the gemini-pipeline-agent skill).

Every prompt has gone through the `gemini-prompt-reviewer` subagent before
being considered finished — this is the part that decides the "Meaningful
use of Gen AI" score (40%) of the competition.
"""

TRANSCRIPTION_PROMPT = """\
You are processing the audio of a children's animated video.
Extract all dialogue lines with timestamps (seconds, as a real number) and
distinguish how many different speakers there are (assign a speaker_tag like
"speaker_1", "speaker_2", ...).

IMPORTANT about speaker_tag: assign it by VOICE IDENTITY, not by
sentence/dialogue line. One person can speak MANY consecutive lines, even
with pauses between them (to breathe, or a mid-sentence break) — a pause, no
matter how short or long, is NOT by itself evidence of a speaker change; only
use it together with the other signals below.

To decide whether to create a NEW speaker_tag, weigh BOTH of the following
signal types (not just one):
1. Audio: has the pitch, timbre, pace, or speaking style changed — including
   SMALL but CONSISTENT differences (note: children of the same age/gender
   can sound very similar; don't only look for obvious differences and miss
   subtle but real ones).
2. Conversational content/context: a question followed by an answer, a
   pronoun-of-address switch ("I"/"you" flipping), addressing a different
   character by name, or a back-and-forth conversational structure (A speaks
   — B replies — A speaks again) — this is a STRONG signal of a speaker
   change even with a very short pause and similar-sounding voices.

Example to distinguish two easily-confused cases:
- "Mom... (0.4s pause) ...I'm hungry" → one person, KEEP the same speaker_tag
  (just a mid-sentence breath pause, no voice change, no change of address).
- "Did you take my toy?" (0.3s pause) "No, I didn't take it!" → two speakers,
  MUST split into separate speaker_tags (question — answer, a change of
  address/pronouns, even though the two kids' voices may sound similar).

The same speaker at different points in the video (even far apart) MUST
always get the same speaker_tag.

Return a JSON list of objects: {start_time, end_time, speaker_tag, text}.
Example of one correctly-formatted object: {"start_time": 2.1, "end_time": 3.45,
"speaker_tag": "speaker_1", "text": "..."}.
start_time and end_time are ALWAYS a JSON number (NOT a string), in seconds,
even for videos longer than a few minutes (e.g. 5 minutes 30 seconds is
written as 330.0, not as a minute:second string).
Do not translate — transcribe verbatim in the original language.
"""

# Appended to TRANSCRIPTION_PROMPT at call time, ONLY when the caller
# successfully probed the local video file's real duration first (see
# gemini_client.py::transcribe_audio) — kept as a SEPARATE constant (not
# baked into TRANSCRIPTION_PROMPT itself as a required placeholder) so it can
# be cleanly omitted, rather than awkwardly filled with a placeholder string,
# on the rare call where probing the file fails.
#
# Real bug this guards against (found via feedback, confirmed on real job
# data): Gemini assigned a dialogue line an end_time of 102.5s for a video
# that was actually only 65.8s long — the line's SPEECH was only ~4.6s, but
# Gemini apparently lost track of elapsed time and kept the slot going far
# past where the media itself had already ended. This is a prompt-level
# mitigation, not a guaranteed fix — grounding the model with the actual
# known duration reduces (but, being a model behavior, can't 100% eliminate)
# this class of timestamp hallucination; see
# ingestion_agent.py::_drop_lines_past_video_duration for the deterministic
# code-level backstop that catches it even if the model ignores this.
TRANSCRIPTION_DURATION_CONSTRAINT = """
IMPORTANT: this video/audio's REAL, MEASURED total length is
{duration_seconds} seconds — treat this as ground truth, more reliable than
your own sense of when the video ends. start_time and end_time must NEVER
exceed {duration_seconds}, under any circumstances, even if the speech
perceptually sounds like it's still continuing at that point (that
perception is your own estimate, and here it is wrong — the file itself does
not contain any audio past {duration_seconds}).

If a line's speech is still audible exactly when the recording reaches
{duration_seconds}, set that line's end_time to {duration_seconds} and KEEP
the line — do not extend end_time past it, and do NOT drop the line entirely
just because it reaches this boundary.
"""

VOICE_SELECTION_PROMPT = """\
This is a real audio sample of one character's voice from a children's
animated video. Listen carefully, then choose ONE available voice (from the
list below) that is the CLOSEST MATCH to this sample — considering gender,
pitch, and speaking style — to use for dubbing this character consistently
throughout the video. Do not choose randomly.

List of available voices (name — official style description):
{voice_options}

Return JSON with 3 fields:
{{"voice_name": "<exactly one name from the list above, spelled exactly>",
"style_description": "<ONLY 2-4 style KEYWORDS, comma-separated — e.g.
'cheerful, gentle' or 'deep, serious'. ABSOLUTELY DO NOT write a full
sentence, NO verbs like 'chose'/'because'/'fits', NO mentioning the voice
name or the audio sample — this field gets inserted DIRECTLY into another
dialogue-reading instruction (in the form 'Speak in a {{style_description}}
voice: ...'); if it's long or a full sentence, the dubbing voice will
mistakenly read this part aloud too>",
"reasoning": "<a FULL explanation, in Vietnamese, of why this voice was
chosen — used ONLY for display to the human reviewer, NOT used directly in
the dialogue-reading step>"}}.
"""

VOICE_DESCRIPTION_PROMPT = """\
This is a real audio sample of one character's voice from a children's
animated video.
Listen very carefully, then give a DETAILED, HONEST description of this
voice's characteristics — the more accurate the description, the closer the
later synthesized voice will match the original. The description MUST
cover:
- Pitch (high/medium/low) and timbre (raspy, clear, warm, sharp, melodious,
  bright, sweet...)
- Speaking pace and rhythm/pausing style
- Dominant style/emotion conveyed by the voice (cheerful, witty, serious,
  gentle, excited, mischievous...)
- Perceived gender and age range (if clear) — describe EXACTLY AS HEARD, do
  not deliberately avoid or soften this if it's a child's or teenager's
  voice. Whether the voice-synthesis tool (a later processing step) accepts
  or rejects this description is that system's decision at that step, NOT
  this description step's concern.

IMPORTANT: write the description in ENGLISH (even if the source audio is in
another language) — this description will be used directly as input to
Gemini's voice-synthesis tool (Voice Design), which is designed around
English-language examples (e.g. "A bright, energetic voice with a sweet,
clear timbre and playful cadence."). Write 2-4 flowing sentences, no bullet
points.

Return JSON: {{"description": "<a detailed, honest description in English,
2-4 sentences>"}}.
"""

VOICE_PROFILING_PROMPT = """\
These are audio samples of the same speaker_tag from different episodes of
a children's animated series. Assess the voice characteristics (relative
pitch, speaking pace, style - cheerful/calm/humorous) and state whether
these samples are likely to be the SAME character, along with a confidence
score (0-1) and a brief explanation.
"""

DIALOGUE_TRANSLATION_PROMPT = """\
Translate the following dialogue line from {source_lang} to {target_lang},
for a children's animated video (target audience roughly 4-8 years old).
Keep the style simple, cheerful, and age-appropriate. Do not translate
mechanically — prefer wording that sounds natural, the way a native
child-speaker would talk.

CONTEXT: the original dialogue was auto-segmented by timestamp, so the line
to translate may only be a PART of a complete sentence (cut mid-sentence due
to a speaking pause) — it is not always a complete, self-contained sentence.
Below are the adjacent lines (context); the line marked ">>>" (ONLY a marker
added by the system, NOT real content) is the line to translate. THE
ADJACENT LINES MAY BE ANOTHER SPEAKER'S TURN (clearly noted if so) — only
use them to understand meaning (pronouns, a subject split across lines)
when they are TRULY the continuation of the SAME sentence, by the SAME
speaker, as the >>> line. If an adjacent line is noted as another speaker's
turn or as song lyrics, ABSOLUTELY DO NOT blend its meaning into the line
being translated:
{context}

IMPORTANT about length: {length_guidance} The closer the translated line is
to the original text's length, the easier it is for the dubbed voice to
read it at a natural pace matching the original duration, instead of an
artificially sped-up/slowed-down delivery to force it to fit afterward.

DO NOT trade off the following for brevity: do not drop numbers, proper
names, or lesson keywords, and do not make the sentence sound stilted or
unnatural for children. If you cannot both match the length AND keep all of
the above, prioritize keeping the full meaning and accept a length
mismatch — the system still has an audio speed-adjustment step downstream
to handle that case.

Line to translate (verbatim, marked ">>>" in the context above): {text}

Return EXACTLY ONE SINGLE LINE that is the translation of that line. Do NOT
include the ">>>" marker, do NOT wrap it in quotes, do NOT add a
label/prefix, do NOT translate or repeat the other context lines, do NOT
add any further explanation.
"""

SONG_TRANSCREATION_PROMPT = """\
These are the lyrics of a song/rhyme in a children's animated video,
original language {source_lang}, to be adapted into {target_lang} for
children in the target market.

IMPORTANT: this is NOT a literal translation. REWRITE it (transcreation) so
that:
1. It keeps the original song's main idea / educational purpose (e.g. if
   it's a counting or alphabet lesson, the new version must still serve
   that same purpose)
2. It keeps a natural rhyme and rhythm when sung in {target_lang}
3. The syllable count per line stays close to the original to match the
   melody (small deviations are acceptable)

Original lyrics:
{text}

Return the adapted lyrics in {target_lang}, keeping the same number of
lines as the original.
"""

CULTURAL_SAFETY_PROMPT = """\
You are moderating the content of a CHILDREN'S animated video to be
released in the {target_market} market. Read the following translated
dialogue line and assess: is there anything culturally, religiously, or
socially inappropriate for children in {target_market}? Consider the
following known rules for this market:

{market_rules}

Dialogue line: "{text}"

Return JSON: {{"safety_status": "ok" | "flagged", "reasoning": "<a brief
explanation, in Vietnamese, easy to understand for a content creator who is
not a specialist in that local culture>"}}. If there is no issue,
safety_status = "ok" and reasoning briefly explains why it's safe.
"""

# The TTS model (gemini-3.1-flash-tts-preview, see gemini_client.py) treats
# the ENTIRE content sent to it as a transcript to read verbatim — it does
# not distinguish an "instruction sentence" from a "sentence to read" the
# way a regular text-generation model does. The old version packed several
# long instruction clauses (target audience, language, target duration)
# before the dialogue line, which sometimes made the model READ THE
# INSTRUCTION SENTENCE ALOUD TOO instead of only reading the dialogue line
# (real feedback received: "the dubbed audio also contains the prompt").
# Trimmed down to the minimal pattern the official docs recommend for a TTS
# model that doesn't support structured speech_metadata yet (e.g. "Say
# cheerfully: ..." — https://ai.google.dev/gemini-api/docs/speech-generation;
# the model that supports speech_metadata is gemini-3.8-flash-tts/-lite, NOT
# the model currently in use — switching to that model previously hit a 100
# requests/day quota, see the _AUDIO_MODEL comment in gemini_client.py, so
# DO NOT change the model here). Dropped the "match the original N-second
# duration" clause entirely — the official docs confirm Gemini TTS has no
# parameter that directly controls duration in seconds; asking the model to
# do something it can't only makes it generate extra filler content to
# "interpret" that request. Duration is correctly handled downstream in the
# time-stretch step instead (see mux_service.py:_encode_audio_track).
DUBBING_AUDIO_INSTRUCTION = """\
Speak in a {voice_style} voice: {text}
"""

EDUCATIONAL_CONTENT_PROMPT = """\
From the following complete dialogue, already translated into
{target_lang}, of one children's animated episode, choose 8-12 vocabulary
words suitable for children to learn (prefer words that repeat or are
important to the episode's content), each with a simple meaning and one
easy-to-understand example sentence. Then write 3-5 simple review questions
(multiple-choice or fill-in-the-blank) based on the episode's content.

Dialogue: {full_transcript}

Return JSON: {{"vocabulary": [{{"term", "meaning", "example_sentence"}}, ...],
"quiz": [{{"question", "answer"}}, ...]}}
"""
