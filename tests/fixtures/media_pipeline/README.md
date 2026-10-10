# Media-pipeline test fixture

The live VPS test input is intentionally not stored in Git because it is a
3 GB archive. Use the user-provided Google Photos URL as the input for the
VPS-only integration test.

The test must verify:

- the source duration is preserved after intro processing;
- a failed intro encode leaves the original source untouched;
- Tamil audio selection remains unchanged;
- the output is not accepted when its duration is materially shorter; and
- `/usr/src/app/torrents/seeding` is never removed by temporary cleanup.
