package com.tabdisplay;

import java.io.DataInputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;

/**
 * Reads the sender's wire format: a sequence of
 * [u32 big-endian length][one H.264 access unit].
 *
 * The explicit length is what makes an access unit known-complete the
 * moment its last byte arrives. (Annex B alone only marks where an AU
 * starts, so a reader has to wait for the next frame to delimit this one.)
 */
final class FrameReader {

    /** Largest access unit accepted; also the decoder's input buffer size. */
    static final int MAX_AU = 2 * 1024 * 1024;

    private final DataInputStream in;
    private byte[] buf = new byte[256 * 1024];

    FrameReader(InputStream in) {
        this.in = new DataInputStream(in);
    }

    /**
     * Blocks until one whole access unit is in {@link #buffer()}.
     *
     * @return its length, or -1 if the stream ended on a frame boundary
     * @throws EOFException if the stream ended inside a frame
     * @throws IOException  if the length is not plausible (stream out of sync)
     */
    int next() throws IOException {
        int first = in.read();
        if (first < 0) return -1;
        int len = (first << 24)
                | (in.readUnsignedByte() << 16)
                | (in.readUnsignedByte() << 8)
                | in.readUnsignedByte();
        if (len <= 0 || len > MAX_AU) {
            throw new IOException("bad frame length " + len);
        }
        if (len > buf.length) buf = new byte[Math.max(len, buf.length * 2)];
        in.readFully(buf, 0, len);
        return len;
    }

    /** Holds the access unit returned by the last {@link #next()} call. */
    byte[] buffer() {
        return buf;
    }
}
