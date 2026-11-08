"""The voice models the .deb carries (decision 0019):
`python -m arcee.bundle CACHE/models TARGET`.

packaging/build-deb.sh puts them in /usr/share/arcee/models, where `model_path` finds
them first: an installed Arcee then hears "hey Arcee" and answers aloud without
downloading anything. Each is fetched once into `build/models` (the cache) and copied
from there on later builds.
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

from arcee import voice_local, voice_reply
from arcee.voice_local import MODELS, VoiceModel


def _complete(folder: Path, model: VoiceModel) -> bool:
    return all((folder / model.name / name).is_file() for name in model.files)


async def fetch_into(cache: Path) -> list[VoiceModel]:
    """Every model, downloaded into `cache` unless it is there already."""
    os.environ["ARCEE_DATA_DIR"] = str(cache.parent)  # models_dir() is <data>/models
    assert voice_local.models_dir() == cache, voice_local.models_dir()
    wanted = [*MODELS, voice_reply.SPEECH]
    for model in wanted:
        if _complete(cache, model):
            continue
        print(f"downloading {model.name} ({model.size_mb} MB)", flush=True)
        if model is voice_reply.SPEECH:
            await voice_reply.fetch()
        else:
            await voice_local.download(model)
    return wanted


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python -m arcee.bundle CACHE TARGET", file=sys.stderr)
        return 2
    cache, target = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    cache.mkdir(parents=True, exist_ok=True)
    for model in asyncio.run(fetch_into(cache)):
        (target / model.name).mkdir(parents=True, exist_ok=True)
        for name in model.files:
            shutil.copy2(cache / model.name / name, target / model.name / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
