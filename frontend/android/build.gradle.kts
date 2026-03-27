import java.util.Properties
import java.io.File

allprojects {
    repositories {
        google()
        mavenCentral()
    }
}

// Set the custom build directory for the root project
val newBuildDir: File = rootProject.layout.buildDirectory
    .dir("../../build") // Ensure this path resolves correctly
    .get()
rootProject.layout.buildDirectory.value(newBuildDir)

// Set the custom build directory for each subproject
subprojects {
    val newSubprojectBuildDir: File = newBuildDir.resolve(project.name)
    project.layout.buildDirectory.value(newSubprojectBuildDir)
}

// Ensure that the 'app' project is evaluated before other subprojects
subprojects {
    project.evaluationDependsOn(":app")
}

// Clean task to delete the custom build directory
tasks.register<Delete>("clean") {
    delete(newBuildDir) // Clean the custom build directory
}

// Properties loading example
val localProperties = Properties().apply {
    val propertiesFile = rootProject.file("local.properties")
    if (propertiesFile.exists()) {
        propertiesFile.inputStream().use { load(it) }
    }
}

// Kotlin options for JVM target
tasks.withType<KotlinCompile> {
    kotlinOptions {
        jvmTarget = "17" // Updated for Kotlin 1.7 and above
    }
}

// Optional: Add a log to verify the custom build directory path
println("New Build Directory: ${newBuildDir.absolutePath}")