package com.acme.model;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.Id;
import javax.persistence.Table;
@Entity
@Table(name = "vendors")
public class Vendor {

    @Id
    @Column(name = "vendor_code")
    private String code;

    @Column(name = "display_name")
    private String displayName;

    public String getCode() {
        return code;
    }

    public String getDisplayName() {
        return displayName;
    }
}
