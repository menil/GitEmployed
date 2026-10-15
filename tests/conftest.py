import os

import pytest

# Python 3.14's zipfile honors SOURCE_DATE_EPOCH, and Nix defaults it to
# 1980-01-01 UTC. West of UTC that is a 1979 local date, which the zip
# format cannot encode, so openpyxl's xlsx writes crash. Clamping to one
# day later keeps the suite green in any shell, not just inside devenv.
_MIN_ZIP_EPOCH = 315619200  # 1980-01-02 00:00:00 UTC


@pytest.fixture(autouse=True)
def _clamp_source_date_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if raw is not None and raw.isdigit() and int(raw) < _MIN_ZIP_EPOCH:
        monkeypatch.setenv("SOURCE_DATE_EPOCH", str(_MIN_ZIP_EPOCH))
