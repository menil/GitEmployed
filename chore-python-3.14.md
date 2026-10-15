# AI Decision Record: Upgrade to Python 3.14

## Context & Goal
Move the project from Python 3.12 to 3.14 (devenv, Docker runner image,
requires-python, ruff target). The locked numpy 1.26.3 had no 3.14
wheels and was held back by python-jobspy 1.1.82.

## Architecture & Key Decisions
- Relocking with `uv lock --python 3.14` pulls python-jobspy 1.3.0, which
  no longer pins numpy, so numpy moves to 2.5.4 (floor raised to 2.3).
  jobspy 1.3.0 swaps tls-client for curl-cffi; markdownify goes 0.13 -> 1.2.
  A live Indeed scrape confirmed the new jobspy works.
- `requires-python >=3.14` and ruff `py314`: ruff's autofix emits PEP 758
  `except A, B:` and unquotes annotations (PEP 649), which cannot run on
  3.12/3.13, so a wider range would be untrue.
- Nix sets SOURCE_DATE_EPOCH to 1980-01-01 UTC and 3.14's zipfile honors
  it; west of UTC that is 1979 and openpyxl xlsx writes crash. Fixed by
  setting 1980-01-02 in devenv.nix and clamping it in tests/conftest.py so
  bare nix-shell runs (commit hook) also pass.
- Added an offline test that the kwargs scraper.py passes still exist in
  jobspy's real scrape_jobs signature, since all other tests mock it.

## Alternatives Considered & Rejected
- Override numpy via `override-dependencies` on the old jobspy: unnecessary
  once a newer jobspy dropped the pin.
- Keep requires-python >=3.12 and only bump the image: invalid with PEP 758
  syntax in the source.
- Patch the xlsx writer instead of the environment: more code for an
  environment-specific problem.
- Split the dependency re-lock into a separate PR: reasonable for bisecting,
  but kept as one change for now.
