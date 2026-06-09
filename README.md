# `action-prepare-fix-outputs`

> Determines target ref and repository for fix workflows.

## Usage

```yaml
- uses: Framework-R-D/action-prepare-fix-outputs@v1  # pin to commit SHA in production
  with:
    input-name: value
```

## Inputs

| Name | Description | Required | Default |
|------|-------------|----------|---------|
| ref | Manual ref override | false | |
| repo | Manual repo override | false | |
| checkout-path | Manual checkout path override | false | |

## Outputs

| Name | Description |
|------|-------------|
| ref | The resolved ref |
| repo | The resolved repository |
| checkout_path | The resolved checkout path |

## License

[Apache 2.0](LICENSE)
