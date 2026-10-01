# Status history map

The full 3,235-line `STATUS.md` before the September 28, 2026 consolidation is
preserved by Git at revision
`d425a9e5a92b511bfa9a64d9a378c9584bbe3612`. Its file SHA-256 is
`cd1c4823ea55d095b783b68262960ee199e7865582278502ce2a24398855b1a3`.
Retrieve and verify the exact original without maintaining a second editable
copy:

```bash
git show d425a9e5a92b511bfa9a64d9a378c9584bbe3612:STATUS.md > status-original.md
sha256sum status-original.md
```

The old file mixed chronological release notes, implementation facts, and
validation boundaries. Its historical facts and exact wording remain in that
Git object; old uses of “current” and “candidate” describe their original
writing dates, not the current release. This map identifies each former
section's present owner.

| Original lines and section | Present owner |
| --- | --- |
| 1–328: release narrative and `Current Release` | Git original for chronology; [`STATUS.md`](../../STATUS.md#release-and-checkout-boundary) for current release identity and evidence boundary. |
| 329–1387: `Historical Release Evidence` and v0.3.x implementation sections | Git original for immutable historical results; [`docs/releases.md`](../releases.md) for repeatable release rules. |
| 1388–1836: `Implementation Evidence History` | Git original; current capability facts in [`STATUS.md`](../../STATUS.md#implemented-in-the-checkout). |
| 1837–2094: `Phase 1 Result` and `Adaptive Orchestration Progress` | Git original for old trials; current user and Planning contracts in [`docs/product-demo-slice.md`](../product-demo-slice.md) and [`docs/adaptive-orchestration.md`](../adaptive-orchestration.md). |
| 2095–2973: `Product Readiness Boundary` | Git original for individual attempts; current journey and remaining limits in [`STATUS.md`](../../STATUS.md#validation-limits-and-open-capabilities). |
| 2974–3167: `Implemented and Offline Verified` | Current grouped implementation facts in [`STATUS.md`](../../STATUS.md#implemented-in-the-checkout); detailed contracts in the appropriate topic specifications under [`docs/`](../README.md). |
| 3168–3235: fixed paths, missing capabilities, and validation boundary | Current comparison and missing capabilities in [`STATUS.md`](../../STATUS.md#validation-limits-and-open-capabilities); topology definitions in [`configs/teams.json`](../../configs/teams.json). |

No implementation or acceptance claim is established merely by this index.
Use the current status page and the cited topic contract for present behavior.

The 199-line status immediately before the 0.6.6 completion update remains in
Git at `d31e7b428f3bdd5f0c9cb557efa0a3c2acbe2edd:STATUS.md`, with SHA-256
`798d9ca9f43dfe9ea27036d130d9f4d1efc1f93a412d8b2e0a1947bcc8f9e65c`. Its pending-publication and pending-journey statements describe that earlier snapshot.
