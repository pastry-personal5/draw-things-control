# Milestone 07: Check performance

**Phase:** [Phase 4](README.md)
**Status:** done (2026-10-14)
**Depends on:** the existing test suite and generated gRPC-stub contract.

## Goal

Make the required `make check` feedback loop substantially faster without weakening its coverage, hiding failures, or introducing concurrent-test flakiness.

## Evidence

On 2026-10-14, a warm local run measured Ruff at 0.06 seconds, Pyright at 4.58 seconds, and the full standard-library `unittest` suite at 165.00 seconds. The TUI suite took 111.19 seconds by itself; the jobs and MCP-server suites took 14.59 and 19.02 seconds. The full suite used 65.64 seconds of CPU time, showing that process, media, polling, and I/O waits leave room for independent test workers to overlap.

`make check` currently declares `proto`, `lint`, `typecheck`, and `test` as peer prerequisites. Passing `-j` directly can race generated-stub removal/regeneration against readers. The Python 3.12 `unittest` command exposes discovery and duration reporting but no worker-count option, so parallel execution needs explicit project-owned shards or a deliberate test-runner change.

The delivered pytest-xdist route uses eight `--dist loadfile` workers and completed `make check` in 29.65 seconds. `tests/core/test_process_output.py` runs in its own pytest process: after gRPC starts threads, its C-core fork diagnostic can otherwise enter that test's deliberately exact child-output assertion. The remaining 1,363 tests run in xdist and the isolated process runs the other seven; repeated full runs passed without warnings or collisions.

A later 2026-10-14 change replaced every project-owned subprocess start with the fork-free `posix_spawn`/clean-exec boundary. The gRPC fork diagnostic no longer exists to contaminate the exact-output test, so that file now runs in the same xdist suite as every other test.

## Scope

In scope:

- Run gRPC-stub generation before every consumer that requires the generated files, then overlap independent linting, type checking, and test work.
- Preserve the existing test cases while selecting either project-owned process shards or a maintained parallel runner such as `pytest-xdist` based on measured reliability and speed.
- Preserve silent successful `make test` output and show complete failing-shard diagnostics with an unambiguous failing target.
- Add an opt-in profiling target that reports the slowest individual tests, so shard balance is maintained from measurements.
- Repair test isolation exposed by concurrent execution: dynamically reserve network ports, close child resources, wait for child termination, and keep every test's filesystem/state data private.
- Measure cold and warm check time before and after the change, record the machine and worker count, and document the supported developer and CI commands.

Out of scope:

- Removing checks from `make check`, reducing test coverage, marking tests skipped for speed, or changing product behavior.
- Running the real Draw Things or H3 generator in the automated suite.
- Changing product dependencies or production behavior.

## Planned changes

1. Refactor the Makefile dependency graph so `proto` completes before generated-stub consumers start. Keep Ruff's two checks together, run lint and Pyright concurrently with tests only after that barrier, and make the aggregate target propagate a failure from every completed shard.
2. Replace the one serial discovery invocation with a measured parallel runner. Evaluate explicit `unittest` shards and `pytest-xdist` against the complete existing suite; keep the option that reaches the target reliably with the clearest focused-test workflow. Use a bounded, configurable default of eight workers rather than all logical CPUs.
3. Preserve the existing `make test`, `make test-verbose`, `make test-tui`, and `make test-tui-verbose` workflows. Add focused diagnosis and slow-test profiling without changing the pass/fail suite.
4. Characterize the suite under repeated parallel runs. Replace fixed or racy listener allocation with reserved ephemeral ports, eliminate leaked file/process resources, and repair any shared state, environment, temporary-directory, or test-order dependency found. A failed shard prints its captured output; successful shards stay quiet.
5. Update the development rules and this phase's documentation with the new targets, worker configuration, timing evidence, and the reason direct `make -j check` is unsupported. Run `make check` through completion after the implementation lands.

## Acceptance criteria

- `make check` runs `proto` before any generated-stub reader, runs every current Ruff, Pyright, and unit-test check exactly once, and fails when any constituent check or shard fails.
- Parallel test execution is deterministic across repeated clean runs: no port collision, leaked child/resource warning, cross-test state, or order-dependent result remains. A failure identifies its shard and retains its diagnostics.
- The existing focused and verbose test commands retain their documented behavior. The profiling target identifies slow individual tests without changing the test selection.
- On the baseline machine with a warm dependency cache, the completed suite takes 60 seconds or less while preserving all current tests. A cold-cache measurement and worker count are documented for comparison.
- `make check` passes when the implementation lands.

## Owner decisions

- The warm-cache wall-clock target is 60 seconds or less on the baseline machine.
- The default is eight workers and is configurable for CI through an environment variable.
- A maintained parallel-test dependency, including `pytest-xdist`, may replace the standard-library runner when it produces the best verified result.
