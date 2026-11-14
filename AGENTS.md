# CryptoLab — Development Guidelines

## 1. Project Scope

CryptoLab is a cryptocurrency market research, historical data analysis, and backtesting platform.

- The project is intended for research, simulation, and evaluation of trading strategies.
- Do not implement, enable, or operate live trading. Never place, modify, or cancel real orders.
- Use private API credentials only with separate explicit approval for work within project scope.
- Offline simulation and fake execution adapters are allowed.
- Prefer simple, maintainable solutions over unnecessary abstractions and premature optimization.

## 2. Architecture

The Python package lives in `src/cryptolab`. Keep responsibilities in their existing modules.

- `market_data/` collects, parses, and validates public market data. `storage/` handles SQLite persistence.
- `analytics/` computes market statistics and indicators. `features/` builds historical features with availability timestamps.
- `strategies/` generates BUY, SELL, and HOLD signals. `backtesting/sizing.py` handles capital allocation and quantities.
- `execution/` defines order intents, lifecycle transitions, fills, and offline adapters.
- `backtesting/` orchestrates deterministic simulations. Its `accounting.py` handles balances, FIFO lots, cost basis, fees, and PnL. Its `benchmarks.py` provides benchmarks used by research.
- `research/` compares strategies and produces diagnostics and reports.
- `cli.py` parses and validates arguments, dispatches commands, and reports results.

Do not move financial accounting into the CLI or strategy implementations.

Reuse existing components before introducing new abstractions.

Avoid duplicate execution paths or independent implementations of portfolio accounting.

## 3. Financial Correctness

Financial correctness takes priority over implementation convenience.

- Use `Decimal` for financial calculations. Do not introduce floating-point arithmetic into monetary calculations.
- Preserve deterministic execution and reproducible results.
- Prevent look-ahead bias. Strategies and position sizing must not access market data unavailable at the decision time.
- Preserve next-candle-open execution for strategy signals and the separate trailing-stop rules unless a change is explicitly approved.
- Apply trade accounting and settled balance changes only from confirmed simulated fills. Track order reservations and releases separately.
- Preserve FIFO cost basis and correct allocation of entry and exit fees.
- Prevent negative balances, overspending, overfills, and duplicate fill application.
- Maintain consistency between balances, open lots, realized PnL, unrealized PnL, and total equity.
- Preserve correct handling of partial positions, scale-in, scale-out, and trailing stops.
- Do not silently change trading assumptions, strategy parameters, or accounting semantics.

If financial behavior is ambiguous, explain the alternatives and request a decision before implementation.

## 4. Backward Compatibility

- Preserve existing public APIs unless a breaking change is explicitly approved.
- Maintain legacy all-in/all-out behavior by default.
- Preserve existing strategy behavior and parameters.
- Keep historical backtesting results reproducible.
- Do not change baseline reports unintentionally.
- Introduce new CLI options without breaking existing commands.
- Document intentional behavior changes and migration requirements.

## 5. Testing and Quality

Every new component or behavior change must include appropriate tests.

- Unit tests must run without internet access.
- Use deterministic fixtures and controlled timestamps.
- Test both successful operations and failure scenarios.
- Add regression tests for bug fixes.
- Verify financial invariants after relevant state transitions.
- Test duplicate events, invalid transitions, insufficient balances, and partial fills where applicable.
- Do not weaken or delete tests merely to make a failing implementation pass.
- Do not modify expected baseline outputs without documenting and justifying the change.

Use Python 3.12 or later. CI tests Python 3.12 and 3.13. To set up a development environment, run `python -m pip install -c requirements/constraints.txt -e ".[test,release]"` in an active virtual environment.

Run the relevant checks from the repository root. Tests live in `tests/unit`, with shared fixtures in `tests/fixtures`. The autouse safeguard in `tests/conftest.py` blocks live socket connections.

```bash
python -m pytest -q -p no:cacheprovider
ruff check src tests scripts
python -m compileall -q src tests scripts
git diff --check
```

For CLI or packaging changes, smoke-test the installed entry point and all six commands.

```bash
cryptolab --help
for command in collect quality analyze signals backtest research; do
  cryptolab "$command" --help || exit 1
done
```

For release verification, follow the build and clean-wheel install steps in `README.md`. Check hosted CI on GitHub before reporting its status.

If a check fails, investigate the cause and report any unresolved failure.

## 6. Development Workflow

For significant changes:

1. Inspect the relevant implementation, interfaces, and tests.
2. Present a concise implementation plan before making substantial architectural changes.
3. Implement changes incrementally.
4. Keep modifications focused on the requested task.
5. Add or update tests and documentation where necessary.
6. Run relevant tests and quality checks.
7. Review the final diff for unintended changes.
8. Summarize implementation decisions, test results, and limitations.

Do not modify unrelated files or introduce unrelated refactoring.

Do not add dependencies without a clear justification.

Prefer existing project conventions and established interfaces.

## 7. Git Workflow

Codex is authorized to create commits and push completed changes to the current working branch after successful verification.

- Use descriptive, conventional commit messages.
- Commit only files related to the completed task.
- Verify the current branch and working tree before committing.
- Do not include secrets, credentials, local databases, generated caches, or temporary files.
- Run tests and quality checks before committing.
- Push normally to the current branch after a successful commit.
- Never force-push, including with `--force-with-lease` or a forced refspec.
- Never rewrite Git history.
- Never delete local or remote branches or tags.
- Do not discard unrelated local changes.
- If the remote branch has diverged or the push is rejected, stop and report the conflict rather than automatically rebasing or force-pushing.
- Report the commit hash and push result after completion.

## 8. Security

- Never hardcode API keys, tokens, passwords, or other secrets.
- Never commit `.env` files containing credentials.
- Do not log authentication secrets or sensitive account information.
- Keep tests independent of private exchange APIs.
- Keep execution adapters offline. Do not introduce authenticated trading functionality.
- Validate external input and treat exchange responses as untrusted data.
- Do not silently execute destructive database migrations.

## 9. Documentation and Reporting

Update relevant documentation when behavior, architecture, or public interfaces change.

Include the following in the task summary.

- What changed and why.
- Which files were added or modified.
- Important architectural decisions.
- Tests and quality checks performed.
- Remaining limitations or risks.
- Commit hash and push status.

Keep explanations technically precise and concise.

## 10. Priority

Apply these priorities in order.

1. Financial correctness and data integrity.
2. Security and prevention of unauthorized trading.
3. Deterministic behavior and reproducibility.
4. Backward compatibility.
5. Maintainability and architectural consistency.
6. Performance and implementation convenience.
