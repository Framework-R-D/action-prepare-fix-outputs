# `action-prepare-fix-outputs`

> Determines target ref and repository for fix workflows.

## Usage

```yaml
- uses: Framework-R-D/action-prepare-fix-outputs@575967002716f2bdeb18caaf9253a4d9aca1d534 # v1
  with:
    input-name: value
```

## Inputs

| Name | Description | Required | Default |
|------|-------------|----------|---------|
| ref | Manual ref override | false | |
| repo | Manual repo override | false | |
| sha | Full immutable source SHA; caller-approved for comments and cross-repository overrides | false | |
| checkout-path | Manual checkout path override | false | |

## Outputs

| Name | Description |
|------|-------------|
| ref | The resolved ref |
| sha | Immutable source SHA, separate from the branch push target |
| repo | The resolved repository |
| checkout_path | The resolved checkout path |

## Snapshot contract

Keep `ref` as a branch push destination and check out `sha` for formatting. Comment commands require a full SHA that the caller has authorized for the exact PR repository; missing approval, moved heads, and mismatched overrides fail closed. Supplying both `ref` and `repo` no longer bypasses PR validation. Current-head lookup alone does not authorize a reviewer's snapshot, and commit timestamps are not approval evidence. All nested `format-all` and individual fix calls must share the same approved `repo`, `ref`, and `sha`.

Manual same-repository branches are resolved once; explicit snapshot overrides must match that resolution. Direct pull-request events now identify the actual PR head repository and branch rather than the synthetic merge ref. SHA-as-branch values, tag destinations, refspecs, output injection, and absolute/traversing checkout paths are rejected. Checkout paths must remain contained within the workspace, including existing symlinks. Privileged callers must authorize execution and keep tool manifests/configuration trusted before reaching this metadata resolver. Pass the approved `sha` to `handle-fix-commit` as `pr-info-sha`; its push lease must not be widened by a later branch lookup.

The resolver and tests use Python 3.12 or newer and the standard library, with no local Node or npm requirement. The runner invokes Python in isolated mode so target-controlled Python configuration is ignored. Metadata GET requests stay on the configured GitHub API endpoint; redirects and lookup failures do not widen or replace the approved snapshot.

## License

[Apache 2.0](LICENSE)
