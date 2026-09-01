package com.example;

/** Compiles, with a deprecation warning at the call site. */
public class Warned {
    public void call() {
        new Retired().retired();
    }
}
