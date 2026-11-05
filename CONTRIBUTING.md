# Contributing

Thanks for your interest in `container-ctl`. Most design documents are written in Traditional Chinese,
and issues or pull requests in either English or Traditional Chinese are welcome.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[doctor,scanner]" pytest
```

Python 3.11 or newer is required. The core `container_ctl` package uses only the
Python standard library at runtime; optional sub-tools use `jsonschema`
(`container-doctor`) and `PyYAML` (`scan-path-refs`).

## Running tests

```bash
python3 -m pytest tests container_control/tests container_params/tests \
  tools/container-doctor/container_doctor/tests tools/scan-path-refs/tests \
  -p no:cacheprovider -q
```

Some tests need `podman` on the host and are skipped without it.

## Design invariants to keep in mind

- `container_ctl/exec_run.py` must remain import-free with respect to the rest
  of `container_ctl` (standard library only). Callers invoke it as a subprocess
  and pass `ExecSpec` JSON on `stdin`; see `docs/EXEC-SPEC-INTERFACE.md`.
- Keep changes small and covered by unit tests. Do not add network access to
  tests.
- Never commit secrets, internal hostnames, or personal paths.

## Licensing

By submitting a contribution, you agree that it will be licensed under the
[Apache License, Version 2.0](LICENSE).
