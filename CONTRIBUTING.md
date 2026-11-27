# Contributing to devup

Thank you for helping make `devup` better for the developer community!

## Development Principles
1. **Zero third-party runtime dependencies:** Everything inside `src/devup/` must use only the Python 3.11+ standard library.
2. **Cross-platform by default:** Keep path handling, process-tree cleanup, and terminal output compatible with Windows, macOS, and Linux.
3. **Test your changes:** Every bug fix or feature should include tests under `tests/`.

## Local Setup

```bash
git clone https://github.com/devup-cli/devup.git
cd devup
python -W error -m unittest discover -s tests -v
```

## Pull Request Checklist
- [ ] All unit & integration tests pass (`python -W error -m unittest discover -s tests -v`).
- [ ] No external runtime dependencies added to `pyproject.toml`.
- [ ] Documentation (`README.md`, `docs/`, `CHANGELOG.md`) updated when user-facing behavior changes.
