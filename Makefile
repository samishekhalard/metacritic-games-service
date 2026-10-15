BASE_URL ?= http://localhost:8000

.PHONY: test e2e install-playwright

# Default unit + integration tests (no browser, no server required).
test:
	PYTHONPATH=src pytest tests/

# End-to-end browser tests.  Requires a running server at BASE_URL.
# To run against the live deployment:
#   make e2e BASE_URL=https://metacrawl.onrender.com
e2e:
	BASE_URL=$(BASE_URL) PYTHONPATH=src pytest tests/e2e/ -v

# One-time setup: install Chromium browser for Playwright.
install-playwright:
	playwright install chromium
