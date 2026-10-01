package com.tabdisplay;

import android.app.Activity;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Log;
import android.view.Surface;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.widget.FrameLayout;

import java.io.IOException;
import java.io.InterruptedIOException;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

// NOTE (R6): there is deliberately no android.graphics import in this
// file. Only MediaCodec may produce into the Surface; drawing to it with
// Canvas even once breaks every later MediaCodec.configure() on this
// device (IllegalArgumentException loop). Stream loss is signalled with
// the overlay View, never with pixels.

/**
 * Receiver: length-prefixed H.264 access units over TCP -> MediaCodec ->
 * Surface.
 *
 * Sender contract (daemon_v2.py): the stream is a sequence of
 * [u32 big-endian length][one Annex B access unit]; the first AU of every
 * connection is SPS+PPS+IDR, with another IDR at least every 0.5 s of
 * wall-clock time; fixed 2560x1600; single client. Cursor is
 * kernel-blended sender-side; this client knows nothing about cursors.
 *
 * Structure: one rx thread owns the socket and one decoder per
 * connection. It only reads frames and queues them. The decoder runs in
 * asynchronous mode, so every decoded frame is rendered from the codec's
 * own callback the instant it is ready — nothing polls for output, and a
 * frame never waits on the next one. Anything thrown inside runOnce() ends
 * the session, never the thread: show overlay, sleep on failure,
 * reconnect.
 */
public class ReceiverActivity_v2 extends Activity implements SurfaceHolder.Callback {

    private static final String TAG = "tabdisplay";
    private static final String HOST = "localhost";
    private static final int PORT = 7070;
    private static final int FRAME_W = 2560;
    private static final int FRAME_H = 1600;
    private static final int CONNECT_TIMEOUT_MS = 5000;
    private static final int RECONNECT_SLEEP_MS = 1000;
    private static final int INPUT_WAIT_MS = 100;
    private static final int STOP_JOIN_MS = 1000;

    private SurfaceView surfaceView;
    private View blackout;
    private volatile Thread rxThread;
    private volatile boolean running;
    // The rx thread's current socket, published so surfaceDestroyed can
    // close it: closing is the only thing that unblocks a socket read
    // (Thread.interrupt() does not).
    private volatile Socket socket;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        FrameLayout frame = new FrameLayout(this);
        surfaceView = new SurfaceView(this);
        frame.addView(surfaceView, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));
        blackout = new View(this);
        blackout.setBackgroundColor(0xFF000000);
        blackout.setVisibility(View.GONE);
        frame.addView(blackout, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));
        surfaceView.getHolder().addCallback(this);
        setContentView(frame);
        hideSystemBars();
    }

    private void setBlackout(final boolean show) {
        runOnUiThread(() -> blackout.setVisibility(
                show ? View.VISIBLE : View.GONE));
    }

    private void hideSystemBars() {
        Window window = getWindow();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            window.setDecorFitsSystemWindows(false);
            WindowInsetsController c = window.getInsetsController();
            if (c != null) {
                c.hide(WindowInsets.Type.statusBars() | WindowInsets.Type.navigationBars());
                c.setSystemBarsBehavior(
                        WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        }
    }

    @Override
    public void surfaceCreated(SurfaceHolder holder) {
        running = true;
        rxThread = new Thread(new ReceiverLoop(holder.getSurface()), "frame-rx");
        rxThread.start();
    }

    @Override
    public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {
        Log.i(TAG, "surfaceChanged " + width + "x" + height + " fmt=" + format);
    }

    /**
     * The Surface is invalid once this returns, so the rx thread and its
     * decoder must be gone by then — and its socket closed, or the next
     * surfaceCreated would open a second connection alongside it.
     */
    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        running = false;
        Thread t = rxThread;
        rxThread = null;
        closeQuietly(socket);
        if (t == null) return;
        t.interrupt();
        try {
            t.join(STOP_JOIN_MS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        if (t.isAlive()) Log.w(TAG, "rx thread still running after surface loss");
    }

    private static void closeQuietly(Socket s) {
        if (s == null) return;
        try { s.close(); } catch (IOException ignore) {}
    }

    private final class ReceiverLoop implements Runnable {
        private final Surface surface;

        // Written by the rx thread, read by the codec callback thread.
        private final AtomicLong byteCount = new AtomicLong();
        // Codec callback thread only.
        private long frameCount = 0;
        private long lastLogNs = System.nanoTime();

        ReceiverLoop(Surface s) {
            this.surface = s;
        }

        @Override
        public void run() {
            while (running) {
                boolean failed = false;
                try {
                    runOnce();
                    Log.i(TAG, "session ended (EOF)");
                } catch (Exception e) {
                    if (!running) return;
                    failed = true;
                    Log.w(TAG, "rx loop error: " + e.getClass().getSimpleName()
                            + ": " + e.getMessage() + " — reconnecting");
                }
                if (!running) return;
                setBlackout(true);
                if (failed) sleepQuiet(RECONNECT_SLEEP_MS);
            }
        }

        private void runOnce() throws IOException {
            Socket s = new Socket();
            socket = s;
            HandlerThread codecThread = new HandlerThread("codec-cb");
            try {
                // Re-check after publishing the socket: either we see the
                // stop here, or surfaceDestroyed sees the socket and closes it.
                if (!running) return;
                s.connect(new InetSocketAddress(HOST, PORT), CONNECT_TIMEOUT_MS);
                s.setTcpNoDelay(true);
                Log.i(TAG, "connected to " + HOST + ":" + PORT);
                FrameReader reader = new FrameReader(s.getInputStream());

                codecThread.start();
                Session session = new Session();
                MediaCodec decoder = createDecoder(
                        session, new Handler(codecThread.getLooper()));
                try {
                    decoder.start();
                    Log.i(TAG, "MediaCodec started (AVC, " + FRAME_W + "x" + FRAME_H
                            + ", Surface)");
                    pump(reader, decoder, session);
                } finally {
                    try { decoder.stop(); } catch (Exception ignore) {}
                    try { decoder.release(); } catch (Exception ignore) {}
                }
            } finally {
                codecThread.quitSafely();
                socket = null;
                closeQuietly(s);
            }
        }

        private MediaCodec createDecoder(Session session, Handler handler)
                throws IOException {
            MediaFormat fmt = MediaFormat.createVideoFormat(
                    MediaFormat.MIMETYPE_VIDEO_AVC, FRAME_W, FRAME_H);
            fmt.setInteger(MediaFormat.KEY_COLOR_FORMAT,
                    MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
            fmt.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, FrameReader.MAX_AU);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                fmt.setInteger(MediaFormat.KEY_LOW_LATENCY, 1);
            }
            MediaCodec decoder =
                    MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC);
            try {
                // Must precede configure(): this is what selects async mode.
                decoder.setCallback(session, handler);
                decoder.configure(fmt, surface, null, 0);
            } catch (RuntimeException e) {
                decoder.release();
                throw e;
            }
            return decoder;
        }

        private void pump(FrameReader reader, MediaCodec decoder, Session session)
                throws IOException {
            while (running) {
                int len = reader.next();
                if (len < 0) {
                    Log.w(TAG, "socket EOF");
                    return;
                }
                byteCount.addAndGet(len + 4);
                feedAU(decoder, session, reader.buffer(), len);
            }
        }

        /**
         * Queue one access unit, waiting as long as it takes for a free
         * input buffer. Never drops: a dropped AU breaks the reference
         * chain until the next IDR, while waiting only pushes back on TCP.
         */
        private void feedAU(MediaCodec decoder, Session session, byte[] data, int len)
                throws IOException {
            Integer inIdx = null;
            while (inIdx == null) {
                if (!running) return;
                if (session.error != null) {
                    throw new IOException("decoder error", session.error);
                }
                try {
                    inIdx = session.freeInputs.poll(INPUT_WAIT_MS, TimeUnit.MILLISECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    throw new InterruptedIOException("interrupted waiting for input buffer");
                }
            }
            ByteBuffer inBuf = decoder.getInputBuffer(inIdx);
            if (inBuf == null || len > inBuf.capacity()) {
                // Ending the session makes the sender open with a fresh IDR.
                throw new IOException("AU of " + len + " B does not fit input buffer");
            }
            inBuf.clear();
            inBuf.put(data, 0, len);
            decoder.queueInputBuffer(inIdx, 0, len, System.nanoTime() / 1000, 0);
        }

        /**
         * One decoder's callbacks, run on the codec-cb thread. Free input
         * buffer indices are handed to the rx thread through a queue;
         * output buffers are rendered right here.
         */
        private final class Session extends MediaCodec.Callback {
            final LinkedBlockingQueue<Integer> freeInputs = new LinkedBlockingQueue<>();
            volatile MediaCodec.CodecException error;
            private boolean firstFramePending = true;

            @Override
            public void onInputBufferAvailable(MediaCodec codec, int index) {
                freeInputs.offer(index);
            }

            @Override
            public void onOutputBufferAvailable(MediaCodec codec, int index,
                                                MediaCodec.BufferInfo info) {
                try {
                    codec.releaseOutputBuffer(index, true);
                } catch (IllegalStateException e) {
                    return;                 // codec is being stopped
                }
                frameCount++;
                if (firstFramePending) {
                    firstFramePending = false;
                    setBlackout(false);
                }
                logStats();
            }

            @Override
            public void onError(MediaCodec codec, MediaCodec.CodecException e) {
                Log.w(TAG, "decoder error: " + e.getDiagnosticInfo());
                error = e;
            }

            @Override
            public void onOutputFormatChanged(MediaCodec codec, MediaFormat format) {
                Log.i(TAG, "output format changed: " + format);
            }
        }

        private void logStats() {
            long now = System.nanoTime();
            if (now - lastLogNs < 5_000_000_000L) return;
            double secs = (now - lastLogNs) / 1e9;
            Log.i(TAG, String.format(
                    "decoded %d frames in %.1fs (%.1f fps, %.1f Mbps in)",
                    frameCount, secs, frameCount / secs,
                    (byteCount.getAndSet(0) * 8.0) / 1_000_000.0 / secs));
            frameCount = 0;
            lastLogNs = now;
        }

        private void sleepQuiet(long ms) {
            try { Thread.sleep(ms); }
            catch (InterruptedException ignore) { Thread.currentThread().interrupt(); }
        }
    }
}
