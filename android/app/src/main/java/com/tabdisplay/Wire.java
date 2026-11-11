package com.tabdisplay;

import java.io.DataInputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/**
 * The link's framing, both directions: [u8 type][u32 LE length][payload].
 * See host/tabdisplayd.py for the message catalogue.
 */
final class Wire {

    static final int VERSION = 3;
    /** Largest message accepted; also sizes the decoder's input buffers. */
    static final int MAX_MSG = 8 * 1024 * 1024;
    /** Bytes of header in front of the access unit in a 'V' message. */
    static final int VIDEO_HEADER = 13;

    static final int CODEC_H264 = 1;
    static final int CODEC_HEVC = 2;

    private final DataInputStream in;
    private final byte[] head = new byte[5];
    private byte[] buf = new byte[256 * 1024];
    private int type;
    private int length;

    Wire(InputStream in) {
        this.in = new DataInputStream(in);
    }

    /**
     * Blocks until one whole message is in {@link #payload()}.
     *
     * @return false on a clean end of stream between messages
     * @throws IOException on a truncated or oversized message
     */
    boolean next() throws IOException {
        int first = in.read();
        if (first < 0) return false;
        head[0] = (byte) first;
        try {
            in.readFully(head, 1, 4);
        } catch (EOFException e) {
            throw new IOException("stream ended inside a message header");
        }
        type = head[0] & 0xff;
        length = ByteBuffer.wrap(head, 1, 4).order(ByteOrder.LITTLE_ENDIAN).getInt();
        if (length < 0 || length > MAX_MSG) {
            throw new IOException("bad message length " + length);
        }
        if (length > buf.length) {
            buf = new byte[Math.max(length, buf.length * 2)];
        }
        try {
            in.readFully(buf, 0, length);
        } catch (EOFException e) {
            throw new IOException("stream ended inside a message");
        }
        return true;
    }

    int type() { return type; }
    int length() { return length; }
    /** Valid until the next call to {@link #next()}; only the first length() bytes. */
    byte[] payload() { return buf; }

    ByteBuffer view() {
        return ByteBuffer.wrap(buf, 0, length).order(ByteOrder.LITTLE_ENDIAN);
    }

    private static ByteBuffer message(char type, int payload) {
        ByteBuffer b = ByteBuffer.allocate(5 + payload).order(ByteOrder.LITTLE_ENDIAN);
        b.put((byte) type).putInt(payload);
        return b;
    }

    static byte[] hello(int width, int height, int dpi, int codecs) {
        return message('H', 22)
                .put((byte) 'T').put((byte) 'A').put((byte) 'B').put((byte) 'D')
                .putShort((short) VERSION)
                .putInt(width).putInt(height).putInt(dpi).putInt(codecs)
                .array();
    }

    /** The current message, sent back as it came (link round-trip probe). */
    static byte[] echo(Wire w) {
        return message((char) w.type(), w.length()).put(w.payload(), 0, w.length()).array();
    }

    /** Frame with capture time {@code ts} is on its way to the panel. */
    static byte[] ack(long ts, int heldUs) {
        return message('A', 12).putLong(ts).putInt(heldUs).array();
    }
}
