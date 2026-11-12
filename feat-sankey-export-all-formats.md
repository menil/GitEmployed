# AI Decision Record: Export All Sankey Diagram Formats at Once

## Context & Goal
Previously, the `sankey.yml` workflow required users to pick a single export format (`sankeymatic`, `mermaid`, `json`, or `csv`) when triggering the workflow via `workflow_dispatch`. Since flow extraction and formatting are fast and lightweight operations, requiring the user to choose a format upfront added unnecessary manual input friction. The goal is to generate all diagram formats simultaneously in one run and package them into the export artifact without needing a `format` workflow parameter.

## Architecture & Key Decisions
1. **Multi-Format Extraction Utility (`format_all_flows`)**:
   - Added `SANKEY_FORMAT_EXTENSIONS` mapping each format identifier to its target file extension (`txt` for SankeyMATIC, `mmd` for Mermaid, `json`, and `csv`).
   - Implemented `format_all_flows` in `src/gitemployed/sankey.py` returning a dictionary of `{filename: content}`, keeping flow generation separate from filesystem I/O.
2. **CLI Directory Output (`--output-dir`)**:
   - Introduced `--output-dir` in `gitemployed.cli.sankey`. When passed, all four files (`sankey.txt`, `sankey.mmd`, `sankey.json`, `sankey.csv`) are written into the target directory.
   - Kept `--format` optional (defaulting to `sankeymatic` when printing to stdout or `--output`) to preserve backwards compatibility for single-file CLI usage while disallowing contradictory combinations (`--output` with `--output-dir`, `--format` with `--output-dir`).
3. **Workflow Simplification**:
   - Removed the `format` input from `template/.github/workflows/sankey.yml`.
   - Updated the workflow to pass `--output-dir sankey-artifacts` and upload the artifact directory directly with `actions/upload-artifact@v7`.

## Alternatives Considered & Rejected
1. *Write all files in the current working directory without `--output-dir`*:
   - Rejected because dumping uncontained files into the working directory risks naming collisions and makes cleanup/artifact packaging error-prone. Using a dedicated directory cleanly scopes the artifact.
2. *Retain `format: all` as a choice option in the workflow*:
   - Rejected because formatting is so lightweight that generating all formats should be the default behavior rather than a choice option.
3. *Rename `sankey.txt` to `sankeymatic.txt`*:
   - Rejected to preserve continuity with the existing artifact conventions established by `sankey.yml`.
