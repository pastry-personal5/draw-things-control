"""Shared test setup: test assertions capture any logs they need themselves."""

from loguru import logger

# Expected validation and network failures are covered throughout the suite.
# Do not send those expected logs to the test command's console; an individual
# test that needs them installs its own sink.
logger.remove()
