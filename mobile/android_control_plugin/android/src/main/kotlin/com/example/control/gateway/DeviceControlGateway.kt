package com.example.control.gateway

import com.example.control.model.ActionResultDto
import com.example.control.model.ScreenStateDto
import com.example.control.model.SelectorDto
import com.example.control.model.SessionConfigDto
import com.example.control.model.UiNodeDto

/**
 * Surface exposed to the Flutter MethodChannel bridge (Stage 4).
 * All methods are synchronous on the Android side; the Flutter layer
 * calls them on a background thread.
 *
 * Implemented by AutomationAccessibilityService.
 */
interface DeviceControlGateway {

    // ── Permission / session lifecycle ────────────────────────────────────────

    /**
     * Returns a map of permission name → granted status.
     * Keys: "accessibilityService", "overlayPermission".
     */
    fun getPermissionStatus(): Map<String, Boolean>

    /** Start tracking the given session. Does NOT grant accessibility permission. */
    fun startSession(config: SessionConfigDto): ActionResultDto

    /** Stop the active session and clear all transient state. */
    fun stopSession(sessionId: String): ActionResultDto

    // ── App inspection ────────────────────────────────────────────────────────

    /** Returns the foreground package name, or null if unavailable. */
    fun getForegroundApp(): String?

    /** Returns true if the package is installed (declared in <queries>). */
    fun isPackageInstalled(packageName: String): Boolean

    /** Launch an app by package name. Fails if not in <queries>. */
    fun launchApp(packageName: String): ActionResultDto

    // ── Screen state ──────────────────────────────────────────────────────────

    /**
     * Capture the current accessibility node tree and return a ScreenStateDto.
     * nodes is empty when isSensitive = true.
     */
    fun getScreenState(): ScreenStateDto

    // ── Element lookup ────────────────────────────────────────────────────────

    /** Find the first node matching the selector, or null. */
    fun findElement(selector: SelectorDto): UiNodeDto?

    /** Find all nodes matching the selector. */
    fun findElements(selector: SelectorDto): List<UiNodeDto>

    // ── Actions ───────────────────────────────────────────────────────────────

    fun tapElement(selector: SelectorDto): ActionResultDto
    fun longPressElement(selector: SelectorDto): ActionResultDto
    fun focusElement(selector: SelectorDto): ActionResultDto

    /**
     * Type text into the currently focused editable field.
     * Uses ACTION_SET_TEXT (replaces content).
     */
    fun typeText(text: String): ActionResultDto

    /** Clear the currently focused editable field via ACTION_SET_TEXT(""). */
    fun clearFocusedField(): ActionResultDto

    /**
     * Scroll the nearest scrollable container.
     * direction: "up" | "down" | "left" | "right"
     */
    fun scroll(direction: String): ActionResultDto

    /**
     * Dispatch a freeform gesture via GestureDescription.
     * Requires canPerformGestures = true in the accessibility config.
     * Only permitted when PolicyConfig.allow_coordinates_fallback is true.
     */
    fun swipe(startX: Int, startY: Int, endX: Int, endY: Int, durationMs: Long): ActionResultDto

    fun goBack(): ActionResultDto
    fun goHome(): ActionResultDto
}
