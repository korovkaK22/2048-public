package ai.bot2048.sampler

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.net.LocalServerSocket
import android.net.LocalSocket
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.OutputStream
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Foreground service that captures the display via MediaProjection, samples the
 * board on-device, and streams 22-byte board packets to the host.
 *
 * Host wiring (no root, no reverse-tether): the service publishes an ABSTRACT
 * LocalServerSocket named `bot2048_sampler`. On the host:
 *
 *     adb forward tcp:27184 localabstract:bot2048_sampler
 *     # then connect a TCP client to 127.0.0.1:27184 and speak the protocol
 *     # from bot2048.ondevice.packet (22-byte BoardPacket records, optional
 *     # "F2" frame messages, single-byte host commands).
 *
 * Emission policy (bandwidth-first: state, not video):
 *  - a packet is sent when the BOARD CHANGES, when the unchanged-board
 *    heartbeat elapses (default 500 ms, host-tunable via CMD_SET_RATE), or on
 *    a host CMD_PING;
 *  - every ~10 s the packet carries FLAG_FRAME_FOLLOWS and a REASON_PERIODIC
 *    JPEG frame follows for host-side digit reconciliation;
 *  - CMD_SYNC_REQ forces the same packet+frame pair immediately;
 *  - an uncertain-storm (>= 8 uncertain cells for 3 consecutive samples, the
 *    [NotInGameDetector] spec from bot2048.ondevice.sampler) latches
 *    FLAG_NOT_IN_GAME; on the False->True edge the JPEG is ALSO saved under
 *    files/incidents/ for later research and pushed as REASON_INCIDENT.
 *
 * WHY MediaProjection (not accessibility, not root): it is the supported
 * non-root screen-capture API and needs only a one-time user consent tap. It
 * does NOT use an AccessibilityService (which banking apps do scan for) and
 * cannot read monobank's memory or network -- it only sees on-screen pixels of
 * whatever is displayed. FLAG_SECURE windows (the monobank auth wall) come
 * through BLACK; the sampler flags that (FLAG_SECURE_SUSPECTED) and the host
 * falls back to its screencap 0-byte AUTH sensor exactly as today. Black
 * frames are NOT fed to the not-in-game detector (no color evidence).
 *
 * Lifecycle caveat: if this process is killed, MediaProjection consent is lost
 * and the operator must re-grant it (re-open the app). Keep the phone on a
 * charger; this is a foreground service to resist background kills.
 */
class SamplerService : Service() {
    private var projection: MediaProjection? = null
    private var reader: ImageReader? = null
    private var display: VirtualDisplay? = null
    private var server: LocalServerSocket? = null
    private var client: LocalSocket? = null
    private var out: OutputStream? = null
    private var captureThread: HandlerThread? = null
    private lateinit var sampler: BoardSampler

    // Strong reference to the projection callback: the platform holds it weakly,
    // so a local would be GC'd and the projection torn down.
    private var projectionCallback: MediaProjection.Callback? = null
    private val mainHandler = Handler(Looper.getMainLooper())
    private var firstFrameLogged = false

    private var seq = 0
    private var prevBoard: IntArray? = null
    private var lastEmitMs = 0L
    private var lastFrameMs = 0L
    private var notInGame = false
    private var notInGameStreak = 0

    // Host commands land from the reader thread; the capture thread consumes.
    @Volatile private var pingRequested = false
    @Volatile private var syncRequested = false
    @Volatile private var heartbeatMs = DEFAULT_HEARTBEAT_MS

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent, flags: Int, startId: Int): Int {
        // Enter the foreground with the mediaProjection type BEFORE the token is
        // redeemed: MIUI's MediaProjectionManagerService validates the caller's
        // FGS type at getMediaProjection() time.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIF_ID, buildNotification(),
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION,
            )
        } else {
            startForeground(NOTIF_ID, buildNotification())
        }
        Log.i(TAG, "onStartCommand: foreground started (type=mediaProjection)")

        val resultCode = intent.getIntExtra(EXTRA_CODE, 0)
        val data = intent.getParcelableExtra<Intent>(EXTRA_DATA)
        if (data == null) { Log.e(TAG, "no consent Intent extra; stopping"); stopSelf(); return START_NOT_STICKY }

        val cal = assets.open("calibration.json").bufferedReader().use { it.readText() }
        sampler = BoardSampler(cal)

        val mpm = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        val mp = mpm.getMediaProjection(resultCode, data)
        if (mp == null) { Log.e(TAG, "getMediaProjection returned null; stopping"); stopSelf(); return START_NOT_STICKY }
        projection = mp
        Log.i(TAG, "getMediaProjection OK (resultCode=$resultCode); token acquired")

        // CRITICAL: register a MediaProjection.Callback BEFORE createVirtualDisplay.
        // With no registered callback the platform tears the projection down in
        // the same tick (addMediaProjection -> removeMediaProjection) -- this is
        // the exact add/remove teardown seen on this MIUI build. onStop is the
        // system's "projection is going away" signal; logging it makes any future
        // teardown observable instead of silent.
        val cb = object : MediaProjection.Callback() {
            override fun onStop() {
                Log.w(TAG, "MediaProjection.Callback.onStop -- projection revoked/stopped; tearing down service")
                stopSelf()
            }

            override fun onCapturedContentResize(width: Int, height: Int) {
                Log.i(TAG, "onCapturedContentResize ${width}x$height")
            }

            override fun onCapturedContentVisibilityChanged(isVisible: Boolean) {
                Log.i(TAG, "onCapturedContentVisibilityChanged visible=$isVisible")
            }
        }
        projectionCallback = cb
        mp.registerCallback(cb, mainHandler)
        Log.i(TAG, "registerCallback done; creating VirtualDisplay")

        // Native size must match the calibration (1080x2400). RGBA_8888, 2 buffers.
        val w = sampler.width; val h = sampler.height
        reader = ImageReader.newInstance(w, h, android.graphics.PixelFormat.RGBA_8888, 2)
        display = mp.createVirtualDisplay(
            "bot2048-sampler", w, h, resources.displayMetrics.densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, reader!!.surface, null, null,
        )
        Log.i(TAG, "createVirtualDisplay -> ${if (display != null) "OK" else "NULL"}")

        acceptClientAsync()
        // Sample + JPEG-compress off the main thread.
        captureThread = HandlerThread("bot2048-capture").also { it.start() }
        reader!!.setOnImageAvailableListener({ onFrame() }, Handler(captureThread!!.looper))
        return START_STICKY
    }

    private fun onFrame() {
        val image = reader?.acquireLatestImage() ?: return
        try {
            if (!firstFrameLogged) {
                firstFrameLogged = true
                Log.i(TAG, "first frame captured (${image.width}x${image.height}) -- projection is LIVE")
            }
            val plane = image.planes[0]
            val s = sampler.sample(plane.buffer, plane.rowStride, plane.pixelStride)
            val now = SystemClock.uptimeMillis()

            var incidentEdge = false
            if (!s.secureSuspected) {
                // Verbatim NotInGameDetector.update() from the Python spec.
                if (s.uncertainCount >= NOT_IN_GAME_MIN_UNCERTAIN) notInGameStreak++
                else notInGameStreak = 0
                val flagged = notInGameStreak >= NOT_IN_GAME_STREAK
                incidentEdge = flagged && !notInGame
                notInGame = flagged
            }

            val changed = prevBoard == null || !prevBoard.contentEquals(s.board)
            val wantPacket = changed || pingRequested || syncRequested || incidentEdge ||
                now - lastEmitMs >= heartbeatMs
            val wantPeriodicFrame = now - lastFrameMs >= FRAME_PERIOD_MS
            if (!wantPacket && !wantPeriodicFrame) return

            val frameReason = when {
                incidentEdge -> FramePacket.REASON_INCIDENT
                syncRequested -> FramePacket.REASON_SYNC_REQ
                wantPeriodicFrame -> FramePacket.REASON_PERIODIC
                else -> -1
            }

            var flags = 0
            if (s.secureSuspected) flags = flags or BoardPacket.FLAG_SECURE_SUSPECTED
            if (s.uncertainMask != 0) flags = flags or BoardPacket.FLAG_HAS_UNCERTAIN
            if (changed && prevBoard != null) flags = flags or BoardPacket.FLAG_BOARD_CHANGED
            if (notInGame) flags = flags or BoardPacket.FLAG_NOT_IN_GAME
            if (frameReason >= 0) flags = flags or BoardPacket.FLAG_FRAME_FOLLOWS

            val board = if (s.secureSuspected) IntArray(16) else s.board
            val mask = if (s.secureSuspected) 0 else s.uncertainMask
            val pkt = BoardPacket.encode(board, seq, mask, flags, now)
            val pktSeq = seq
            seq = (seq + 1) and 0xFFFF
            prevBoard = board.copyOf()
            pingRequested = false; syncRequested = false
            lastEmitMs = now

            var payload: ByteArray? = null
            if (frameReason >= 0) {
                payload = encodeJpeg(image, plane)
                lastFrameMs = now
                if (incidentEdge) saveIncident(payload)
            }

            out?.let { stream ->
                try {
                    stream.write(pkt)
                    if (payload != null) {
                        stream.write(FramePacket.encodeHeader(frameReason, pktSeq, payload.size))
                        stream.write(payload)
                    }
                    stream.flush()
                } catch (e: Exception) {
                    Log.w(TAG, "host write failed, waiting for reconnect", e)
                    closeClient(); acceptClientAsync()
                }
            }
        } finally {
            image.close()
        }
    }

    /** RGBA plane -> JPEG, honouring rowStride padding (crop to real width). */
    private fun encodeJpeg(image: Image, plane: Image.Plane): ByteArray {
        val strideW = plane.rowStride / plane.pixelStride
        plane.buffer.rewind()
        var bmp = Bitmap.createBitmap(strideW, image.height, Bitmap.Config.ARGB_8888)
        bmp.copyPixelsFromBuffer(plane.buffer)
        if (strideW != image.width) {
            val cropped = Bitmap.createBitmap(bmp, 0, 0, image.width, image.height)
            bmp.recycle(); bmp = cropped
        }
        val bos = ByteArrayOutputStream(256 * 1024)
        bmp.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, bos)
        bmp.recycle()
        return bos.toByteArray()
    }

    private fun saveIncident(jpeg: ByteArray) {
        try {
            val dir = File(filesDir, "incidents").apply { mkdirs() }
            val ts = SimpleDateFormat("yyyyMMdd_HHmmss_SSS", Locale.US).format(Date())
            File(dir, "not_in_game_$ts.jpg").writeBytes(jpeg)
        } catch (e: Exception) {
            Log.e(TAG, "failed to save incident jpeg", e)
        }
    }

    private fun acceptClientAsync() {
        Thread {
            try {
                if (server == null) server = LocalServerSocket(SOCKET_NAME)
                val c = server!!.accept()
                closeClient()
                client = c
                out = c.outputStream
                // Force a fresh packet+frame so a reconnecting host resyncs fast.
                syncRequested = true
                readCommands(c)
                Log.i(TAG, "host connected")
            } catch (e: Exception) {
                Log.e(TAG, "accept failed", e)
            }
        }.start()
    }

    /**
     * Host -> device opcodes: one byte each, except CMD_SET_RATE (+1 arg byte)
     * and CMD_SWIPE (+[FramePacket.SWIPE_ARG_SIZE] arg bytes). A truncated read
     * (-1) means the socket closed; the write path re-accepts.
     */
    private fun readCommands(c: LocalSocket) {
        Thread {
            try {
                val ins = c.inputStream
                while (true) {
                    when (ins.read()) {
                        -1 -> break
                        FramePacket.CMD_SYNC_REQ -> syncRequested = true
                        FramePacket.CMD_PING -> pingRequested = true
                        FramePacket.CMD_SET_RATE -> {
                            val arg = ins.read()
                            if (arg < 0) break
                            heartbeatMs = arg * 10L
                        }
                        FramePacket.CMD_SWIPE -> {
                            val args = ByteArray(FramePacket.SWIPE_ARG_SIZE)
                            if (!readFully(ins, args)) break
                            handleSwipe(args)
                        }
                        else -> Log.w(TAG, "unknown host command byte")
                    }
                }
            } catch (_: Exception) {
                // Socket died; the write path notices and re-accepts.
            }
        }.start()
    }

    /** Read exactly [buf].size bytes; false if EOF hit first (socket closed). */
    private fun readFully(ins: java.io.InputStream, buf: ByteArray): Boolean {
        var off = 0
        while (off < buf.size) {
            val n = ins.read(buf, off, buf.size - off)
            if (n < 0) return false
            off += n
        }
        return true
    }

    /** Decode 5 big-endian u16 (x1,y1,x2,y2,dur_ms) and inject the gesture. */
    private fun handleSwipe(args: ByteArray) {
        fun u16(i: Int) = ((args[i].toInt() and 0xFF) shl 8) or (args[i + 1].toInt() and 0xFF)
        val x1 = u16(0); val y1 = u16(2); val x2 = u16(4); val y2 = u16(6)
        val dur = u16(8)
        val svc = SwipeAccessibilityService.instance
        if (svc == null) {
            // Host should gate on the accessibility-enabled probe before sending;
            // if it slips through (service disabled mid-run) drop it and let the
            // host's board-settle gate + fallback recover.
            Log.w(TAG, "CMD_SWIPE but accessibility service not enabled; ignoring")
            return
        }
        svc.dispatchSwipe(x1, y1, x2, y2, dur)
    }

    private fun closeClient() {
        try { out?.close() } catch (_: Exception) {}
        try { client?.close() } catch (_: Exception) {}
        out = null; client = null
    }

    override fun onDestroy() {
        Log.i(TAG, "onDestroy: tearing down capture + projection")
        closeClient()
        try { server?.close() } catch (_: Exception) {}
        display?.release(); reader?.close()
        projectionCallback?.let { cb -> try { projection?.unregisterCallback(cb) } catch (_: Exception) {} }
        projection?.stop()
        captureThread?.quitSafely()
        super.onDestroy()
    }

    private fun buildNotification(): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "bot2048 sampler", NotificationManager.IMPORTANCE_LOW),
        )
        return Notification.Builder(this, CHANNEL)
            .setContentTitle("bot2048 board sampler")
            .setContentText("Streaming board packets to host")
            .setSmallIcon(android.R.drawable.presence_video_online)
            .build()
    }

    companion object {
        const val TAG = "BoardSampler"
        const val SOCKET_NAME = "bot2048_sampler"
        const val EXTRA_CODE = "code"
        const val EXTRA_DATA = "data"
        private const val CHANNEL = "sampler"
        private const val NOTIF_ID = 1

        /** Mirrors bot2048.ondevice.sampler.NOT_IN_GAME_MIN_UNCERTAIN. */
        private const val NOT_IN_GAME_MIN_UNCERTAIN = 8

        /** Mirrors bot2048.ondevice.sampler.NOT_IN_GAME_STREAK. */
        private const val NOT_IN_GAME_STREAK = 3

        /** Unchanged-board heartbeat; host-tunable via CMD_SET_RATE (arg*10 ms). */
        private const val DEFAULT_HEARTBEAT_MS = 500L

        /** ~10 s control tick: full JPEG for host digit reconciliation. */
        private const val FRAME_PERIOD_MS = 10_000L

        private const val JPEG_QUALITY = 70
    }
}
