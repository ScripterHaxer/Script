/*
 * gideon-gfx-probe: graphics-stack test client for GideonOS (milestone 3).
 *
 * Opens an xdg-shell window and fills it with a known colour, either through
 * wl_shm (CPU) or EGL/GLES2 (--egl, exercises Mesa). It reports what it sees
 * on stdout so tests can verify the stack end to end:
 *
 *   probe: output name=Virtual-1 mode=1280x800 scale=1
 *   probe: configured 1280x800 fullscreen=1
 *   probe: renderer=egl gl_renderer=softpipe
 *   probe: drawn color=3db8c6
 *   probe: key 30            (keyboard focus + key routing)
 *   probe: button 272        (pointer routing)
 *
 * A key press switches the colour to amber, a pointer button to red, so a
 * screenshot proves the input reached this client and it re-rendered.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <wayland-client.h>

#include "xdg-shell-client-protocol.h"

#ifdef HAVE_EGL
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <wayland-egl.h>
#endif

static const uint32_t colors[] = {
    0x3db8c6, /* idle: Gideon Teal (accent) */
    0xe8b04b, /* after a key press: amber */
    0xf06a6a, /* after a pointer button: red */
};

struct output {
    struct wl_output *wl;
    char name[64];
    int width, height, scale;
    struct output *next;
};

static struct {
    struct wl_display *display;
    struct wl_compositor *compositor;
    struct wl_shm *shm;
    struct xdg_wm_base *wm_base;
    struct wl_seat *seat;
    struct wl_keyboard *keyboard;
    struct wl_pointer *pointer;
    struct output *outputs;
    struct wl_surface *surface;
    struct xdg_surface *xdg_surface;
    struct xdg_toplevel *toplevel;
    int width, height;
    bool configured, fullscreen, use_egl, running, dirty;
    int color;
    struct wl_buffer *buffer;
#ifdef HAVE_EGL
    EGLDisplay egl_display;
    EGLContext egl_context;
    EGLSurface egl_surface;
    struct wl_egl_window *egl_window;
#endif
} app = { .width = 640, .height = 480, .running = true, .dirty = true };

static void say(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    fputs("probe: ", stdout);
    vprintf(fmt, ap);
    fputc('\n', stdout);
    fflush(stdout);
    va_end(ap);
}

/* ---- wl_shm rendering ---------------------------------------------------- */

static void buffer_release(void *data, struct wl_buffer *buffer)
{
    (void)data;
    wl_buffer_destroy(buffer);
}
static const struct wl_buffer_listener buffer_listener = { .release = buffer_release };

static bool draw_shm(void)
{
    int stride = app.width * 4, size = stride * app.height;
    int fd = memfd_create("gideon-gfx-probe", MFD_CLOEXEC);
    if (fd < 0 || ftruncate(fd, size) < 0) {
        perror("memfd");
        return false;
    }
    uint32_t *px = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (px == MAP_FAILED) {
        perror("mmap");
        close(fd);
        return false;
    }
    uint32_t c = 0xff000000u | colors[app.color];
    for (int i = 0; i < app.width * app.height; i++)
        px[i] = c;
    munmap(px, size);

    struct wl_shm_pool *pool = wl_shm_create_pool(app.shm, fd, size);
    struct wl_buffer *buf = wl_shm_pool_create_buffer(pool, 0, app.width, app.height,
                                                      stride, WL_SHM_FORMAT_XRGB8888);
    wl_buffer_add_listener(buf, &buffer_listener, NULL);
    wl_shm_pool_destroy(pool);
    close(fd);

    wl_surface_attach(app.surface, buf, 0, 0);
    wl_surface_damage_buffer(app.surface, 0, 0, app.width, app.height);
    wl_surface_commit(app.surface);
    return true;
}

/* ---- EGL / GLES2 rendering ----------------------------------------------- */

#ifdef HAVE_EGL
static bool egl_init(void)
{
    PFNEGLGETPLATFORMDISPLAYEXTPROC get_platform_display =
        (void *)eglGetProcAddress("eglGetPlatformDisplayEXT");
    app.egl_display = get_platform_display
        ? get_platform_display(EGL_PLATFORM_WAYLAND_KHR, app.display, NULL)
        : eglGetDisplay((EGLNativeDisplayType)app.display);
    EGLint major, minor;
    if (app.egl_display == EGL_NO_DISPLAY || !eglInitialize(app.egl_display, &major, &minor)) {
        say("error egl_initialize failed (0x%x)", eglGetError());
        return false;
    }
    eglBindAPI(EGL_OPENGL_ES_API);
    const EGLint cfg_attrs[] = {
        EGL_SURFACE_TYPE, EGL_WINDOW_BIT, EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8,
        EGL_BLUE_SIZE, 8, EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT, EGL_NONE };
    EGLConfig cfg;
    EGLint n = 0;
    if (!eglChooseConfig(app.egl_display, cfg_attrs, &cfg, 1, &n) || n < 1) {
        say("error no suitable EGL config");
        return false;
    }
    const EGLint ctx_attrs[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
    app.egl_context = eglCreateContext(app.egl_display, cfg, EGL_NO_CONTEXT, ctx_attrs);
    app.egl_window = wl_egl_window_create(app.surface, app.width, app.height);
    app.egl_surface = eglCreateWindowSurface(app.egl_display, cfg,
                                             (EGLNativeWindowType)app.egl_window, NULL);
    if (app.egl_context == EGL_NO_CONTEXT || app.egl_surface == EGL_NO_SURFACE ||
        !eglMakeCurrent(app.egl_display, app.egl_surface, app.egl_surface, app.egl_context)) {
        say("error egl context/surface failed (0x%x)", eglGetError());
        return false;
    }
    say("renderer=egl egl=%d.%d gl_vendor=%s gl_renderer=%s", major, minor,
        (const char *)glGetString(GL_VENDOR), (const char *)glGetString(GL_RENDERER));
    return true;
}

static bool draw_egl(void)
{
    wl_egl_window_resize(app.egl_window, app.width, app.height, 0, 0);
    uint32_t c = colors[app.color];
    glViewport(0, 0, app.width, app.height);
    glClearColor(((c >> 16) & 0xff) / 255.0f, ((c >> 8) & 0xff) / 255.0f, (c & 0xff) / 255.0f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    return eglSwapBuffers(app.egl_display, app.egl_surface);
}
#endif

static void redraw(void)
{
    if (!app.configured || !app.dirty)
        return;
    bool ok;
#ifdef HAVE_EGL
    ok = app.use_egl ? draw_egl() : draw_shm();
#else
    ok = draw_shm();
#endif
    if (ok)
        say("drawn color=%06x size=%dx%d", colors[app.color], app.width, app.height);
    app.dirty = false;
}

/* ---- xdg-shell --------------------------------------------------------- */

static void wm_ping(void *data, struct xdg_wm_base *wm, uint32_t serial)
{
    (void)data;
    xdg_wm_base_pong(wm, serial);
}
static const struct xdg_wm_base_listener wm_listener = { .ping = wm_ping };

static void surface_configure(void *data, struct xdg_surface *s, uint32_t serial)
{
    (void)data;
    xdg_surface_ack_configure(s, serial);
    if (!app.configured)
        say("configured %dx%d fullscreen=%d", app.width, app.height, app.fullscreen);
    app.configured = true;
    app.dirty = true;
}
static const struct xdg_surface_listener surface_listener = { .configure = surface_configure };

static void toplevel_configure(void *data, struct xdg_toplevel *t, int32_t w, int32_t h,
                               struct wl_array *states)
{
    (void)data; (void)t;
    if (w > 0 && h > 0 && (w != app.width || h != app.height)) {
        app.width = w;
        app.height = h;
        app.dirty = true;
    }
    uint32_t *st;
    wl_array_for_each(st, states)
        if (*st == XDG_TOPLEVEL_STATE_FULLSCREEN)
            app.fullscreen = true;
}
static void toplevel_close(void *data, struct xdg_toplevel *t)
{
    (void)data; (void)t;
    app.running = false;
}
static const struct xdg_toplevel_listener toplevel_listener = {
    .configure = toplevel_configure, .close = toplevel_close };

/* ---- input ----------------------------------------------------------------- */

static void kb_keymap(void *d, struct wl_keyboard *k, uint32_t fmt, int32_t fd, uint32_t size)
{
    (void)d; (void)k; (void)fmt; (void)size;
    close(fd); /* raw keycodes are enough for the probe */
}
static void kb_enter(void *d, struct wl_keyboard *k, uint32_t s, struct wl_surface *sf, struct wl_array *keys)
{
    (void)d; (void)k; (void)s; (void)sf; (void)keys;
    say("keyboard focus");
}
static void kb_leave(void *d, struct wl_keyboard *k, uint32_t s, struct wl_surface *sf)
{
    (void)d; (void)k; (void)s; (void)sf;
}
static void kb_key(void *d, struct wl_keyboard *k, uint32_t s, uint32_t t, uint32_t key, uint32_t state)
{
    (void)d; (void)k; (void)s; (void)t;
    if (state != WL_KEYBOARD_KEY_STATE_PRESSED)
        return;
    say("key %u", key);
    if (key == 1 /* KEY_ESC */) {
        app.running = false;
        return;
    }
    app.color = 1;
    app.dirty = true;
}
static void kb_modifiers(void *d, struct wl_keyboard *k, uint32_t s, uint32_t a, uint32_t b, uint32_t c, uint32_t g)
{
    (void)d; (void)k; (void)s; (void)a; (void)b; (void)c; (void)g;
}
static void kb_repeat(void *d, struct wl_keyboard *k, int32_t rate, int32_t delay)
{
    (void)d; (void)k; (void)rate; (void)delay;
}
static const struct wl_keyboard_listener kb_listener = {
    kb_keymap, kb_enter, kb_leave, kb_key, kb_modifiers, kb_repeat };

static void ptr_enter(void *d, struct wl_pointer *p, uint32_t s, struct wl_surface *sf, wl_fixed_t x, wl_fixed_t y)
{
    (void)d; (void)p; (void)s; (void)sf;
    say("pointer enter %d,%d", wl_fixed_to_int(x), wl_fixed_to_int(y));
}
static void ptr_leave(void *d, struct wl_pointer *p, uint32_t s, struct wl_surface *sf)
{
    (void)d; (void)p; (void)s; (void)sf;
}
static void ptr_motion(void *d, struct wl_pointer *p, uint32_t t, wl_fixed_t x, wl_fixed_t y)
{
    (void)d; (void)p; (void)t; (void)x; (void)y;
}
static void ptr_button(void *d, struct wl_pointer *p, uint32_t s, uint32_t t, uint32_t button, uint32_t state)
{
    (void)d; (void)p; (void)s; (void)t;
    if (state != WL_POINTER_BUTTON_STATE_PRESSED)
        return;
    say("button %u", button);
    app.color = 2;
    app.dirty = true;
}
static void ptr_axis(void *d, struct wl_pointer *p, uint32_t t, uint32_t a, wl_fixed_t v)
{
    (void)d; (void)p; (void)t; (void)a; (void)v;
}
/* wl_pointer v5 events: every opcode the bound version can send needs a handler,
 * or libwayland aborts the client on the first such event. */
static void ptr_frame(void *d, struct wl_pointer *p)
{
    (void)d; (void)p;
}
static void ptr_axis_source(void *d, struct wl_pointer *p, uint32_t src)
{
    (void)d; (void)p; (void)src;
}
static void ptr_axis_stop(void *d, struct wl_pointer *p, uint32_t t, uint32_t a)
{
    (void)d; (void)p; (void)t; (void)a;
}
static void ptr_axis_discrete(void *d, struct wl_pointer *p, uint32_t a, int32_t v)
{
    (void)d; (void)p; (void)a; (void)v;
}
static const struct wl_pointer_listener ptr_listener = {
    .enter = ptr_enter, .leave = ptr_leave, .motion = ptr_motion,
    .button = ptr_button, .axis = ptr_axis, .frame = ptr_frame,
    .axis_source = ptr_axis_source, .axis_stop = ptr_axis_stop,
    .axis_discrete = ptr_axis_discrete };

static void seat_caps(void *d, struct wl_seat *seat, uint32_t caps)
{
    (void)d;
    if ((caps & WL_SEAT_CAPABILITY_KEYBOARD) && !app.keyboard) {
        app.keyboard = wl_seat_get_keyboard(seat);
        wl_keyboard_add_listener(app.keyboard, &kb_listener, NULL);
    }
    if ((caps & WL_SEAT_CAPABILITY_POINTER) && !app.pointer) {
        app.pointer = wl_seat_get_pointer(seat);
        wl_pointer_add_listener(app.pointer, &ptr_listener, NULL);
    }
    say("seat keyboard=%d pointer=%d", !!(caps & WL_SEAT_CAPABILITY_KEYBOARD),
        !!(caps & WL_SEAT_CAPABILITY_POINTER));
}
static void seat_name(void *d, struct wl_seat *s, const char *name)
{
    (void)d; (void)s; (void)name;
}
static const struct wl_seat_listener seat_listener = { seat_caps, seat_name };

/* ---- outputs ----------------------------------------------------------------- */

static void out_geometry(void *d, struct wl_output *o, int32_t x, int32_t y, int32_t pw, int32_t ph,
                         int32_t sp, const char *make, const char *model, int32_t tr)
{
    (void)d; (void)o; (void)x; (void)y; (void)pw; (void)ph; (void)sp; (void)make; (void)model; (void)tr;
}
static void out_mode(void *data, struct wl_output *o, uint32_t flags, int32_t w, int32_t h, int32_t r)
{
    (void)o; (void)r;
    struct output *out = data;
    if (flags & WL_OUTPUT_MODE_CURRENT) {
        out->width = w;
        out->height = h;
    }
}
static void out_done(void *data, struct wl_output *o)
{
    (void)o;
    struct output *out = data;
    say("output name=%s mode=%dx%d scale=%d", out->name[0] ? out->name : "?",
        out->width, out->height, out->scale);
}
static void out_scale(void *data, struct wl_output *o, int32_t factor)
{
    (void)o;
    ((struct output *)data)->scale = factor;
}
static void out_name(void *data, struct wl_output *o, const char *name)
{
    (void)o;
    snprintf(((struct output *)data)->name, sizeof(((struct output *)data)->name), "%s", name);
}
static void out_description(void *d, struct wl_output *o, const char *desc)
{
    (void)d; (void)o; (void)desc;
}
static const struct wl_output_listener out_listener = {
    out_geometry, out_mode, out_done, out_scale, out_name, out_description };

static void global(void *data, struct wl_registry *reg, uint32_t id, const char *iface, uint32_t ver)
{
    (void)data;
    if (!strcmp(iface, wl_compositor_interface.name))
        app.compositor = wl_registry_bind(reg, id, &wl_compositor_interface, ver < 4 ? ver : 4);
    else if (!strcmp(iface, wl_shm_interface.name))
        app.shm = wl_registry_bind(reg, id, &wl_shm_interface, 1);
    else if (!strcmp(iface, xdg_wm_base_interface.name)) {
        app.wm_base = wl_registry_bind(reg, id, &xdg_wm_base_interface, 1);
        xdg_wm_base_add_listener(app.wm_base, &wm_listener, NULL);
    } else if (!strcmp(iface, wl_seat_interface.name)) {
        app.seat = wl_registry_bind(reg, id, &wl_seat_interface, ver < 5 ? ver : 5);
        wl_seat_add_listener(app.seat, &seat_listener, NULL);
    } else if (!strcmp(iface, wl_output_interface.name)) {
        struct output *o = calloc(1, sizeof(*o));
        o->scale = 1;
        o->wl = wl_registry_bind(reg, id, &wl_output_interface, ver < 4 ? ver : 4);
        wl_output_add_listener(o->wl, &out_listener, o);
        o->next = app.outputs;
        app.outputs = o;
    }
}
static void global_remove(void *d, struct wl_registry *r, uint32_t id)
{
    (void)d; (void)r; (void)id;
}
static const struct wl_registry_listener registry_listener = { global, global_remove };

int main(int argc, char **argv)
{
    bool want_fullscreen = false;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--fullscreen"))
            want_fullscreen = true;
        else if (!strcmp(argv[i], "--egl"))
            app.use_egl = true;
        else {
            fprintf(stderr, "usage: %s [--fullscreen] [--egl]\n", argv[0]);
            return 2;
        }
    }
#ifndef HAVE_EGL
    if (app.use_egl) {
        fprintf(stderr, "built without EGL support\n");
        return 2;
    }
#endif

    app.display = wl_display_connect(NULL);
    if (!app.display) {
        say("error cannot connect to Wayland display (%s)", getenv("WAYLAND_DISPLAY") ?: "unset");
        return 1;
    }
    struct wl_registry *reg = wl_display_get_registry(app.display);
    wl_registry_add_listener(reg, &registry_listener, NULL);
    wl_display_roundtrip(app.display); /* globals */
    wl_display_roundtrip(app.display); /* output/seat details */
    if (!app.compositor || !app.shm || !app.wm_base) {
        say("error compositor lacks wl_compositor/wl_shm/xdg_wm_base");
        return 1;
    }
    int n = 0;
    for (struct output *o = app.outputs; o; o = o->next)
        n++;
    say("outputs=%d", n);

    app.surface = wl_compositor_create_surface(app.compositor);
    app.xdg_surface = xdg_wm_base_get_xdg_surface(app.wm_base, app.surface);
    xdg_surface_add_listener(app.xdg_surface, &surface_listener, NULL);
    app.toplevel = xdg_surface_get_toplevel(app.xdg_surface);
    xdg_toplevel_add_listener(app.toplevel, &toplevel_listener, NULL);
    xdg_toplevel_set_title(app.toplevel, "GideonOS graphics probe");
    xdg_toplevel_set_app_id(app.toplevel, "org.gideonos.GfxProbe");
    if (want_fullscreen)
        xdg_toplevel_set_fullscreen(app.toplevel, NULL);
    wl_surface_commit(app.surface);
    while (!app.configured && wl_display_dispatch(app.display) != -1)
        ;

#ifdef HAVE_EGL
    if (app.use_egl && !egl_init())
        return 1;
#endif
    if (!app.use_egl)
        say("renderer=shm");

    while (app.running) {
        redraw();
        if (wl_display_dispatch(app.display) == -1) {
            say("error display connection lost (%s)", strerror(errno));
            return 1;
        }
    }
    say("exit");
    return 0;
}
