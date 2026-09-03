package com.tabdisplay;

import android.app.Activity;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.os.Build;
import android.os.Bundle;
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
import java.io.InputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.ByteBuffer;

// NOTE (R6): there is deliberately no android.graphics import in this
// file. Only MediaCodec may produce into the Surface; drawing to it with
// Canvas even once breaks every later MediaCodec.configure() on this
// device (IllegalArgumentException loop). Stream loss is signalled with
// the overlay View, never with pixels.

/**
 * Phase 6 receiver: H.264 Annex B over TCP -> MediaCodec -> Surface.
 *
 * Sender contract (see phase6_spec.md): one AU per frame led by an AUD
 * NAL, fresh SPS+PPS+IDR at every stream start and at least every 0.5 s
 * thereafter, fixed 2560x1600, single client. Cursor is kernel-blended
 * sender-side; this client knows nothing about cursors.
 *
 * Structure: one rx thread owns the socket and one decoder per
 * connection. Anything thrown inside runOnce() ends the session, never
 * the thread: show overlay, sleep on failure, reconnect.
 */
public class ReceiverActivity_v2 extends Activity implements SurfaceHolder.Callback {

    private static final String TAG = "tabdisplay";
    private static final String HOST = "localhost";
    private static final int PORT = 7070;
    private static final int FRAME_W = 2560;
    private static final int FRAME_H = 1600;
    private static final int CONNECT_TIMEOUT_MS = 5000;
    private static final int RECONNECT_SLEEP_MS = 1000;
    private static final int READ_CHUNK = 64 * 1024;
    private static final int ACCUM_MAX = 4 * 1024 * 1024;
    private static final int DEQUEUE_INPUT_TIMEOUT_US = 100_000;
    private static final int TAIL_DRAIN_TIMEOUT_US = 5_000;

    private SurfaceView surfaceView;
    private View blackout;
    private volatile Thread rxThread;
    private volatile boolean running;

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

    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        running = false;
        Thread t = rxThread;
        if (t != null) t.interrupt();
        rxThread = null;
    }

    private final class ReceiverLoop implements Runnable {
        private final Surface surface;

        private long frameCount = 0;
        private long byteCount = 0;
        private long lastLogNs = System.nanoTime();
        private boolean firstFramePending = false;

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
            try (Socket socket = openSocket()) {
                InputStream in = socket.getInputStream();
                Log.i(TAG, "connected to " + HOST + ":" + PORT);

                MediaCodec decoder = createDecoder();
                decoder.start();
                Log.i(TAG, "MediaCodec started (AVC, " + FRAME_W + "x" + FRAME_H
                        + ", Surface)");
                firstFramePending = true;

                try {
                    pump(in, decoder);
                } finally {
                    try { decoder.stop(); } catch (Exception ignore) {}
                    try { decoder.release(); } catch (Exception ignore) {}
                }
            }
        }

        private Socket openSocket() throws IOException {
            Socket s = new Socket();
            s.connect(new InetSocketAddress(HOST, PORT), CONNECT_TIMEOUT_MS);
            s.setTcpNoDelay(true);
            return s;
        }

        private MediaCodec createDecoder() throws IOException {
            MediaFormat fmt = MediaFormat.createVideoFormat(
                    MediaFormat.MIMETYPE_VIDEO_AVC, FRAME_W, FRAME_H);
            fmt.setInteger(MediaFormat.KEY_COLOR_FORMAT,
                    MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
            fmt.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 2 * 1024 * 1024);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                fmt.setInteger(MediaFormat.KEY_LOW_LATENCY, 1);
            }
            MediaCodec decoder =
                    MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC);
            decoder.configure(fmt, surface, null, 0);
            return decoder;
        }

        private void pump(InputStream in, MediaCodec decoder) throws IOException {
            final byte[] accum = new byte[ACCUM_MAX];
            int accumLen = 0;
            final byte[] rxBuf = new byte[READ_CHUNK];
            final MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();

            while (running) {
                int n = in.read(rxBuf, 0, rxBuf.length);
                if (n < 0) {
                    Log.w(TAG, "socket EOF");
                    return;
                }
                byteCount += n;

                if (accumLen + n > ACCUM_MAX) {
                    Log.w(TAG, "accumulator overflow (have " + accumLen
                            + ", adding " + n + "); resetting until next AUD");
                    accumLen = 0;
                }
                System.arraycopy(rxBuf, 0, accum, accumLen, n);
                accumLen += n;

                int firstAud = findAudStart(accum, 0, accumLen);
                if (firstAud < 0) continue;
                if (firstAud > 0) {
                    System.arraycopy(accum, firstAud, accum, 0, accumLen - firstAud);
                    accumLen -= firstAud;
                }

                int audIdx = 0;
                while (true) {
                    int nextAud = findAudStart(accum, audIdx + 4, accumLen);
                    if (nextAud < 0) break;
                    feedAU(decoder, accum, audIdx, nextAud - audIdx);
                    drain(decoder, info, 0);
                    audIdx = nextAud;
                }
                // Burst-tail catch: the per-AU drain above trails one AU
                // behind, so the final decoded frame of a burst would sit
                // unrendered until the next AU — seconds away at idle fps,
                // forever if updates stop. One short blocking wait renders
                // it now.
                drain(decoder, info, TAIL_DRAIN_TIMEOUT_US);

                if (audIdx > 0) {
                    System.arraycopy(accum, audIdx, accum, 0, accumLen - audIdx);
                    accumLen -= audIdx;
                }
            }
        }

        /**
         * Locate an Annex-B NAL of type 9 (Access Unit Delimiter).
         * Returns the start-code index, or -1 if none in [from, to).
         */
        private int findAudStart(byte[] buf, int from, int to) {
            int i = Math.max(0, from);
            int last = to - 4;
            while (i <= last) {
                if (buf[i] == 0 && buf[i + 1] == 0) {
                    if (buf[i + 2] == 1) {                                   // 00 00 01 SC
                        if ((buf[i + 3] & 0x1F) == 9) return i;
                        i += 3;
                        continue;
                    }
                    if (i + 4 < to && buf[i + 2] == 0 && buf[i + 3] == 1) {  // 00 00 00 01 SC
                        if ((buf[i + 4] & 0x1F) == 9) return i;
                        i += 4;
                        continue;
                    }
                }
                i++;
            }
            return -1;
        }

        private void feedAU(MediaCodec decoder, byte[] data, int offset, int len) {
            int inIdx = decoder.dequeueInputBuffer(DEQUEUE_INPUT_TIMEOUT_US);
            if (inIdx < 0) {
                Log.w(TAG, "no input buf; dropping AU of " + len + " B");
                return;
            }
            ByteBuffer inBuf = decoder.getInputBuffer(inIdx);
            if (inBuf == null) return;
            inBuf.clear();
            int toCopy = Math.min(len, inBuf.remaining());
            if (toCopy < len) {
                Log.w(TAG, "AU truncated: " + len + " -> " + toCopy);
            }
            inBuf.put(data, offset, toCopy);
            decoder.queueInputBuffer(inIdx, 0, toCopy,
                    System.nanoTime() / 1000, 0);
        }

        private void drain(MediaCodec decoder, MediaCodec.BufferInfo info,
                           long timeoutUs) {
            while (running) {
                int outIdx = decoder.dequeueOutputBuffer(info, timeoutUs);
                if (outIdx == MediaCodec.INFO_TRY_AGAIN_LATER) return;
                if (outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    Log.i(TAG, "output format changed: " + decoder.getOutputFormat());
                    continue;
                }
                if (outIdx < 0) continue;

                decoder.releaseOutputBuffer(outIdx, true);
                frameCount++;
                if (firstFramePending) {
                    firstFramePending = false;
                    setBlackout(false);
                }

                long now = System.nanoTime();
                if (now - lastLogNs >= 5_000_000_000L) {
                    double secs = (now - lastLogNs) / 1e9;
                    Log.i(TAG, String.format(
                            "decoded %d frames in %.1fs (%.1f fps, %.1f Mbps in)",
                            frameCount, secs, frameCount / secs,
                            (byteCount * 8.0) / 1_000_000.0 / secs));
                    frameCount = 0;
                    byteCount = 0;
                    lastLogNs = now;
                }
            }
        }

        private void sleepQuiet(long ms) {
            try { Thread.sleep(ms); }
            catch (InterruptedException ignore) { Thread.currentThread().interrupt(); }
        }
    }
}
