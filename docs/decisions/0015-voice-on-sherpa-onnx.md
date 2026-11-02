# 0015. Local voice on sherpa-onnx, with models downloaded on request

Date: 2026-11-03
Status: accepted

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

1. Voice runs on sherpa-onnx, a dependency of the package (43 MB).
2. The models are not in the package: the app downloads them from sherpa-onnx's
   releases on GitHub after the user agrees, showing their size, into
   `~/.local/share/arcee/models/`, keeping only the files used. The keyword spotter
   and voice activity detector are small; the speech-to-text model is chosen by
   measured accuracy and speed on this machine's CPU (the GPU is Ollama's).
3. Listening for the wake word is off until the user turns it on. When on, arceed
   reads the microphone through `pw-record`, as the mic button does, and nothing but
   the keyword spotter sees the audio until it hears "hey Arcee"; then the voice
   activity detector cuts the request out and the local model transcribes it.
   "Arcee stop" cancels whatever is running. Audio never leaves the machine and is
   not stored.
4. Groq's Whisper stays as the speech-to-text the user may choose instead (more
   accurate, not local), and as the fallback until the local models are downloaded.
5. Spoken replies (Piper voice through sherpa-onnx) are a setting, off by default.

## Consequences

- Fully local voice for 43 MB in the package plus a few hundred MB of models,
  downloaded only by people who use voice.
- No custom wake-word training: phrases can be changed in settings, at some cost in
  false accepts compared with a trained model. The threshold is tunable.
- sherpa-onnx is a C++ library behind Python bindings: a crash there takes arceed
  down, so the listener runs where a failure restarts cleanly (systemd restarts
  arceed) and the mic button keeps working without it.
