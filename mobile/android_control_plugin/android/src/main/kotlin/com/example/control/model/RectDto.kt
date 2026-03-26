package com.example.control.model

data class RectDto(
    val left: Int,
    val top: Int,
    val right: Int,
    val bottom: Int,
) {
    fun toMap(): Map<String, Any> = mapOf(
        "left" to left,
        "top" to top,
        "right" to right,
        "bottom" to bottom,
    )
}
