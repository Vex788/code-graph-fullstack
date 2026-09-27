package com.acme.audit;

import java.time.Instant;
public class AuditEntry {

    private final String action;
    private final Instant at;

    public AuditEntry(String action, Instant at) {
        this.action = action;
        this.at = at;
    }

    public String getAction() {
        return action;
    }

    public Instant getAt() {
        return at;
    }
}
