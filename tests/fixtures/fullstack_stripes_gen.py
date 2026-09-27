"""Deterministic generator for the fullstack Stripes/Hibernate/JSP fixture.

Writes a Maven-layout Java 17 web application in the shape of PMS: Stripes
ActionBeans, Spring-injected services, Hibernate 5.6 DAOs and entities (both
annotated and ``*.hbm.xml`` mapped), JSP pages under ``web/WEB-INF/jsp`` with
jQuery calls back to ``*.action`` URLs, and per-page CSS.

The small base app is committed under ``tests/fixtures/fullstack_stripes/``
and its golden graph lives in ``expected_edges.tsv`` next to it. ``--scale N``
adds generated modules until the tree holds about N files; that variant is
for benchmarks and is written to a temporary directory, never committed.

Usage::

    python tests/fixtures/fullstack_stripes_gen.py                 # refresh the committed copy
    python tests/fixtures/fullstack_stripes_gen.py --out DIR --scale 2000
    python tests/fixtures/fullstack_stripes_gen.py --check         # exit 1 if the copy drifted
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fullstack_stripes"
# Files in the committed fixture directory that the generator does not own.
NON_GENERATED = frozenset({"expected_edges.tsv"})

J = "src/main/java/com/acme"
R = "src/main/resources"
T = "src/test/java/com/acme"
W = "web"

_STRIPES = "net.sourceforge.stripes"


def _java(package: str, imports: list[str], body: str) -> str:
    lines = [f"package {package};", ""]
    if imports:
        lines.extend(f"import {name};" for name in imports)
        lines.append("")
    return "\n".join(lines) + body.strip("\n") + "\n"


def _base_files() -> dict[str, str]:
    files: dict[str, str] = {}

    files["pom.xml"] = """\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.acme</groupId>
  <artifactId>acme-vendor-portal</artifactId>
  <version>1.0.0-SNAPSHOT</version>
  <packaging>war</packaging>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
    <hibernate.version>5.6.15.Final</hibernate.version>
    <stripes.version>1.6.0</stripes.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>net.sourceforge.stripes</groupId>
      <artifactId>stripes</artifactId>
      <version>${stripes.version}</version>
    </dependency>
    <dependency>
      <groupId>org.hibernate</groupId>
      <artifactId>hibernate-core</artifactId>
      <version>${hibernate.version}</version>
    </dependency>
    <dependency>
      <groupId>org.springframework</groupId>
      <artifactId>spring-context</artifactId>
      <version>5.3.39</version>
    </dependency>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
      <version>4.13.2</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
</project>
"""

    # ------------------------------------------------------------------ model
    files[f"{J}/model/User.java"] = _java(
        "com.acme.model",
        [
            "javax.persistence.Column",
            "javax.persistence.Entity",
            "javax.persistence.GeneratedValue",
            "javax.persistence.Id",
            "javax.persistence.Table",
        ],
        """
@Entity
@Table(name = "users")
public class User {

    @Id
    @GeneratedValue
    @Column(name = "user_id")
    private Long id;

    @Column(name = "user_name", nullable = false, length = 64)
    private String name;

    @Column(name = "email")
    private String email;

    @Column(name = "active")
    private boolean active;

    public Long getId() {
        return id;
    }

    public void setId(Long id) {
        this.id = id;
    }

    public String getName() {
        return name;
    }

    public void setName(String name) {
        this.name = name;
    }

    public String getEmail() {
        return email;
    }

    public void setEmail(String email) {
        this.email = email;
    }

    public boolean isActive() {
        return active;
    }

    public void setActive(boolean active) {
        this.active = active;
    }
}
""",
    )

    files[f"{J}/model/Invoice.java"] = _java(
        "com.acme.model",
        [
            "javax.persistence.Column",
            "javax.persistence.Entity",
            "javax.persistence.EnumType",
            "javax.persistence.Enumerated",
            "javax.persistence.GeneratedValue",
            "javax.persistence.Id",
            "javax.persistence.ManyToOne",
            "javax.persistence.Table",
        ],
        """
@Entity
@Table(name = "invoices")
public class Invoice {

    @Id
    @GeneratedValue
    @Column(name = "invoice_id")
    private Long id;

    @Column(name = "amount_cents", nullable = false)
    private long amount;

    @Column(name = "currency", length = 3)
    private String currency;

    @Column(name = "vendor_code")
    private String vendorCode;

    @Enumerated(EnumType.STRING)
    @Column(name = "status")
    private InvoiceStatus status = InvoiceStatus.DRAFT;

    @ManyToOne
    private Vendor vendor;

    public Long getId() {
        return id;
    }

    public long getAmount() {
        return amount;
    }

    public void setAmount(long amount) {
        this.amount = amount;
    }

    public String getCurrency() {
        return currency;
    }

    public void setCurrency(String currency) {
        this.currency = currency;
    }

    public String getVendorCode() {
        return vendorCode;
    }

    public void setVendorCode(String vendorCode) {
        this.vendorCode = vendorCode;
    }

    public InvoiceStatus getStatus() {
        return status;
    }

    public void setStatus(InvoiceStatus status) {
        this.status = status;
    }

    public Vendor getVendor() {
        return vendor;
    }

    public void setVendor(Vendor vendor) {
        this.vendor = vendor;
    }
}
""",
    )

    files[f"{J}/model/Vendor.java"] = _java(
        "com.acme.model",
        [
            "javax.persistence.Column",
            "javax.persistence.Entity",
            "javax.persistence.Id",
            "javax.persistence.Table",
        ],
        """
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
""",
    )

    files[f"{J}/model/Order.java"] = _java(
        "com.acme.model",
        ["java.util.ArrayList", "java.util.List"],
        """
/** Mapped by Order.hbm.xml, not by annotations. */
public class Order {

    private Long id;
    private User customer;
    private List<OrderLine> lines = new ArrayList<>();

    public Long getId() {
        return id;
    }

    public User getCustomer() {
        return customer;
    }

    public void setCustomer(User customer) {
        this.customer = customer;
    }

    public List<OrderLine> getLines() {
        return lines;
    }

    public Money total(String currency) {
        long cents = 0;
        for (OrderLine line : lines) {
            cents += line.amountCents();
        }
        return new Money(cents, currency);
    }
}
""",
    )

    files[f"{J}/model/OrderLine.java"] = _java(
        "com.acme.model",
        [],
        """
public class OrderLine {

    private String sku;
    private int quantity;
    private long unitCents;

    public long amountCents() {
        return unitCents * quantity;
    }

    public String getSku() {
        return sku;
    }
}
""",
    )

    files[f"{J}/model/Money.java"] = _java(
        "com.acme.model",
        ["java.util.Objects"],
        """
public record Money(long cents, String currency) {

    public Money {
        Objects.requireNonNull(currency, "currency");
    }

    public Money plus(Money other) {
        if (!currency.equals(other.currency())) {
            throw new IllegalArgumentException("currency mismatch");
        }
        return new Money(cents + other.cents(), currency);
    }

    public String format() {
        return String.format("%d.%02d %s", cents / 100, cents % 100, currency);
    }
}
""",
    )

    files[f"{J}/model/InvoiceStatus.java"] = _java(
        "com.acme.model",
        [],
        """
public enum InvoiceStatus {
    DRAFT,
    SUBMITTED,
    PAID;

    public boolean isOpen() {
        return this != PAID;
    }
}
""",
    )

    # -------------------------------------------------------------------- dao
    files[f"{J}/dao/GenericDao.java"] = _java(
        "com.acme.dao",
        ["java.util.List"],
        """
public interface GenericDao<T> {

    T findById(Long id);

    void save(T entity);

    List<T> findAll();
}
""",
    )

    files[f"{J}/dao/UserDaoApi.java"] = _java(
        "com.acme.dao",
        ["com.acme.model.User", "java.util.List"],
        """
public interface UserDaoApi extends GenericDao<User> {

    List<User> findAllSortedByName();
}
""",
    )

    files[f"{J}/dao/HibernateSupport.java"] = _java(
        "com.acme.dao",
        ["org.hibernate.Session", "org.hibernate.SessionFactory"],
        """
public abstract class HibernateSupport {

    private final SessionFactory sessionFactory;

    protected HibernateSupport(SessionFactory sessionFactory) {
        this.sessionFactory = sessionFactory;
    }

    protected Session currentSession() {
        return sessionFactory.getCurrentSession();
    }
}
""",
    )

    files[f"{J}/dao/UserDao.java"] = _java(
        "com.acme.dao",
        [
            "com.acme.model.User",
            "java.util.ArrayList",
            "java.util.Collections",
            "java.util.Comparator",
            "java.util.List",
            "org.hibernate.Session",
            "org.hibernate.SessionFactory",
        ],
        """
public class UserDao extends HibernateSupport implements UserDaoApi {

    public UserDao(SessionFactory sessionFactory) {
        super(sessionFactory);
    }

    @Override
    public User findById(Long id) {
        return currentSession().get(User.class, id);
    }

    @Override
    public void save(User u) {
        save(u, false);
    }

    public void save(User u, boolean flush) {
        Session session = currentSession();
        session.save(u);
        if (flush) {
            session.flush();
        }
    }

    @Override
    public List<User> findAll() {
        return currentSession().createQuery("from User", User.class).list();
    }

    @Override
    public List<User> findAllSortedByName() {
        List<User> users = new ArrayList<>(findAll());
        Collections.sort(users, new Comparator<User>() {
            @Override
            public int compare(User a, User b) {
                return a.getName().compareTo(b.getName());
            }
        });
        return users;
    }
}
""",
    )

    files[f"{J}/dao/OrderDao.java"] = _java(
        "com.acme.dao",
        [
            "com.acme.model.Order",
            "java.util.List",
            "org.hibernate.Session",
            "org.hibernate.SessionFactory",
        ],
        """
public class OrderDao extends HibernateSupport {

    public OrderDao(SessionFactory sessionFactory) {
        super(sessionFactory);
    }

    public Order load(Long id) {
        return currentSession().get(Order.class, id);
    }

    public void persist(Order order) {
        Session session = currentSession();
        session.save(order);
    }

    public List<Order> findOpen() {
        return currentSession().createQuery("from Order o where o.closed = false", Order.class)
            .list();
    }
}
""",
    )

    files[f"{J}/dao/InvoiceDao.java"] = _java(
        "com.acme.dao",
        [
            "com.acme.model.Invoice",
            "java.util.List",
            "org.hibernate.SessionFactory",
        ],
        """
public class InvoiceDao extends HibernateSupport {

    public InvoiceDao(SessionFactory sessionFactory) {
        super(sessionFactory);
    }

    public Invoice find(Long id) {
        return currentSession().get(Invoice.class, id);
    }

    public void store(Invoice invoice) {
        currentSession().saveOrUpdate(invoice);
    }

    public List<Invoice> findByVendor(String vendorCode) {
        return currentSession()
            .createQuery("from Invoice i where i.vendorCode = :code", Invoice.class)
            .setParameter("code", vendorCode)
            .list();
    }
}
""",
    )

    # ---------------------------------------------------------------- service
    files[f"{J}/service/InvoiceService.java"] = _java(
        "com.acme.service",
        ["com.acme.model.Invoice", "com.acme.model.Money", "java.util.List"],
        """
public interface InvoiceService {

    Invoice findInvoice(Long id);

    void saveInvoice(Invoice invoice, Money total);

    List<Invoice> listForVendor(String vendorCode);
}
""",
    )

    files[f"{J}/service/InvoiceServiceImpl.java"] = _java(
        "com.acme.service",
        [
            "static com.acme.util.Strings.isBlank",
            "com.acme.dao.InvoiceDao",
            "com.acme.model.Invoice",
            "com.acme.model.InvoiceStatus",
            "com.acme.model.Money",
            "java.util.List",
            "org.springframework.stereotype.Service",
            "org.springframework.transaction.annotation.Transactional",
        ],
        """
@Service("invoiceService")
@Transactional
public class InvoiceServiceImpl implements InvoiceService {

    private final InvoiceDao invoiceDao;

    public InvoiceServiceImpl(InvoiceDao invoiceDao) {
        this.invoiceDao = invoiceDao;
    }

    @Override
    public Invoice findInvoice(Long id) {
        return invoiceDao.find(id);
    }

    @Override
    public void saveInvoice(Invoice invoice, Money total) {
        if (isBlank(invoice.getVendorCode())) {
            throw new IllegalArgumentException("vendor code is required");
        }
        invoice.setAmount(total.cents());
        invoice.setCurrency(total.currency());
        invoice.setStatus(InvoiceStatus.SUBMITTED);
        invoiceDao.store(invoice);
    }

    @Override
    public List<Invoice> listForVendor(String vendorCode) {
        return invoiceDao.findByVendor(vendorCode);
    }
}
""",
    )

    files[f"{J}/service/UserService.java"] = _java(
        "com.acme.service",
        [
            "com.acme.dao.UserDao",
            "com.acme.model.User",
            "java.util.List",
            "org.springframework.stereotype.Service",
        ],
        """
@Service("userService")
public class UserService {

    private final UserDao userDao;

    public UserService(UserDao userDao) {
        this.userDao = userDao;
    }

    public void register(User user) {
        user.setActive(true);
        userDao.save(user);
    }

    public void registerAndFlush(User user) {
        userDao.save(user, true);
    }

    public List<User> listUsers() {
        return userDao.findAllSortedByName();
    }
}
""",
    )

    files[f"{J}/service/OrderService.java"] = _java(
        "com.acme.service",
        [
            "com.acme.dao.OrderDao",
            "com.acme.model.Money",
            "com.acme.model.Order",
            "org.springframework.stereotype.Service",
        ],
        """
@Service("orderService")
public class OrderService {

    private final OrderDao orderDao;

    public OrderService(OrderDao orderDao) {
        this.orderDao = orderDao;
    }

    public Order find(Long id) {
        return orderDao.load(id);
    }

    public Money place(Order order) {
        orderDao.persist(order);
        return order.total("USD");
    }
}
""",
    )

    # ------------------------------------------------------------------- util
    files[f"{J}/util/Strings.java"] = _java(
        "com.acme.util",
        [],
        """
public final class Strings {

    private Strings() {
    }

    public static boolean isBlank(String value) {
        return value == null || value.trim().isEmpty();
    }

    public static String abbreviate(String value, int max) {
        if (value == null || value.length() <= max) {
            return value;
        }
        return value.substring(0, max - 1) + "...";
    }
}
""",
    )

    files[f"{J}/util/Dates.java"] = _java(
        "com.acme.util",
        ["java.time.LocalDate", "java.time.format.DateTimeFormatter"],
        """
public final class Dates {

    private static final DateTimeFormatter ISO = DateTimeFormatter.ISO_LOCAL_DATE;

    private Dates() {
    }

    public static String iso(LocalDate date) {
        return date == null ? "" : ISO.format(date);
    }
}
""",
    )

    # ------------------------------------------------ production "latest" code
    files[f"{J}/latest/LatestRatesService.java"] = _java(
        "com.acme.latest",
        ["com.acme.model.Money", "java.util.HashMap", "java.util.Map"],
        """
/** Production code: the package name only looks like a test directory. */
public class LatestRatesService {

    private final Map<String, Long> ratesPerMille = new HashMap<>();

    public void publish(String currency, long perMille) {
        ratesPerMille.put(currency, perMille);
    }

    public Money convert(Money amount, String target) {
        long rate = ratesPerMille.getOrDefault(target, 1000L);
        return new Money(amount.cents() * rate / 1000, target);
    }
}
""",
    )

    files[f"{J}/latest/RateSnapshot.java"] = _java(
        "com.acme.latest",
        ["java.time.Instant"],
        """
public class RateSnapshot {

    private final Instant takenAt;

    public RateSnapshot(Instant takenAt) {
        this.takenAt = takenAt;
    }

    public Instant takenAt() {
        return takenAt;
    }
}
""",
    )

    # ------------------------------------------------------------------ audit
    files[f"{J}/audit/AuditLogWriter.java"] = _java(
        "com.acme.audit",
        ["org.hibernate.Session", "org.hibernate.SessionFactory"],
        """
/** Writes audit rows straight through the Hibernate session. */
public class AuditLogWriter {

    private final SessionFactory sessionFactory;

    public AuditLogWriter(SessionFactory sessionFactory) {
        this.sessionFactory = sessionFactory;
    }

    public void write(AuditEntry entry) {
        Session session = sessionFactory.getCurrentSession();
        session.save(entry);
    }
}
""",
    )

    files[f"{J}/audit/AuditEntry.java"] = _java(
        "com.acme.audit",
        ["java.time.Instant"],
        """
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
""",
    )

    # ---------------------------------------------------------------- report
    files[f"{J}/report/InvoiceReport.java"] = _java(
        "com.acme.report",
        [
            "static com.acme.util.Strings.abbreviate",
            "com.acme.model.Invoice",
            "com.acme.service.InvoiceService",
            "java.util.List",
        ],
        """
public class InvoiceReport {

    private final InvoiceService invoiceService;

    public InvoiceReport(InvoiceService invoiceService) {
        this.invoiceService = invoiceService;
    }

    public String render(String vendorCode) {
        StringBuilder out = new StringBuilder();
        List<Invoice> invoices = invoiceService.listForVendor(vendorCode);
        for (Invoice invoice : invoices) {
            out.append(abbreviate(invoice.getVendorCode(), 12)).append('\\n');
        }
        return out.toString();
    }
}
""",
    )

    # ------------------------------------------------------------------- web
    files[f"{J}/web/action/BaseActionBean.java"] = _java(
        "com.acme.web.action",
        [f"{_STRIPES}.action.ActionBean", f"{_STRIPES}.action.ActionBeanContext"],
        """
public abstract class BaseActionBean implements ActionBean {

    private ActionBeanContext context;

    @Override
    public ActionBeanContext getContext() {
        return context;
    }

    @Override
    public void setContext(ActionBeanContext context) {
        this.context = context;
    }
}
""",
    )

    files[f"{J}/web/action/VendorInvoiceActionBean.java"] = _java(
        "com.acme.web.action",
        [
            "com.acme.model.Invoice",
            "com.acme.model.Money",
            "com.acme.service.InvoiceService",
            f"{_STRIPES}.action.ActionBean",
            f"{_STRIPES}.action.ActionBeanContext",
            f"{_STRIPES}.action.DefaultHandler",
            f"{_STRIPES}.action.ForwardResolution",
            f"{_STRIPES}.action.HandlesEvent",
            f"{_STRIPES}.action.RedirectResolution",
            f"{_STRIPES}.action.Resolution",
            f"{_STRIPES}.action.StreamingResolution",
            f"{_STRIPES}.action.UrlBinding",
            f"{_STRIPES}.integration.spring.SpringBean",
            f"{_STRIPES}.validation.Validate",
            f"{_STRIPES}.validation.ValidateNestedProperties",
        ],
        """
@UrlBinding("/vendor/Invoice.action")
public class VendorInvoiceActionBean implements ActionBean {

    private ActionBeanContext context;

    @SpringBean
    private InvoiceService service;

    @ValidateNestedProperties({
        @Validate(field = "amount", required = true, minvalue = 0),
        @Validate(field = "currency", required = true, maxlength = 3),
        @Validate(field = "vendorCode", required = true, maxlength = 32)
    })
    private Invoice invoice;

    private Long invoiceId;

    @Override
    public ActionBeanContext getContext() {
        return context;
    }

    @Override
    public void setContext(ActionBeanContext context) {
        this.context = context;
    }

    public Invoice getInvoice() {
        return invoice;
    }

    public void setInvoice(Invoice invoice) {
        this.invoice = invoice;
    }

    public Long getInvoiceId() {
        return invoiceId;
    }

    public void setInvoiceId(Long invoiceId) {
        this.invoiceId = invoiceId;
    }

    @DefaultHandler
    public Resolution view() {
        if (invoiceId != null) {
            invoice = service.findInvoice(invoiceId);
        }
        return new ForwardResolution("/WEB-INF/jsp/vendor/invoice.jsp");
    }

    @HandlesEvent("save")
    public Resolution save() {
        Money total = new Money(invoice.getAmount(), invoice.getCurrency());
        service.saveInvoice(invoice, total);
        return new RedirectResolution(VendorInvoiceActionBean.class)
            .addParameter("invoiceId", invoice.getId());
    }

    @HandlesEvent("load")
    public Resolution load() {
        Invoice found = service.findInvoice(invoiceId);
        String json = "{\\"amount\\":" + found.getAmount() + "}";
        return new StreamingResolution("application/json", json);
    }

    @HandlesEvent("list")
    public Resolution list() {
        return new ForwardResolution("/WEB-INF/jsp/vendor/invoice_list.jsp");
    }
}
""",
    )

    files[f"{J}/web/action/UserListActionBean.java"] = _java(
        "com.acme.web.action",
        [
            "com.acme.model.User",
            "com.acme.service.UserService",
            "java.util.List",
            f"{_STRIPES}.action.DefaultHandler",
            f"{_STRIPES}.action.ForwardResolution",
            f"{_STRIPES}.action.HandlesEvent",
            f"{_STRIPES}.action.RedirectResolution",
            f"{_STRIPES}.action.Resolution",
            f"{_STRIPES}.action.UrlBinding",
            f"{_STRIPES}.integration.spring.SpringBean",
        ],
        """
@UrlBinding("/user/List.action")
public class UserListActionBean extends BaseActionBean {

    @SpringBean
    private UserService userService;

    private User user;

    private List<User> users;

    public User getUser() {
        return user;
    }

    public void setUser(User user) {
        this.user = user;
    }

    public List<User> getUsers() {
        return users;
    }

    @DefaultHandler
    public Resolution list() {
        users = userService.listUsers();
        return new ForwardResolution("/WEB-INF/jsp/user/list.jsp");
    }

    @HandlesEvent("register")
    public Resolution register() {
        userService.register(user);
        return new RedirectResolution("/user/List.action");
    }
}
""",
    )

    files[f"{J}/web/action/OrderActionBean.java"] = _java(
        "com.acme.web.action",
        [
            "com.acme.model.Money",
            "com.acme.model.Order",
            "com.acme.service.OrderService",
            f"{_STRIPES}.action.DefaultHandler",
            f"{_STRIPES}.action.ForwardResolution",
            f"{_STRIPES}.action.HandlesEvent",
            f"{_STRIPES}.action.Resolution",
            f"{_STRIPES}.action.UrlBinding",
            f"{_STRIPES}.integration.spring.SpringBean",
        ],
        """
@UrlBinding("/order/Order.action")
public class OrderActionBean extends BaseActionBean {

    @SpringBean("orderService")
    private OrderService orderService;

    private Long orderId;

    private Order order;

    private Money total;

    public Long getOrderId() {
        return orderId;
    }

    public void setOrderId(Long orderId) {
        this.orderId = orderId;
    }

    public Order getOrder() {
        return order;
    }

    public Money getTotal() {
        return total;
    }

    @DefaultHandler
    public Resolution view() {
        order = orderService.find(orderId);
        return new ForwardResolution("/WEB-INF/jsp/order/view.jsp");
    }

    @HandlesEvent("place")
    public Resolution place() {
        order = orderService.find(orderId);
        total = orderService.place(order);
        return new ForwardResolution("/WEB-INF/jsp/order/view.jsp");
    }
}
""",
    )

    # -------------------------------------------------------------- resources
    files[f"{R}/com/acme/model/Order.hbm.xml"] = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE hibernate-mapping PUBLIC
    "-//Hibernate/Hibernate Mapping DTD 3.0//EN"
    "http://www.hibernate.org/dtd/hibernate-mapping-3.0.dtd">
<hibernate-mapping package="com.acme.model">
  <class name="Order" table="orders">
    <id name="id" column="order_id">
      <generator class="native"/>
    </id>
    <many-to-one name="customer" class="User" column="customer_id"/>
    <property name="closed" column="closed" type="boolean"/>
    <bag name="lines" table="order_lines" cascade="all">
      <key column="order_id"/>
      <composite-element class="OrderLine">
        <property name="sku" column="sku"/>
        <property name="quantity" column="quantity"/>
        <property name="unitCents" column="unit_cents"/>
      </composite-element>
    </bag>
  </class>
</hibernate-mapping>
"""

    files[f"{R}/hibernate.cfg.xml"] = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE hibernate-configuration PUBLIC
    "-//Hibernate/Hibernate Configuration DTD 3.0//EN"
    "http://www.hibernate.org/dtd/hibernate-configuration-3.0.dtd">
<hibernate-configuration>
  <session-factory>
    <property name="hibernate.dialect">org.hibernate.dialect.MySQL57Dialect</property>
    <property name="hibernate.current_session_context_class">thread</property>
    <mapping class="com.acme.model.User"/>
    <mapping class="com.acme.model.Invoice"/>
    <mapping class="com.acme.model.Vendor"/>
    <mapping resource="com/acme/model/Order.hbm.xml"/>
  </session-factory>
</hibernate-configuration>
"""

    files[f"{R}/applicationContext.xml"] = """\
<?xml version="1.0" encoding="UTF-8"?>
<beans xmlns="http://www.springframework.org/schema/beans"
       xmlns:context="http://www.springframework.org/schema/context">
  <context:component-scan base-package="com.acme.service"/>
  <bean id="userDao" class="com.acme.dao.UserDao">
    <constructor-arg ref="sessionFactory"/>
  </bean>
  <bean id="orderDao" class="com.acme.dao.OrderDao">
    <constructor-arg ref="sessionFactory"/>
  </bean>
  <bean id="invoiceDao" class="com.acme.dao.InvoiceDao">
    <constructor-arg ref="sessionFactory"/>
  </bean>
</beans>
"""

    files[f"{R}/StripesResources.properties"] = """\
invoice.amount=Amount
invoice.currency=Currency
invoice.vendorCode=Vendor code
validation.required.valueNotPresent={0} is required
"""

    # ------------------------------------------------------------------ tests
    files[f"{T}/dao/UserDaoTest.java"] = _java(
        "com.acme.dao",
        [
            "static org.junit.Assert.assertEquals",
            "static org.mockito.Mockito.mock",
            "static org.mockito.Mockito.verify",
            "static org.mockito.Mockito.when",
            "com.acme.model.User",
            "org.hibernate.Session",
            "org.hibernate.SessionFactory",
            "org.junit.Before",
            "org.junit.Test",
        ],
        """
public class UserDaoTest {

    private Session session;
    private UserDao dao;

    @Before
    public void setUp() {
        SessionFactory factory = mock(SessionFactory.class);
        session = mock(Session.class);
        when(factory.getCurrentSession()).thenReturn(session);
        dao = new UserDao(factory);
    }

    @Test
    public void saveDelegatesToSession() {
        User user = new User();
        dao.save(user);
        verify(session).save(user);
    }

    @Test
    public void saveWithFlushFlushes() {
        User user = new User();
        dao.save(user, true);
        verify(session).flush();
        assertEquals(null, user.getId());
    }
}
""",
    )

    files[f"{T}/model/MoneyTest.java"] = _java(
        "com.acme.model",
        ["static org.junit.Assert.assertEquals", "org.junit.Test"],
        """
public class MoneyTest {

    @Test
    public void plusAddsCents() {
        Money a = new Money(150, "USD");
        Money b = new Money(275, "USD");
        assertEquals(425, a.plus(b).cents());
    }

    @Test
    public void formatPadsCents() {
        assertEquals("1.05 USD", new Money(105, "USD").format());
    }
}
""",
    )

    # -------------------------------------------------------------------- web
    files[f"{W}/WEB-INF/web.xml"] = """\
<?xml version="1.0" encoding="UTF-8"?>
<web-app xmlns="http://xmlns.jcp.org/xml/ns/javaee" version="3.1">
  <filter>
    <filter-name>StripesFilter</filter-name>
    <filter-class>net.sourceforge.stripes.controller.StripesFilter</filter-class>
    <init-param>
      <param-name>ActionResolver.Packages</param-name>
      <param-value>com.acme.web.action</param-value>
    </init-param>
  </filter>
  <servlet>
    <servlet-name>DispatcherServlet</servlet-name>
    <servlet-class>net.sourceforge.stripes.controller.DispatcherServlet</servlet-class>
  </servlet>
  <servlet-mapping>
    <servlet-name>DispatcherServlet</servlet-name>
    <url-pattern>*.action</url-pattern>
  </servlet-mapping>
</web-app>
"""

    files[f"{W}/WEB-INF/jsp/common/header.jspf"] = """\
<%@ taglib prefix="stripes" uri="http://stripes.sourceforge.net/stripes.tld" %>
<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>
<link rel="stylesheet" href="/css/common.css"/>
<script src="/js/jquery-3.7.1.min.js"></script>
<script src="/js/common.js"></script>
<div class="page-header">
  <a href="/user/List.action">Users</a>
  <a href="/vendor/Invoice.action">Invoices</a>
</div>
"""

    files[f"{W}/WEB-INF/jsp/common/footer.jspf"] = """\
<div class="page-footer">
  <span class="copyright">ACME Vendor Portal</span>
</div>
"""

    files[f"{W}/WEB-INF/jsp/vendor/invoice.jsp"] = """\
<%@ page contentType="text/html;charset=UTF-8" %>
<%@ include file="/WEB-INF/jsp/common/header.jspf" %>
<link rel="stylesheet" href="/css/invoice.css"/>
<script src="/js/invoice.js"></script>
<h1>Vendor invoice</h1>
<stripes:form beanclass="com.acme.web.action.VendorInvoiceActionBean" class="invoice-form">
  <stripes:errors/>
  <stripes:hidden name="invoiceId"/>
  <label for="amount">Amount</label>
  <stripes:text id="amount" name="invoice.amount" class="invoice-amount"/>
  <stripes:text name="invoice.currency" class="invoice-currency"/>
  <input type="text" name="invoice.vendorCode" class="invoice-vendor"/>
  <stripes:submit name="save" value="Save" class="invoice-save"/>
</stripes:form>
<div id="invoice-total" class="invoice-total"></div>
<script type="text/javascript">
  var ctx = '${pageContext.request.contextPath}';
  $(function () {
    $('.invoice-form').on('submit', function (event) {
      event.preventDefault();
      $.post(ctx + '/vendor/Invoice.action', $(this).serialize(), function (data) {
        $('#invoice-total').text(data.amount);
      });
    });
  });
</script>
<%@ include file="/WEB-INF/jsp/common/footer.jspf" %>
"""

    files[f"{W}/WEB-INF/jsp/vendor/invoice_list.jsp"] = """\
<%@ page contentType="text/html;charset=UTF-8" %>
<%@ include file="/WEB-INF/jsp/common/header.jspf" %>
<link rel="stylesheet" href="/css/invoice.css"/>
<table class="invoice-table">
  <c:forEach items="${actionBean.invoices}" var="inv">
    <tr>
      <td>${inv.vendorCode}</td>
      <td><stripes:link beanclass="com.acme.web.action.VendorInvoiceActionBean">
        <stripes:param name="invoiceId" value="${inv.id}"/>Open</stripes:link></td>
    </tr>
  </c:forEach>
</table>
<%@ include file="/WEB-INF/jsp/common/footer.jspf" %>
"""

    files[f"{W}/WEB-INF/jsp/user/list.jsp"] = """\
<%@ page contentType="text/html;charset=UTF-8" %>
<%@ include file="/WEB-INF/jsp/common/header.jspf" %>
<script src="/js/user.js"></script>
<stripes:form action="/user/List.action" class="user-form">
  <stripes:text name="user.name"/>
  <stripes:text name="user.email"/>
  <stripes:submit name="register" value="Register"/>
</stripes:form>
<ul class="user-list">
  <c:forEach items="${actionBean.users}" var="u">
    <li class="user-row">${u.name}</li>
  </c:forEach>
</ul>
<%@ include file="/WEB-INF/jsp/common/footer.jspf" %>
"""

    files[f"{W}/WEB-INF/jsp/order/view.jsp"] = """\
<%@ page contentType="text/html;charset=UTF-8" %>
<%@ include file="/WEB-INF/jsp/common/header.jspf" %>
<stripes:form beanclass="com.acme.web.action.OrderActionBean" class="order-form">
  <stripes:hidden name="orderId"/>
  <stripes:submit name="place" value="Place order"/>
</stripes:form>
<div class="order-total">${actionBean.total}</div>
<%@ include file="/WEB-INF/jsp/common/footer.jspf" %>
"""

    files[f"{W}/index.jsp"] = """\
<%@ page contentType="text/html;charset=UTF-8" %>
<jsp:forward page="/vendor/Invoice.action"/>
"""

    files[f"{W}/js/common.js"] = """\
var Acme = window.Acme || {};

Acme.showError = function (message) {
  $('.page-header').append('<div class="error">' + message + '</div>');
};

Acme.formatCents = function (cents) {
  return (cents / 100).toFixed(2);
};
"""

    files[f"{W}/js/invoice.js"] = """\
function renderInvoiceTotal(data) {
  $('#invoice-total').text(Acme.formatCents(data.amount));
}

function loadInvoice(invoiceId) {
  $.getJSON('/vendor/Invoice.action?load=&invoiceId=' + invoiceId, renderInvoiceTotal);
}

$(document).ready(function () {
  var invoiceId = $('input[name="invoiceId"]').val();
  if (invoiceId) {
    loadInvoice(invoiceId);
  }
  $('.invoice-save').on('click', function () {
    $('.invoice-form').addClass('invoice-form--busy');
  });
});
"""

    files[f"{W}/js/user.js"] = """\
function highlightUser(row) {
  $(row).toggleClass('user-row--active');
}

$(document).ready(function () {
  $('.user-row').on('click', function () {
    highlightUser(this);
  });
  $.get('/user/List.action', function (html) {
    $('.user-list').replaceWith($(html).find('.user-list'));
  });
});
"""

    files[f"{W}/css/common.css"] = """\
.page-header {
  display: flex;
  gap: 1rem;
}

.page-footer {
  margin-top: 2rem;
  color: #666;
}

.error {
  color: #b00020;
}
"""

    files[f"{W}/css/invoice.css"] = """\
.invoice-form {
  display: grid;
  grid-template-columns: 8rem 1fr;
}

.invoice-form--busy {
  opacity: 0.5;
}

.invoice-total {
  font-weight: bold;
}

.invoice-table td {
  padding: 0.25rem 0.5rem;
}
"""
    return files


def _module_files(index: int) -> dict[str, str]:
    """One generated vertical slice: entity, dao, service, action, test, jsp, js, css."""
    n = f"{index:04d}"
    pkg = f"com.acme.m{n}"
    base = f"src/main/java/com/acme/m{n}"
    prev = f"com.acme.m{index - 1:04d}.Item{index - 1:04d}Service" if index else None
    files: dict[str, str] = {}

    files[f"{base}/Item{n}.java"] = _java(
        pkg,
        [
            "javax.persistence.Column",
            "javax.persistence.Entity",
            "javax.persistence.Id",
            "javax.persistence.Table",
        ],
        f"""
@Entity
@Table(name = "item_{n}")
public class Item{n} {{

    @Id
    @Column(name = "item_id")
    private Long id;

    @Column(name = "label")
    private String label;

    @Column(name = "qty")
    private int qty;

    public Long getId() {{
        return id;
    }}

    public String getLabel() {{
        return label;
    }}

    public void setLabel(String label) {{
        this.label = label;
    }}

    public int getQty() {{
        return qty;
    }}

    public void setQty(int qty) {{
        this.qty = qty;
    }}
}}
""",
    )

    files[f"{base}/Item{n}Dao.java"] = _java(
        pkg,
        [
            "com.acme.dao.HibernateSupport",
            "java.util.List",
            "org.hibernate.SessionFactory",
        ],
        f"""
public class Item{n}Dao extends HibernateSupport {{

    public Item{n}Dao(SessionFactory sessionFactory) {{
        super(sessionFactory);
    }}

    public Item{n} find(Long id) {{
        return currentSession().get(Item{n}.class, id);
    }}

    public void store(Item{n} item) {{
        currentSession().saveOrUpdate(item);
    }}

    public List<Item{n}> findAll() {{
        return currentSession().createQuery("from Item{n}", Item{n}.class).list();
    }}
}}
""",
    )

    service_imports = [
        "static com.acme.util.Strings.isBlank",
        "java.util.List",
        "org.springframework.stereotype.Service",
    ]
    if prev:
        service_imports.insert(1, prev)
    chain = (
        f"""
    private Item{index - 1:04d}Service upstream;

    public void setUpstream(Item{index - 1:04d}Service upstream) {{
        this.upstream = upstream;
    }}
"""
        if prev
        else ""
    )
    chain_call = (
        "        if (upstream != null) {\n            upstream.count();\n        }\n"
        if prev
        else ""
    )
    files[f"{base}/Item{n}Service.java"] = _java(
        pkg,
        service_imports,
        f"""
@Service
public class Item{n}Service {{

    private Item{n}Dao dao;
{chain}
    public Item{n} load(Long id) {{
        return dao.find(id);
    }}

    public void rename(Long id, String label) {{
        if (isBlank(label)) {{
            throw new IllegalArgumentException("label");
        }}
        Item{n} item = dao.find(id);
        item.setLabel(label);
        dao.store(item);
    }}

    public int count() {{
{chain_call}        List<Item{n}> all = dao.findAll();
        return all.size();
    }}
}}
""",
    )

    files[f"{base}/web/Item{n}ActionBean.java"] = _java(
        f"{pkg}.web",
        [
            f"{pkg}.Item{n}",
            f"{pkg}.Item{n}Service",
            "com.acme.web.action.BaseActionBean",
            f"{_STRIPES}.action.DefaultHandler",
            f"{_STRIPES}.action.ForwardResolution",
            f"{_STRIPES}.action.HandlesEvent",
            f"{_STRIPES}.action.RedirectResolution",
            f"{_STRIPES}.action.Resolution",
            f"{_STRIPES}.action.UrlBinding",
            f"{_STRIPES}.integration.spring.SpringBean",
        ],
        f"""
@UrlBinding("/m{n}/Item.action")
public class Item{n}ActionBean extends BaseActionBean {{

    @SpringBean
    private Item{n}Service service;

    private Long id;

    private Item{n} item;

    public Long getId() {{
        return id;
    }}

    public void setId(Long id) {{
        this.id = id;
    }}

    public Item{n} getItem() {{
        return item;
    }}

    public void setItem(Item{n} item) {{
        this.item = item;
    }}

    @DefaultHandler
    public Resolution view() {{
        item = service.load(id);
        return new ForwardResolution("/WEB-INF/jsp/m{n}/item.jsp");
    }}

    @HandlesEvent("rename")
    public Resolution rename() {{
        service.rename(id, item.getLabel());
        return new RedirectResolution(Item{n}ActionBean.class).addParameter("id", id);
    }}
}}
""",
    )

    files[f"src/test/java/com/acme/m{n}/Item{n}ServiceTest.java"] = _java(
        pkg,
        ["static org.junit.Assert.assertEquals", "org.junit.Test"],
        f"""
public class Item{n}ServiceTest {{

    @Test
    public void renameRejectsBlank() {{
        Item{n}Service service = new Item{n}Service();
        try {{
            service.rename(1L, " ");
        }} catch (IllegalArgumentException expected) {{
            assertEquals("label", expected.getMessage());
        }}
    }}
}}
""",
    )

    files[f"{W}/WEB-INF/jsp/m{n}/item.jsp"] = f"""\
<%@ page contentType="text/html;charset=UTF-8" %>
<%@ include file="/WEB-INF/jsp/common/header.jspf" %>
<link rel="stylesheet" href="/css/m{n}.css"/>
<script src="/js/m{n}.js"></script>
<stripes:form beanclass="{pkg}.web.Item{n}ActionBean" class="item-{n}-form">
  <stripes:hidden name="id"/>
  <stripes:text name="item.label" class="item-{n}-label"/>
  <stripes:submit name="rename" value="Rename"/>
</stripes:form>
<%@ include file="/WEB-INF/jsp/common/footer.jspf" %>
"""

    files[f"{W}/js/m{n}.js"] = f"""\
function refreshItem{n}(id) {{
  $.getJSON('/m{n}/Item.action?id=' + id, function (data) {{
    $('.item-{n}-label').val(data.label);
  }});
}}

$(document).ready(function () {{
  $('.item-{n}-form').on('submit', function () {{
    $(this).addClass('item-{n}-form--busy');
  }});
}});
"""

    files[f"{W}/css/m{n}.css"] = f"""\
.item-{n}-form {{
  display: flex;
}}

.item-{n}-form--busy {{
  opacity: 0.5;
}}

.item-{n}-label {{
  width: 20rem;
}}
"""
    return files


MODULE_FILE_COUNT = 8


def build_files(scale: int = 0) -> dict[str, str]:
    """Return ``{relative_path: content}`` for the app, padded toward *scale* files."""
    files = _base_files()
    if scale > len(files):
        modules = -(-(scale - len(files)) // MODULE_FILE_COUNT)
        for index in range(modules):
            files.update(_module_files(index))
    return dict(sorted(files.items()))


def generate(target: Path, scale: int = 0) -> list[Path]:
    """Write the fixture app into *target* and return the written paths."""
    target = Path(target)
    written: list[Path] = []
    for relative, content in build_files(scale).items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def drift(target: Path = FIXTURE_DIR) -> list[str]:
    """Paths whose committed content differs from what the generator writes."""
    expected = build_files()
    problems: list[str] = []
    for relative, content in expected.items():
        path = target / relative
        if not path.is_file():
            problems.append(f"missing: {relative}")
        elif path.read_text(encoding="utf-8") != content:
            problems.append(f"changed: {relative}")
    for path in sorted(target.rglob("*")):
        relative = path.relative_to(target).as_posix()
        if path.is_file() and relative not in expected and relative not in NON_GENERATED:
            problems.append(f"extra: {relative}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=FIXTURE_DIR)
    parser.add_argument("--scale", type=int, default=0, help="approximate file count")
    parser.add_argument("--check", action="store_true", help="report drift, write nothing")
    args = parser.parse_args(argv)
    if args.check:
        problems = drift(args.out)
        for problem in problems:
            print(problem)
        return 1 if problems else 0
    if args.scale and args.out.resolve() == FIXTURE_DIR:
        parser.error("--scale output is for benchmarks; pass --out to a temporary directory")
    written = generate(args.out, args.scale)
    print(f"wrote {len(written)} files to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
