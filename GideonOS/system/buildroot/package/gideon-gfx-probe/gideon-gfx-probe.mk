################################################################################
#
# gideon-gfx-probe (source: components/gfx-probe)
#
################################################################################

GIDEON_GFX_PROBE_VERSION = 1.0
GIDEON_GFX_PROBE_SITE = $(BR2_EXTERNAL_GIDEONOS_PATH)/../../components/gfx-probe
GIDEON_GFX_PROBE_SITE_METHOD = local
GIDEON_GFX_PROBE_DEPENDENCIES = host-wayland wayland wayland-protocols libegl libgles

define GIDEON_GFX_PROBE_BUILD_CMDS
	$(TARGET_MAKE_ENV) $(MAKE) -C $(@D) \
		CC="$(TARGET_CC)" CFLAGS="$(TARGET_CFLAGS)" LDFLAGS="$(TARGET_LDFLAGS)" \
		PKG_CONFIG="$(PKG_CONFIG_HOST_BINARY)" \
		WAYLAND_SCANNER="$(HOST_DIR)/bin/wayland-scanner" \
		PROTOCOLS_DIR="$(STAGING_DIR)/usr/share/wayland-protocols"
endef

define GIDEON_GFX_PROBE_INSTALL_TARGET_CMDS
	$(TARGET_MAKE_ENV) $(MAKE) -C $(@D) DESTDIR="$(TARGET_DIR)" install
endef

$(eval $(generic-package))
