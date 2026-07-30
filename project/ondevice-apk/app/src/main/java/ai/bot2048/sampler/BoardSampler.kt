package ai.bot2048.sampler

import org.json.JSONObject
import java.nio.ByteBuffer

/**
 * The cheap color math, on the phone. Port of the Python reference
 * `bot2048.ondevice.sampler` -- it MUST stay byte-identical (verify with the
 * golden vectors in docs/ondevice-vision-design.md).
 *
 * Per cell center it crops a square of half-size round(0.37 * column_pitch),
 * takes the per-channel MEDIAN of the non-white pixels (min(R,G,B) < 170; if
 * under 5% are non-white it uses all pixels), classifies by nearest calibrated
 * palette color, and flags the cell uncertain when that distance exceeds
 * [uncertainMaxDist]. No digit OCR -- the host does that on its ~10s control
 * frame.
 *
 * Calibration (16 centers + palette + screen size) is loaded from the
 * `calibration.json` asset, itself copied from the host's data/calibration.json.
 */
class BoardSampler(calibrationJson: String) {
    val width: Int
    val height: Int
    private val half: Int
    private val centers: Array<IntArray>          // 16 x [x, y]
    private val palRgb: Array<IntArray>           // N x [r, g, b]
    private val palExp: IntArray                  // N
    var uncertainMaxDist = 20.0

    companion object {
        private const val WHITE_MIN = 170          // min(R,G,B) >= this => "white"
        private const val NON_WHITE_MIN_FRAC = 0.05
        const val BLACK_PIXEL_MAX = 8
        private const val CROP_HALF_FRACTION = 0.37

        /**
         * Fraction of crop ROWS dropped from the top before the median: the
         * cat's paw drapes over the top edge of top-row cells. Mirrors
         * `bot2048.vision.parser.PAW_SKIP_TOP_FRACTION` exactly, including the
         * degenerate-crop fallback (fewer than 2 remaining rows => use all).
         */
        private const val PAW_SKIP_TOP_FRACTION = 0.40
    }

    init {
        val root = JSONObject(calibrationJson)
        val ss = root.getJSONArray("screen_size")
        width = ss.getInt(0); height = ss.getInt(1)
        val cc = root.getJSONArray("cell_centers")
        centers = Array(cc.length()) {
            val p = cc.getJSONArray(it); intArrayOf(p.getInt(0), p.getInt(1))
        }
        val pal = root.getJSONArray("palette")
        palRgb = Array(pal.length()) {
            val rgb = pal.getJSONObject(it).getJSONArray("rgb")
            intArrayOf(rgb.getInt(0), rgb.getInt(1), rgb.getInt(2))
        }
        palExp = IntArray(pal.length()) { pal.getJSONObject(it).getInt("exponent") }
        // Column pitch from the distinct x-centers (matches parser.cell_half_size).
        val xs = centers.map { it[0] }.distinct().sorted()
        val pitch = (xs.last() - xs.first()).toDouble() / (xs.size - 1)
        half = Math.round(CROP_HALF_FRACTION * pitch).toInt()
    }

    /** One classified frame: 16 exponents + uncertain bitmask + black-frame flag. */
    class Sample(val board: IntArray, val uncertainMask: Int, val secureSuspected: Boolean) {
        val uncertainCount: Int get() = Integer.bitCount(uncertainMask)
    }

    /**
     * Classify one RGBA_8888 frame.
     *
     * @param buf ImageReader plane buffer (RGBA, R at byte 0).
     * @param rowStride bytes per row (>= width*pixelStride -- padding is real!).
     * @param pixelStride bytes per pixel (4 for RGBA_8888).
     */
    fun sample(buf: ByteBuffer, rowStride: Int, pixelStride: Int): Sample {
        if (isBlack(buf, rowStride, pixelStride)) {
            return Sample(IntArray(16), 0, secureSuspected = true)
        }
        val board = IntArray(16)
        var uncertain = 0
        for (i in 0 until 16) {
            val (exp, dist) = classifyCell(buf, rowStride, pixelStride, centers[i][0], centers[i][1])
            board[i] = exp
            if (dist > uncertainMaxDist) uncertain = uncertain or (1 shl i)
        }
        return Sample(board, uncertain, secureSuspected = false)
    }

    /** Sample one frame straight into a 22-byte packet (reference-path helper). */
    fun samplePacket(
        buf: ByteBuffer,
        rowStride: Int,
        pixelStride: Int,
        seq: Int,
        prevBoard: IntArray?,
        deviceTsMs: Long,
    ): ByteArray {
        val s = sample(buf, rowStride, pixelStride)
        if (s.secureSuspected) {
            return BoardPacket.encode(IntArray(16), seq, 0, BoardPacket.FLAG_SECURE_SUSPECTED, deviceTsMs)
        }
        var flags = 0
        if (s.uncertainMask != 0) flags = flags or BoardPacket.FLAG_HAS_UNCERTAIN
        if (prevBoard != null && !prevBoard.contentEquals(s.board)) flags = flags or BoardPacket.FLAG_BOARD_CHANGED
        return BoardPacket.encode(s.board, seq, s.uncertainMask, flags, deviceTsMs)
    }

    private fun px(buf: ByteBuffer, rowStride: Int, pixelStride: Int, x: Int, y: Int, c: Int): Int {
        return buf.get(y * rowStride + x * pixelStride + c).toInt() and 0xFF
    }

    private fun isBlack(buf: ByteBuffer, rowStride: Int, pixelStride: Int): Boolean {
        // Sparse scan (every 16th pixel) is enough to spot a non-black frame fast.
        var y = 0
        while (y < height) {
            var x = 0
            while (x < width) {
                if (px(buf, rowStride, pixelStride, x, y, 0) > BLACK_PIXEL_MAX ||
                    px(buf, rowStride, pixelStride, x, y, 1) > BLACK_PIXEL_MAX ||
                    px(buf, rowStride, pixelStride, x, y, 2) > BLACK_PIXEL_MAX
                ) return false
                x += 16
            }
            y += 16
        }
        return true
    }

    /** @return Pair(exponent, distanceToNearestPaletteEntry). */
    private fun classifyCell(
        buf: ByteBuffer, rowStride: Int, pixelStride: Int, cx: Int, cy: Int,
    ): Pair<Int, Double> {
        val x0 = maxOf(cx - half, 0); val x1 = minOf(cx + half, width)
        val cy0 = maxOf(cy - half, 0); val y1 = minOf(cy + half, height)
        // Paw skip: drop the top PAW_SKIP_TOP_FRACTION of the crop ROWS (the
        // cat's white paw occludes top-row cells from above). Same math as
        // parser.tile_background_color: int-truncate, and if fewer than 2 rows
        // survive, fall back to the full crop.
        val skip = ((y1 - cy0) * PAW_SKIP_TOP_FRACTION).toInt()
        val y0 = if (y1 - (cy0 + skip) < 2) cy0 else cy0 + skip
        val n = (x1 - x0) * (y1 - y0)
        val r = IntArray(n); val g = IntArray(n); val b = IntArray(n)
        val rn = IntArray(n); val gn = IntArray(n); val bn = IntArray(n)
        var k = 0; var kn = 0
        var y = y0
        while (y < y1) {
            var x = x0
            while (x < x1) {
                val rr = px(buf, rowStride, pixelStride, x, y, 0)
                val gg = px(buf, rowStride, pixelStride, x, y, 1)
                val bb = px(buf, rowStride, pixelStride, x, y, 2)
                r[k] = rr; g[k] = gg; b[k] = bb; k++
                if (minOf(rr, gg, bb) < WHITE_MIN) { rn[kn] = rr; gn[kn] = gg; bn[kn] = bb; kn++ }
                x++
            }
            y++
        }
        // < 5% non-white (a popup card etc.): fall back to all pixels.
        val useAll = kn < NON_WHITE_MIN_FRAC * n
        val cnt = if (useAll) k else kn
        val mr = median(if (useAll) r else rn, cnt)
        val mg = median(if (useAll) g else gn, cnt)
        val mb = median(if (useAll) b else bn, cnt)
        var bestExp = 0; var bestD2 = Double.MAX_VALUE
        for (p in palRgb.indices) {
            val dr = palRgb[p][0] - mr; val dg = palRgb[p][1] - mg; val db = palRgb[p][2] - mb
            val d2 = dr * dr + dg * dg + db * db
            if (d2 < bestD2) { bestD2 = d2; bestExp = palExp[p] }
        }
        return Pair(bestExp, Math.sqrt(bestD2))
    }

    /** numpy-compatible median: even counts average the two middle values. */
    private fun median(arr: IntArray, count: Int): Double {
        val a = arr.copyOf(count)
        a.sort()
        val m = count / 2
        return if (count % 2 == 1) a[m].toDouble() else (a[m - 1] + a[m]) / 2.0
    }
}
