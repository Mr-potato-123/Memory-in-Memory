# Bank2 formal experiment

Bank2 is the no-regression release produced by the second training and
diagnosis round. The new Access and Construction candidates were evaluated but
did not pass the validation gate, so the published Bank2 files preserve the
129 active Bank1 Skills under the physical v2 publication schema.

- `banks/`: Runtime-only published Bank2 files.
- `build/`: diagnosis inputs, candidates, CRUD transactions, internal snapshots,
  and the automatic selection manifest.
- `validation/`: selected validation artifacts and three rejected ablations.
- `ROUND2_REPORT_CN.md`: complete Chinese execution and analysis report.

The selected validation set contains 392 unique LoCoMo questions, zero Runtime
protocol errors, and zero permanent Judge errors. See `validation/comparison.csv`
for the four-candidate comparison.
