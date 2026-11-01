# 0013. Finding things on screen with a vision model, by grid cells

Date: 2026-11-01
Status: accepted

## Context

The Core models read no images, so the screen reaches them as OCR text (decision 0009).
Icons, toggles and pictures have no text, so OCR cannot name them. qwen/qwen3.8-27b on
Groq reads images (checked: it read "Save" and "42" off a made-up picture), shares the
Groq budget, and Groq limits its input to 7,000 tokens a minute; a screenshot sent 1,280
pixels wide costs about 2,100.

Asked for raw coordinates of a button centred at (703, 694) in thousandths, it answered
(200, 700), and (197, 707) in pixels: not usable. Asked which cell of a labelled grid holds
the button, it answered the right cell.

## Decision

1. `screen.find(what)` finds a thing in two grid rounds: an 8x6 grid over the screenshot
   picks a cell; that cell, with a quarter of its size around it, is zoomed to 768 pixels
   wide with a 6x6 grid, which picks a smaller cell; the point snaps to the centre of what
   is drawn there, the background judged from the ring around it. The result is a `v_N`
   target that `screen.click` clicks; a target described in risky words (delete, send, pay)
   asks first, like an OCR line.
2. `screen.look(question)` answers a question about the screenshot in words; its answer is
   marked as untrusted outside content.
3. Each image is budgeted at 2,400 tokens, and a 429 is waited out once.
4. Pointer moves glide over about 0.3 s, easing out, so the user can follow them; the
   existing check-and-retry follows the glide.

Measured on a made-up 1920x1080 desktop: a gear icon without text found 9 px from its
centre, a "Save" button 21 px from its centre and inside it.

## Consequences

- Arcee can click icons and pictures, at two vision calls (about 5,000 tokens) a find.
- At most about one find a minute on the free tier before waiting.
- A real vision Core could later take whole screenshots each step; this keeps the text
  Core and calls vision only when it asks.
