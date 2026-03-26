plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.example.control"
    compileSdk = 35

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    defaultConfig {
        minSdk = 26  // AccessibilityService ACTION_SET_TEXT requires API 21; GestureDescription requires API 24;
                     // foregroundServiceType requires API 29; we target 26+ for modern accessibility APIs.
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    compileOnly("io.flutter:flutter_embedding_debug:1.0.0-3.27.4") {
        isChanging = true
    }
}
