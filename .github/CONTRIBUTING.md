# Contributing to Thinkback

Thanks for your interest in contributing to Thinkback! This document explains the process.

## Getting started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/<you>/thinkback.git`
3. Create a feature branch: `git checkout -b feat/my-feature`
4. Install dependencies: `uv sync --frozen --group dev && cd web && npm install`
5. Run tests: `uv run pytest tests/ && cd web && npx tsc --noEmit`

## What we look for

- **Bug fixes** with a failing test first
- **New backends** (implement `MemoryBackend` port from `thinkback.domain.ports`)
- **Admin dashboard improvements** (new pages, better UX, a11y)
- **Documentation** (API docs, deployment guides, tutorials)
- **Tests** (we have 615+ and want more)

## Code style

**Python**: `ruff` for lint + format, `mypy --strict` for types.

```bash
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/
uv run mypy --strict src/
```

**TypeScript**: strict mode, no `any` at boundaries.

```bash
cd web && npx tsc --noEmit
```

## Testing

- **Backend**: `uv run pytest tests/` (615+ tests, no external dependencies)
- **Frontend**: `cd web && npx tsc --noemit && npm run build`
- **Integration**: `make dev` starts API + web UI with mock data

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```
feat: add Qdrant backend
fix: prevent duplicate L3 extraction on retry
docs: add architecture diagram
test: cover scoped delete edge cases
refactor: extract task state machine to domain layer
```

## Pull request process

1. Update `CHANGELOG.md` under `[Unreleased]`
2. Ensure `make test` and `cd web && npm run build` pass
3. Write a clear PR description with screenshots for UI changes
4. Link related issues
5. Request review

## Good first issues

Check issues labeled `good first issue`. These are scoped and self-contained.

## Architecture decisions

Significant changes should be discussed first. Open a [Discussion](https://github.com/mcgrapeng/Thinkback/discussions) or issue to propose direction before implementing.

## License

By contributing, you agree to license your contributions under the Apache 2.0 license.
