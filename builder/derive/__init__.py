"""
Per-build data, derived where the build runs: the normalised package tables, the diff against the previous release
of the branch, the vulnerability scan and the base image's layer digests, assembled into the release's record.json
(`builder record`). The hub ingests that file instead of deriving the same data itself, and imports these modules
for older releases that have none, so there is one implementation.
"""
