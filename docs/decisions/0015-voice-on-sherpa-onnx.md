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

## Amended 2026-11-04: five samples, and learning from use

At the user's word: training asks for "hey Arcee" five times (three were one sitting's
voice, at one volume), and a wake the user goes on to use (the request after it has
words, and is not "stop") teaches the wake word that "hey Arcee" too: the stretch the
match found, kept as a sample beside the trained ones, the newest three; training
again or forgetting clears them. A false wake followed by nothing is not learned.
Matching is at 40 ms (frames pooled in pairs) with a tighter loop, checked every
240 ms: three samples had cost 57% of a core while someone spoke (91 ms a check, the
first measurement on the user's own samples); eight plus two "Arcee stop" now cost
12%. Distances are not those of 20 ms: a wake word trained before needs training again.

## Amended 2026-11-05: one training for every room

At the user's word: the wake word must be heard in a quiet room, by a fan and in a
crowd without training again for each. Measured on the user's recorded "hey Arcee"s,
mixed with hiss, fan-like noise and babble (four voices at once) at 15, 10 and 5 dB of
voice over noise, against another speaker's sentences in the same noise:

| what is matched | own, quiet | own, hiss/fan 5 dB | own, babble 15 dB | other speaker, nearest |
|---|---|---|---|---|
| log-mel, as before | 0.16 | 0.42-0.44 | 0.40 | 0.25-0.37 |
| floored only | 0.05 | 0.25-0.28 | 0.12 | 0.16-0.27 |
| denoised and floored | 0.04 | 0.05-0.07 | 0.04 | 0.14-0.20 |

(Training sets the threshold between the samples and the ordinary sentence.) As before, any noise moved the user's own "hey Arcee"
further from their samples than another speaker's sentences were: noise woke nothing,
and the user could not wake it either. Now what is matched is first denoised by GTCRN
(sherpa-onnx's online speech denoiser, a 0.5 MB model downloaded with the others),
and every band is then held at a floor 17 dB under the loudest (`wake_match.FLOOR`),
so whatever noise is left looks the same in every room. The samples are denoised the
same way when trained. Babble at 10 dB and below (a crowd as loud as the user) still
fails; speaking up, or nearer the mic, brings it back to 15 dB.

Free warping was replaced too: the match may run between half and twice the speed of
the samples (steps 1-1, 2-1 and 1-2), not fold the whole word onto a moment of
someone's speech.

Cost: denoising runs only while the VAD hears speech, as matching does: 12% of a core
then, so a stretch of speech now costs about 30% of one core (features, denoising,
matching), and a quiet room is unchanged (0.3%). In a crowd, where someone is always
speaking, that 30% is steady. Samples trained before are not loaded (their sound was
not denoised: `wake_match.FEATURES`); the app asks for training again.

## Amended 2026-11-06: learning only from sure wakes

Learning from use poisoned the samples. While the user dictated a message, a stretch
of their sentence came under the threshold (0.217, loosened by one sample unlike the
rest; ordinary speech had come to 0.22-0.29 all evening). The words that followed
counted as a meant request, so that stretch of ordinary speech was kept as a sample,
and a second one seconds later; ordinary speech then woke Arcee six times in 50 s.
Words after a wake do not show it was meant. A wake is now learned only when its
"hey Arcee" is as close to the trained samples as they are to each other
(`wake_match.sure`), never when only a learned sample came near, so a learned sample
cannot pull the wake word toward ordinary speech. (A first rule, within three quarters
of the threshold, still let a stretch of "With the agent." in at 0.120, the samples
being 0.054-0.071 apart.) Learned samples that fail the rule are dropped when the
wake word loads. Each wake is logged with its distance, beside the request it led to
in the history, so a false wake can be traced.

Every false wake that day came in the middle of the user talking: dictating, or to
someone in the room. "Hey Arcee" starts a stretch of speech, so the trained wake word
is matched only in the first 3.5 s of speech after a pause the VAD hears (0.7 s), and
again after the next; long talk costs no matching or denoising. A "hey Arcee" run on
from a sentence without a pause is not heard; that is the price.

## Amended 2026-11-08: three samples, a stricter threshold, no learning

False wakes kept coming. With every wake now logged with its distance and the request
it led to, the user's real wakes came at 0.084-0.108 and a stretch of their talk ("I
don't think I have enough time to sit for a while…") at 0.098, under a threshold of
0.116: real and false overlap, and no threshold parts them cleanly. At the user's word:

- Learning from wakes is off by default (Settings → Voice, "Learn from your wakes"): a wake's words did not show it
  was meant, and learned samples pulled the wake word toward ordinary speech. Learned
  samples are neither kept nor used while it is off; those already saved stay in the
  file, and the switch (`learn_wakes`, read by arceed as it learns) brings them back,
  still only the `sure` ones. Switching it reloads arceed's wake word at once.
- Training asks for three samples again: five gave more chances for one unlike the
  rest, which loosened the threshold.
- The threshold sits a quarter of the way from the samples' spread to the ordinary
  sentence (`CALIBRATION`, was 0.4). For the user's samples that is 0.095, under the
  false wake at 0.098; some real wakes near the top (0.106, 0.108) will be missed and
  said again. Their trained file was moved to it at once (0.116 to 0.095, "Arcee stop"
  0.179 to 0.141).
