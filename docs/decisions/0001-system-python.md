# 0001. Run on the system Python

Date: 2026-10-23
Status: accepted

## Context

The daemon reads the accessibility tree through pyatspi and the app is built with GTK4 and libadwaita through PyGObject. On Ubuntu both come from apt and are compiled for the distro's system Python, which is 3.14 on Ubuntu 26.04, the reference machine. A virtualenv on a different interpreter (the spec originally said 3.12) cannot import them, and building PyGObject from source in the virtualenv adds C build dependencies and a second copy of the bindings.

## Decision

The project requires Python 3.14 (`>=3.14,<3.15`). The development virtualenv and the packaged one are both created on `/usr/bin/python3` with `--system-site-packages`, so apt's `gi` and `pyatspi` are importable while everything else comes from `uv.lock`.

## Consequences

- No compiled GTK bindings to maintain; the app uses the same libraries as the desktop.
- The deb depends on `python3 (>= 3.14), python3 (<< 3.15)` and must be rebuilt for a new distro Python.
- Some ML packages may lag behind on 3.14 wheels (faster-whisper/CTranslate2, onnxruntime, lancedb). They are not needed before phase 5; if they are still missing then, the voice pipeline runs as a separate Python 3.12 process.
- CI has no apt bindings, so code that imports `gi` or `pyatspi` must import them lazily and be tested through fakes.
