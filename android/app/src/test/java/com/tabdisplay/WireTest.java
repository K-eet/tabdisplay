package com.tabdisplay;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import org.junit.Test;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.Arrays;

public class WireTest {

    private static byte[] msg(char type, byte[] payload) {
        ByteBuffer b = ByteBuffer.allocate(5 + payload.length).order(ByteOrder.LITTLE_ENDIAN);
        b.put((byte) type).putInt(payload.length).put(payload);
        return b.array();
    }

    /** Hands out one byte per read, like a socket under load might. */
    private static InputStream trickle(byte[] data) {
        return new ByteArrayInputStream(data) {
            @Override
            public synchronized int read(byte[] b, int off, int len) {
                return super.read(b, off, Math.min(len, 1));
            }
        };
    }

    @Test
    public void readsMessagesInOrderThenCleanEof() throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.write(msg('V', new byte[] {1, 2, 3}));
        out.write(msg('P', new byte[0]));
        out.write(msg('S', new byte[] {9}));
        Wire w = new Wire(trickle(out.toByteArray()));

        assertTrue(w.next());
        assertEquals('V', w.type());
        assertEquals(3, w.length());
        assertArrayEquals(new byte[] {1, 2, 3}, Arrays.copyOf(w.payload(), 3));
        assertTrue(w.next());
        assertEquals('P', w.type());
        assertEquals(0, w.length());
        assertTrue(w.next());
        assertEquals('S', w.type());
        assertFalse(w.next());
    }

    @Test
    public void growsForMessagesLargerThanTheInitialBuffer() throws IOException {
        byte[] big = new byte[700 * 1024];
        big[big.length - 1] = 42;
        Wire w = new Wire(new ByteArrayInputStream(msg('V', big)));
        assertTrue(w.next());
        assertEquals(big.length, w.length());
        assertEquals(42, w.payload()[big.length - 1]);
    }

    @Test
    public void truncatedMessageIsAnErrorNotEof() {
        byte[] whole = msg('V', new byte[100]);
        for (int cut : new int[] {3, 50}) {
            Wire w = new Wire(new ByteArrayInputStream(Arrays.copyOf(whole, cut)));
            try {
                w.next();
                fail("expected IOException at cut " + cut);
            } catch (IOException expected) {
            }
        }
    }

    @Test
    public void rejectsOversizedLength() {
        ByteBuffer b = ByteBuffer.allocate(5).order(ByteOrder.LITTLE_ENDIAN);
        b.put((byte) 'V').putInt(Wire.MAX_MSG + 1);
        Wire w = new Wire(new ByteArrayInputStream(b.array()));
        try {
            w.next();
            fail("expected IOException");
        } catch (IOException expected) {
        }
    }

    @Test
    public void helloAndAckLayout() {
        ByteBuffer h = ByteBuffer.wrap(Wire.hello(2560, 1600, 244, 3))
                .order(ByteOrder.LITTLE_ENDIAN);
        assertEquals('H', h.get());
        assertEquals(22, h.getInt());
        byte[] magic = new byte[4];
        h.get(magic);
        assertArrayEquals("TABD".getBytes(), magic);
        assertEquals(Wire.VERSION, h.getShort());
        assertEquals(2560, h.getInt());
        assertEquals(1600, h.getInt());
        assertEquals(244, h.getInt());
        assertEquals(3, h.getInt());

        ByteBuffer a = ByteBuffer.wrap(Wire.ack(123456789012L, 4200))
                .order(ByteOrder.LITTLE_ENDIAN);
        assertEquals('A', a.get());
        assertEquals(12, a.getInt());
        assertEquals(123456789012L, a.getLong());
        assertEquals(4200, a.getInt());
    }
}
