---
name: graph-bootstrap
description: Build or refresh the code-review-graph index for the current repository.
---

# graph-bootstrap

Run `code-review-graph build` once per checkout, then rely on the update hook. Rules live in `{{rules_file}}`.
<!-- if:zcode -->
ZCode: the hook also fires on ApplyPatch.
<!-- endif -->

{{block:graph-search}}
