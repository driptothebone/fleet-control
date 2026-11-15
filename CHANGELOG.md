# Changelog

## 2.0.0

First release of **Fleet Control**, a cleaner v2 of the hackathon Model Board
([v1 as submitted](https://github.com/driptothebone/hermes-model-board/tree/17de6b85a7e746909d6e524b245d78ccf3187dc7)).
It has a new repository and plugin id (`fleet-control`). v1 is unchanged.

- Restores the full Model Board interface with the overview, profile table and
  phone cards, filter, All or Ticked selection, per-profile sheets and bulk
  editors.
- New controls cover all auxiliary tasks (specific, same as main or automatic,
  per task or all at once), Fast or Normal, ordered fallback chains and max
  subagents.
- Switching provider works on profiles pinned to a first-party endpoint. Only
  pins matching the old provider's public definition are cleared. Custom
  endpoints and auth are still refused. Same-provider model changes keep them.
- Native look that follows the active Hermes dashboard theme, dark or light,
  live. An optional **Colour** switch is off by default.
- Clearer review with plain-language before and after values, exact fields one
  click away, a scope line naming exactly which profiles change and saved values
  in results.
- Stale saves explain that nothing was written and offer a reload with a fresh
  preview. Transport failures are reported as uncertain, never as "not saved".
- One broken profile config or catalogue no longer blocks the others. Profiles
  using external secret sources are shown read-only.
- Accessibility improvements include keyboard-operable rows, a focus-trapped
  dialog, focus return and 44 px phone targets.
