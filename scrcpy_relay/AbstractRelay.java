package org.phonecapture.scrcpy;

import android.net.LocalSocket;
import android.net.LocalSocketAddress;
import java.io.InputStream;
import java.io.OutputStream;

/** Connect a scrcpy abstract socket to one binary ADB shell channel. */
public final class AbstractRelay {
    private AbstractRelay() {}

    public static void main(String[] args) throws Exception {
        if (args.length != 2 || !args[0].matches("scrcpy_[0-9a-f]{8}")
                || !(args[1].equals("video") || args[1].equals("control"))) {
            System.err.println("usage: AbstractRelay scrcpy_<8 hex digits> video|control");
            System.exit(2);
        }
        LocalSocket socket = new LocalSocket();
        socket.connect(new LocalSocketAddress(args[0], LocalSocketAddress.Namespace.ABSTRACT));
        // scrcpy assigns its sockets by connection order: video, then control.
        // Signal the host only after this connection succeeds so it can start
        // the next relay without racing Android's process scheduler.
        System.err.println("PHONE_CAPTURE_RELAY_READY");
        byte[] buffer = new byte[65536];
        if (args[1].equals("video")) {
            InputStream input = socket.getInputStream();
            OutputStream output = System.out;
            int count;
            while ((count = input.read(buffer)) >= 0) {
                output.write(buffer, 0, count);
                output.flush();
            }
        } else {
            InputStream input = System.in;
            OutputStream output = socket.getOutputStream();
            int count;
            while ((count = input.read(buffer)) >= 0) {
                output.write(buffer, 0, count);
                output.flush();
            }
        }
        socket.close();
    }
}
