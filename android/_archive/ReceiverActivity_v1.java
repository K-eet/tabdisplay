package com.tabdisplay;

import android.app.Activity;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;

import java.io.DataInputStream;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.ByteBuffer;

public class ReceiverActivity extends Activity implements SurfaceHolder.Callback {

    private static final String TAG = "tabdisplay";
    private static final String HOST = "localhost";
    private static final int PORT = 7070;
    private static final int FRAME_WIDTH = 2560;
    private static final int FRAME_HEIGHT = 1600;
    private static final int FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT * 4;
    private static final int MAX_RECTS = 64;

    private SurfaceView surfaceView;
    private volatile Thread receiverThread;
    private volatile boolean running;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        surfaceView = new SurfaceView(this);
        SurfaceHolder holder = surfaceView.getHolder();
        // RGBA_8888 surface so our ARGB_8888 backing bitmap isn't dithered
        // down to RGB_565 by SurfaceFlinger (observed during Phase 2).
        holder.setFormat(PixelFormat.RGBA_8888);
        holder.addCallback(this);
        setContentView(surfaceView);

        hideSystemBars();
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
        } else {
            window.getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                            | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                            | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
        }
    }

    @Override
    public void surfaceCreated(SurfaceHolder holder) {
        running = true;
        receiverThread = new Thread(new ReceiverLoop(holder), "frame-rx");
        receiverThread.start();
    }

    @Override
    public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {
        Log.i(TAG, "surfaceChanged " + width + "x" + height + " fmt=" + format);
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        running = false;
        Thread t = receiverThread;
        if (t != null) t.interrupt();
        receiverThread = null;
    }

    private final class ReceiverLoop implements Runnable {
        private final SurfaceHolder holder;
        private final byte[] payload = new byte[FRAME_BYTES];
        private Bitmap backing;       // persistent virtual-screen mirror
        private Canvas backingCanvas;
        // tempBmp is allocated at full frame size and reconfigure()'d per rect,
        // so we never need a fresh allocation no matter how rect sizes vary.
        private Bitmap tempBmp;

        ReceiverLoop(SurfaceHolder h) {
            this.holder = h;
        }

        @Override
        public void run() {
            backing = Bitmap.createBitmap(FRAME_WIDTH, FRAME_HEIGHT, Bitmap.Config.ARGB_8888);
            backingCanvas = new Canvas(backing);
            tempBmp = Bitmap.createBitmap(FRAME_WIDTH, FRAME_HEIGHT, Bitmap.Config.ARGB_8888);

            while (running) {
                try (Socket socket = openSocket()) {
                    DataInputStream in = new DataInputStream(socket.getInputStream());
                    Log.i(TAG, "connected to " + HOST + ":" + PORT);
                    while (running) {
                        int numRects = in.readInt();
                        if (numRects < 1 || numRects > MAX_RECTS) {
                            Log.w(TAG, "bad numRects " + numRects + " — reconnecting");
                            break;
                        }
                        for (int i = 0; i < numRects; i++) {
                            readAndApplyRect(in);
                        }
                        present();
                    }
                } catch (IOException e) {
                    if (!running) return;
                    Log.w(TAG, "rx loop error: " + e.getMessage() + " — retrying in 1s");
                    try {
                        Thread.sleep(1000);
                    } catch (InterruptedException ie) {
                        return;
                    }
                }
            }
        }

        private Socket openSocket() throws IOException {
            Socket s = new Socket();
            s.connect(new InetSocketAddress(HOST, PORT), 5000);
            s.setTcpNoDelay(true);
            return s;
        }

        private void readAndApplyRect(DataInputStream in) throws IOException {
            int x1 = in.readInt();
            int y1 = in.readInt();
            int x2 = in.readInt();
            int y2 = in.readInt();
            int payloadLen = in.readInt();

            int rw = x2 - x1;
            int rh = y2 - y1;
            boolean valid = rw > 0 && rh > 0
                    && x1 >= 0 && y1 >= 0
                    && x2 <= FRAME_WIDTH && y2 <= FRAME_HEIGHT
                    && payloadLen == rw * rh * 4
                    && payloadLen <= payload.length;
            if (!valid) {
                Log.w(TAG, "bad rect (" + x1 + "," + y1 + ")-(" + x2 + "," + y2
                        + ") len=" + payloadLen + " — skipping");
                in.skipBytes(payloadLen);
                return;
            }
            in.readFully(payload, 0, payloadLen);
            tempBmp.reconfigure(rw, rh, Bitmap.Config.ARGB_8888);
            tempBmp.copyPixelsFromBuffer(ByteBuffer.wrap(payload, 0, payloadLen));
            backingCanvas.drawBitmap(tempBmp, x1, y1, null);
        }

        private void present() {
            Canvas surfaceCanvas = holder.lockCanvas();
            if (surfaceCanvas == null) return;
            try {
                Rect dst = holder.getSurfaceFrame();
                if (dst.width() == FRAME_WIDTH && dst.height() == FRAME_HEIGHT) {
                    surfaceCanvas.drawBitmap(backing, 0, 0, null);
                } else {
                    surfaceCanvas.drawBitmap(backing, null, dst, null);
                }
            } finally {
                holder.unlockCanvasAndPost(surfaceCanvas);
            }
        }
    }
}
