package ai.bot2048.sampler

/**
 * On-device -> host board packet, byte-for-byte identical to the Python
 * reference in `bot2048.ondevice.packet` (fixed 22-byte, big-endian, CRC-16/
 * CCITT-FALSE over bytes[0:20]). See docs/ondevice-vision-design.md for the
 * wire layout. The Python golden vectors in that doc are this code's
 * conformance contract -- keep the two in lockstep.
 */
object BoardPacket {
    const val SIZE = 22
    private val MAGIC = byteArrayOf('B'.code.toByte(), '2'.code.toByte())
    private const val VERSION = 1

    const val FLAG_BOARD_CHANGED = 1
    const val FLAG_HAS_UNCERTAIN = 1 shl 1
    const val FLAG_SECURE_SUSPECTED = 1 shl 2

    /** Uncertain-storm latched: the screen no longer looks like the board. */
    const val FLAG_NOT_IN_GAME = 1 shl 3

    /** A [FramePacket] message follows this packet immediately on the stream. */
    const val FLAG_FRAME_FOLLOWS = 1 shl 4

    /** CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, xorout 0). */
    fun crc16(data: ByteArray, len: Int): Int {
        var crc = 0xFFFF
        for (i in 0 until len) {
            crc = crc xor ((data[i].toInt() and 0xFF) shl 8)
            repeat(8) {
                crc = if (crc and 0x8000 != 0) ((crc shl 1) xor 0x1021) else (crc shl 1)
                crc = crc and 0xFFFF
            }
        }
        return crc
    }

    /**
     * Encode a sampled board into the 22-byte record.
     *
     * @param board 16 exponents, row-major (0 = empty), each 0..15.
     * @param seq   sample counter, wraps at 2^16.
     * @param uncertain 16-bit mask, bit i => cell i is low-confidence.
     * @param flags FLAG_* bitfield.
     * @param deviceTsMs device monotonic ms, low 32 bits.
     */
    fun encode(board: IntArray, seq: Int, uncertain: Int, flags: Int, deviceTsMs: Long): ByteArray {
        require(board.size == 16) { "board must have 16 cells" }
        val b = ByteArray(SIZE)
        b[0] = MAGIC[0]; b[1] = MAGIC[1]
        b[2] = VERSION.toByte()
        b[3] = (flags and 0xFF).toByte()
        b[4] = ((seq ushr 8) and 0xFF).toByte(); b[5] = (seq and 0xFF).toByte()
        for (i in 0 until 16) {
            val exp = board[i]
            require(exp in 0..15) { "cell $i exponent $exp out of nibble range" }
            val bi = 6 + i / 2
            if (i % 2 == 0) b[bi] = (exp shl 4).toByte()
            else b[bi] = (b[bi].toInt() or exp).toByte()
        }
        b[14] = ((uncertain ushr 8) and 0xFF).toByte(); b[15] = (uncertain and 0xFF).toByte()
        b[16] = ((deviceTsMs ushr 24) and 0xFF).toByte()
        b[17] = ((deviceTsMs ushr 16) and 0xFF).toByte()
        b[18] = ((deviceTsMs ushr 8) and 0xFF).toByte()
        b[19] = (deviceTsMs and 0xFF).toByte()
        val crc = crc16(b, 20)
        b[20] = ((crc ushr 8) and 0xFF).toByte(); b[21] = (crc and 0xFF).toByte()
        return b
    }
}
