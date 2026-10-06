################################################################################
#
# gideon-compositor (source: components/compositor)
#
################################################################################

GIDEON_COMPOSITOR_VERSION = 0.4.0
GIDEON_COMPOSITOR_SITE = $(BR2_EXTERNAL_GIDEONOS_PATH)/../../components/compositor
GIDEON_COMPOSITOR_SITE_METHOD = local
GIDEON_COMPOSITOR_DEPENDENCIES = \
	host-pkgconf wayland libinput libxkbcommon pixman seatd libdrm mesa3d systemd

# Exclude the developer's build tree from the copy.
GIDEON_COMPOSITOR_OVERRIDE_SRCDIR_RSYNC_EXCLUSIONS = --exclude target --exclude vendor --exclude .cargo

# Buildroot vendors crates for downloaded tarballs only; for this local
# package vendor the exact versions from Cargo.lock so the build itself runs
# `cargo build --offline --locked` like every other cargo package.
define GIDEON_COMPOSITOR_VENDOR_CRATES
	cd $(@D) && \
		CARGO_HOME=$(BR_CARGO_HOME) $(HOST_DIR)/bin/cargo vendor --locked --versioned-dirs \
			--manifest-path Cargo.toml vendor > $(@D)/.vendor-config.toml
	mkdir -p $(@D)/.cargo
	mv $(@D)/.vendor-config.toml $(@D)/.cargo/config.toml
endef
GIDEON_COMPOSITOR_POST_RSYNC_HOOKS += GIDEON_COMPOSITOR_VENDOR_CRATES

$(eval $(cargo-package))
