---
fest_type: phase
fest_id: 001_INGEST
fest_name: INGEST
fest_parent: ai-advisers-system-AA0001
fest_order: 1
fest_status: pending
fest_created: 2026-04-16T18:17:46.400384-07:00
fest_phase_type: ingest
fest_tracking: true
---

# Phase Goal: 001_INGEST

**Phase:** 001_INGEST | **Status:** Pending | **Type:** Ingest

## Phase Objective

**Primary Goal:** Ingest and structure input materials into actionable specifications

**Context:** AI-Advisers codebase at ~/Documents/AI-Advisers

## Input Sources

Place all raw input materials in `input_specs/`:

- [ ] 163 Python agent files in AI-Advisers/
- [ ] install_launchagents.py — agent schedule registry
- [ ] memory/ directory — JSON reports from each agent

## Expected Outputs

The following structured documents will be created in `output_specs/`:

| Output | Purpose |
|--------|---------|
| `purpose.md` | Festival purpose, success criteria, motivation |
| `requirements.md` | Prioritized requirements (P0/P1/P2) with traceability |
| `constraints.md` | Technical and process constraints |
| `context.md` | Prior art, related systems, key references |

## Success Criteria

This ingest phase is complete when:

- [ ] All input sources reviewed and understood
- [ ] Output specs created following standard structure
- [ ] User has approved the structured output
- [ ] No unresolved questions or ambiguities

## Workflow

This phase uses step-based workflow guidance. See `WORKFLOW.md` for the step-by-step process.

Use `fest next` to see the current step.
Use `fest workflow advance` to move to the next step.

## Notes

All agents must support --auto flag. macOS LaunchAgents required.

---

*Ingest phases transform unstructured input into structured specifications ready for planning.*