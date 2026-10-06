# Contributing

Use Python 3.12 or 3.13 and uv 0.11.13. From the repository root:

```sh
make setup
make hooks
make quality
```

`make format` applies formatting. Portable uv command equivalents, policy scope,
coverage expectations and CI evidence are in [the quality contract](../docs/QUALITY.md).
Keep behavior changes covered by deterministic regression tests and describe
unverified browser/runtime acceptance explicitly. Keep changes scoped to the
module owner and exclude generated artifacts, profiles and secrets from Git.
