package com.acme.latest;

import java.time.Instant;
public class RateSnapshot {

    private final Instant takenAt;

    public RateSnapshot(Instant takenAt) {
        this.takenAt = takenAt;
    }

    public Instant takenAt() {
        return takenAt;
    }
}
