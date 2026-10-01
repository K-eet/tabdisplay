package com.tabdisplay;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

import org.junit.Test;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.util.Arrays;

public class FrameReaderTest {

    private static byte[] framed(byte[]... aus) throws IOException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        DataOutputStream out = new DataOutputStream(bytes);
        for (byte[] au : aus) {
            out.writeInt(au.length);
            out.write(au);
        }
        return bytes.toByteArray();
    }

    private static byte[] taken(FrameReader r, int len) {
        return Arrays.copyOf(r.buffer(), len);
    }

    /** Delivers one byte per read() call, like a badly fragmented socket. */
    private static InputStream trickle(byte[] data) {
        return new ByteArrayInputStream(data) {
            @Override
            public synchronized int read(byte[] b, int off, int len) {
                return super.read(b, off, Math.min(len, 1));
            }
        };
    }

    @Test
    public void readsConsecutiveFrames() throws IOException {
        byte[] a = {0, 0, 0, 1, 9}, b = {7, 7};
        FrameReader r = new FrameReader(new ByteArrayInputStream(framed(a, b)));
        assertArrayEquals(a, taken(r, r.next()));
        assertArrayEquals(b, taken(r, r.next()));
    }

    @Test
    public void returnsMinusOneAtEndOfStreamOnAFrameBoundary() throws IOException {
        FrameReader r = new FrameReader(new ByteArrayInputStream(framed(new byte[]{1})));
        assertEquals(1, r.next());
        assertEquals(-1, r.next());
    }

    @Test
    public void reassemblesFrameThatArrivesOneByteAtATime() throws IOException {
        byte[] au = new byte[1000];
        Arrays.fill(au, (byte) 0x5A);
        FrameReader r = new FrameReader(trickle(framed(au)));
        assertArrayEquals(au, taken(r, r.next()));
    }

    @Test
    public void growsBufferForFrameLargerThanInitialCapacity() throws IOException {
        byte[] au = new byte[FrameReader.MAX_AU];
        au[au.length - 1] = 42;
        FrameReader r = new FrameReader(new ByteArrayInputStream(framed(au)));
        int len = r.next();
        assertEquals(FrameReader.MAX_AU, len);
        assertEquals(42, r.buffer()[len - 1]);
    }

    @Test
    public void rejectsLengthAboveMaximum() {
        byte[] header = {0x7F, 0, 0, 0};
        FrameReader r = new FrameReader(new ByteArrayInputStream(header));
        assertThrows(IOException.class, r::next);
    }

    @Test
    public void rejectsZeroLength() {
        FrameReader r = new FrameReader(new ByteArrayInputStream(new byte[4]));
        assertThrows(IOException.class, r::next);
    }

    @Test
    public void rejectsStreamThatEndsInsideAFrame() throws IOException {
        byte[] whole = framed(new byte[]{1, 2, 3, 4});
        byte[] cut = Arrays.copyOf(whole, whole.length - 2);
        FrameReader r = new FrameReader(new ByteArrayInputStream(cut));
        assertThrows(EOFException.class, r::next);
    }

    @Test
    public void rejectsStreamThatEndsInsideALengthHeader() {
        FrameReader r = new FrameReader(new ByteArrayInputStream(new byte[]{0, 0}));
        assertThrows(EOFException.class, r::next);
    }
}
