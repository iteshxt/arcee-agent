# 0015. Voice in two tiers: the wake word here (sherpa-onnx), the request on Groq

Date: 2026-11-03
Status: accepted; amended 2026-11-03 (two tiers, no local speech-to-text; training)

## Context

Phase 5's voice (spec section 14): a wake word ("hey Arcee") heard at near-zero cost,
local speech-to-text, a voice stop ("Arcee stop"), and spoken replies. The gate: under
1.5 s from the end of speech to the transcript.

Today the mic button records with `pw-record` and sends the audio to Groq's
`whisper-large-v3-turbo`: no wake word, and audio leaves the machine.

The spec names faster-whisper, openWakeWord and Piper. All have Python 3.14 wheels,
but installed they take 427 MB, 277 MB and 166 MB (CTranslate2, PyAV, scikit-learn,
onnxruntime, numpy), against an 11 MB .deb. openWakeWord also needs a wake-word model
trained per phrase, offline, from thousands of synthetic clips.

sherpa-onnx (k2-fsa) is one 43 MB wheel that does keyword spotting, voice activity
detection, speech-to-text (Whisper, Moonshine, streaming Zipformer) and text-to-speech
(Piper voices), on onnxruntime it carries itself. Its keyword spotter takes the phrases
as text, so "hey Arcee" and "Arcee stop" need no training.

## Decision

Voice works the way phone assistants do, in two tiers (the user's call, 2026-11-03):

1. **Tier one, on this computer: the wake word.** sherpa-onnx (a dependency, 43 MB)
   runs a small keyword spotter over the microphone; it is all that hears the audio
   until "hey Arcee". Silero VAD then cuts out the request that follows (0.7 s of
   silence ends it). "Arcee stop" cancels whatever runs, with no transcription. The
   two models (7 MB) are downloaded from sherpa-onnx's releases on GitHub when the user
   turns the wake word on, after asking with their size. Listening is off until then.
2. **Tier two, on Groq: the request.** Only the audio after the wake word goes to Groq's
   `whisper-large-v3-turbo`, as the mic button's does. Nothing before it is stored or
   sent. A stalled request gives up after 8 s and is tried once more.

**Training, for voices the spotter misses** (amended, at the user's word). The
spotter is an English model and "Arcee" is not English: on the user's built-in mic,
which clips and hisses, it heard 2 of 4 "Hey Arcee"s with any spelling and at any
threshold, while Whisper heard all four. So the wake word can be trained to the
user's voice, as phone assistants set themselves up: three "Hey Arcee"s and one
ordinary sentence. What is kept is their sound (24 log-mel bands per 20 ms, rounded;
no audio, nothing that plays back), in the models folder. Speech wakes Arcee when a
stretch of it is close enough to one of the three by subsequence dynamic time
warping; "close enough" is set between how far apart the samples were and how close
the sentence came. It runs alongside the spotter, in plain Python (no numpy), only
while the VAD hears speech, over enough of it for the longest sample (a pause
between "hey" and "Arcee" included). Training asks for "Arcee stop" twice as well,
matched the same way and kept apart from the wake word; the spotter still hears it
too, and a woken request that is only "stop" (Whisper's "Arcee stopped.") stops what
runs instead of running. Training
is a page of first-run setup and can be done again from the Voice settings; arceed
holds the listener meanwhile (`HoldWake`) and takes up the result without
restarting.

A local speech-to-text model was built and measured (Moonshine base: 274 MB, about
0.4 s a request on the CPU) and dropped: Whisper on Groq is more accurate with names
and needs no download, and the wake word already keeps everything before it on the
machine.

Spoken replies are open: sherpa-onnx 2.0.1 has no frontend for Piper voices, and no
older release supports Python 3.14.

## Consequences

- Voice for 43 MB in the package and a 7 MB download; the requests themselves use
  the Groq speech allowance, and need the network.
- No model training: the spotter takes phrases as text, at some cost in false
  accepts compared with a trained model. Where it misses a voice, the user's own
  samples (templates, not a trained network) make up for it; they are as good as the
  samples, so a new mic or room may need training again. False accepts from the
  user's own speech are untested beyond the one sentence asked for.
- sherpa-onnx is a C++ library behind Python bindings: a crash there takes arceed
  down, so the listener runs where a failure restarts cleanly (systemd restarts
  arceed) and the mic button keeps working without it.
