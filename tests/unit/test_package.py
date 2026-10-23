import re

import arcee


def test_version_is_read_from_package_metadata() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", arcee.__version__)
