# 0019: Spoken replies, made on this computer

Date: 2026-11-08. Status: accepted.

## Context

A request spoken to Arcee was answered on screen only: the user who asked by voice had
to look for the answer, and a question back (allow or deny?) came without a word. Speech
output (CHAT-006 in the task list) waited on a voice: decision 0015 found that
sherpa-onnx 2.0.1 has no frontend for Piper's voices (they need espeak-ng), and no
older release supports Python 3.14. speech-dispatcher's espeak-ng is installed, but
sounds like a machine from 2005.

sherpa-onnx 2.0.1 does run Supertonic (Supertone, model under OpenRAIL-M), which reads
plain text through a unicode indexer: no phonemizer. Measured here (int8, 2 threads):
loaded in 0.9 s, a 2.8 s sentence made in 0.4 s and an 11.5 s answer in 1.1 s (about
ten times faster than it plays), about 250 MB held while loaded, ten voices. Kitten nano
(29 MB) could not be tried: GitHub's release downloads failed that day.

## Decision

- Answers to spoken requests are said aloud, with Supertonic, when the user turns on
  "Answer aloud" (Settings → Voice, off by default). Turning it on downloads the voice
  (92 MB, asked first). Typed requests are never answered aloud.
- What is said is `speakable(reply)`: no Markdown, code or addresses ("the link on
  screen"), list items as sentences, units and clock times in words ("18 °C" was said
  "18 to see", "13:00" "1300", "10:30 AM" "30 AM"), at most 600 characters, cut at a
  sentence, then "The rest is on screen." A failed request says only that it failed.
- A spoken request's questions are said too, with how to answer ("Say allow, always
  allow, or deny."), and the mic opens for the answer only once the question has been
  said: it would otherwise hear the question.
- It is made a sentence at a time, each while the one before plays through `pw-play`
  (media role Communication). The model is loaded while the request is thought about
  and let go after 5 minutes unused: the first words come about 0.4 s after the answer.
- The wake word does not listen while Arcee talks (it would hear Arcee's own voice).
  Talking stops at once for Esc or Stop, closing the pill, a new request, or the mic
  button.
- Settings → Voice offers the ten voices, each heard with a play button (arceed says it,
  so the app does not load the model too).

## Consequences

- Spoken answers on this computer, nothing sent anywhere to be said; 92 MB on disk and
  about 250 MB of memory in arceed while it talks, given back after 5 minutes.
- "Arcee stop" cannot interrupt an answer: without echo cancellation the wake word would
  hear the answer itself. Closing the pill or Esc stops it.
- English only (this Supertonic release); the text normalisation is English too.
