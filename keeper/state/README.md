# Keeper state

`regime.json` is the trailing series the regime read is computed from: routed
volume and per-coin value change, one observation per treasury run.

It is committed on purpose. The series is what distinguishes an expansion from
a contraction, and a scheduled runner keeps no disk between runs — so a series
that is not persisted reads as empty every time, and an empty series classifies
every market as normal. That is the one reading that is never useful, because
it is also the reading a crash produces.
