package ai.bot2048.sampler

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.accessibility.AccessibilityEvent

/**
 * On-device swipe injector: replays a `(x1,y1,x2,y2,duration_ms)` command from
 * the host as a real touch gesture via [dispatchGesture], so the host no longer
 * spawns `adb shell input swipe` (and the device no longer forks a Dalvik
 * `input` process) once per move.
 *
 * WHY an AccessibilityService (the one API that can synthesize a touch off the
 * app's own window, no root): `dispatchGesture` is a method ON an
 * AccessibilityService, so the [SamplerService] (which owns the host socket)
 * reaches this instance through the [instance] singleton and forwards the
 * decoded [FramePacket.CMD_SWIPE] payload here.
 *
 * TRADE-OFF (documented, opt-in): the rest of this APK deliberately avoids an
 * AccessibilityService because banking apps scan for enabled ones. This service
 * is therefore SEPARATE and OFF by default -- it does nothing until the operator
 * enables it once in Settings -> Accessibility. It requests no events
 * (`accessibilityEventTypes` is minimal, `onAccessibilityEvent` is a no-op) and
 * reads NOTHING from the screen; it only emits gestures. If the operator prefers
 * not to run it, leave it disabled and the host stays on the `input swipe`
 * fallback with zero behavioural change.
 *
 * Only a swipe whose endpoints were already safe-zone clamped by the host
 * [bot2048.device.humanizer.Humanizer] is ever sent; this service changes the
 * DELIVERY of that gesture, never its geometry.
 */
class SwipeAccessibilityService : AccessibilityService() {

    private val mainHandler = Handler(Looper.getMainLooper())

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        Log.i(TAG, "SwipeAccessibilityService connected -- on-device swipe injection ready")
    }

    // No screen reading: this service exists only to emit gestures.
    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}

    override fun onInterrupt() {}

    override fun onUnbind(intent: android.content.Intent?): Boolean {
        if (instance === this) instance = null
        Log.i(TAG, "SwipeAccessibilityService unbound")
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    /**
     * Dispatch a straight-line swipe. Safe to call from any thread: the actual
     * [dispatchGesture] is posted to the main looper (the gesture engine expects
     * the service's main thread). Returns false if the platform rejected the
     * gesture synchronously (e.g. bad bounds); dispatch is otherwise async and
     * the host times the gesture by its own `duration_ms`.
     */
    fun dispatchSwipe(x1: Int, y1: Int, x2: Int, y2: Int, durationMs: Int): Boolean {
        val dur = if (durationMs < 1) 1L else durationMs.toLong()
        val path = Path().apply {
            moveTo(x1.toFloat(), y1.toFloat())
            lineTo(x2.toFloat(), y2.toFloat())
        }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0L, dur))
            .build()
        var accepted = true
        mainHandler.post {
            val ok = dispatchGesture(
                gesture,
                object : GestureResultCallback() {
                    override fun onCancelled(g: GestureDescription?) {
                        Log.w(TAG, "swipe gesture cancelled ($x1,$y1)->($x2,$y2)")
                    }
                },
                null,
            )
            if (!ok) Log.w(TAG, "dispatchGesture returned false ($x1,$y1)->($x2,$y2)")
        }
        return accepted
    }

    companion object {
        const val TAG = "BoardSampler"

        /**
         * The live service instance, or null when the operator has not enabled
         * it. [SamplerService] reads this to route [FramePacket.CMD_SWIPE].
         * Written on the main thread (connect/unbind), read from the socket
         * command thread -- volatile for visibility.
         */
        @Volatile
        var instance: SwipeAccessibilityService? = null
    }
}
