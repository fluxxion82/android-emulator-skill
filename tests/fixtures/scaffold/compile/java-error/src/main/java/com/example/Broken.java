package com.example;

/** Does not compile: `MissingType` does not exist. */
public class Broken {
    public void call() {
        MissingType value = new MissingType();
    }
}
