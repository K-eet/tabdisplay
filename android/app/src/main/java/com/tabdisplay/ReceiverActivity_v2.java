package com.tabdisplay;

import android.app.Activity;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
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

import java.io.IOException;
import java.io.InputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.ByteBuffer;

/**
 * Phase 3v2 receiver: stream of H.264 Annex B bytes from the laptop -> MediaCodec
 * hardware decoder -> Surface (zero-copy to SurfaceFlinger).
 *
 * The decoder is configured with a Surface output, which means decoded frames
 * land directly in the display compositor's GraphicBuffer. There is no Bitmap,
 * no Canvas, no Java pixel data path. We only push encoded bytes in and tell
 * MediaCodec to release each output buffer with render=true.
 */
public class ReceiverActivity_v2 extends Activity implements SurfaceHolder.Callback {

    private static final String TAG = "tabdisplay";
    private static final String HOST = "localhost";
    private static final int PORT = 7070;
    private static final int FRAME_W = 2560;
    private static final int FRAME_H = 1600;
    // Socket read granularity. We accumulate into a 4 MB working buffer and
    // parse out Annex-B Access Units (delimited by AUD NALs, type 9), so the
    // chunk size here is purely an I/O efficiency knob now — it no longer
    // affects correctness the way it did before AU-aware framing.
    private static final int READ_CHUNK = 64 * 1024;
    private static final int ACCUM_MAX = 4 * 1024 * 1024;

    private SurfaceView surfaceView;
    private View blackout;
    private volatile Thread rxThread;
    private volatile boolean running;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        // SurfaceView + an opaque cover View. Stream loss is signalled by
        // showing the cover — NOT by drawing to the surface: once Canvas
        // has produced into the SurfaceView's buffer queue, MediaCodec
        // configure() on that Surface fails (observed on One UI), and
        // mixing producers on one BufferQueue is undefined anyway.
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
        runOnUiThread(() -> blackout.setVisibility(show ? View.VISIBLE
                                                        : View.GONE));
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
                            + ": " + e.getMessage() + " — reconnecting in 1s");
                }
                if (!running) return;
                // Cover the (stale) last frame with the blackout View until
                // the reconnected stream renders its first frame.
                setBlackout(true);
                if (failed) sleepQuiet(1000);
            }
        }

        private void runOnce() throws IOException {
            try (Socket socket = openSocket()) {
                InputStream in = socket.getInputStream();
                Log.i(TAG, "connected to " + HOST + ":" + PORT);

                MediaCodec decoder = createDecoder();
                decoder.start();
                Log.i(TAG, "MediaCodec started (AVC, " + FRAME_W + "x" + FRAME_H + ", Surface)");
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
            s.connect(new InetSocketAddress(HOST, PORT), 5000);
            s.setTcpNoDelay(true);
            return s;
        }

        private MediaCodec createDecoder() throws IOException {
            MediaFormat fmt = MediaFormat.createVideoFormat(
                    MediaFormat.MIMETYPE_VIDEO_AVC, FRAME_W, FRAME_H);
            fmt.setInteger(MediaFormat.KEY_COLOR_FORMAT,
                    MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
            // 2 MB is comfortable for an IDR at 10 Mbps (each keyframe is
            // ~30 KB-300 KB depending on content); avoids dynamic input-buffer
            // growth which some MediaCodec implementations don't support.
            fmt.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 2 * 1024 * 1024);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                // Decode-and-display without reorder queues: visibly reduces
                // glass-to-glass latency (cursor afterimages drain faster).
                fmt.setInteger(MediaFormat.KEY_LOW_LATENCY, 1);
            }

            MediaCodec decoder = MediaCodec.createDecoderByType(
                    MediaFormat.MIMETYPE_VIDEO_AVC);
            decoder.configure(fmt, surface, null, 0);
            return decoder;
        }

        // Stats updated from drainOutput().
        private long frameCount = 0;
        private long byteCount = 0;
        private long lastLogNs = System.nanoTime();
        private boolean firstFramePending = false;

        private void pump(InputStream in, MediaCodec decoder) throws IOException {
            // AU-aware framing: accumulate Annex-B bytes, split on AUD start
            // codes, feed one complete access unit per queueInputBuffer call.
            // That's what MediaCodec expects per its docs. The prior chunked
            // path occasionally split NALs (especially big IDRs) which
            // corrupted the GOP -> intermittent artifact bursts.
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
                    firstAud = 0;
                }

                int audIdx = firstAud;
                while (true) {
                    int nextAud = findAudStart(accum, audIdx + 4, accumLen);
                    if (nextAud < 0) break;
                    feedAccessUnit(decoder, accum, audIdx, nextAud - audIdx);
                    drainOutput(decoder, info);
                    audIdx = nextAud;
                }

                // The per-AU drain above always trails one AU behind: an
                // output becomes releasable only after the NEXT input is
                // queued. If a burst ends here (idle follows), its final
                // decoded frame would sit unrendered until the next AU —
                // seconds later at idle fps, forever if updates stop (seen
                // as a frozen cursor afterimage). One short blocking wait
                // catches the tail output of the burst.
                {
                    int outIdx = decoder.dequeueOutputBuffer(info, 5_000);
                    if (outIdx >= 0) {
                        decoder.releaseOutputBuffer(outIdx, true);
                        frameCount++;
                        if (firstFramePending) {
                            firstFramePending = false;
                            setBlackout(false);
                        }
                    } else if (outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                        Log.i(TAG, "output format changed: " + decoder.getOutputFormat());
                    }
                }

                if (audIdx > 0) {
                    System.arraycopy(accum, audIdx, accum, 0, accumLen - audIdx);
                    accumLen -= audIdx;
                }
            }
        }

        /**
         * Locate the start of an Annex-B NAL whose type is 9 (Access Unit
         * Delimiter). Returns the index of the first byte of the start-code
         * prefix (3- or 4-byte form), or -1 if none found in [from, to).
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

        private void feedAccessUnit(MediaCodec decoder, byte[] data,
                                    int offset, int len) {
            // 100 ms: during fast cursor sweeps AUs arrive at full frame
            // rate and input buffers recycle at decode speed. The old 20 ms
            // timeout dropped AUs under burst, breaking the reference chain
            // — with a wall-clock IDR every 0.5 s that now self-heals in
            // half a second, but not dropping at all is better.
            int inIdx = decoder.dequeueInputBuffer(100_000);
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

        private void drainOutput(MediaCodec decoder, MediaCodec.BufferInfo info) {
            while (running) {
                int outIdx = decoder.dequeueOutputBuffer(info, 0);
                if (outIdx == MediaCodec.INFO_TRY_AGAIN_LATER) return;
                if (outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    Log.i(TAG, "output format changed: " + decoder.getOutputFormat());
                    continue;
                }
                if (outIdx < 0) continue;

                decoder.releaseOutputBuffer(outIdx, true);    // render to Surface
                frameCount++;
                if (firstFramePending) {
                    firstFramePending = false;
                    setBlackout(false);       // stream is visibly alive again
                }

                long now = System.nanoTime();
                if (now - lastLogNs >= 5_000_000_000L) {
                    double secs = (now - lastLogNs) / 1e9;
                    double fps = frameCount / secs;
                    double mbps = (byteCount * 8.0) / 1_000_000.0 / secs;
                    Log.i(TAG, String.format(
                            "decoded %d frames in %.1fs (%.1f fps, %.1f Mbps in)",
                            frameCount, secs, fps, mbps));
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
