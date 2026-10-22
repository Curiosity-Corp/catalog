PREFIX ?= /usr/local
DESTDIR ?=
PYTHON ?= python3
LIBDIR = $(DESTDIR)$(PREFIX)/lib/streamstack
BINDIR = $(DESTDIR)$(PREFIX)/bin
SHAREDIR = $(DESTDIR)$(PREFIX)/share
USERUNITDIR ?= $(DESTDIR)$(PREFIX)/lib/systemd/user
UDEVDIR ?= $(DESTDIR)$(PREFIX)/lib/udev/rules.d

.PHONY: all test e2e lint build install uninstall clean

all: lint test

test:
	PYTHONPATH=src $(PYTHON) -m pytest

# Browser tests against the demo rig (needs `pip install -e ".[e2e]"` and `playwright install chromium firefox`).
# Screenshots land in e2e-artifacts/ (override with E2E_ARTIFACTS), E2E_BROWSERS=chromium skips Firefox.
e2e:
	PYTHONPATH=src $(PYTHON) -m pytest tests/e2e

lint:
	ruff check src tests
	ruff format --check src tests

build:
	$(PYTHON) -m build

# A self-contained install for hosts that should not pip-install into the
# system Python: the package under PREFIX/lib/streamstack, launchers on PATH.
install:
	install -d $(LIBDIR) $(BINDIR) $(USERUNITDIR) $(SHAREDIR)/applications
	rm -rf $(LIBDIR)/streamstack
	cp -r src/streamstack $(LIBDIR)/
	find $(LIBDIR) -name __pycache__ -prune -exec rm -rf {} +
	install -m 755 packaging/bin/streamstack packaging/bin/streamstack-auto packaging/bin/streamstack-console \
		packaging/bin/streamstack-tray $(BINDIR)/
	install -m 644 packaging/systemd/user/*.service $(USERUNITDIR)/
	install -m 644 packaging/applications/*.desktop $(SHAREDIR)/applications/
	install -D -m 644 packaging/udev/60-streamstack-litra.rules $(UDEVDIR)/60-streamstack-litra.rules
	install -D -m 644 src/streamstack/data/icons/streamstack-live.svg \
		$(SHAREDIR)/icons/hicolor/scalable/apps/streamstack.svg
	for state in live raw warn off; do \
		install -D -m 644 src/streamstack/data/icons/streamstack-$$state.svg \
			$(SHAREDIR)/icons/hicolor/scalable/status/streamstack-$$state.svg; \
	done

uninstall:
	rm -rf $(LIBDIR)
	rm -f $(BINDIR)/streamstack $(BINDIR)/streamstack-auto $(BINDIR)/streamstack-console \
		$(BINDIR)/streamstack-tray
	rm -f $(USERUNITDIR)/streamstack-auto.service $(USERUNITDIR)/streamstack-canon.service
	rm -f $(SHAREDIR)/applications/streamstack*.desktop $(UDEVDIR)/60-streamstack-litra.rules
	rm -f $(SHAREDIR)/icons/hicolor/scalable/apps/streamstack.svg $(SHAREDIR)/icons/hicolor/scalable/status/streamstack-*.svg

clean:
	rm -rf build dist src/*.egg-info .pytest_cache .ruff_cache .coverage coverage.xml
	find . -name __pycache__ -prune -exec rm -rf {} +
