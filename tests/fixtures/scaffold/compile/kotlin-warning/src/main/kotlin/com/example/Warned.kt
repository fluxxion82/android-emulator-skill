package com.example

@Deprecated("Use current() instead.")
fun retired(): Int = 41

/** Compiles, with a deprecation warning at the call site. */
fun warned(): Int = retired()
