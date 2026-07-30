package ai.bot2048.sampler

import android.app.Activity
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle

/**
 * Trivial launcher: asks for MediaProjection consent, then hands the grant to
 * [SamplerService] and finishes. The operator taps "Start now" once per session
 * (the only manual step). No other UI.
 */
class MainActivity : Activity() {
    private val requestCode = 1001

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val mpm = getSystemService(MediaProjectionManager::class.java)
        startActivityForResult(mpm.createScreenCaptureIntent(), requestCode)
    }

    @Deprecated("startActivityForResult is fine for a one-screen helper")
    override fun onActivityResult(rc: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(rc, resultCode, data)
        if (rc == requestCode && resultCode == RESULT_OK && data != null) {
            val svc = Intent(this, SamplerService::class.java)
                .putExtra(SamplerService.EXTRA_CODE, resultCode)
                .putExtra(SamplerService.EXTRA_DATA, data)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) startForegroundService(svc)
            else startService(svc)
        }
        finish()
    }
}
