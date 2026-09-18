# Crainbow Protected Baseline

The approved source baseline is the original archive:

`Crainbow_CBT_Phase6K4_FINAL_APPROVED.zip`

The original archive is preserved outside the development working tree. The working tree is a separate copy used for controlled changes.

Important baseline hashes captured before foundation changes:

- `app.py`: `78210f5165efa5d271eb6be68d252b25109d69d72c3eed6b74cea1f6a637740e`
- `cbt.db`: `90d470b886e018b607e9a5ed6001164e6e5782ad122c9d3492b83fab5ed94c49`

The approved ZIP remains the ultimate rollback/reference artifact. No migration, database conversion, or destructive modification is performed against the baseline archive.

## Working-copy rule
All architectural changes must occur in a development copy. Before each material phase, create a versioned checkpoint and run the current regression suite.
