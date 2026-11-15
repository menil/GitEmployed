# AI Decision Record: Preserve work.description and Extra Resume Fields

## Context & Goal
`GitEmployed`'s `format-resume` workflow and resume tailoring pipeline round-trip `resumes/resume.yaml` through `Resume.from_dict()` -> `Resume.to_dict()`. Previously, `Work` lacked `description` and `location`, and all JSON Resume Pydantic models used `ConfigDict(extra="ignore")`, silently dropping `work[].description` and any other unmodeled JSON Resume sections/fields (e.g. `volunteer`, `languages`, `basics.image`, `skill.level`).

## Architecture & Key Decisions
- **Shared `_JsonResumeModel` Base Class (`src/gitemployed/schema.py`)**: Extracted a common base with `ConfigDict(extra="allow", arbitrary_types_allowed=True)` and `_extra_fields()` / `_merge_extras()` helpers so all 8 JSON Resume models (`Location`, `Profile`, `Basics`, `Work`, `Education`, `Skill`, `Project`, `Resume`) preserve unmodeled fields across `from_dict()` -> `to_dict()` round-trips without 8x boilerplate.
- **Automatic Model Field & Alias Exclusion**: `_extra_fields()` excludes `cls.model_fields.keys() | cls._ALIASES` so camelCase-to-snake_case aliased keys (e.g. `startDate` -> `start_date`) are neither duplicated in `model_extra` nor passed twice on `cls(**extras)`.
- **First-Class `Work.location` and `Work.description`**: Added explicit `location` and `description` fields to `Work` (parsed with `_parse_str` and emitted in JSON Resume schema order before `position`).
- **Defensive Deep Copy on Output**: `_merge_extras()` uses `res.setdefault(k, copy.deepcopy(v))` so callers mutating `to_dict()` output cannot mutate `model_extra`, and extra keys cannot overwrite canonical serialized keys.

## Alternatives Considered & Rejected
- **Adding only `Work.description` with `extra="ignore"`**: Rejected because other official JSON Resume fields (`work[].location`, `basics.image`, `volunteer`, `awards`, `languages`, etc.) and custom user fields would still be silently stripped.
- **Replacing Pydantic round-trip in `format-resume` with YAML AST only**: Rejected as the sole fix because `triage.py` and `recompile_tailored.py` also construct/serialize `Resume` via Pydantic and would still drop `description` on tailored resumes.
