/*
 * tdcap — PipeWire screencast node -> VAAPI H.264/HEVC, zero-copy.
 *
 *   tdcap --node N --size WxH [--fps 60] [--refresh 60] [--codec h264|hevc]
 *         [--rc cqp|vbr|cbr] [--qp 24] [--bitrate kbit] [--maxrate kbit]
 *         [--device /dev/dri/renderD128]
 *
 * Frames arrive from the compositor as dmabufs. Each one is imported into
 * VAAPI, converted BGRx->NV12 on the GPU and encoded; pixels never touch
 * system memory.
 *
 * stdout: a sequence of records, all integers little-endian:
 *     u8 type, u32 payload length, payload
 *   'V' video   u64 capture time (CLOCK_MONOTONIC us), u32 us spent between
 *               capture and packet out, u8 flags (1 = keyframe),
 *               then one Annex B access unit
 *   'P' cursor  i32 x, i32 y, u8 visible   (stream pixels, hotspot applied
 *               by the receiver)
 *   'S' sprite  u32 w, u32 h, i32 hot x, i32 hot y, then w*h RGBA
 *               (w == 0: cursor has no image, i.e. hidden)
 *
 * stdin: newline-terminated commands
 *   idr      next encoded frame is an IDR
 *   refine   re-encode the last frame (idle quality top-up / IDR carrier)
 * EOF on stdin ends the process.
 *
 * Threads: the PipeWire loop thread receives buffers, forwards cursor
 * metadata at once and leaves the newest converted frame in a one-slot
 * mailbox; the encoder thread drains the mailbox. A frame that is still
 * waiting when a newer one arrives is dropped *before* encoding, so the
 * reference chain the receiver sees is never broken.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

#include <libdrm/drm_fourcc.h>

#include <libavcodec/avcodec.h>
#include <libavfilter/avfilter.h>
#include <libavfilter/buffersink.h>
#include <libavfilter/buffersrc.h>
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_drm.h>
#include <libavutil/opt.h>
#include <libavutil/pixdesc.h>

#include <pipewire/pipewire.h>
#include <spa/debug/types.h>
#include <spa/param/video/format-utils.h>
#include <spa/param/video/type-info.h>

#define CURSOR_MAX 384
#define CURSOR_META_SIZE(w, h) \
    (sizeof(struct spa_meta_cursor) + sizeof(struct spa_meta_bitmap) + (w) * (h) * 4)

static struct {
    uint32_t node;
    int width, height, fps;
    int mon_num, mon_den;       /* refresh rate asked of the compositor */
    const char *codec, *rc, *device;
    int qp, bitrate_k, maxrate_k;
} opt = {
    .fps = 60, .mon_num = 60, .mon_den = 1, .codec = "h264", .rc = "cqp", .qp = 24,
    .bitrate_k = 20000, .maxrate_k = 40000,
    .device = "/dev/dri/renderD128",
};

static struct pw_thread_loop *loop;
static struct pw_stream *stream;
static struct spa_hook stream_listener;
static struct spa_video_info_raw vfmt;
static int have_format;

static AVBufferRef *drm_dev, *drm_frames;
static AVFilterGraph *graph;
static AVFilterContext *fsrc, *fsink;
static AVCodecContext *enc;

/* Mailbox between the PipeWire thread and the encoder thread. */
static pthread_mutex_t mbox_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t mbox_cond = PTHREAD_COND_INITIALIZER;
static AVFrame *pending;
static int want_idr = 1, want_refine, quitting;
static uint64_t n_captured, n_dropped;

static pthread_mutex_t out_lock = PTHREAD_MUTEX_INITIALIZER;

static void die(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    fputs("tdcap: ", stderr);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
    _exit(1);
}

static void avdie(const char *what, int err)
{
    char buf[128];
    av_strerror(err, buf, sizeof buf);
    die("%s: %s", what, buf);
}

static uint64_t now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000u + ts.tv_nsec / 1000;
}

/* Write one record; exits the process if stdout is gone. */
static void emit(char type, const void *head, size_t head_len,
                 const void *body, size_t body_len)
{
    uint8_t pre[5];
    uint32_t len = head_len + body_len;
    pre[0] = type;
    memcpy(pre + 1, &len, 4);
    struct iovec iov[3] = {
        { pre, 5 }, { (void *)head, head_len }, { (void *)body, body_len },
    };
    pthread_mutex_lock(&out_lock);
    int idx = 0;
    while (idx < 3) {
        if (iov[idx].iov_len == 0) { idx++; continue; }
        ssize_t n = writev(STDOUT_FILENO, iov + idx, 3 - idx);
        if (n < 0) {
            if (errno == EINTR) continue;
            _exit(0);               /* reader went away: normal shutdown */
        }
        while (n > 0 && idx < 3) {
            if ((size_t)n >= iov[idx].iov_len) {
                n -= iov[idx].iov_len;
                iov[idx].iov_len = 0;
                idx++;
            } else {
                iov[idx].iov_base = (char *)iov[idx].iov_base + n;
                iov[idx].iov_len -= n;
                n = 0;
            }
        }
    }
    pthread_mutex_unlock(&out_lock);
}

/* ------------------------------------------------------------------ */
/* ffmpeg: DRM PRIME -> VAAPI NV12 -> encoder                          */
/* ------------------------------------------------------------------ */

static void init_graph(void)
{
    int err;

    err = av_hwdevice_ctx_create(&drm_dev, AV_HWDEVICE_TYPE_DRM, opt.device,
                                 NULL, 0);
    if (err < 0) avdie("open DRM device", err);

    drm_frames = av_hwframe_ctx_alloc(drm_dev);
    AVHWFramesContext *fc = (AVHWFramesContext *)drm_frames->data;
    fc->format = AV_PIX_FMT_DRM_PRIME;
    fc->sw_format = AV_PIX_FMT_BGR0;
    fc->width = opt.width;
    fc->height = opt.height;
    if ((err = av_hwframe_ctx_init(drm_frames)) < 0)
        avdie("init DRM frames", err);

    graph = avfilter_graph_alloc();
    fsrc = avfilter_graph_alloc_filter(graph, avfilter_get_by_name("buffer"),
                                       "in");
    AVBufferSrcParameters *par = av_buffersrc_parameters_alloc();
    par->format = AV_PIX_FMT_DRM_PRIME;
    par->width = opt.width;
    par->height = opt.height;
    par->time_base = (AVRational){ 1, 1000000 };
    par->hw_frames_ctx = drm_frames;
    if ((err = av_buffersrc_parameters_set(fsrc, par)) < 0)
        avdie("buffersrc parameters", err);
    av_free(par);
    if ((err = avfilter_init_str(fsrc, NULL)) < 0) avdie("buffersrc", err);

    if ((err = avfilter_graph_create_filter(
             &fsink, avfilter_get_by_name("buffersink"), "out", NULL, NULL,
             graph)) < 0)
        avdie("buffersink", err);

    AVFilterInOut *outputs = avfilter_inout_alloc();
    AVFilterInOut *inputs = avfilter_inout_alloc();
    outputs->name = av_strdup("in");
    outputs->filter_ctx = fsrc;
    inputs->name = av_strdup("out");
    inputs->filter_ctx = fsink;
    /* BT.709 limited range, stated explicitly so the tablet's decoder and
     * the VPP block agree on what the numbers mean. */
    err = avfilter_graph_parse_ptr(
        graph,
        "hwmap=mode=read:derive_device=vaapi,"
        "scale_vaapi=format=nv12:out_color_matrix=bt709:out_range=limited",
        &inputs, &outputs, NULL);
    if (err < 0) avdie("parse filter graph", err);
    if ((err = avfilter_graph_config(graph, NULL)) < 0)
        avdie("configure filter graph", err);
    avfilter_inout_free(&inputs);
    avfilter_inout_free(&outputs);
}

static void init_encoder(void)
{
    int hevc = !strcmp(opt.codec, "hevc");
    const AVCodec *codec =
        avcodec_find_encoder_by_name(hevc ? "hevc_vaapi" : "h264_vaapi");
    if (!codec) die("encoder for %s not available", opt.codec);

    enc = avcodec_alloc_context3(codec);
    enc->width = opt.width;
    enc->height = opt.height;
    enc->time_base = (AVRational){ 1, 1000000 };
    enc->framerate = (AVRational){ opt.fps, 1 };
    enc->pix_fmt = AV_PIX_FMT_VAAPI;
    enc->hw_frames_ctx = av_buffer_ref(av_buffersink_get_hw_frames_ctx(fsink));
    /* The link is reliable and ordered, so there is nothing a periodic
     * keyframe would repair. IDRs are sent on request only (new client);
     * every other frame is a small P-frame. */
    enc->gop_size = 1 << 20;
    enc->max_b_frames = 0;
    enc->flags |= AV_CODEC_FLAG_LOW_DELAY;
    enc->color_primaries = AVCOL_PRI_BT709;
    enc->color_trc = AVCOL_TRC_BT709;
    enc->colorspace = AVCOL_SPC_BT709;
    enc->color_range = AVCOL_RANGE_MPEG;
    if (!hevc)
        enc->profile = FF_PROFILE_H264_HIGH;

    AVDictionary *o = NULL;
    av_dict_set(&o, "async_depth", "1", 0);
    av_dict_set(&o, "sei", "0", 0);
    if (!strcmp(opt.rc, "cqp")) {
        av_dict_set(&o, "rc_mode", "CQP", 0);
        av_dict_set_int(&o, "qp", opt.qp, 0);
    } else {
        int cbr = !strcmp(opt.rc, "cbr");
        av_dict_set(&o, "rc_mode", cbr ? "CBR" : "VBR", 0);
        enc->bit_rate = (int64_t)opt.bitrate_k * 1000;
        enc->rc_max_rate =
            (int64_t)(cbr ? opt.bitrate_k : opt.maxrate_k) * 1000;
        /* A short rate buffer: a burst may borrow ~1/4 s of bits. */
        enc->rc_buffer_size = enc->rc_max_rate / 4;
    }
    int err = avcodec_open2(enc, codec, &o);
    if (err < 0) avdie("open encoder", err);
    av_dict_free(&o);
}

static void free_desc(void *opaque, uint8_t *data)
{
    (void)opaque;
    av_free(data);
}

/* Import one dmabuf and return it converted to an NV12 VAAPI frame. */
static AVFrame *convert(struct spa_buffer *b, uint64_t ts)
{
    struct spa_data *d = &b->datas[0];
    AVDRMFrameDescriptor *desc = av_mallocz(sizeof *desc);
    desc->nb_objects = 1;
    desc->objects[0].fd = d->fd;
    desc->objects[0].size = d->maxsize;
    desc->objects[0].format_modifier =
vfmt.modifier;
    desc->nb_layers = 1;
    desc->layers[0].format =
        vfmt.format == SPA_VIDEO_FORMAT_BGRA ? DRM_FORMAT_ARGB8888
                                             : DRM_FORMAT_XRGB8888;
    desc->layers[0].nb_planes = 1;
    desc->layers[0].planes[0].object_index = 0;
    desc->layers[0].planes[0].offset = d->chunk->offset;
    desc->layers[0].planes[0].pitch = d->chunk->stride;

    AVFrame *in = av_frame_alloc();
    in->format = AV_PIX_FMT_DRM_PRIME;
    in->width = opt.width;
    in->height = opt.height;
    in->pts = ts;
    in->hw_frames_ctx = av_buffer_ref(drm_frames);
    in->data[0] = (uint8_t *)desc;
    in->buf[0] = av_buffer_create((uint8_t *)desc, sizeof *desc, free_desc,
                                  NULL, 0);

    int err = av_buffersrc_add_frame_flags(fsrc, in, 0);
    av_frame_free(&in);
    if (err < 0) avdie("import dmabuf", err);

    AVFrame *out = av_frame_alloc();
    err = av_buffersink_get_frame(fsink, out);
    if (err < 0) avdie("convert frame", err);
    out->pts = ts;
    return out;
}

static void *encoder_thread(void *arg)
{
    (void)arg;
    AVFrame *last = NULL;
    AVPacket *pkt = av_packet_alloc();
    int64_t last_pts = 0;

    for (;;) {
        pthread_mutex_lock(&mbox_lock);
        while (!pending && !(want_refine && last) && !quitting)
            pthread_cond_wait(&mbox_cond, &mbox_lock);
        if (quitting) {
            pthread_mutex_unlock(&mbox_lock);
            break;
        }
        AVFrame *f = pending;
        pending = NULL;
        if (!f) {
            f = av_frame_clone(last);
            f->pts = now_us();
        }
        want_refine = 0;
        int idr = want_idr;
        want_idr = 0;
        pthread_mutex_unlock(&mbox_lock);

        if (f->pts <= last_pts) f->pts = last_pts + 1;
        last_pts = f->pts;
        f->pict_type = idr ? AV_PICTURE_TYPE_I : AV_PICTURE_TYPE_NONE;

        int err = avcodec_send_frame(enc, f);
        if (err < 0) avdie("encode", err);
        while ((err = avcodec_receive_packet(enc, pkt)) >= 0) {
            struct __attribute__((packed)) {
                uint64_t ts;
                uint32_t spent;
                uint8_t flags;
            } h = {
                .ts = pkt->pts,
                .spent = (uint32_t)(now_us() - (uint64_t)pkt->pts),
                .flags = (pkt->flags & AV_PKT_FLAG_KEY) ? 1 : 0,
            };
            emit('V', &h, sizeof h, pkt->data, pkt->size);
            av_packet_unref(pkt);
        }
        if (err != AVERROR(EAGAIN)) avdie("receive packet", err);

        av_frame_free(&last);
        last = f;
    }
    return NULL;
}

/* ------------------------------------------------------------------ */
/* PipeWire                                                            */
/* ------------------------------------------------------------------ */

static void handle_cursor(struct spa_buffer *b)
{
    static int was_visible = -1;
    struct spa_meta_cursor *c =
        spa_buffer_find_meta_data(b, SPA_META_Cursor, sizeof *c);
    if (!c) return;

    struct __attribute__((packed)) { int32_t x, y; uint8_t visible; } p;
    if (!spa_meta_cursor_is_valid(c)) {
        /* Pointer is on another monitor. */
        if (was_visible != 0) {
            p.x = p.y = 0;
            p.visible = 0;
            emit('P', &p, sizeof p, NULL, 0);
            was_visible = 0;
        }
        return;
    }

    if (c->bitmap_offset >= sizeof *c) {
        struct spa_meta_bitmap *bm = SPA_PTROFF(c, c->bitmap_offset, void);
        struct __attribute__((packed)) {
            uint32_t w, h;
            int32_t hx, hy;
        } s = { bm->size.width, bm->size.height, c->hotspot.x, c->hotspot.y };
        if (bm->size.width == 0 || bm->size.height == 0 ||
            bm->size.width > CURSOR_MAX || bm->size.height > CURSOR_MAX) {
            s.w = s.h = 0;
            emit('S', &s, sizeof s, NULL, 0);
        } else {
            const uint8_t *src = SPA_PTROFF(bm, bm->offset, uint8_t);
            size_t n = (size_t)s.w * s.h * 4;
            uint8_t *rgba = malloc(n);
            for (uint32_t y = 0; y < s.h; y++) {
                const uint8_t *row = src + (size_t)y * bm->stride;
                uint8_t *dst = rgba + (size_t)y * s.w * 4;
                if (bm->format == SPA_VIDEO_FORMAT_BGRA) {
                    for (uint32_t x = 0; x < s.w; x++) {
                        dst[x * 4 + 0] = row[x * 4 + 2];
                        dst[x * 4 + 1] = row[x * 4 + 1];
                        dst[x * 4 + 2] = row[x * 4 + 0];
                        dst[x * 4 + 3] = row[x * 4 + 3];
                    }
                } else {
                    memcpy(dst, row, (size_t)s.w * 4);
                }
            }
            emit('S', &s, sizeof s, rgba, n);
            free(rgba);
        }
    }

    p.x = c->position.x;
    p.y = c->position.y;
    p.visible = 1;
    emit('P', &p, sizeof p, NULL, 0);
    was_visible = 1;
}

/*
 * Pacing. Two properties of mutter 46's virtual monitors shape this:
 *
 * 1. Their frame clock has no vblank to wait for and never gets render
 *    time measurements, so under continuous damage it repaints every
 *    ~2.5 ms (at a nominal 60 Hz) instead of every 16.7 ms.
 * 2. A stream is rate-limited by "time since the last buffer recorded"
 *    against the negotiated max framerate. In cursor-metadata mode a
 *    cursor-only buffer is recorded whenever *any* monitor prepares a
 *    frame, which restarts that limiter and starves the real frames
 *    (measured: ~20 fps with the limiter at the refresh rate).
 *
 * The monitor's refresh rate is taken from the max framerate when the
 * monitor is created; a later format change of the same size keeps the
 * monitor and only moves the limiter. So the refresh rate is negotiated
 * first, then the limiter is raised to RECORD_FPS, out of the way (240
 * was tried and still starved metadata mode: 51 fps with 78 ms holes),
 * and the 60 Hz cadence is made here: one frame is accepted per interval
 * on a fixed grid, and it is always the newest the compositor has drawn
 * (a few ms old at most). Frames in between are returned unconverted.
 */
#define RECORD_FPS 1000     /* the most mutter accepts: a 1 ms limiter */
static struct spa_fraction cur_max_fps;
static int unthrottle_sent;

static struct spa_source *pace_timer;
static struct pw_buffer *held;      /* newest frame waiting for its slot */
static uint64_t held_at;
static uint64_t slot_us;            /* start of the current interval */
static uint64_t interval_us;

/*
 * mutter only hands out dmabufs to a consumer that negotiates DRM
 * modifiers; without the property it silently falls back to a CPU copy
 * into shared memory (measured: 39 fps at 2560x1600 instead of 60).
 * DRM_FORMAT_MOD_INVALID means "the driver's own implicit layout", which
 * a same-GPU VAAPI import understands.
 */
static const uint64_t offered_mods[] = {
    DRM_FORMAT_MOD_INVALID,
    DRM_FORMAT_MOD_LINEAR,
};
#define N_OFFERED (int)(sizeof offered_mods / sizeof offered_mods[0])
static uint64_t chosen_mod = DRM_FORMAT_MOD_INVALID;

/* The size is ours to pick; mutter builds the virtual monitor from it. */
static const struct spa_pod *build_format(struct spa_pod_builder *b,
                                          const uint64_t *mods, int n_mods,
                                          int fixed, struct spa_fraction max_fps)
{
    struct spa_pod_frame f[2];
    spa_pod_builder_push_object(b, &f[0], SPA_TYPE_OBJECT_Format,
                                SPA_PARAM_EnumFormat);
    spa_pod_builder_add(
        b, SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_video),
        SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
        SPA_FORMAT_VIDEO_format, SPA_POD_Id(SPA_VIDEO_FORMAT_BGRx), 0);
    if (fixed) {
        spa_pod_builder_prop(b, SPA_FORMAT_VIDEO_modifier,
                             SPA_POD_PROP_FLAG_MANDATORY);
        spa_pod_builder_long(b, mods[0]);
    } else {
        spa_pod_builder_prop(b, SPA_FORMAT_VIDEO_modifier,
                             SPA_POD_PROP_FLAG_MANDATORY |
                                 SPA_POD_PROP_FLAG_DONT_FIXATE);
        spa_pod_builder_push_choice(b, &f[1], SPA_CHOICE_Enum, 0);
        spa_pod_builder_long(b, mods[0]);       /* default */
        for (int i = 0; i < n_mods; i++)
            spa_pod_builder_long(b, mods[i]);
        spa_pod_builder_pop(b, &f[1]);
    }
    spa_pod_builder_add(
        b, SPA_FORMAT_VIDEO_size,
        SPA_POD_Rectangle(&SPA_RECTANGLE(opt.width, opt.height)),
        SPA_FORMAT_VIDEO_framerate, SPA_POD_Fraction(&SPA_FRACTION(0, 1)),
        SPA_FORMAT_VIDEO_maxFramerate, SPA_POD_Fraction(&max_fps), 0);
    return spa_pod_builder_pop(b, &f[0]);
}

/* Convert one buffer, hand it to the encoder and give it back. */
static void accept(struct pw_buffer *pb, uint64_t now, uint64_t arrived)
{
    /* Stay on the grid while frames keep coming; restart it after a gap. */
    if (now < slot_us + 2 * interval_us)
        slot_us += interval_us;
    else
        slot_us = now;

    /* Stamped with its arrival, so time spent held counts as latency. */
    AVFrame *f = convert(pb->buffer, arrived);
    pthread_mutex_lock(&mbox_lock);
    n_captured++;
    if (pending) {
        av_frame_free(&pending);
        n_dropped++;
    }
    pending = f;
    pthread_cond_signal(&mbox_cond);
    pthread_mutex_unlock(&mbox_lock);
    pw_stream_queue_buffer(stream, pb);
}

static void on_process(void *data)
{
    (void)data;
    struct pw_buffer *newest = NULL, *pb;

    while ((pb = pw_stream_dequeue_buffer(stream))) {
        struct spa_buffer *b = pb->buffer;
        handle_cursor(b);
        struct spa_data *d = &b->datas[0];
        int has_pixels = d->type == SPA_DATA_DmaBuf && d->chunk->size != 0 &&
                         !(d->chunk->flags & SPA_CHUNK_FLAG_CORRUPTED);
        struct spa_meta_header *h =
            spa_buffer_find_meta_data(b, SPA_META_Header, sizeof *h);
        if (h && (h->flags & SPA_META_HEADER_FLAG_CORRUPTED)) has_pixels = 0;
        if (has_pixels) {
            if (newest) pw_stream_queue_buffer(stream, newest);
            newest = pb;
        } else {
            pw_stream_queue_buffer(stream, pb);
        }
    }
    if (!newest) return;
    if (!have_format) {
        pw_stream_queue_buffer(stream, newest);
        return;
    }

    if (held) pw_stream_queue_buffer(stream, held);
    held = NULL;
    uint64_t now = now_us();
    if (now + 500 >= slot_us + interval_us) {
        accept(newest, now, now);
    } else {
        /* Too soon after the last one: keep it until the slot opens,
         * unless something newer replaces it first. Nothing is lost when
         * the screen stops changing; the last frame still goes out. */
        held = newest;
        held_at = now;
        struct timespec ts;
        uint64_t wait = slot_us + interval_us - now;
        ts.tv_sec = wait / 1000000;
        ts.tv_nsec = (wait % 1000000) * 1000;
        pw_loop_update_timer(pw_thread_loop_get_loop(loop), pace_timer, &ts,
                             NULL, false);
    }
}

static void on_pace_timer(void *data, uint64_t expirations)
{
    (void)data;
    (void)expirations;
    if (!held) return;
    struct pw_buffer *b = held;
    held = NULL;
    accept(b, now_us(), held_at);
}

static void on_remove_buffer(void *data, struct pw_buffer *b)
{
    (void)data;
    if (held == b) held = NULL;     /* pool is being torn down */
}

static void on_param_changed(void *data, uint32_t id,
                             const struct spa_pod *param)
{
    (void)data;
    if (!param || id != SPA_PARAM_Format) return;

    struct spa_video_info_raw info = { 0 };
    if (spa_format_video_raw_parse(param, &info) < 0)
        die("unparseable stream format");
    if ((int)info.size.width != opt.width ||
        (int)info.size.height != opt.height)
        die("stream is %ux%u, expected %dx%d", info.size.width,
            info.size.height, opt.width, opt.height);

    /* The compositor answered with a set of modifiers it can allocate:
     * pick one and re-announce, as the dmabuf negotiation protocol asks. */
    const struct spa_pod_prop *mp =
        spa_pod_find_prop(param, NULL, SPA_FORMAT_VIDEO_modifier);
    if (mp && (mp->flags & SPA_POD_PROP_FLAG_DONT_FIXATE)) {
        uint32_t n_vals, choice;
        const struct spa_pod *vals =
            spa_pod_get_values(&mp->value, &n_vals, &choice);
        const uint64_t *m = SPA_POD_BODY(vals);
        if (n_vals == 0) die("compositor offered no modifiers");
        /* vals[0] of an enum choice is the default and repeats below.
         * Prefer the implicit (tiled) layout: rendering into LINEAR
         * buffers is slow on AMD. */
        uint64_t pick = m[n_vals > 1 ? 1 : 0];
        for (uint32_t i = 0; i < n_vals; i++)
            if (m[i] == DRM_FORMAT_MOD_INVALID) pick = m[i];
        if (getenv("TDCAP_MODIFIER"))
            pick = strtoull(getenv("TDCAP_MODIFIER"), NULL, 0);
        fprintf(stderr, "tdcap: %u modifiers offered, using 0x%llx\n",
                n_vals, (unsigned long long)pick);
        uint8_t fb[2048];
        struct spa_pod_builder fbld = SPA_POD_BUILDER_INIT(fb, sizeof fb);
        const struct spa_pod *fp[2];
        chosen_mod = pick;
        fp[0] = build_format(&fbld, &pick, 1, 1, cur_max_fps);
        fp[1] = build_format(&fbld, offered_mods, N_OFFERED, 0, cur_max_fps);
        pw_stream_update_params(stream, fp, 2);
        return;
    }
    vfmt = info;
    fprintf(stderr,
            "tdcap: format %s %ux%u modifier 0x%llx max %u/%u fps\n",
            spa_debug_type_find_name(spa_type_video_format, info.format),
            info.size.width, info.size.height,
            (unsigned long long)info.modifier, info.max_framerate.num,
            info.max_framerate.denom);

    uint8_t buf[1024];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buf, sizeof buf);
    const struct spa_pod *params[3];
    params[0] = spa_pod_builder_add_object(
        &b, SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
        SPA_PARAM_BUFFERS_buffers, SPA_POD_CHOICE_RANGE_Int(8, 4, 32),
        SPA_PARAM_BUFFERS_blocks, SPA_POD_Int(1),
        SPA_PARAM_BUFFERS_dataType, SPA_POD_Int(1 << SPA_DATA_DmaBuf));
    params[1] = spa_pod_builder_add_object(
        &b, SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
        SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Header),
        SPA_PARAM_META_size, SPA_POD_Int(sizeof(struct spa_meta_header)));
    params[2] = spa_pod_builder_add_object(
        &b, SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
        SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Cursor),
        SPA_PARAM_META_size,
        SPA_POD_CHOICE_RANGE_Int(CURSOR_META_SIZE(CURSOR_MAX, CURSOR_MAX),
                                 CURSOR_META_SIZE(1, 1),
                                 CURSOR_META_SIZE(CURSOR_MAX, CURSOR_MAX)));
    pw_stream_update_params(stream, params, 3);

    if (!have_format) {
        init_graph();
        init_encoder();
        have_format = 1;
        pthread_t t;
        pthread_create(&t, NULL, encoder_thread, NULL);
    }
}

static void on_state_changed(void *data, enum pw_stream_state old,
                             enum pw_stream_state state, const char *error)
{
    (void)data;
    (void)old;
    fprintf(stderr, "tdcap: stream %s%s%s\n", pw_stream_state_as_string(state),
            error ? ": " : "", error ? error : "");
    if (state == PW_STREAM_STATE_STREAMING && !unthrottle_sent) {
        unthrottle_sent = 1;
        cur_max_fps = SPA_FRACTION(RECORD_FPS, 1);
        uint8_t fb[1024];
        struct spa_pod_builder b = SPA_POD_BUILDER_INIT(fb, sizeof fb);
        const struct spa_pod *fp[1];
        fp[0] = build_format(&b, &chosen_mod, 1, 1, cur_max_fps);
        pw_stream_update_params(stream, fp, 1);
    }
    if (state == PW_STREAM_STATE_ERROR ||
        (state == PW_STREAM_STATE_UNCONNECTED && have_format))
        _exit(3);
}

static const struct pw_stream_events stream_events = {
    PW_VERSION_STREAM_EVENTS,
    .state_changed = on_state_changed,
    .param_changed = on_param_changed,
    .process = on_process,
    .remove_buffer = on_remove_buffer,
};

static void start_stream(void)
{
    loop = pw_thread_loop_new("tdcap", NULL);
    struct pw_context *ctx =
        pw_context_new(pw_thread_loop_get_loop(loop), NULL, 0);
    pace_timer = pw_loop_add_timer(pw_thread_loop_get_loop(loop),
                                   on_pace_timer, NULL);
    interval_us = 1000000 / opt.fps;
    pw_thread_loop_lock(loop);
    if (pw_thread_loop_start(loop) < 0) die("cannot start PipeWire loop");
    struct pw_core *core = pw_context_connect(ctx, NULL, 0);
    if (!core) die("cannot connect to PipeWire");

    stream = pw_stream_new(
        core, "tabdisplay",
        pw_properties_new(PW_KEY_MEDIA_TYPE, "Video", PW_KEY_MEDIA_CATEGORY,
                          "Capture", PW_KEY_MEDIA_ROLE, "Screen", NULL));
    pw_stream_add_listener(stream, &stream_listener, &stream_events, NULL);

    uint8_t buf[2048];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buf, sizeof buf);
    const struct spa_pod *params[1];
    params[0] = build_format(&b, offered_mods, N_OFFERED, 0, cur_max_fps);
    if (pw_stream_connect(stream, PW_DIRECTION_INPUT, opt.node,
                          PW_STREAM_FLAG_AUTOCONNECT, params, 1) < 0)
        die("cannot connect stream to node %u", opt.node);
    pw_thread_loop_unlock(loop);
}

int main(int argc, char **argv)
{
    static const struct option longopts[] = {
        { "node", 1, 0, 'n' },   { "size", 1, 0, 's' },
        { "fps", 1, 0, 'f' },    { "codec", 1, 0, 'c' },
        { "rc", 1, 0, 'r' },     { "qp", 1, 0, 'q' },
        { "bitrate", 1, 0, 'b' }, { "maxrate", 1, 0, 'm' },
        { "device", 1, 0, 'd' }, { "refresh", 1, 0, 'R' }, { 0 },
    };
    int c;
    while ((c = getopt_long(argc, argv, "", longopts, NULL)) != -1) {
        switch (c) {
        case 'n': opt.node = strtoul(optarg, NULL, 10); break;
        case 's':
            if (sscanf(optarg, "%dx%d", &opt.width, &opt.height) != 2)
                die("bad --size");
            break;
        case 'f': opt.fps = atoi(optarg); break;
        case 'R':
            opt.mon_den = 1;
            if (sscanf(optarg, "%d/%d", &opt.mon_num, &opt.mon_den) < 1 ||
                opt.mon_num <= 0 || opt.mon_den <= 0)
                die("bad --refresh");
            break;
        case 'c': opt.codec = optarg; break;
        case 'r': opt.rc = optarg; break;
        case 'q': opt.qp = atoi(optarg); break;
        case 'b': opt.bitrate_k = atoi(optarg); break;
        case 'm': opt.maxrate_k = atoi(optarg); break;
        case 'd': opt.device = optarg; break;
        default: die("bad arguments");
        }
    }
    if (!opt.node || !opt.width || !opt.height)
        die("--node and --size are required");

    signal(SIGPIPE, SIG_IGN);
    av_log_set_level(AV_LOG_WARNING);
    cur_max_fps = SPA_FRACTION(opt.mon_num, opt.mon_den);
    pw_init(NULL, NULL);
    start_stream();

    char line[64];
    uint64_t last_stat = now_us();
    while (fgets(line, sizeof line, stdin)) {
        pthread_mutex_lock(&mbox_lock);
        if (!strncmp(line, "idr", 3)) {
            want_idr = 1;
            want_refine = 1;
        } else if (!strncmp(line, "refine", 6)) {
            want_refine = 1;
        } else if (!strncmp(line, "stat", 4)) {
            uint64_t t = now_us();
            fprintf(stderr, "tdcap: captured %llu dropped %llu in %.1fs\n",
                    (unsigned long long)n_captured,
                    (unsigned long long)n_dropped, (t - last_stat) / 1e6);
            n_captured = n_dropped = 0;
            last_stat = t;
        }
        pthread_cond_signal(&mbox_cond);
        pthread_mutex_unlock(&mbox_lock);
    }
    _exit(0);
}
