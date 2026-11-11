package com.tabdisplay;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaCodecList;
import android.media.MediaFormat;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.util.DisplayMetrics;
import android.util.Log;
import android.view.Display;
import android.view.Gravity;
import android.view.Surface;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.TextView;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

/**
 * The tablet half of tabdisplay: behaves like a monitor.
 *
 * Connects to the host daemon through `adb reverse` (localhost:7070),
 * reports the panel size, then shows what it is sent. Video goes
 * MediaCodec -> SurfaceView with no pacing of our own: every decoded frame
 * is handed to the display at once and acknowledged to the host. The
 * pointer is not part of the video; it is a small view moved on top of it,
 * so pointer motion costs one tiny message instead of an encode + decode.
 *
 * Nothing but MediaCodec ever draws into the video Surface: on this device
 * a Surface that has been drawn to with Canvas rejects every later
 * MediaCodec.configure(). Status text and the pointer are separate views.
 */
public class DisplayActivity extends Activity implements SurfaceHolder.Callback {

    private static final String TAG = "tabdisplay";
    private static final String HOST = "localhost";
    private static final int PORT = 7070;
    private static final int RETRY_MS = 300;
    private static final int FRAME_RATE = 60;
    // The decoder's clocks follow the rate it is told to expect, and left
    // alone it assumes 30 fps. Declaring more than the real rate buys
    // shorter per-frame decode time at some cost in power.
    private static final int OPERATING_RATE = 120;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private SurfaceView video;
    private ImageView pointer;
    private TextView status;

    private volatile boolean running;
    private volatile Thread linkThread;
    private volatile Socket socket;

    // Pointer state: written by the link thread, applied on the UI thread.
    private final Object pointerLock = new Object();
    private int ptrX, ptrY, hotX, hotY;
    private boolean ptrVisible;
    private Bitmap ptrSprite;
    private boolean ptrSpriteChanged;
    private boolean ptrUpdateQueued;
    private volatile float streamToView = 1f;

    /** Unplugging the cable is how a monitor is turned off. */
    private final BroadcastReceiver unplugged = new BroadcastReceiver() {
        @Override
        public void onReceive(Context c, Intent i) {
            Log.i(TAG, "power disconnected; closing");
            finishAndRemoveTask();
        }
    };

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        Window w = getWindow();
        w.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        w.getAttributes().layoutInDisplayCutoutMode =
                WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);

        video = new SurfaceView(this);
        video.getHolder().addCallback(this);
        root.addView(video, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));

        // The window declares the area under a SurfaceView transparent and
        // the system composer discards whatever the window draws there,
        // except inside the *layout* bounds of views that say they draw.
        // The pointer is moved by translation, which those bounds do not
        // follow, so it would only ever show in its home corner. A
        // full-screen parent that claims to draw keeps the whole window
        // composited over the video.
        FrameLayout overlay = new FrameLayout(this);
        overlay.setWillNotDraw(false);
        root.addView(overlay, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));

        pointer = new ImageView(this);
        pointer.setVisibility(View.GONE);
        overlay.addView(pointer, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT,
                FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.TOP | Gravity.LEFT));

        status = new TextView(this);
        status.setTextColor(0xFF808080);
        status.setTextSize(18);
        status.setGravity(Gravity.CENTER);
        status.setBackgroundColor(Color.BLACK);
        status.setText("tabdisplay\nwaiting for the computer");
        root.addView(status, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));

        setContentView(root);
        hideSystemBars();
        registerReceiver(unplugged, new IntentFilter(Intent.ACTION_POWER_DISCONNECTED));
    }

    @Override
    protected void onDestroy() {
        unregisterReceiver(unplugged);
        super.onDestroy();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) hideSystemBars();
    }

    private void hideSystemBars() {
        Window w = getWindow();
        w.setDecorFitsSystemWindows(false);
        WindowInsetsController c = w.getInsetsController();
        if (c != null) {
            c.hide(WindowInsets.Type.systemBars());
            c.setSystemBarsBehavior(
                    WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
        }
    }

    private void showStatus(final boolean show) {
        ui.post(() -> {
            status.setVisibility(show ? View.VISIBLE : View.GONE);
            if (show) pointer.setVisibility(View.GONE);
        });
    }

    // ------------------------------------------------------------------
    // Surface lifecycle: the link (socket + decoder) lives exactly as long
    // as the Surface does.
    // ------------------------------------------------------------------

    @Override
    public void surfaceCreated(SurfaceHolder holder) {
        running = true;
        Thread t = new Thread(new Link(holder.getSurface()), "link");
        linkThread = t;
        t.start();
    }

    @Override
    public void surfaceChanged(SurfaceHolder holder, int format, int w, int h) {
        Log.i(TAG, "surface " + w + "x" + h);
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        running = false;
        Thread t = linkThread;
        linkThread = null;
        closeQuietly(socket);       // the only thing that unblocks a read
        if (t == null) return;
        t.interrupt();
        try {
            t.join(1500);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        if (t.isAlive()) Log.w(TAG, "link thread outlived its surface");
    }

    private static void closeQuietly(Socket s) {
        if (s == null) return;
        try { s.close(); } catch (IOException ignore) { }
    }

    /** Panel size in pixels, long side first (the activity is landscape). */
    private int[] panelSize() {
        Display.Mode m = getDisplay().getMode();
        int a = m.getPhysicalWidth(), b = m.getPhysicalHeight();
        return new int[] {Math.max(a, b), Math.min(a, b)};
    }

    private static int supportedCodecs() {
        int mask = 0;
        MediaCodecList list = new MediaCodecList(MediaCodecList.REGULAR_CODECS);
        if (list.findDecoderForFormat(MediaFormat.createVideoFormat(
                MediaFormat.MIMETYPE_VIDEO_AVC, 1920, 1080)) != null) {
            mask |= Wire.CODEC_H264;
        }
        if (list.findDecoderForFormat(MediaFormat.createVideoFormat(
                MediaFormat.MIMETYPE_VIDEO_HEVC, 1920, 1080)) != null) {
            mask |= Wire.CODEC_HEVC;
        }
        return mask;
    }

    // ------------------------------------------------------------------
    // Pointer overlay
    // ------------------------------------------------------------------

    private void queuePointerUpdate() {
        // Caller holds pointerLock. One pending runnable is enough: it
        // applies whatever is newest when it runs.
        if (ptrUpdateQueued) return;
        ptrUpdateQueued = true;
        ui.postAtFrontOfQueue(this::applyPointer);
    }

    private void applyPointer() {
        int x, y;
        boolean visible;
        Bitmap sprite = null;
        synchronized (pointerLock) {
            ptrUpdateQueued = false;
            x = ptrX - hotX;
            y = ptrY - hotY;
            visible = ptrVisible && ptrSprite != null;
            if (ptrSpriteChanged) {
                sprite = ptrSprite;
                ptrSpriteChanged = false;
            }
        }
        float k = streamToView;
        if (sprite != null) {
            pointer.setImageBitmap(sprite);
            pointer.setPivotX(0);
            pointer.setPivotY(0);
            pointer.setScaleX(k);
            pointer.setScaleY(k);
        }
        pointer.setTranslationX(x * k);
        pointer.setTranslationY(y * k);
        int want = visible && status.getVisibility() != View.VISIBLE
                ? View.VISIBLE : View.GONE;
        if (pointer.getVisibility() != want) pointer.setVisibility(want);
    }

    private void onPointerMessage(ByteBuffer b) {
        synchronized (pointerLock) {
            ptrX = b.getInt();
            ptrY = b.getInt();
            ptrVisible = b.get() != 0;
            queuePointerUpdate();
        }
    }

    private void onSpriteMessage(ByteBuffer b) {
        int w = b.getInt(), h = b.getInt(), hx = b.getInt(), hy = b.getInt();
        Bitmap bmp = null;
        if (w > 0 && h > 0 && b.remaining() >= w * h * 4) {
            // The compositor's sprite is premultiplied RGBA, which is
            // exactly ARGB_8888's in-memory layout.
            bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
            bmp.copyPixelsFromBuffer(b.slice());
        }
        synchronized (pointerLock) {
            ptrSprite = bmp;
            ptrSpriteChanged = true;
            hotX = hx;
            hotY = hy;
            queuePointerUpdate();
        }
    }

    // ------------------------------------------------------------------
    // Link: socket + decoder
    // ------------------------------------------------------------------

    private final class Link implements Runnable {
        private final Surface surface;

        Link(Surface surface) {
            this.surface = surface;
        }

        @Override
        public void run() {
            while (running) {
                try {
                    session();
                } catch (Exception e) {
                    if (!running) return;
                    Log.w(TAG, "link: " + e.getClass().getSimpleName() + ": "
                            + e.getMessage());
                }
                if (!running) return;
                showStatus(true);
                try {
                    Thread.sleep(RETRY_MS);
                } catch (InterruptedException e) {
                    return;
                }
            }
        }

        private void session() throws IOException {
            Socket s = new Socket();
            socket = s;
            HandlerThread codecThread = null;
            MediaCodec decoder = null;
            try {
                if (!running) return;
                s.setTcpNoDelay(true);
                s.connect(new InetSocketAddress(HOST, PORT), 2000);
                OutputStream out = s.getOutputStream();
                int[] size = panelSize();
                DisplayMetrics dm = getResources().getDisplayMetrics();
                out.write(Wire.hello(size[0], size[1], Math.round(dm.xdpi),
                        supportedCodecs()));
                out.flush();

                Wire wire = new Wire(s.getInputStream());
                if (!wire.next() || wire.type() != 'C') {
                    throw new IOException("no configuration from host");
                }
                ByteBuffer c = wire.view();
                int version = c.getShort();
                final int width = c.getInt(), height = c.getInt();
                int codec = c.get();
                if (version != Wire.VERSION) {
                    throw new IOException("host speaks v" + version);
                }
                Log.i(TAG, "connected: " + width + "x" + height
                        + (codec == 1 ? " HEVC" : " H.264"));
                ui.post(() -> {
                    // The buffer is the stream's size; if the view differs
                    // the compositor scales, and the pointer follows.
                    video.getHolder().setFixedSize(width, height);
                    streamToView = video.getWidth() > 0
                            ? (float) video.getWidth() / width : 1f;
                });

                codecThread = new HandlerThread("codec");
                codecThread.start();
                Decoder cb = new Decoder(out);
                decoder = createDecoder(codec, width, height, cb,
                        new Handler(codecThread.getLooper()));
                decoder.start();

                while (running && wire.next()) {
                    switch (wire.type()) {
                        case 'V':
                            cb.feed(decoder, wire);
                            break;
                        case 'P':
                            onPointerMessage(wire.view());
                            break;
                        case 'S':
                            onSpriteMessage(wire.view());
                            break;
                        case 'T':
                            cb.send(Wire.echo(wire));
                            break;
                        default:
                            break;      // newer host: ignore what we don't know
                    }
                }
                Log.i(TAG, "host closed the stream");
            } finally {
                if (decoder != null) {
                    try { decoder.stop(); } catch (Exception ignore) { }
                    try { decoder.release(); } catch (Exception ignore) { }
                }
                if (codecThread != null) codecThread.quitSafely();
                socket = null;
                closeQuietly(s);
            }
        }

        private MediaCodec createDecoder(int codec, int width, int height,
                                         MediaCodec.Callback cb, Handler handler)
                throws IOException {
            String mime = codec == 1 ? MediaFormat.MIMETYPE_VIDEO_HEVC
                    : MediaFormat.MIMETYPE_VIDEO_AVC;
            MediaFormat fmt = MediaFormat.createVideoFormat(mime, width, height);
            fmt.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 4 * 1024 * 1024);
            // Ask for frames out as soon as they are decodable, at the
            // highest clock the SoC offers. The vendor keys are Qualcomm's
            // spelling of the same request; unknown keys are ignored.
            fmt.setInteger(MediaFormat.KEY_LOW_LATENCY, 1);
            fmt.setInteger(MediaFormat.KEY_PRIORITY, 0);
            fmt.setInteger(MediaFormat.KEY_FRAME_RATE, FRAME_RATE);
            fmt.setInteger(MediaFormat.KEY_OPERATING_RATE, OPERATING_RATE);
            fmt.setInteger("vendor.qti-ext-dec-low-latency.enable", 1);
            fmt.setInteger("vendor.qti-ext-dec-picture-order.enable", 1);
            // Prefer a decoder that advertises low-latency operation; on
            // Qualcomm that is a separate component (…decoder.low_latency).
            MediaFormat probe = MediaFormat.createVideoFormat(mime, width, height);
            probe.setFeatureEnabled(
                    MediaCodecInfo.CodecCapabilities.FEATURE_LowLatency, true);
            String name = new MediaCodecList(MediaCodecList.REGULAR_CODECS)
                    .findDecoderForFormat(probe);
            MediaCodec decoder = name != null
                    ? MediaCodec.createByCodecName(name)
                    : MediaCodec.createDecoderByType(mime);
            try {
                decoder.setCallback(cb, handler);   // selects async mode
                decoder.configure(fmt, surface, null, 0);
            } catch (RuntimeException e) {
                decoder.release();
                throw e;
            }
            Log.i(TAG, "decoder " + decoder.getName());
            return decoder;
        }
    }

    /**
     * One decoder's callbacks (codec thread) plus the feeding side (link
     * thread). Free input buffers travel between the two through a queue.
     */
    private final class Decoder extends MediaCodec.Callback {
        private final OutputStream out;
        private final LinkedBlockingQueue<Integer> freeInputs = new LinkedBlockingQueue<>();
        private final ConcurrentHashMap<Long, Long> arrivedNs = new ConcurrentHashMap<>();
        private volatile MediaCodec.CodecException error;
        private boolean first = true;

        Decoder(OutputStream out) {
            this.out = out;
        }

        /** Queue the access unit in {@code wire}; waits for an input buffer. */
        void feed(MediaCodec decoder, Wire wire) throws IOException {
            Integer idx = null;
            while (idx == null) {
                if (!running) return;
                if (error != null) throw new IOException("decoder error", error);
                try {
                    idx = freeInputs.poll(100, TimeUnit.MILLISECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    throw new IOException("interrupted");
                }
            }
            ByteBuffer v = wire.view();
            long ts = v.getLong();
            int len = wire.length() - Wire.VIDEO_HEADER;
            ByteBuffer in = decoder.getInputBuffer(idx);
            if (in == null || len > in.capacity()) {
                throw new IOException("access unit of " + len + " B does not fit");
            }
            in.clear();
            in.put(wire.payload(), Wire.VIDEO_HEADER, len);
            arrivedNs.put(ts, System.nanoTime());
            decoder.queueInputBuffer(idx, 0, len, ts, 0);
        }

        /** Callable from either thread. */
        void send(byte[] message) {
            try {
                synchronized (out) {
                    out.write(message);
                }
            } catch (IOException ignore) {
                // the link thread will notice the dead socket
            }
        }

        @Override
        public void onInputBufferAvailable(MediaCodec codec, int index) {
            freeInputs.offer(index);
        }

        @Override
        public void onOutputBufferAvailable(MediaCodec codec, int index,
                                            MediaCodec.BufferInfo info) {
            long now = System.nanoTime();
            try {
                // An explicit "now" timestamp: show at the next vsync, and
                // if two frames are due at the same vsync the display
                // keeps the newer one instead of queueing both.
                codec.releaseOutputBuffer(index, now);
            } catch (IllegalStateException e) {
                return;             // codec is being stopped
            }
            Long arrived = arrivedNs.remove(info.presentationTimeUs);
            int held = arrived == null ? 0 : (int) ((now - arrived) / 1000);
            send(Wire.ack(info.presentationTimeUs, held));
            if (first) {
                first = false;
                showStatus(false);
            }
            if (arrivedNs.size() > 64) arrivedNs.clear();   // frames the codec dropped
        }

        @Override
        public void onError(MediaCodec codec, MediaCodec.CodecException e) {
            Log.w(TAG, "decoder error: " + e.getDiagnosticInfo());
            error = e;
        }

        @Override
        public void onOutputFormatChanged(MediaCodec codec, MediaFormat format) {
            Log.i(TAG, "output format: " + format);
        }
    }
}
