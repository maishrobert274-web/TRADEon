plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "ke.co.tradeon.mobile"
    compileSdk = 35

    defaultConfig {
        applicationId = "ke.co.tradeon.mobile"
        minSdk = 23
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
    }

    flavorDimensions += "app"
    productFlavors {
        create("tradeon") {
            dimension = "app"
            applicationId = "ke.co.tradeon.mobile"
            buildConfigField("String", "SITE_URL", "\"https://tradeon-sw5q.onrender.com/\"")
            resValue("string", "app_name", "TRADEON")
        }
        create("manager") {
            dimension = "app"
            applicationId = "ke.co.tradeon.manager"
            buildConfigField("String", "SITE_URL", "\"https://tradeon-sw5q.onrender.com/manager/login\"")
            resValue("string", "app_name", "TRADEON Manager")
        }
    }

    buildFeatures {
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}
