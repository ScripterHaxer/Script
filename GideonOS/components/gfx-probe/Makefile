# gideon-gfx-probe. Works on a dev host and inside Buildroot (which passes
# CC, PKG_CONFIG and WAYLAND_SCANNER for the target).
CC ?= cc
PKG_CONFIG ?= pkg-config
WAYLAND_SCANNER ?= wayland-scanner
PROTOCOLS_DIR ?= $(shell $(PKG_CONFIG) --variable=pkgdatadir wayland-protocols)
EGL ?= 1

PKGS := wayland-client
ifeq ($(EGL),1)
PKGS += wayland-egl egl glesv2
DEFS := -DHAVE_EGL
endif
CFLAGS ?= -O2
# Kept separate from CFLAGS/LDLIBS: build systems (Buildroot) pass those on the
# command line, which would override any `+=` here.
PROBE_CFLAGS := -std=c11 -Wall -Wextra -Werror $(DEFS) $(shell $(PKG_CONFIG) --cflags $(PKGS)) -I.
PROBE_LIBS := $(shell $(PKG_CONFIG) --libs $(PKGS))

all: gideon-gfx-probe

xdg-shell-client-protocol.h:
	$(WAYLAND_SCANNER) client-header $(PROTOCOLS_DIR)/stable/xdg-shell/xdg-shell.xml $@
xdg-shell-protocol.c:
	$(WAYLAND_SCANNER) private-code $(PROTOCOLS_DIR)/stable/xdg-shell/xdg-shell.xml $@

gideon-gfx-probe: gideon-gfx-probe.c xdg-shell-protocol.c xdg-shell-client-protocol.h
	$(CC) $(PROBE_CFLAGS) $(CFLAGS) $(LDFLAGS) -o $@ gideon-gfx-probe.c xdg-shell-protocol.c $(PROBE_LIBS) $(LDLIBS)

install: gideon-gfx-probe
	install -D -m 0755 gideon-gfx-probe $(DESTDIR)/usr/bin/gideon-gfx-probe

clean:
	rm -f gideon-gfx-probe xdg-shell-protocol.c xdg-shell-client-protocol.h

.PHONY: all install clean
