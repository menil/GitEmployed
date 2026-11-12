# AI Decision Record: Distinguish Rejections and Ghosting by Pipeline Stage in Sankey Funnel

## Context & Goal
The Sankey diagram generation for job search tracking (`gitemployed-sankey`) previously directed all rejection outcomes from both the initial application stage and the interview loop into a single sink node named `Rejected`. Additionally, inactivity after applications was labeled `Ghosted / Stale` while inactivity during interview loops was labeled `Loop Stale`.

This led to two issues:
1. Rejection losses were merged into one sink, obscuring whether applicants were failing resume/application screening versus failing after technical interview loops.
2. Inconsistent and ambiguous naming for ghosting/stale applications across stages.
3. If an issue was erroneously marked `in-loop` and subsequently reverted back to `applied` (as happened with role #1607), the previous monotonic milestone extractor permanently considered the role to have reached the interview loop, polluting both `In Loop` and `Interviewing (Active)` funnel counts.

The goal is to cleanly distinguish terminal rejections and ghosting by pipeline stage (`Rejected after Application`, `Rejected after Interview Loop`, `Ghosted after Application`, `Ghosted after Interview Loop`), while properly handling backward lifecycle transitions and error corrections.

## Architecture & Key Decisions

1. **Stage-Specific Sink Nodes**:
   - `Applied -> Rejected after Application`
   - `In Loop -> Rejected after Interview Loop`
   - `Applied -> Ghosted after Application`
   - `In Loop -> Ghosted after Interview Loop`
   This avoids merging into a single sink node in SankeyMATIC and Mermaid diagrams and provides immediate visual clarity into where drop-offs happen in the funnel.

2. **Ranked Milestone Hierarchy**:
   Introduced a strict milestone ranking (`ready-to-apply` < `applied` < `in-loop` < `offer-received`).

3. **Chronological Replay & Error Invalidation**:
   Events are evaluated chronologically. When an issue moves backward to an upstream milestone (e.g. `applied` after `in-loop`), downstream milestones are revoked because moving backward represents an error correction (e.g. accidental status move).

4. **Current Label Reconciliation**:
   If an open issue's current labels contain an upstream milestone (`applied`), any historical downstream milestones (`in-loop`) are reconciled and removed, preventing active applied roles from being incorrectly categorized under active interview loops.

## Alternatives Considered & Rejected

- *Keep single 'Rejected' sink and distinguish only via tooltips/sub-labels*: Rejected because SankeyMATIC and Mermaid merge identically-named target nodes into a single block, making visual separation impossible.
- *Strict unlabeled-event matching*: Rejected because in GitEmployed automation and GitHub Projects boards, status transitions can produce complex event sequences; ranking the milestones and enforcing forward monotonicity while revoking downstream states on upstream re-labeling handles all move-back cases reliably without fragile event pairing.
