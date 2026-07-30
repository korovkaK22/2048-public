plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "ai.bot2048.sampler"
    compileSdk = 34

    defaultConfig {
        applicationId = "ai.bot2048.sampler"
        minSdk = 29          // MediaProjection + abstract LocalServerSocket
        targetSdk = 33       // matches the reference device (Android 13)
        versionCode = 1
        versionName = "0.1"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    // Intentionally dependency-free (org.json ships with Android). No AndroidX
    // needed for this headless helper.
}
