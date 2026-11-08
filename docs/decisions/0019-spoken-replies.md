# 0019: Spoken replies, made on this computer

Date: 2026-11-08. Status: accepted (amended the same day: Kitten, not Supertonic).

## Context

A request spoken to Arcee was answered on screen only: the user who asked by voice had
to look for the answer, and a question back (allow or deny?) came without a word. Speech
output (CHAT-006 in the task list) waited on a voice: decision 0015 found that
sherpa-onnx 2.0.1 has no frontend for Piper's voices, and no older release supports
Python 3.14. speech-dispatcher's espeak-ng is installed, but sounds like a machine from
2005.

Two voices run on sherpa-onnx 2.0.1, measured here:

| | Kitten TTS nano 0.8 (KittenML) | Supertonic (Supertone) |
| --- | --- | --- |
| Size | 57 MB (float32; int8 24 MB) | 92 MB |
| Licence | Apache-2.0 | OpenRAIL-M (use restrictions passed on) |
| Speed, CPU | 0.2 s a second of speech (int8: 0.5) | 0.1 s |
| Voices | eight, named (Bella, Jasper, ...) | ten, numbered |
| Text in | phonemes | plain text |

sherpa-onnx 2.0 no longer carries espeak-ng ("espeak-ng is no longer used. Use lexicon,
or ... tokens with an external phonemizer"), which is what kept Piper out. Ubuntu's own
libespeak-ng1 (speech-dispatcher's voice uses it, so every Ubuntu desktop has it) can be
that phonemizer, called through ctypes as KittenTTS's own `phonemizer` does.

Supertonic was built first, then replaced the same day: the voice is to ship inside the
.deb rather than be downloaded from somewhere else, which favours the smaller model under
the plainer licence; and its voices have names.

## Decision

- Arcee's voice is Kitten TTS nano 0.8, float32 (the int8 model was 2.5 times slower on
  this CPU; more threads did not help either), through sherpa-onnx's Kitten model. Text
  becomes phonemes by libespeak-ng (`arcee/phonemes.py`), punctuation kept between them
  so pauses stay ("Done. I set it" lost its pause without it); KittenTTS's symbol table
  ships as `arcee/data/kitten-tokens.txt`.
- KittenML publishes the model and its voices (`voices.npz`); sherpa-onnx wants the
  voices as one `voices.bin` and its own metadata in the model. `voice_reply.prepare`
  does both without the onnx package: the styles are concatenated in a fixed order, and
  the metadata is appended as protobuf fields (fields appended to a message merge into
  it), so the weights stay byte for byte as published.
- The .deb carries it (and the wake word's models) in /usr/share/arcee/models, where
  `model_path` looks first: `packaging/build-deb.sh` runs `python -m arcee.bundle`, which
  fetches each model once into build/bundle. The .deb depends on libespeak-ng1. Without
  the .deb, turning "Answer aloud" on downloads it from Hugging Face (60 MB) and prepares
  it the same way.
- Answers to spoken requests are said aloud when the user turns on "Answer aloud"
  (Settings → Voice, off by default). Typed requests are not answered aloud unless asked.
- "Read it out loud", "say that aloud", "read me the file": a `voice.say` tool, offered
  when a request asks to hear something, says the text it is given (up to 4,000
  characters), typed request or spoken; the reply is then not said over it. The Agent
  page has a Read aloud button under each reply (arceed's `Say`; `Hush` stops it).
- What is said is `speakable(reply)`: no Markdown, code or addresses ("the link on
  screen"), list items as sentences, units and clock times in words ("18 °C" was said
  "18 to see", "13:00" "1300", "10:30 AM" "30 AM"), at most 600 characters for a reply,
  cut at a sentence, then "The rest is on screen." A failed request says only that it
  failed.
- A spoken request's questions are said too, with how to answer ("Say allow, always
  allow, or deny."), and the mic opens for the answer only once the question has been
  said: it would otherwise hear the question.
- It is made a piece at a time, each while the one before plays through `pw-play`
  (media role Communication); a long first sentence is cut at its first comma, so the
  first words come sooner. The model is loaded while the request is thought about and
  let go after 5 minutes unused.
- The wake word does not listen while Arcee talks (it would hear Arcee's own voice).
  Talking stops at once for Esc or Stop, closing the pill, a new request, or the mic
  button.
- Settings → Voice shows the eight voices as cards: each named as KittenML names it, with
  how it sounds, measured on one sentence (median pitch and its spread, words a minute):
  Bruno the only deep voice (121 Hz); Jasper, Hugo and Leo lighter men's voices; Kiki the
  highest (255 Hz); Leo and Bella slow (110 and 125 words a minute), Hugo and Kiki quick.
  Choosing one says a sentence in it. Luna (natural pace) is the default.

## Consequences

- Spoken answers on this computer, nothing sent anywhere to be said. The .deb grows by
  64 MB (the voice, 58, and the wake word's models, 6), and an installed Arcee downloads
  nothing to listen or to speak.
- About 0.2 s of work for each second of speech: the first words of a reply come about
  0.7 s after it; the rest keeps ahead of playback.
- "Arcee stop" cannot interrupt an answer: without echo cancellation the wake word would
  hear the answer itself. Closing the pill, Esc, or the Read aloud button stops it.
- English only; the text normalisation is English too.
