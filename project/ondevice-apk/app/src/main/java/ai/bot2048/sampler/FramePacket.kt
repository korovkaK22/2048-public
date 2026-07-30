package ai.bot2048.sampler

/**
 * Device -> host JPEG frame message + host -> device command opcodes.
 * Byte-for-byte identical to the Python contract in `bot2048.ondevice.packet`
 * (see its module docstring for the wire layout) -- keep the two in lockstep.
 *
 * Frame header (12 bytes, big-endian), followed by `length` JPEG bytes:
 *
 *     0  2  magic   "F2"
 *     2  1  version (1)
 *     3  1  reason  REASON_*
 *     4  2  seq     u16 -- seq of the board packet this frame accompanies
 *     6  4  length  u32 -- JPEG payload bytes
 *     10 2  crc16   CRC-16/CCITT-FALSE over bytes[0:10]
 *
 * The board packet emitted immediately before a frame message carries
 * [BoardPacket.FLAG_FRAME_FOLLOWS] so hosts can pair them.
 */
object FramePacket {
    const val HEADER_SIZE = 12
    private const val VERSION = 1

    /** The ~10 s control tick (digit reconciliation / popup / drift check). */
    const val REASON_PERIODIC = 0

    /** Direct reply to a host [CMD_SYNC_REQ]. */
    const val REASON_SYNC_REQ = 1

    /** Device-detected anomaly (NOT_IN_GAME storm); also saved on-device. */
    const val REASON_INCIDENT = 2

    // Host -> device command opcodes (single bytes on the same socket).
    /** 'S': push one full JPEG frame now. */
    const val CMD_SYNC_REQ = 0x53

    /** 'P': emit a board packet immediately even if the board is unchanged. */
    const val CMD_PING = 0x50

    /** 'R' + u8 arg: unchanged-board heartbeat = arg * 10 ms (0 = every frame). */
    const val CMD_SET_RATE = 0x52

    /**
     * 'W' + [SWIPE_ARG_SIZE] arg bytes: inject a swipe gesture on-device via the
     * accessibility service (dispatchGesture), replacing a host `adb shell input
     * swipe`. Args are five big-endian u16 fields: x1 y1 x2 y2 duration_ms.
     * Mirrors `bot2048.ondevice.packet.CMD_SWIPE` / `encode_swipe`.
     */
    const val CMD_SWIPE = 0x57

    /** Argument bytes after [CMD_SWIPE]: 5 big-endian u16 (x1,y1,x2,y2,dur_ms). */
    const val SWIPE_ARG_SIZE = 10

    /** Encode the 12-byte header for a frame of [length] JPEG bytes. */
    fun encodeHeader(reason: Int, seq: Int, length: Int): ByteArray {
        require(reason in 0..255) { "reason $reason out of byte range" }
        require(length >= 0) { "length must be >= 0" }
        val b = ByteArray(HEADER_SIZE)
        b[0] = 'F'.code.toByte(); b[1] = '2'.code.toByte()
        b[2] = VERSION.toByte()
        b[3] = reason.toByte()
        b[4] = ((seq ushr 8) and 0xFF).toByte(); b[5] = (seq and 0xFF).toByte()
        b[6] = ((length ushr 24) and 0xFF).toByte()
        b[7] = ((length ushr 16) and 0xFF).toByte()
        b[8] = ((length ushr 8) and 0xFF).toByte()
        b[9] = (length and 0xFF).toByte()
        val crc = BoardPacket.crc16(b, 10)
        b[10] = ((crc ushr 8) and 0xFF).toByte(); b[11] = (crc and 0xFF).toByte()
        return b
    }
}
